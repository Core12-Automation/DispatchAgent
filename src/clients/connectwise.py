"""
src/clients/connectwise.py

Canonical ConnectWise Manage API client.
Merges the functionality of:
  - app/core/connectwise.py  (functional helpers used by existing services)
  - cw_agent_tools/connectwise_manage_client.py  (OOP class used by agent tools)

Use this module for all new code.  The two originals are kept in place for
backward-compat but carry a deprecation notice pointing here.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth
from urllib3.util.retry import Retry


# ── Configuration ─────────────────────────────────────────────────────────────

@dataclass(slots=True)
class CWConfig:
    """All ConnectWise Manage connection parameters, loadable from env vars."""

    site: str
    company_id: str
    public_key: str
    private_key: str
    client_id: Optional[str] = None
    request_timeout: int = 20
    retry_total: int = 3
    # Seconds to sleep between paginated GET requests (rate-limit courtesy)
    page_delay: float = 0.05

    @classmethod
    def from_env(cls) -> "CWConfig":
        return cls(
            site=os.getenv("CWM_SITE", "").strip(),
            company_id=os.getenv("CWM_COMPANY_ID", "").strip(),
            public_key=os.getenv("CWM_PUBLIC_KEY", "").strip(),
            private_key=os.getenv("CWM_PRIVATE_KEY", "").strip(),
            client_id=(os.getenv("CLIENT_ID") or "").strip() or None,
            request_timeout=int(os.getenv("CWM_REQUEST_TIMEOUT", "20")),
            retry_total=int(os.getenv("CWM_RETRY_TOTAL", "3")),
        )

    def validate(self) -> None:
        missing = [
            name
            for name, value in {
                "CWM_SITE":       self.site,
                "CWM_COMPANY_ID": self.company_id,
                "CWM_PUBLIC_KEY": self.public_key,
                "CWM_PRIVATE_KEY": self.private_key,
            }.items()
            if not value
        ]
        if missing:
            raise RuntimeError(
                f"Missing required ConnectWise environment variables: {', '.join(missing)}"
            )

    def missing_credentials_error(self) -> Optional[str]:
        """Return a human-readable error string if any credential is absent, else None."""
        try:
            self.validate()
            return None
        except RuntimeError as exc:
            return str(exc)


# ── Module-level patch helpers ────────────────────────────────────────────────

def _op_set(
    ops: List[Dict[str, Any]],
    ticket_obj: Dict[str, Any],
    path: str,
    value: Any,
) -> None:
    """Append an add-or-replace JSON Patch op."""
    key = path.lstrip("/")
    op = "replace" if key in ticket_obj else "add"
    ops.append({"op": op, "path": path, "value": value})


def _build_custom_fields_patch(
    ticket_obj: Dict[str, Any],
    updates: List[Dict[str, Any]],
) -> Optional[List[Dict[str, Any]]]:
    """
    Return the full customFields list with requested values applied.
    Matches each update by id, caption, or connectWiseId.
    Returns None if nothing changed.
    """
    import copy
    if not updates:
        return None
    fields = copy.deepcopy(ticket_obj.get("customFields") or [])
    changed = False

    for upd in updates:
        from src.clients.resolver import parse_maybe_int
        wanted_value  = upd.get("value")
        target_id     = parse_maybe_int(upd.get("id"))
        target_caption = str(upd.get("caption") or "").strip().lower() or None
        target_cwid   = str(upd.get("connectWiseId") or "").strip().lower() or None

        match = None
        for cf in fields:
            if target_id is not None and parse_maybe_int(cf.get("id")) == target_id:
                match = cf; break
            if target_caption and str(cf.get("caption") or "").strip().lower() == target_caption:
                match = cf; break
            if target_cwid and str(cf.get("connectWiseId") or "").strip().lower() == target_cwid:
                match = cf; break

        if match is None:
            raise RuntimeError(f"Custom field not found for update {upd!r}.")
        if match.get("value") != wanted_value:
            match["value"] = wanted_value
            changed = True

    return fields if changed else None


# ── Client ────────────────────────────────────────────────────────────────────

class CWManageClient:
    """
    Thread-safe ConnectWise Manage REST API client.

    Provides low-level get/post/patch/delete methods plus higher-level
    ticket helpers.  All write operations (post, patch, delete) respect
    an optional dry_run flag — when True they log the intended operation
    and return None without touching the API.
    """

    def __init__(
        self,
        config: Optional[CWConfig] = None,
        *,
        dry_run: bool = False,
    ) -> None:
        self.config = config or CWConfig.from_env()
        self.config.validate()
        self.dry_run = dry_run
        self._session = self._build_session()

    # ── Session / auth ────────────────────────────────────────────────────────

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        session.auth = HTTPBasicAuth(
            f"{self.config.company_id}+{self.config.public_key}",
            self.config.private_key,
        )
        session.headers.update(self._base_headers())
        retry = Retry(
            total=self.config.retry_total,
            connect=self.config.retry_total,
            read=self.config.retry_total,
            status=self.config.retry_total,
            backoff_factor=0.8,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "POST", "PUT", "DELETE", "PATCH"),
            raise_on_status=False,
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(
            max_retries=retry,
            pool_connections=10,
            pool_maxsize=10,
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _base_headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.config.client_id:
            headers["clientId"] = self.config.client_id
        return headers

    def _url(self, path: str) -> str:
        base = self.config.site.rstrip("/") + "/"
        return urljoin(base, str(path).lstrip("/"))

    # ── Core HTTP ─────────────────────────────────────────────────────────────

    def request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        url = self._url(path)
        resp = self._session.request(
            method, url, timeout=self.config.request_timeout, **kwargs
        )
        if resp.status_code >= 400:
            preview = (resp.text or "")[:4000]
            raise requests.HTTPError(
                f"[HTTP {resp.status_code}] {method} {url}\n{preview}",
                response=resp,
            )
        return resp

    def _json(self, resp: requests.Response) -> Any:
        if not resp.text.strip():
            return None
        try:
            return resp.json()
        except ValueError:
            # CW returned non-JSON (HTML error page, redirect, etc.)
            raise requests.HTTPError(
                f"Non-JSON response from CW API: {resp.text[:500]}",
                response=resp,
            )

    def _record_cw_call(self) -> None:
        """Record one CW API call against the rate limiter (best-effort)."""
        try:
            from app.core.rate_limiter import get_cw_limiter
            get_cw_limiter().record_call()
        except Exception:
            pass

    def get(self, path: str, **kwargs: Any) -> Any:
        self._record_cw_call()
        return self._json(self.request("GET", path, **kwargs))

    def post(self, path: str, **kwargs: Any) -> Any:
        if self.dry_run:
            return None
        self._record_cw_call()
        return self._json(self.request("POST", path, **kwargs))

    def patch(self, path: str, ops: List[Dict[str, Any]]) -> Any:
        if self.dry_run:
            return None
        self._record_cw_call()
        return self._json(
            self.request("PATCH", path, json=ops)
        )

    def delete(self, path: str, **kwargs: Any) -> Any:
        if self.dry_run:
            return None
        self._record_cw_call()
        return self._json(self.request("DELETE", path, **kwargs))

    # ── Absolute URL (follows _info hrefs) ───────────────────────────────────

    def fetch_absolute_url(self, href: str) -> Any:
        resp = self._session.get(href, timeout=self.config.request_timeout)
        if resp.status_code >= 400:
            raise requests.HTTPError(
                f"[HTTP {resp.status_code}] GET {href}\n{(resp.text or '')[:4000]}",
                response=resp,
            )
        return self._json(resp)

    # ── Ticket helpers ────────────────────────────────────────────────────────

    def get_ticket(self, ticket_id: int) -> Dict[str, Any]:
        return self.get(f"service/tickets/{int(ticket_id)}")

    def list_tickets(
        self,
        *,
        conditions: Optional[str] = None,
        order_by: Optional[str] = None,
        page_size: int = 100,
        page: int = 1,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"pageSize": page_size, "page": page}
        if conditions:
            params["conditions"] = conditions
        if order_by:
            params["orderBy"] = order_by
        return self.get("service/tickets", params=params) or []

    def fetch_all_tickets(
        self,
        *,
        conditions: Optional[str] = None,
        order_by: str = "dateEntered asc",
        page_size: int = 200,
    ) -> List[Dict[str, Any]]:
        """Paginate through ALL matching tickets, handling rate limits automatically."""
        all_tickets: List[Dict[str, Any]] = []
        page = 1
        while True:
            batch = self.list_tickets(
                conditions=conditions,
                order_by=order_by,
                page_size=page_size,
                page=page,
            )
            if not isinstance(batch, list):
                break
            all_tickets.extend(batch)
            if len(batch) < page_size:
                break
            page += 1
            time.sleep(self.config.page_delay)
        return all_tickets

    def patch_ticket(self, ticket_id: int, ops: List[Dict[str, Any]]) -> Any:
        if not ops:
            return None
        return self.patch(f"service/tickets/{int(ticket_id)}", ops)

    def add_ticket_note(
        self,
        ticket_id: int,
        text: str,
        *,
        internal_analysis_flag: bool = True,
        detail_description_flag: bool = False,
        resolution_flag: bool = False,
        process_notifications: Optional[bool] = None,
    ) -> Any:
        payload: Dict[str, Any] = {
            "text": text,
            "detailDescriptionFlag": detail_description_flag,
            "internalAnalysisFlag": internal_analysis_flag,
            "resolutionFlag": resolution_flag,
        }
        if process_notifications is not None:
            payload["processNotifications"] = bool(process_notifications)
        return self.post(f"service/tickets/{int(ticket_id)}/notes", json=payload)

    def get_ticket_notes(self, ticket_id: int) -> List[Dict[str, Any]]:
        return self.get(f"service/tickets/{int(ticket_id)}/notes") or []

    def get_audit_trail(self, ticket_id: int) -> List[Dict[str, Any]]:
        # Correct CW Manage REST v3 endpoint for ticket audit trail
        return self.get(f"service/tickets/{int(ticket_id)}/audittrail") or []

    def list_related_resources(self, ticket_id: int) -> Dict[str, str]:
        """
        Return the _info href links present on a ticket.
        CW embeds hrefs for related data (notes, tasks, configurations,
        documents, products, timeentries, expenseEntries, activities,
        scheduleentries) in the ticket's _info dict.

        Returns:
            { "notes_href": "https://...", "tasks_href": "https://...", ... }
        """
        ticket = self.get_ticket(ticket_id)
        info = ticket.get("_info") or {}
        return {k: v for k, v in info.items() if isinstance(v, str) and k.endswith("_href")}

    def fetch_related_resource(self, ticket_id: int, relation: str) -> Any:
        """
        Fetch a resource linked from ticket._info.

        Args:
            ticket_id: The CW ticket ID.
            relation:  The relation name with or without the trailing '_href',
                       e.g. "notes", "tasks", "configurations", "documents",
                       "products", "timeentries", "expenseEntries",
                       "activities", "scheduleentries".

        Returns:
            The parsed JSON payload from the href, or an error dict.
        """
        hrefs = self.list_related_resources(ticket_id)
        key = relation if relation.endswith("_href") else f"{relation}_href"
        href = hrefs.get(key)
        if not href:
            available = list(hrefs.keys())
            raise ValueError(
                f"Relation {key!r} not present on ticket {ticket_id}. "
                f"Available: {available}"
            )
        return self.fetch_absolute_url(href)

    # ── Smart patch helpers ───────────────────────────────────────────────────

    # Reference fields that take {id: <int>} objects in CW's JSON Patch API
    _REFERENCE_FIELDS = {
        "board", "status", "type", "company", "owner", "project", "phase",
        "site", "country", "contact", "priority", "serviceLocation",
        "source", "opportunity", "location", "department",
    }

    def patch_fields(
        self,
        ticket_id: int,
        changes: Dict[str, Any],
        *,
        resolver=None,
    ) -> Dict[str, Any]:
        """
        Patch ticket fields with intelligent name-to-ID resolution.

        Accepts human-readable names (e.g. board="Support", status="Assigned",
        owner="akloss") and resolves them to CW numeric IDs via MappingResolver
        before building the JSON Patch ops list.

        Handles board→status→type atomically: board is always resolved first
        so that board-scoped status/type names resolve to the right section.

        Args:
            ticket_id: CW ticket ID.
            changes:   Dict of field → value.  Supported keys:
                         board, status, type (ticket_type), owner, company,
                         contact, site, priority, serviceLocation, source,
                         location, department, project, phase, wbsCode,
                         budgetHours, opportunity, summary, approved,
                         closedFlag, subBillingMethod, billTime, billExpenses,
                         billProducts, automaticEmailContactFlag,
                         automaticEmailResourceFlag, automaticEmailCcFlag,
                         automaticEmailCc, allowAllClientsPortalView,
                         customerUpdatedFlag, customFields (list of updates).
            resolver:  Optional MappingResolver instance.  If not provided,
                       one is created from the default mappings.json path.

        Returns:
            {"ok": True, "ops": [...], "dry_run": bool}
        """
        from src.clients.resolver import MappingResolver

        if resolver is None:
            resolver = MappingResolver()

        ticket = self.get_ticket(ticket_id)
        ops: List[Dict[str, Any]] = []

        # Determine effective board name (needed for status/type resolution)
        target_board_name: Optional[str] = None
        target_board_id: Optional[int] = None

        if "board" in changes:
            target_board_id   = resolver.resolve_board_id(changes["board"])
            target_board_name = resolver.reverse_lookup_name("boards", target_board_id) or str(changes["board"])
            _op_set(ops, ticket, "/board", {"id": target_board_id})

        current_board = ticket.get("board") or {}
        effective_board = target_board_name or (
            current_board.get("name") if isinstance(current_board, dict) else None
        )

        for field, value in changes.items():
            if field == "board":
                continue  # already handled

            resolved: Any

            if field == "status":
                sid = resolver.resolve_status_id(effective_board or "", value)
                resolved = {"id": sid}
            elif field in ("type", "ticket_type"):
                tid = resolver.resolve_type_id(effective_board or "", value)
                resolved = {"id": tid}
                field = "type"
            elif field == "owner":
                mid = resolver.resolve_member_id(value)
                resolved = {"id": mid}
            elif field == "company":
                cid = resolver.resolve_company_id(value)
                resolved = {"id": cid}
            elif field == "priority":
                pid = resolver.resolve_priority_id(value)
                resolved = {"id": pid}
            elif field in self._REFERENCE_FIELDS:
                from src.clients.resolver import parse_maybe_int
                ref_id = parse_maybe_int(value if not isinstance(value, dict) else value.get("id"))
                if ref_id is None:
                    raise RuntimeError(
                        f"{field} must be a numeric ID or {{'id': <id>}}. Got: {value!r}"
                    )
                resolved = {"id": ref_id}
            elif field == "customFields":
                resolved = _build_custom_fields_patch(ticket, value)
                if resolved is None:
                    continue
            else:
                resolved = value

            if ticket.get(field) != resolved:
                _op_set(ops, ticket, f"/{field}", resolved)

        if not ops:
            return {"ok": True, "ops": [], "dry_run": self.dry_run, "message": "No changes needed."}

        if self.dry_run:
            return {"ok": True, "ops": ops, "dry_run": True, "ticket_id": ticket_id}

        self.patch_ticket(ticket_id, ops)
        return {"ok": True, "ops": ops, "dry_run": False, "ticket_id": ticket_id}

    # ── Board / member helpers ────────────────────────────────────────────────

    def list_boards(self) -> List[Dict[str, Any]]:
        return self.get("service/boards", params={"pageSize": 200}) or []

    def list_board_statuses(self, board_id: int) -> List[Dict[str, Any]]:
        return (
            self.get(f"service/boards/{int(board_id)}/statuses", params={"pageSize": 200})
            or []
        )

    def list_members(self) -> List[Dict[str, Any]]:
        return self.get("system/members", params={"pageSize": 200}) or []

    def get_member(self, member_id: int) -> Dict[str, Any]:
        return self.get(f"system/members/{int(member_id)}")

    # ── Utility ───────────────────────────────────────────────────────────────

    def dump_json(self, payload: Any) -> str:
        return json.dumps(payload, indent=2, ensure_ascii=False)
