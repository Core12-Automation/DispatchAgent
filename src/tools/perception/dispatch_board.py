"""
src/tools/perception/dispatch_board.py

Dispatch board perception tool.

get_dispatch_board(cw, config, mappings, *, include_closed)
    Fetch all open tickets across configured boards and return a
    structured snapshot grouped by:  board → status → assigned tech

    Also computes per-tech and per-board summary counts, and flags any
    tickets older than 24 hours that haven't been updated (potential SLA risk).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


def _age_hours(date_str: Optional[str]) -> Optional[float]:
    """Return age in hours from an ISO-8601 date string, or None if unparseable."""
    if not date_str:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            dt = datetime.strptime(date_str[:26], fmt).replace(tzinfo=timezone.utc)
            return round((datetime.now(timezone.utc) - dt).total_seconds() / 3600, 1)
        except ValueError:
            continue
    return None


def _slim_board_ticket(t: Dict) -> Dict:
    age = _age_hours(t.get("dateEntered"))
    last_update = _age_hours(
        (t.get("_info") or {}).get("lastUpdated") or t.get("dateEntered")
    )
    return {
        "id":               t.get("id"),
        "summary":          (t.get("summary") or "")[:100],
        "priority":         (t.get("priority") or {}).get("name"),
        "company":          (t.get("company") or {}).get("name"),
        "type":             (t.get("type") or {}).get("name"),
        "age_hours":        age,
        "hours_since_update": last_update,
        "sla_risk":         bool(last_update is not None and last_update > 24),
    }


def get_dispatch_board(
    cw,
    config: Dict[str, Any],
    mappings: Dict[str, Any],
    *,
    include_closed: bool = False,
    boards: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Fetch all open tickets across configured boards, grouped by
    board → status → assigned tech.

    Args:
        cw:            CWManageClient instance.
        config:        Portal config dict.
        mappings:      Mappings dict.
        include_closed: Also include closed tickets (default False).
        boards:        Override which boards to scan. Defaults to
                       config["boards_to_scan"] plus config["route_to_board"].

    Returns:
        {
          "total_open": int,
          "boards": {
            "<board_name>": {
              "total": int,
              "sla_at_risk": int,
              "by_status": {
                "<status_name>": {
                  "total": int,
                  "by_tech": {
                    "<tech_identifier>": {
                      "count": int,
                      "tickets": [{id, summary, priority, company,
                                   age_hours, hours_since_update, sla_risk}]
                    }
                  }
                }
              }
            }
          },
          "workload_summary": {
            "<tech_identifier>": {
              "total": int,
              "by_priority": {"Critical": N, "High": N, ...}
            }
          }
        }
    """
    board_map = {
        str(k).lower(): int(v)
        for k, v in (mappings.get("boards") or {}).items()
    }

    # Collect boards to scan: configured scan boards + destination board
    boards_to_scan: List[str] = boards or []
    if not boards_to_scan:
        boards_to_scan = list(config.get("boards_to_scan", []))
        route_to = config.get("route_to_board", "")
        if route_to and route_to not in boards_to_scan:
            boards_to_scan.append(route_to)

    closed_clause = "" if include_closed else " AND closedFlag = false"
    board_data: Dict[str, Any] = {}
    workload: Dict[str, Dict] = {}
    total_open = 0

    for board_name in boards_to_scan:
        board_id = board_map.get(board_name.lower())
        if board_id is None:
            log.warning("Board %r not found in mappings — skipping", board_name)
            continue

        conditions = f"board/id = {board_id}{closed_clause}"
        try:
            tickets = cw.fetch_all_tickets(conditions=conditions, page_size=200)
        except Exception as exc:
            log.error("Failed to fetch board %r: %s", board_name, exc)
            board_data[board_name] = {"error": str(exc)}
            continue

        total_open += len(tickets)
        by_status: Dict[str, Any] = {}
        sla_at_risk = 0

        for t in tickets:
            status_name = (t.get("status") or {}).get("name") or "Unknown"
            owner = t.get("owner") or {}
            tech_id = (
                owner.get("identifier")
                or owner.get("name")
                or "unassigned"
            )

            slim = _slim_board_ticket(t)
            if slim["sla_risk"]:
                sla_at_risk += 1

            # Group: by_status → by_tech
            if status_name not in by_status:
                by_status[status_name] = {"total": 0, "by_tech": {}}
            by_status[status_name]["total"] += 1

            tech_bucket = by_status[status_name]["by_tech"]
            if tech_id not in tech_bucket:
                tech_bucket[tech_id] = {"count": 0, "tickets": []}
            tech_bucket[tech_id]["count"] += 1
            tech_bucket[tech_id]["tickets"].append(slim)

            # Global workload summary
            if tech_id not in workload:
                workload[tech_id] = {"total": 0, "by_priority": {}}
            workload[tech_id]["total"] += 1
            prio = (t.get("priority") or {}).get("name") or "Unknown"
            workload[tech_id]["by_priority"][prio] = (
                workload[tech_id]["by_priority"].get(prio, 0) + 1
            )

        board_data[board_name] = {
            "total": len(tickets),
            "sla_at_risk": sla_at_risk,
            "by_status": by_status,
        }

    return {
        "total_open": total_open,
        "boards": board_data,
        "workload_summary": workload,
    }
