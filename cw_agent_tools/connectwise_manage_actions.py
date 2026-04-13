from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from connectwise_manage_client import CWManageClient
from connectwise_manage_resolvers import MappingResolver, parse_maybe_int


REFERENCE_FIELDS = {
    "board",
    "status",
    "type",
    "company",
    "owner",
    "project",
    "phase",
    "site",
    "country",
    "contact",
    "priority",
    "serviceLocation",
    "source",
    "opportunity",
    "location",
    "department",
}


@dataclass(slots=True)
class ActionResult:
    ok: bool
    action: str
    ticket_id: int
    dry_run: bool
    message: str
    ops: Optional[List[Dict[str, Any]]] = None
    before: Optional[Dict[str, Any]] = None
    after: Optional[Dict[str, Any]] = None
    data: Optional[Any] = None


def op_set(ops: List[Dict[str, Any]], ticket_obj: Dict[str, Any], path: str, value: Any) -> None:
    key = path.lstrip("/")
    exists = key in ticket_obj
    ops.append({"op": "replace" if exists else "add", "path": path, "value": value})


class TicketActions:
    def __init__(self, client: CWManageClient, resolver: MappingResolver, *, dry_run: bool = True) -> None:
        self.client = client
        self.resolver = resolver
        self.dry_run = dry_run

    def get_ticket(self, ticket_id: int) -> Dict[str, Any]:
        return self.client.get_ticket(ticket_id)

    def list_recent_open_tickets(self, page_size: int = 100) -> list[dict[str, Any]]:
        return self.client.list_tickets(
            conditions="status/name <> 'Closed'",
            order_by="dateEntered desc",
            page_size=page_size,
        )

    def list_related_resources(self, ticket_id: int) -> Dict[str, str]:
        ticket = self.get_ticket(ticket_id)
        info = ticket.get("_info") or {}
        return {k: v for k, v in info.items() if isinstance(v, str) and k.endswith("_href")}

    def fetch_related_resource(self, ticket_id: int, relation: str) -> ActionResult:
        ticket = self.get_ticket(ticket_id)
        info = ticket.get("_info") or {}
        key = relation if relation.endswith("_href") else f"{relation}_href"
        href = info.get(key)
        if not href:
            return ActionResult(False, "fetch_related_resource", ticket_id, self.dry_run, f"Relation {key!r} not present on ticket.")
        payload = self.client.fetch_absolute_url(href)
        return ActionResult(True, "fetch_related_resource", ticket_id, self.dry_run, f"Fetched {key}.", data=payload, before=ticket)

    def add_internal_note(self, ticket_id: int, text: str) -> ActionResult:
        before = self.get_ticket(ticket_id)
        note_flags = {
            "text": text,
            "detailDescriptionFlag": False,
            "internalAnalysisFlag": True,
            "resolutionFlag": False,
        }
        if self.dry_run:
            return ActionResult(True, "add_internal_note", ticket_id, True, "Would post internal note.", before=before, data=note_flags)
        data = self.client.add_ticket_note(
            ticket_id,
            text,
            internal_analysis_flag=True,
            detail_description_flag=False,
            resolution_flag=False,
        )
        after = self.get_ticket(ticket_id)
        return ActionResult(True, "add_internal_note", ticket_id, False, "Internal note added.", before=before, after=after, data=data)

    def add_discussion_note(
        self,
        ticket_id: int,
        text: str,
        *,
        detail_description_flag: bool = True,
        resolution_flag: bool = False,
        process_notifications: Optional[bool] = None,
    ) -> ActionResult:
        before = self.get_ticket(ticket_id)
        note_flags = {
            "text": text,
            "detailDescriptionFlag": bool(detail_description_flag),
            "internalAnalysisFlag": False,
            "resolutionFlag": bool(resolution_flag),
        }
        if process_notifications is not None:
            note_flags["processNotifications"] = bool(process_notifications)
        if self.dry_run:
            return ActionResult(True, "add_discussion_note", ticket_id, True, "Would post discussion note.", before=before, data=note_flags)
        data = self.client.add_ticket_note(
            ticket_id,
            text,
            internal_analysis_flag=False,
            detail_description_flag=bool(detail_description_flag),
            resolution_flag=bool(resolution_flag),
            process_notifications=process_notifications,
        )
        after = self.get_ticket(ticket_id)
        return ActionResult(True, "add_discussion_note", ticket_id, False, "Discussion note added.", before=before, after=after, data=data)

    def build_field_patch_ops(self, ticket_obj: Dict[str, Any], changes: Dict[str, Any]) -> List[Dict[str, Any]]:
        ops: List[Dict[str, Any]] = []
        target_board_name = None
        target_board_id = None

        if "board" in changes:
            board_value = changes["board"]
            target_board_id = self.resolver.resolve_board_id(board_value)
            target_board_name = str(board_value) if not isinstance(board_value, dict) else None
            if not target_board_name:
                current_name = self.resolver.reverse_lookup_name(self.resolver.mappings.get("boards", {}), target_board_id)
                target_board_name = current_name
            new_value = {"id": int(target_board_id)}
            if ticket_obj.get("board") != new_value:
                op_set(ops, ticket_obj, "/board", new_value)

        current_board_name = None
        current_board = ticket_obj.get("board") or {}
        if isinstance(current_board, dict):
            current_board_name = current_board.get("name")

        effective_board_name = target_board_name or current_board_name
        effective_board_id = target_board_id or parse_maybe_int((ticket_obj.get("board") or {}).get("id"))

        for field_name, target_value in changes.items():
            if field_name == "board":
                continue

            if field_name == "status":
                resolved = {"id": self.resolver.resolve_status_id(effective_board_name or effective_board_id, target_value)}
            elif field_name == "type":
                resolved = {"id": self.resolver.resolve_type_id(effective_board_name or effective_board_id, target_value)}
            elif field_name == "company":
                resolved = {"id": self.resolver.resolve_company_id(target_value)}
            elif field_name == "owner":
                resolved = {"id": self.resolver.resolve_member_id(target_value)}
            elif field_name in REFERENCE_FIELDS:
                ref_id = parse_maybe_int(target_value if not isinstance(target_value, dict) else target_value.get("id"))
                if ref_id is None:
                    raise RuntimeError(f"{field_name} must be a numeric id or {{'id': <id>}}. Received: {target_value!r}")
                resolved = {"id": ref_id}
            elif field_name == "customFields":
                resolved = self._build_custom_fields_value(ticket_obj, target_value)
            else:
                resolved = target_value

            if resolved is None:
                continue
            current_value = ticket_obj.get(field_name)
            if current_value != resolved:
                op_set(ops, ticket_obj, f"/{field_name}", resolved)

        return ops

    def _build_custom_fields_value(self, ticket_obj: Dict[str, Any], updates: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
        if not updates:
            return None
        fields = copy.deepcopy(ticket_obj.get("customFields") or [])
        changed = False
        for upd in updates:
            wanted_value = upd.get("value")
            target_id = parse_maybe_int(upd.get("id"))
            target_caption = str(upd.get("caption") or "").strip().lower() or None
            target_cwid = str(upd.get("connectWiseId") or "").strip().lower() or None

            match = None
            for cf in fields:
                if target_id is not None and parse_maybe_int(cf.get("id")) == target_id:
                    match = cf
                    break
                if target_caption and str(cf.get("caption") or "").strip().lower() == target_caption:
                    match = cf
                    break
                if target_cwid and str(cf.get("connectWiseId") or "").strip().lower() == target_cwid:
                    match = cf
                    break
            if match is None:
                raise RuntimeError(f"Custom field not found for update {upd!r}.")
            if match.get("value") != wanted_value:
                match["value"] = wanted_value
                changed = True
        return fields if changed else None

    def patch_fields(self, ticket_id: int, changes: Dict[str, Any]) -> ActionResult:
        before = self.get_ticket(ticket_id)
        ops = self.build_field_patch_ops(before, changes)
        if not ops:
            return ActionResult(True, "patch_fields", ticket_id, self.dry_run, "No changes needed.", ops=[], before=before, after=before)
        if self.dry_run:
            return ActionResult(True, "patch_fields", ticket_id, True, "Would patch ticket fields.", ops=ops, before=before)
        self.client.patch_ticket(ticket_id, ops)
        after = self.get_ticket(ticket_id)
        return ActionResult(True, "patch_fields", ticket_id, False, "Ticket patched.", ops=ops, before=before, after=after)

    def assign_owner(self, ticket_id: int, owner: Any) -> ActionResult:
        return self.patch_fields(ticket_id, {"owner": owner})

    def set_board_status_type(
        self,
        ticket_id: int,
        *,
        board: Optional[Any] = None,
        status: Optional[Any] = None,
        ticket_type: Optional[Any] = None,
    ) -> ActionResult:
        changes: Dict[str, Any] = {}
        if board is not None:
            changes["board"] = board
        if status is not None:
            changes["status"] = status
        if ticket_type is not None:
            changes["type"] = ticket_type
        return self.patch_fields(ticket_id, changes)

    def set_company_contact_site(
        self,
        ticket_id: int,
        *,
        company: Optional[Any] = None,
        contact: Optional[Any] = None,
        site: Optional[Any] = None,
        contact_name: Optional[str] = None,
        contact_phone: Optional[str] = None,
        contact_email: Optional[str] = None,
        site_name: Optional[str] = None,
        address_line1: Optional[str] = None,
        address_line2: Optional[str] = None,
        city: Optional[str] = None,
        state_identifier: Optional[str] = None,
        zip_code: Optional[str] = None,
    ) -> ActionResult:
        changes: Dict[str, Any] = {}
        if company is not None:
            changes["company"] = company
        if contact is not None:
            changes["contact"] = contact
        if site is not None:
            changes["site"] = site
        if contact_name is not None:
            changes["contactName"] = contact_name
        if contact_phone is not None:
            changes["contactPhoneNumber"] = contact_phone
        if contact_email is not None:
            changes["contactEmailAddress"] = contact_email
        if site_name is not None:
            changes["siteName"] = site_name
        if address_line1 is not None:
            changes["addressLine1"] = address_line1
        if address_line2 is not None:
            changes["addressLine2"] = address_line2
        if city is not None:
            changes["city"] = city
        if state_identifier is not None:
            changes["stateIdentifier"] = state_identifier
        if zip_code is not None:
            changes["zip"] = zip_code
        return self.patch_fields(ticket_id, changes)

    def set_priority_and_routing(
        self,
        ticket_id: int,
        *,
        priority: Optional[Any] = None,
        service_location: Optional[Any] = None,
        source: Optional[Any] = None,
        location: Optional[Any] = None,
        department: Optional[Any] = None,
    ) -> ActionResult:
        changes: Dict[str, Any] = {}
        if priority is not None:
            changes["priority"] = priority
        if service_location is not None:
            changes["serviceLocation"] = service_location
        if source is not None:
            changes["source"] = source
        if location is not None:
            changes["location"] = location
        if department is not None:
            changes["department"] = department
        return self.patch_fields(ticket_id, changes)

    def set_project_fields(
        self,
        ticket_id: int,
        *,
        project: Optional[Any] = None,
        phase: Optional[Any] = None,
        wbs_code: Optional[str] = None,
        budget_hours: Optional[float] = None,
        opportunity: Optional[Any] = None,
        summary: Optional[str] = None,
    ) -> ActionResult:
        changes: Dict[str, Any] = {}
        if project is not None:
            changes["project"] = project
        if phase is not None:
            changes["phase"] = phase
        if wbs_code is not None:
            changes["wbsCode"] = wbs_code
        if budget_hours is not None:
            changes["budgetHours"] = budget_hours
        if opportunity is not None:
            changes["opportunity"] = opportunity
        if summary is not None:
            changes["summary"] = summary
        return self.patch_fields(ticket_id, changes)

    def set_billing_flags(
        self,
        ticket_id: int,
        *,
        approved: Optional[bool] = None,
        closed_flag: Optional[bool] = None,
        sub_billing_method: Optional[str] = None,
        bill_time: Optional[str] = None,
        bill_expenses: Optional[str] = None,
        bill_products: Optional[str] = None,
        automatic_email_contact_flag: Optional[bool] = None,
        automatic_email_resource_flag: Optional[bool] = None,
        automatic_email_cc_flag: Optional[bool] = None,
        automatic_email_cc: Optional[str] = None,
        allow_all_clients_portal_view: Optional[bool] = None,
        customer_updated_flag: Optional[bool] = None,
    ) -> ActionResult:
        changes: Dict[str, Any] = {}
        if approved is not None:
            changes["approved"] = approved
        if closed_flag is not None:
            changes["closedFlag"] = closed_flag
        if sub_billing_method is not None:
            changes["subBillingMethod"] = sub_billing_method
        if bill_time is not None:
            changes["billTime"] = bill_time
        if bill_expenses is not None:
            changes["billExpenses"] = bill_expenses
        if bill_products is not None:
            changes["billProducts"] = bill_products
        if automatic_email_contact_flag is not None:
            changes["automaticEmailContactFlag"] = automatic_email_contact_flag
        if automatic_email_resource_flag is not None:
            changes["automaticEmailResourceFlag"] = automatic_email_resource_flag
        if automatic_email_cc_flag is not None:
            changes["automaticEmailCcFlag"] = automatic_email_cc_flag
        if automatic_email_cc is not None:
            changes["automaticEmailCc"] = automatic_email_cc
        if allow_all_clients_portal_view is not None:
            changes["allowAllClientsPortalView"] = allow_all_clients_portal_view
        if customer_updated_flag is not None:
            changes["customerUpdatedFlag"] = customer_updated_flag
        return self.patch_fields(ticket_id, changes)

    def update_custom_fields(self, ticket_id: int, updates: List[Dict[str, Any]]) -> ActionResult:
        return self.patch_fields(ticket_id, {"customFields": updates})

    def raw_patch(self, ticket_id: int, ops: List[Dict[str, Any]]) -> ActionResult:
        before = self.get_ticket(ticket_id)
        if self.dry_run:
            return ActionResult(True, "raw_patch", ticket_id, True, "Would apply raw JSON Patch ops.", ops=ops, before=before)
        self.client.patch_ticket(ticket_id, ops)
        after = self.get_ticket(ticket_id)
        return ActionResult(True, "raw_patch", ticket_id, False, "Raw JSON Patch applied.", ops=ops, before=before, after=after)
