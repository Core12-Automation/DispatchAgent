"""
src/tools/perception/tickets.py

Ticket-centric perception tools.

Functions
---------
get_new_tickets(cw, config, mappings, *, priority_filter, limit)
    Fetch all unrouted / unassigned tickets using the same board/status/owner
    logic as services/router.py but cleaned up and decoupled from SSE.

get_single_ticket_history(cw, ticket_id, *, include_audit)
    Return all notes and audit entries for a single ticket.
    Used by the agent when it needs context on a specific ticket.

get_ticket_history(cw, *, company_id, member_id, days)
    Search closed/recent tickets for a company or assigned member.
    Adapted from services/search.py — pages through tickets and
    extracts resolution info.  Returns a compact summary list.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# ── Priority sort order (CW names → rank, lower = more urgent) ───────────────
_PRIORITY_RANK: Dict[str, int] = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}


def _priority_rank(ticket: Dict) -> int:
    name = ((ticket.get("priority") or {}).get("name") or "low").lower()
    return _PRIORITY_RANK.get(name, 99)


def _slim(t: Dict) -> Dict:
    """Minimal ticket representation for agent consumption."""
    return {
        "id":           t.get("id"),
        "summary":      (t.get("summary") or "")[:120],
        "priority":     (t.get("priority") or {}).get("name"),
        "company":      (t.get("company") or {}).get("name"),
        "board":        (t.get("board") or {}).get("name"),
        "status":       (t.get("status") or {}).get("name"),
        "owner":        (t.get("owner") or {}).get("identifier"),
        "date_entered": t.get("dateEntered"),
        "type":         (t.get("type") or {}).get("name"),
    }


def _extract_identifier(field) -> Optional[str]:
    """CW returns member/createdBy as either a dict {id, identifier} or a plain string."""
    if field is None:
        return None
    if isinstance(field, dict):
        return field.get("identifier") or field.get("name")
    return str(field)  # plain string — it IS the identifier


def _needs_routing(ticket: Dict, unrouted_ids: set) -> bool:
    """True if the ticket has no owner or is owned by a bot/queue account."""
    owner = ticket.get("owner")
    if owner is None:
        return True
    owner_id = owner.get("id") if isinstance(owner, dict) else None
    if owner_id is None:
        return True
    return int(owner_id) in unrouted_ids


# ─────────────────────────────────────────────────────────────────────────────

def get_new_tickets(
    cw,
    config: Dict[str, Any],
    mappings: Dict[str, Any],
    *,
    priority_filter: Optional[str] = None,
    limit: int = 20,
) -> Dict[str, Any]:
    """
    Fetch all tickets that are sitting unrouted in the dispatch queue.

    Logic mirrors services/router.py:
      - Scans boards listed in config["boards_to_scan"]
      - Matches statuses in config["route_from_statuses"]
      - Skips tickets already owned by a real tech
      - Treats tickets owned by bot/queue accounts as unrouted
        (via config["unrouted_owner_identifiers"])

    Args:
        cw:              CWManageClient instance.
        config:          Portal config dict.
        mappings:        Mappings dict (boards, members, statuses).
        priority_filter: Optional CW priority name to restrict results,
                         e.g. "Critical" or "High".
        limit:           Maximum tickets to return (default 20, max 500).

    Returns:
        {
          "total_unrouted": int,
          "boards_scanned": [...],
          "tickets": [
            {id, summary, priority, company, board, status,
             owner, date_entered, type}
          ]
        }
    """
    boards_cfg: List[str] = config.get("boards_to_scan", [])
    route_from: List[str] = config.get("route_from_statuses", [
        "New", "New (Email connector)", "New (email connector)"
    ])
    unrouted_idents: List[str] = config.get("unrouted_owner_identifiers", [])
    limit = min(int(limit), 500)

    # Build set of "bot" member IDs to treat as unowned
    board_map = {str(k).lower(): int(v) for k, v in (mappings.get("boards") or {}).items()}
    member_map = {str(k).lower(): v for k, v in (mappings.get("members") or {}).items()}

    unrouted_ids: set = set()
    for ident in unrouted_idents:
        val = member_map.get(ident.lower())
        if val is not None:
            try:
                unrouted_ids.add(int(val))
            except (TypeError, ValueError):
                pass

    all_tickets: List[Dict] = []
    boards_scanned: List[str] = []

    for board_name in boards_cfg:
        board_id = board_map.get(board_name.lower())
        if board_id is None:
            log.warning("Board %r not found in mappings.boards — skipping", board_name)
            continue

        boards_scanned.append(board_name)
        status_cond = " OR ".join(f'status/name = "{s}"' for s in route_from)
        conditions = f"board/id = {board_id} AND ({status_cond}) AND closedFlag = false"

        if priority_filter:
            conditions += f' AND priority/name = "{priority_filter}"'

        try:
            batch = cw.fetch_all_tickets(
                conditions=conditions,
                order_by="dateEntered asc",
                page_size=200,
            )
            all_tickets.extend(batch)
        except Exception as exc:
            log.error("Failed to fetch tickets from board %r: %s", board_name, exc)

    # Keep only unrouted, sort by priority then age, truncate
    candidates = [t for t in all_tickets if _needs_routing(t, unrouted_ids)]
    candidates.sort(key=_priority_rank)
    candidates = candidates[:limit]

    return {
        "total_unrouted": len(candidates),
        "boards_scanned": boards_scanned,
        "tickets": [_slim(t) for t in candidates],
    }


# ─────────────────────────────────────────────────────────────────────────────

def get_single_ticket_history(
    cw,
    ticket_id: int,
    *,
    include_audit: bool = True,
) -> Dict[str, Any]:
    """
    Return the full history of a single ticket: notes and audit trail.

    Args:
        cw:            CWManageClient instance.
        ticket_id:     ConnectWise ticket ID.
        include_audit: Include audit trail (owner/status changes).

    Returns:
        {
          ticket_id, summary, status, owner, date_entered,
          notes: [{id, text, internal, created_by, date}],
          audit_trail: [{action, member, date}]
        }
    """
    ticket = cw.get_ticket(ticket_id)
    notes_raw = cw.get_ticket_notes(ticket_id) or []

    notes = [
        {
            "id":         n.get("id"),
            "text":       (n.get("text") or "")[:600],
            "internal":   bool(n.get("internalAnalysisFlag")),
            "resolution": bool(n.get("resolutionFlag")),
            "created_by": _extract_identifier(n.get("createdBy") or n.get("member")),
            "date": (
                n.get("dateCreated")
                or (n.get("_info") or {}).get("dateCreated")
            ),
        }
        for n in notes_raw
    ]

    audit: List[Dict] = []
    if include_audit:
        try:
            raw_audit = cw.get_audit_trail(ticket_id) or []
            audit = [
                {
                    "action": a.get("text"),
                    "member": (
                        a.get("memberIdentifier")
                        or _extract_identifier(a.get("member"))
                    ),
                    "date": a.get("auditDate") or a.get("dateTime"),
                }
                for a in raw_audit
            ][:30]
        except Exception as exc:
            log.debug("Audit trail unavailable for ticket %s: %s", ticket_id, exc)

    return {
        "ticket_id":    ticket_id,
        "summary":      ticket.get("summary"),
        "status":       (ticket.get("status") or {}).get("name"),
        "owner":        (ticket.get("owner") or {}).get("identifier"),
        "date_entered": ticket.get("dateEntered"),
        "notes":        notes,
        "audit_trail":  audit,
    }


# ─────────────────────────────────────────────────────────────────────────────

def get_ticket_history(
    cw,
    *,
    company_id: Optional[int] = None,
    member_id: Optional[int] = None,
    days: int = 30,
    max_results: int = 50,
) -> Dict[str, Any]:
    """
    Search recent/closed tickets for a specific company or technician.

    Pages through CW tickets (newest first) looking for closed tickets
    in the last ``days`` days, filtered by company and/or assigned member.
    For each matched ticket, extracts resolution notes.

    Adapted from services/search.py pagination logic.

    Args:
        cw:          CWManageClient instance.
        company_id:  CW company ID to filter by, or None for any.
        member_id:   CW member ID (owner) to filter by, or None for any.
        days:        How many days back to look (default 30).
        max_results: Cap on returned tickets (default 50).

    Returns:
        {
          "tickets_found": int,
          "days_searched": int,
          "tickets": [
            {id, summary, assigned_to, company,
             closed_date, resolution, priority}
          ]
        }
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    cond_parts = [
        "closedFlag = true",
        f"dateEntered >= [{cutoff}]",
    ]
    if company_id is not None:
        cond_parts.append(f"company/id = {company_id}")
    if member_id is not None:
        cond_parts.append(f"owner/id = {member_id}")

    conditions = " AND ".join(f"({p})" for p in cond_parts)

    try:
        raw = cw.fetch_all_tickets(
            conditions=conditions,
            order_by="_info/lastUpdated desc",
            page_size=200,
        )
    except Exception as exc:
        log.error("get_ticket_history fetch failed: %s", exc)
        return {"tickets_found": 0, "days_searched": days, "tickets": [], "error": str(exc)}

    results: List[Dict] = []
    for t in raw[:max_results]:
        # Try to get the resolution note text (notes with resolutionFlag=True)
        resolution_text: Optional[str] = None
        try:
            notes = cw.get_ticket_notes(t["id"]) or []
            for n in notes:
                if n.get("resolutionFlag"):
                    resolution_text = (n.get("text") or "")[:300]
                    break
        except Exception:
            pass
        time.sleep(0.02)  # Rate-limit courtesy between per-ticket calls

        # Extract closed date from various possible field names
        closed_date = (
            t.get("closedDate")
            or t.get("dateResolved")
            or (t.get("_info") or {}).get("dateModified")
        )

        results.append({
            "id":          t.get("id"),
            "summary":     (t.get("summary") or "")[:120],
            "priority":    (t.get("priority") or {}).get("name"),
            "company":     (t.get("company") or {}).get("name"),
            "assigned_to": (t.get("owner") or {}).get("identifier"),
            "closed_date": closed_date,
            "resolution":  resolution_text,
        })

    return {
        "tickets_found": len(results),
        "days_searched": days,
        "tickets": results,
    }
