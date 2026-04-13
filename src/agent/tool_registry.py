"""
src/agent/tool_registry.py

Maps tool names to callable implementations.

Tools that already have working implementations are wired to the real code.
Tools that require external integrations not yet fully available are stubbed
with realistic mock responses and a # TODO comment.

Usage:
    registry = ToolRegistry(config=cfg, mappings=mappings, dry_run=True)
    result   = registry.call("get_technician_workload", {"technician_identifier": "akloss"})
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _member_id_from_mappings(mappings: Dict, identifier: str) -> Optional[int]:
    """Look up a member ID by login identifier (case-insensitive)."""
    from src.clients.resolver import MappingResolver, normalize_key
    try:
        r = MappingResolver()
        return r.resolve_member_id(identifier)
    except Exception:
        # Fallback: simple dict lookup for cases where resolver can't load mappings
        members = {str(k).lower(): v for k, v in (mappings.get("members") or {}).items()}
        val = members.get(normalize_key(identifier))
        return int(val) if val is not None else None


def _board_id_from_mappings(mappings: Dict, board_name: str) -> Optional[int]:
    from src.clients.resolver import MappingResolver
    try:
        return MappingResolver().resolve_board_id(board_name)
    except Exception:
        boards = {str(k).lower(): int(v) for k, v in (mappings.get("boards") or {}).items()}
        return boards.get(board_name.strip().lower())


def _status_id_from_mappings(mappings: Dict, board_name: str, status_name: str) -> Optional[int]:
    from src.clients.resolver import MappingResolver
    try:
        return MappingResolver().resolve_status_id(board_name, status_name)
    except Exception:
        key = f"{board_name.lower()} statuses"
        statuses = {str(k).lower(): v for k, v in (mappings.get(key) or {}).items()}
        val = statuses.get(status_name.strip().lower())
        return int(val) if val is not None else None


def _tech_identifier_from_member_id(mappings: Dict, member_id: int) -> Optional[str]:
    """Reverse lookup: member_id → identifier string."""
    from src.clients.resolver import MappingResolver
    try:
        return MappingResolver().reverse_lookup_name("members", member_id)
    except Exception:
        for k, v in (mappings.get("members") or {}).items():
            try:
                if int(v) == member_id:
                    return str(k)
            except (TypeError, ValueError):
                pass
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────────────

class ToolRegistry:
    """
    Lazily-initialised collection of tool callables.

    All write methods respect the dry_run flag:
      - In dry_run mode the action is described but not applied.
      - Reads always execute regardless of dry_run.

    Args:
        config:     Portal config dict (from load_config()).
        mappings:   Mappings dict (from load_mappings()).
        dry_run:    Override dry_run from config if needed.
        broadcaster: Optional callable(str) used to stream tool activity to
                     the UI via SSE.  Falls back to logging if not provided.
    """

    def __init__(
        self,
        *,
        config: Dict[str, Any],
        mappings: Dict[str, Any],
        dry_run: Optional[bool] = None,
        broadcaster: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._config = config
        self._mappings = mappings
        self._dry_run = dry_run if dry_run is not None else config.get("dry_run", True)
        self._broadcast = broadcaster or (lambda msg: log.info("[tool] %s", msg))

        # Lazily-created clients
        self.__cw: Any = None
        self.__teams: Any = None
        self.__db_session: Any = None
        self.__resolver: Any = None

    # ── Lazy client accessors ─────────────────────────────────────────────────

    @property
    def _cw(self):
        if self.__cw is None:
            from src.clients.connectwise import CWManageClient, CWConfig
            self.__cw = CWManageClient(dry_run=self._dry_run)
        return self.__cw

    @property
    def _teams(self):
        if self.__teams is None:
            try:
                from src.clients.teams import TeamsClient
                self.__teams = TeamsClient()
            except Exception as exc:
                log.warning("Teams client unavailable: %s", exc)
                self.__teams = None
        return self.__teams

    @property
    def _resolver(self):
        if self.__resolver is None:
            from src.clients.resolver import MappingResolver
            self.__resolver = MappingResolver()
        return self.__resolver

    def _db_session_ctx(self):
        from src.clients.database import SessionLocal
        return SessionLocal()

    # ── Dispatcher ────────────────────────────────────────────────────────────

    def call(self, name: str, inputs: Dict[str, Any]) -> Any:
        """
        Dispatch a tool call by name.  Returns a JSON-serialisable result.
        Raises ValueError for unknown tools.
        """
        self._broadcast(f"  → tool: {name}({_fmt_inputs(inputs)})")
        handler = self._get_handler(name)
        result = handler(inputs)
        self._broadcast(f"  ← {name}: {_fmt_result(result)}")
        return result

    def _get_handler(self, name: str) -> Callable[[Dict[str, Any]], Any]:
        table: Dict[str, Callable] = {
            # PERCEPTION
            "get_new_tickets":          self._get_new_tickets,
            "get_dispatch_board":       self._get_dispatch_board,
            "get_technician_schedule":  self._get_technician_schedule,
            "get_technician_workload":  self._get_technician_workload,
            "get_ticket_history":       self._get_ticket_history,
            "get_tech_availability":    self._get_tech_availability,
            # ACTION
            "assign_ticket":            self._assign_ticket,
            "reassign_ticket":          self._reassign_ticket,
            "escalate_ticket":          self._escalate_ticket,
            "message_technician":       self._message_technician,
            "message_team_channel":     self._message_team_channel,
            "send_reminder":            self._send_reminder,
            "message_client":           self._message_client,
            "update_ticket_notes":      self._update_ticket_notes,
            "flag_for_human_review":    self._flag_for_human_review,
            # TICKET FIELD ACTIONS (ported from cw_agent_tools)
            "patch_ticket_fields":      self._patch_ticket_fields,
            "set_board_status_type":    self._set_board_status_type,
            "set_company_contact_site": self._set_company_contact_site,
            "set_priority_and_routing": self._set_priority_and_routing,
            "set_project_fields":       self._set_project_fields,
            "set_billing_flags":        self._set_billing_flags,
            "update_custom_fields":     self._update_custom_fields,
            "list_related_resources":   self._list_related_resources,
            "fetch_related_resource":   self._fetch_related_resource,
            # MEMORY
            "get_tech_profile":         self._get_tech_profile,
            "update_tech_profile":      self._update_tech_profile,
            "log_dispatch_decision":    self._log_dispatch_decision,
            "get_similar_past_tickets": self._get_similar_past_tickets,
        }
        if name not in table:
            raise ValueError(f"Unknown tool: {name!r}")
        return table[name]

    # ═════════════════════════════════════════════════════════════════════════
    # PERCEPTION — delegates to src/tools/perception/
    # ═════════════════════════════════════════════════════════════════════════

    def _get_new_tickets(self, inp: Dict) -> Dict:
        from src.tools.perception.tickets import get_new_tickets
        return get_new_tickets(
            self._cw,
            self._config,
            self._mappings,
            priority_filter=inp.get("priority"),
            limit=inp.get("max_results", 20),
        )

    def _get_dispatch_board(self, inp: Dict) -> Dict:
        from src.tools.perception.dispatch_board import get_dispatch_board
        board_arg = inp.get("board_name")
        return get_dispatch_board(
            self._cw,
            self._config,
            self._mappings,
            include_closed=inp.get("include_closed", False),
            boards=[board_arg] if board_arg else None,
        )

    def _get_technician_schedule(self, inp: Dict) -> Dict:
        from src.tools.perception.technicians import get_technician_schedule
        ident = inp["technician_identifier"]
        member_id = _member_id_from_mappings(self._mappings, ident)
        if member_id is None:
            return {
                "technician": ident,
                "error": f"Member {ident!r} not found in mappings",
                "entries": [],
            }
        result = get_technician_schedule(
            self._cw,
            member_id,
            days_ahead=inp.get("days_ahead", 2),
        )
        result["technician"] = ident
        return result

    def _get_technician_workload(self, inp: Dict) -> Dict:
        from src.tools.perception.technicians import get_technician_workload
        ident = inp["technician_identifier"]
        member_id = _member_id_from_mappings(self._mappings, ident)
        if member_id is None:
            return {"error": f"Member {ident!r} not found in mappings", "open_tickets": 0}
        result = get_technician_workload(
            self._cw,
            self._mappings,
            member_id=member_id,
            max_workload_threshold=int(self._config.get("max_tech_workload", 5)),
        )
        # Ensure identifier is always present
        result.setdefault("identifier", ident)
        return result

    def _get_ticket_history(self, inp: Dict) -> Dict:
        from src.tools.perception.tickets import get_single_ticket_history
        return get_single_ticket_history(
            self._cw,
            int(inp["ticket_id"]),
            include_audit=inp.get("include_audit", True),
        )

    def _get_tech_availability(self, inp: Dict) -> Dict:
        from src.tools.perception.technicians import get_tech_availability
        from pathlib import Path
        ident = inp["technician_identifier"]
        member_id = _member_id_from_mappings(self._mappings, ident)
        data_dir = Path(__file__).resolve().parent.parent.parent / "data"
        return get_tech_availability(
            self._teams,       # None-safe: function handles missing client gracefully
            ident,
            member_id=member_id,
            data_dir=data_dir,
        )

    # ═════════════════════════════════════════════════════════════════════════
    # ACTION implementations
    # ═════════════════════════════════════════════════════════════════════════

    def _assign_ticket(self, inp: Dict) -> Dict:
        """Assign owner, optionally change status and/or board atomically."""
        ticket_id: int = inp["ticket_id"]
        ident: str = inp["technician_identifier"]
        changes: Dict = {"owner": ident}
        if inp.get("new_board"):
            changes["board"] = inp["new_board"]
        if inp.get("new_status"):
            changes["status"] = inp["new_status"]
        try:
            return self._cw.patch_fields(ticket_id, changes, resolver=self._resolver)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "ticket_id": ticket_id}

    def _reassign_ticket(self, inp: Dict) -> Dict:
        """Real implementation — same as assign but with a mandatory reason note."""
        ticket_id: int = inp["ticket_id"]
        new_ident: str = inp["new_technician_identifier"]
        reason: str = inp["reason"]

        result = self._assign_ticket({"ticket_id": ticket_id, "technician_identifier": new_ident})
        if result.get("ok"):
            # Always post the reassignment reason as an internal note
            self._update_ticket_notes({
                "ticket_id": ticket_id,
                "note_text": f"Reassigned to {new_ident} — {reason}",
            })
        return result

    def _escalate_ticket(self, inp: Dict) -> Dict:
        """Update priority, optionally board, and add an escalation note."""
        ticket_id: int = inp["ticket_id"]
        new_priority: str = inp["new_priority"]
        reason: str = inp["escalation_reason"]
        changes: Dict = {"priority": new_priority}
        if inp.get("new_board"):
            changes["board"] = inp["new_board"]
        try:
            result = self._cw.patch_fields(ticket_id, changes, resolver=self._resolver)
        except Exception as exc:
            result = {"ok": False, "error": str(exc), "ticket_id": ticket_id}
        # Always add escalation note
        self._update_ticket_notes({
            "ticket_id": ticket_id,
            "note_text": f"Escalated to {new_priority} — {reason}",
        })
        return result

    def _message_technician(self, inp: Dict) -> Dict:
        """Real Teams message if client is available, stub otherwise."""
        ident: str = inp["technician_identifier"]
        message: str = inp["message"]
        ticket_id: Optional[int] = inp.get("ticket_id")

        full_message = message
        if ticket_id:
            full_message = f"[Ticket #{ticket_id}] {message}"

        if self._teams:
            try:
                # TODO: look up the tech's Teams chat ID from their profile
                # For now stub with a note on the ticket if ticket_id provided
                log.info("[STUB] message_technician: Teams chat ID not yet linked for %s", ident)
            except Exception as exc:
                log.warning("Teams message failed: %s", exc)

        # Fallback: add as internal note on the ticket
        if ticket_id:
            self._update_ticket_notes({
                "ticket_id": ticket_id,
                "note_text": f"[Teams notification would be sent to {ident}]: {message}",
            })

        # TODO: implement full Teams DM once per-tech chat IDs are stored
        return {
            "ok": True,
            "technician": ident,
            "message": full_message,
            "_stub": not bool(self._teams),
            "_note": "Teams DM not sent — chat ID not yet linked to tech profile",
        }

    def _message_team_channel(self, inp: Dict) -> Dict:
        """Post to the default Teams channel."""
        message: str = inp["message"]
        channel: Optional[str] = inp.get("channel")

        if self._teams:
            try:
                # TODO: configure team_id and channel_id in .env / config
                # self._teams.send_channel_message(team_id, channel_id, message)
                log.info("[STUB] message_team_channel: team/channel IDs not configured")
            except Exception as exc:
                log.warning("Teams channel message failed: %s", exc)

        # TODO: implement once TEAMS_TEAM_ID and TEAMS_DISPATCH_CHANNEL_ID are in .env
        return {
            "ok": True,
            "channel": channel or "dispatch",
            "message": message,
            "_stub": True,
            "_note": "Team/channel IDs not yet configured in .env",
        }

    def _send_reminder(self, inp: Dict) -> Dict:
        """Post reminder note + stub Teams message."""
        ticket_id: int = inp["ticket_id"]
        ident: str = inp["technician_identifier"]
        reason: str = inp["reminder_reason"]
        custom: str = inp.get("custom_message", "")

        reminder_texts = {
            "sla_approaching": "SLA deadline is approaching — please update or resolve this ticket.",
            "idle_too_long":   "This ticket has been idle — please provide an update or escalate.",
            "client_waiting":  "Client is waiting for an update — please respond promptly.",
            "custom":          custom or "Reminder: please review this ticket.",
        }
        note_text = f"Reminder to {ident}: {reminder_texts.get(reason, reason)}"

        self._update_ticket_notes({"ticket_id": ticket_id, "note_text": note_text})
        teams_result = self._message_technician({
            "technician_identifier": ident,
            "message": note_text,
            "ticket_id": ticket_id,
        })
        return {"ok": True, "reminder_reason": reason, "teams": teams_result}

    def _message_client(self, inp: Dict) -> Dict:
        """Add a customer-visible discussion note."""
        ticket_id: int = inp["ticket_id"]
        message: str = inp["message"]
        send_email: bool = inp.get("send_email_notification", False)

        if self._dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "ticket_id": ticket_id,
                "would_post": message,
            }

        self._cw.add_ticket_note(
            ticket_id,
            message,
            internal_analysis_flag=False,
            detail_description_flag=True,
            resolution_flag=False,
            process_notifications=send_email,
        )
        return {"ok": True, "ticket_id": ticket_id, "posted": True}

    def _update_ticket_notes(self, inp: Dict) -> Dict:
        """Add an internal analyst note."""
        ticket_id: int = inp["ticket_id"]
        note_text: str = inp["note_text"]

        if self._dry_run:
            return {"ok": True, "dry_run": True, "ticket_id": ticket_id, "would_post": note_text}

        self._cw.add_ticket_note(
            ticket_id,
            note_text,
            internal_analysis_flag=True,
            detail_description_flag=False,
        )
        return {"ok": True, "ticket_id": ticket_id, "posted": True}

    def _flag_for_human_review(self, inp: Dict) -> Dict:
        """Add a 'needs review' internal note and stub Teams alert."""
        ticket_id: int = inp["ticket_id"]
        reason: str = inp["reason"]
        suggested: Optional[str] = inp.get("suggested_technician")

        note = f"⚠️ FLAGGED FOR HUMAN REVIEW — {reason}"
        if suggested:
            note += f"  Suggested assignee: {suggested}"

        self._update_ticket_notes({"ticket_id": ticket_id, "note_text": note})
        self._message_team_channel({
            "message": f"🚩 Ticket #{ticket_id} flagged for human review: {reason}",
        })
        return {
            "ok": True,
            "ticket_id": ticket_id,
            "flagged": True,
            "reason": reason,
            "suggested_technician": suggested,
        }

    # ═════════════════════════════════════════════════════════════════════════
    # TICKET FIELD ACTIONS — ported from cw_agent_tools
    # ═════════════════════════════════════════════════════════════════════════

    def _patch_ticket_fields(self, inp: Dict) -> Dict:
        """General-purpose patch with name resolution for any ticket field."""
        try:
            return self._cw.patch_fields(
                int(inp["ticket_id"]),
                dict(inp.get("changes") or {}),
                resolver=self._resolver,
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc), "ticket_id": inp.get("ticket_id")}

    def _set_board_status_type(self, inp: Dict) -> Dict:
        """Atomically change board, status, and/or ticket type."""
        changes: Dict = {}
        if inp.get("board"):
            changes["board"] = inp["board"]
        if inp.get("status"):
            changes["status"] = inp["status"]
        if inp.get("ticket_type"):
            changes["type"] = inp["ticket_type"]
        try:
            return self._cw.patch_fields(int(inp["ticket_id"]), changes, resolver=self._resolver)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "ticket_id": inp.get("ticket_id")}

    def _set_company_contact_site(self, inp: Dict) -> Dict:
        """Update company, contact, site, and address fields on a ticket."""
        field_map = {
            "company": "company", "contact": "contact", "site": "site",
            "contact_name": "contactName", "contact_phone": "contactPhoneNumber",
            "contact_email": "contactEmailAddress", "site_name": "siteName",
            "address_line1": "addressLine1", "address_line2": "addressLine2",
            "city": "city", "state_identifier": "stateIdentifier", "zip_code": "zip",
        }
        changes = {cw_key: inp[arg] for arg, cw_key in field_map.items() if inp.get(arg)}
        try:
            return self._cw.patch_fields(int(inp["ticket_id"]), changes, resolver=self._resolver)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "ticket_id": inp.get("ticket_id")}

    def _set_priority_and_routing(self, inp: Dict) -> Dict:
        """Set priority, service location, source, location, or department."""
        field_map = {
            "priority": "priority", "service_location": "serviceLocation",
            "source": "source", "location": "location", "department": "department",
        }
        changes = {cw_key: inp[arg] for arg, cw_key in field_map.items() if inp.get(arg)}
        try:
            return self._cw.patch_fields(int(inp["ticket_id"]), changes, resolver=self._resolver)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "ticket_id": inp.get("ticket_id")}

    def _set_project_fields(self, inp: Dict) -> Dict:
        """Update project-related ticket fields."""
        field_map = {
            "project": "project", "phase": "phase", "wbs_code": "wbsCode",
            "budget_hours": "budgetHours", "opportunity": "opportunity",
            "summary": "summary",
        }
        changes = {cw_key: inp[arg] for arg, cw_key in field_map.items() if inp.get(arg) is not None}
        try:
            return self._cw.patch_fields(int(inp["ticket_id"]), changes, resolver=self._resolver)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "ticket_id": inp.get("ticket_id")}

    def _set_billing_flags(self, inp: Dict) -> Dict:
        """Update billing booleans and email notification flags."""
        field_map = {
            "approved": "approved", "closed_flag": "closedFlag",
            "sub_billing_method": "subBillingMethod", "bill_time": "billTime",
            "bill_expenses": "billExpenses", "bill_products": "billProducts",
            "automatic_email_contact_flag": "automaticEmailContactFlag",
            "automatic_email_resource_flag": "automaticEmailResourceFlag",
            "automatic_email_cc_flag": "automaticEmailCcFlag",
            "automatic_email_cc": "automaticEmailCc",
            "allow_all_clients_portal_view": "allowAllClientsPortalView",
            "customer_updated_flag": "customerUpdatedFlag",
        }
        changes = {cw_key: inp[arg] for arg, cw_key in field_map.items() if inp.get(arg) is not None}
        try:
            return self._cw.patch_fields(int(inp["ticket_id"]), changes, resolver=self._resolver)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "ticket_id": inp.get("ticket_id")}

    def _update_custom_fields(self, inp: Dict) -> Dict:
        """Update ticket customFields matched by id, caption, or connectWiseId."""
        try:
            return self._cw.patch_fields(
                int(inp["ticket_id"]),
                {"customFields": list(inp.get("updates") or [])},
                resolver=self._resolver,
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc), "ticket_id": inp.get("ticket_id")}

    def _list_related_resources(self, inp: Dict) -> Dict:
        """List _info hrefs available on a ticket (notes, tasks, configs, etc.)."""
        try:
            hrefs = self._cw.list_related_resources(int(inp["ticket_id"]))
            return {"ok": True, "ticket_id": inp["ticket_id"], "resources": hrefs}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "ticket_id": inp.get("ticket_id")}

    def _fetch_related_resource(self, inp: Dict) -> Dict:
        """Fetch a linked resource from a ticket's _info hrefs."""
        try:
            data = self._cw.fetch_related_resource(
                int(inp["ticket_id"]), str(inp["relation"])
            )
            return {"ok": True, "ticket_id": inp["ticket_id"], "relation": inp["relation"], "data": data}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "ticket_id": inp.get("ticket_id")}

    # ═════════════════════════════════════════════════════════════════════════
    # MEMORY implementations
    # ═════════════════════════════════════════════════════════════════════════

    def _get_tech_profile(self, inp: Dict) -> Dict:
        """Load tech profile from the database."""
        from src.tools.memory.tech_profiles import get_tech_profile
        return get_tech_profile(inp["technician_identifier"], self._mappings)

    def _update_tech_profile(self, inp: Dict) -> Dict:
        """Upsert tech profile in the database."""
        from src.tools.memory.tech_profiles import update_tech_profile
        return update_tech_profile(
            inp["technician_identifier"],
            inp.get("updates", {}),
            self._mappings,
        )

    def _log_dispatch_decision(self, inp: Dict) -> Dict:
        """Persist a dispatch decision to the database."""
        from src.tools.memory.decision_log import log_dispatch_decision
        return log_dispatch_decision(
            ticket_id=int(inp["ticket_id"]),
            tech_identifier=inp["assigned_technician"],
            reason=inp["reason"],
            confidence=float(inp.get("confidence", 0.5)),
            alternatives_considered=inp.get("alternatives_considered", []),
            ticket_summary=inp.get("ticket_summary", ""),
            was_dry_run=self._dry_run,
            mappings=self._mappings,
        )

    def _get_similar_past_tickets(self, inp: Dict) -> Dict:
        """Search dispatch_decisions for similar summaries."""
        from src.tools.memory.rag import get_similar_past_tickets
        return get_similar_past_tickets(
            summary=inp["query"],
            limit=inp.get("limit", 5),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Private formatting helpers
# ─────────────────────────────────────────────────────────────────────────────


def _fmt_inputs(inp: Dict) -> str:
    parts = [f"{k}={v!r}" for k, v in list(inp.items())[:3]]
    if len(inp) > 3:
        parts.append("…")
    return ", ".join(parts)


def _fmt_result(result: Any) -> str:
    if isinstance(result, dict):
        keys = list(result.keys())[:4]
        preview = {k: result[k] for k in keys}
        return str(preview)[:120]
    return str(result)[:120]
