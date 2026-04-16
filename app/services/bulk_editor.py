"""
app/services/bulk_editor.py

Bulk ticket editor service.
Filters open tickets by board/status/summary/company and applies
configurable changes (assign, status, type, note) in bulk.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, List
from urllib.parse import urljoin

from dotenv import find_dotenv, load_dotenv

from app.core.connectwise import build_auth, build_headers, get_base_url, make_session
from app.core.config_manager import load_config, load_mappings
from app.core.state import broadcast, record_summary


def run_bulk_editor(params: Dict[str, Any], stop_event: threading.Event) -> None:
    """
    Bulk-edit ConnectWise tickets matching the given filter params.
    Logs progress via broadcast().
    Intended to be called from a background thread.
    """
    load_dotenv(find_dotenv(), override=True)

    from app.core.connectwise import check_credentials
    err = check_credentials()
    if err:
        broadcast(f"ERROR: {err}")
        return

    site    = get_base_url()
    auth    = build_auth()
    headers = build_headers()
    sess    = make_session()

    board_name     = params.get("board_name", "Dispatch")
    source_status  = params.get("source_status", "New")
    summary_any    = [t.strip().lower() for t in params.get("summary_any", []) if t.strip()]
    company_filter = params.get("company_filter", "").strip().lower()
    diagnostic     = bool(params.get("diagnostic", True))

    do_assign      = bool(params.get("do_assign", False))
    assign_to      = (params.get("assign_to") or "").strip()
    do_status      = bool(params.get("do_status", False))
    new_status     = (params.get("new_status") or "").strip()
    do_type        = bool(params.get("do_type", False))
    new_type       = (params.get("new_type") or "").strip()
    do_note        = bool(params.get("do_note", False))
    note_text      = (params.get("note_text") or "").strip()

    try:
        mappings = load_mappings(load_config()["mappings_path"])
    except Exception as e:
        broadcast(f"ERROR loading mappings: {e}")
        return

    boards_map = {str(k).lower(): int(v) for k, v in (mappings.get("boards") or {}).items()}
    board_id   = boards_map.get(board_name.lower())
    if board_id is None:
        broadcast(f"ERROR: Board '{board_name}' not found in mappings.")
        return

    status_key   = f"{board_name.lower()} statuses"
    statuses_map = {str(k).lower(): int(v) for k, v in (mappings.get(status_key) or {}).items()}
    members_map  = {str(k).lower(): int(v) for k, v in (mappings.get("members") or {}).items()}

    # Resolve type ID: DB is primary source; mappings.json is legacy fallback
    new_type_id: Optional[int] = None
    if do_type and new_type:
        from src.clients.database import lookup_support_type
        new_type_id = lookup_support_type(new_type)
        if new_type_id is None:
            # Legacy fallback: board-scoped then global "support types" in mappings.json
            type_map: Dict[str, int] = {}
            for tkey in (f"{board_name.lower()} types", "support types"):
                type_map.update(
                    {str(k).lower(): int(v) for k, v in (mappings.get(tkey) or {}).items()}
                )
            new_type_id = type_map.get(new_type.lower())

    new_status_id    = statuses_map.get(new_status.lower()) if do_status and new_status else None
    assign_member_id = members_map.get(assign_to.lower())   if do_assign and assign_to  else None

    broadcast("=" * 60)
    broadcast(f"Bulk Ticket Editor  \u2014  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    broadcast(f"Board:          {board_name} (id={board_id})")
    broadcast(f"Source status:  {source_status}")
    broadcast(f"Summary filter: {summary_any or '(any)'}")
    broadcast(f"Company filter: {company_filter or '(any)'}")
    broadcast(f"DIAGNOSTIC:     {diagnostic}")
    broadcast("=" * 60)

    cond = f'board/id = {board_id} AND status/name = "{source_status}" AND closedFlag = false'
    all_tickets: List[Dict] = []
    page = 1
    while True:
        if stop_event.is_set():
            break
        r = sess.get(
            urljoin(site + "/", "service/tickets"),
            auth=auth,
            headers=headers,
            timeout=20,
            params={"conditions": cond, "orderBy": "dateEntered asc", "pageSize": 200, "page": page},
        )
        if not r.ok:
            broadcast(f"ERROR fetching: HTTP {r.status_code}: {r.text[:200]}")
            break
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break
        all_tickets.extend(batch)
        if len(batch) < 200:
            break
        page += 1

    filtered = []
    for t in all_tickets:
        summary = (t.get("summary") or "").lower()
        co_name = ((t.get("company") or {}).get("name") or "").lower()
        if summary_any and not any(term in summary for term in summary_any):
            continue
        if company_filter and company_filter not in co_name:
            continue
        filtered.append(t)

    broadcast(f"\nFetched {len(all_tickets)}, after filters: {len(filtered)} tickets\n")
    if not filtered:
        broadcast("No tickets match. Done.")
        return

    processed = errors = 0
    for ticket in filtered:
        if stop_event.is_set():
            broadcast("\nStopped by user.")
            break

        tid     = ticket["id"]
        summary = (ticket.get("summary") or "").strip()[:70]
        co_name = ((ticket.get("company") or {}).get("name") or "")
        broadcast(f"  Ticket #{tid} | {co_name} | {summary}")

        ops: List[Dict] = []
        if do_assign and assign_member_id:
            owner_exists = isinstance(ticket.get("owner"), dict) and ticket["owner"].get("id")
            ops.append({
                "op": "replace" if owner_exists else "add",
                "path": "/owner",
                "value": {"id": assign_member_id},
            })
        if do_status and new_status_id:
            ops.append({"op": "replace", "path": "/status", "value": {"id": new_status_id}})
        if do_type and new_type_id:
            type_exists = isinstance(ticket.get("type"), dict)
            ops.append({
                "op": "replace" if type_exists else "add",
                "path": "/type",
                "value": {"id": new_type_id},
            })

        if ops:
            if diagnostic:
                broadcast(f"    [DIAGNOSTIC] Would PATCH: {[o['path'] for o in ops]}")
            else:
                r = sess.patch(
                    urljoin(site + "/", f"service/tickets/{tid}"),
                    auth=auth, headers=headers, json=ops, timeout=20,
                )
                if not r.ok:
                    broadcast(f"    ERROR PATCH: HTTP {r.status_code}")
                    errors += 1
                    continue

        if do_note and note_text:
            if diagnostic:
                broadcast(f"    [DIAGNOSTIC] Would add note: {note_text[:80]}")
            else:
                body = {
                    "text": note_text,
                    "detailDescriptionFlag": False,
                    "internalAnalysisFlag":  True,
                    "resolutionFlag":        False,
                }
                r = sess.post(
                    urljoin(site + "/", f"service/tickets/{tid}/notes"),
                    auth=auth, headers=headers, json=body, timeout=20,
                )
                if not r.ok:
                    broadcast(f"    WARN note failed: HTTP {r.status_code}")

        processed += 1
        broadcast(f"    {'[DIAGNOSTIC]' if diagnostic else 'Done.'}")
        time.sleep(0.1)

    broadcast("\n" + "=" * 60)
    broadcast(
        f"Bulk edit complete \u2014 Processed: {processed}  "
        f"Errors: {errors}  Mode: {'DIAGNOSTIC' if diagnostic else 'LIVE'}"
    )
    broadcast("=" * 60)

    record_summary({"routed": processed, "skipped": 0, "errors": errors, "dry_run": diagnostic})
