"""
src/tools/perception/technicians.py

Technician-centric perception tools.

Functions
---------
get_technician_schedule(cw, member_id, *, days_ahead)
    Query ConnectWise Schedule API for a tech's upcoming entries.

get_technician_workload(cw, mappings, *, member_id, all_techs)
    Count open tickets for one tech, or for all routable techs.

get_tech_availability(teams_client, identifier, *, member_id, data_dir)
    Query Microsoft Teams presence via Graph API.
    Resolves CW identifier → Teams user ID via DB, JSON mapping file,
    or live Graph API lookup by email (three-tier fallback).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# ── Teams presence → simple availability string ───────────────────────────────
# Graph availability values: Available, AvailableIdle, Away, BeRightBack,
# Busy, BusyIdle, DoNotDisturb, Offline, PresenceUnknown
_PRESENCE_MAP: Dict[str, str] = {
    "available":      "Available",
    "availableidle":  "Available",
    "away":           "Away",
    "berightback":    "Away",
    "busy":           "Busy",
    "busyidle":       "Busy",
    "donotdisturb":   "Busy",
    "offline":        "Offline",
    "presenceunknown": "Unknown",
}


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_hours(date_str: Optional[str]) -> Optional[float]:
    if not date_str:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(date_str[:19], fmt).replace(tzinfo=timezone.utc)
            return round((datetime.now(timezone.utc) - dt).total_seconds() / 3600, 1)
        except ValueError:
            continue
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Schedule
# ─────────────────────────────────────────────────────────────────────────────

def get_technician_schedule(
    cw,
    member_id: int,
    *,
    days_ahead: int = 2,
) -> Dict[str, Any]:
    """
    Fetch a technician's schedule from ConnectWise Schedule API.

    Uses GET /schedule/entries with conditions:
        member/id = {member_id}
        AND dateStart >= [now]
        AND dateStart <= [now + days_ahead]

    Args:
        cw:         CWManageClient instance.
        member_id:  ConnectWise member ID.
        days_ahead: How many days ahead to look (default 2, max 14).

    Returns:
        {
          "member_id": int,
          "days_ahead": int,
          "entries": [
            {type, subject, date_start, date_end,
             duration_hours, ticket_id, location, all_day}
          ],
          "has_conflicts": bool,   # True if any entries overlap today
          "busy_today": bool
        }
    """
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=max(1, min(int(days_ahead), 14)))

    conditions = (
        f"member/id = {int(member_id)}"
        f" AND dateStart >= [{_iso(now)}]"
        f" AND dateStart <= [{_iso(end)}]"
    )

    try:
        raw = cw.get(
            "schedule/entries",
            params={
                "conditions": conditions,
                "orderBy":    "dateStart asc",
                "pageSize":   100,
            },
        ) or []
    except Exception as exc:
        log.error("Schedule API failed for member %s: %s", member_id, exc)
        return {
            "member_id": member_id,
            "days_ahead": days_ahead,
            "entries": [],
            "has_conflicts": False,
            "busy_today": False,
            "error": str(exc),
        }

    entries = []
    busy_today = False
    today_end = now.replace(hour=23, minute=59, second=59)

    for e in (raw if isinstance(raw, list) else []):
        date_start = e.get("dateStart") or e.get("datestart") or ""
        date_end   = e.get("dateEnd")   or e.get("dateend")   or ""

        # duration in hours
        try:
            s = datetime.strptime(date_start[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            en = datetime.strptime(date_end[:19],   "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            duration_h = round((en - s).total_seconds() / 3600, 2)
            if s <= today_end:
                busy_today = True
        except (ValueError, TypeError):
            duration_h = None

        # Linked ticket or project ID
        ticket_id = None
        obj_type = (e.get("objectType") or e.get("type") or {})
        if isinstance(obj_type, dict) and obj_type.get("name") in ("Service Ticket", "Service"):
            ticket_id = e.get("objectId")

        entries.append({
            "type":           (e.get("type") or {}).get("name") if isinstance(e.get("type"), dict) else e.get("type"),
            "subject":        e.get("name") or e.get("subject") or e.get("description"),
            "date_start":     date_start,
            "date_end":       date_end,
            "duration_hours": duration_h,
            "ticket_id":      ticket_id or e.get("objectId"),
            "location":       (e.get("where") or {}).get("name") if isinstance(e.get("where"), dict) else e.get("where"),
            "all_day":        bool(e.get("allDay") or e.get("allDayFlag")),
        })

    return {
        "member_id":      member_id,
        "days_ahead":     days_ahead,
        "entries":        entries,
        "entry_count":    len(entries),
        "has_conflicts":  len(entries) > 0,
        "busy_today":     busy_today,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Workload
# ─────────────────────────────────────────────────────────────────────────────

def get_technician_workload(
    cw,
    mappings: Dict[str, Any],
    *,
    member_id: Optional[int] = None,
    all_techs: bool = False,
    max_workload_pct: float = 0.40,
) -> Dict[str, Any]:
    """
    Return open-ticket counts and oldest-ticket age for one or all techs.

    When member_id is provided: returns a single-tech dict.
    When all_techs=True (or member_id is None): fetches ALL open tickets
    once and groups by owner — more efficient than N serial API calls.

    Args:
        cw:                CWManageClient instance.
        mappings:          Mappings dict (members, agent_routing).
        member_id:         CW member ID for single-tech mode.
        all_techs:         Return all routable techs if True.
        max_workload_pct:  Fraction of total open tickets above which a tech
                           is considered overloaded (e.g. 0.40 = 40%).

    Returns (single-tech):
        {
          "member_id": int, "identifier": str,
          "open_tickets": int, "by_priority": {...},
          "oldest_ticket_age_hours": float|null,
          "overloaded": bool,
          "workload_threshold": int,   # computed ticket count limit
          "total_open_tickets": int,
          "workload_pct_limit": float,
        }

    Returns (all-techs):
        {
          "techs": [ ... same per-tech dicts ... ],
          "total_open": int,
          "workload_threshold": int,
          "workload_pct_limit": float,
        }
    """
    if member_id is not None and not all_techs:
        return _single_tech_workload(cw, mappings, member_id, max_workload_pct)

    # All-techs mode: one big fetch, group locally
    return _all_techs_workload(cw, mappings, max_workload_pct)


def _single_tech_workload(
    cw,
    mappings: Dict[str, Any],
    member_id: int,
    pct: float,
) -> Dict[str, Any]:
    import math
    # Fetch all open tickets to get a system-wide total for threshold computation,
    # then filter locally to this tech's tickets.
    try:
        all_tickets = cw.fetch_all_tickets(conditions="closedFlag = false", page_size=200)
    except Exception as exc:
        return {"member_id": member_id, "error": str(exc), "open_tickets": 0}

    total_open = len(all_tickets)
    threshold = max(1, math.ceil(total_open * pct))

    tech_tickets = [
        t for t in all_tickets
        if (t.get("owner") or {}).get("id") == member_id
    ]

    by_priority: Dict[str, int] = {}
    oldest_hours: Optional[float] = None
    for t in tech_tickets:
        p = (t.get("priority") or {}).get("name") or "Unknown"
        by_priority[p] = by_priority.get(p, 0) + 1
        age = _age_hours(t.get("dateEntered"))
        if age is not None and (oldest_hours is None or age > oldest_hours):
            oldest_hours = age

    ident = _reverse_member_lookup(mappings, member_id)

    return {
        "member_id":               member_id,
        "identifier":              ident,
        "open_tickets":            len(tech_tickets),
        "by_priority":             by_priority,
        "oldest_ticket_age_hours": oldest_hours,
        "overloaded":              len(tech_tickets) >= threshold,
        "workload_threshold":      threshold,
        "total_open_tickets":      total_open,
        "workload_pct_limit":      pct,
    }


def _all_techs_workload(
    cw,
    mappings: Dict[str, Any],
    pct: float,
) -> Dict[str, Any]:
    """
    Fetch ALL open tickets once, group by owner, return per-tech counts.
    Only includes techs marked routable=True in agent_routing.
    """
    routing = mappings.get("agent_routing") or {}
    member_map = {str(k).lower(): v for k, v in (mappings.get("members") or {}).items()}

    # Build set of routable member IDs
    routable: Dict[int, Dict] = {}
    for ident, info in routing.items():
        if not (isinstance(info, dict) and info.get("routable")):
            continue
        mid_raw = member_map.get(ident.lower())
        if mid_raw is None:
            continue
        try:
            mid = int(mid_raw)
            routable[mid] = {
                "identifier":   ident,
                "display_name": info.get("display_name", ident),
            }
        except (TypeError, ValueError):
            pass

    if not routable:
        return {"techs": [], "total_open": 0, "error": "No routable techs in mappings"}

    # Fetch all open tickets across all boards
    import math
    try:
        all_tickets = cw.fetch_all_tickets(
            conditions="closedFlag = false",
            order_by="dateEntered asc",
            page_size=200,
        )
    except Exception as exc:
        return {"techs": [], "total_open": 0, "error": str(exc)}

    total_open = len(all_tickets)
    threshold = max(1, math.ceil(total_open * pct))

    # Group by owner member_id
    buckets: Dict[int, List[Dict]] = {mid: [] for mid in routable}
    unassigned_count = 0

    for t in all_tickets:
        owner = t.get("owner") or {}
        oid = owner.get("id")
        if oid is None:
            unassigned_count += 1
            continue
        try:
            oid = int(oid)
        except (TypeError, ValueError):
            continue
        if oid in buckets:
            buckets[oid].append(t)

    techs = []
    for mid, tickets in buckets.items():
        by_priority: Dict[str, int] = {}
        oldest_hours: Optional[float] = None
        for t in tickets:
            p = (t.get("priority") or {}).get("name") or "Unknown"
            by_priority[p] = by_priority.get(p, 0) + 1
            age = _age_hours(t.get("dateEntered"))
            if age is not None and (oldest_hours is None or age > oldest_hours):
                oldest_hours = age

        techs.append({
            "member_id":               mid,
            "identifier":              routable[mid]["identifier"],
            "display_name":            routable[mid]["display_name"],
            "open_tickets":            len(tickets),
            "by_priority":             by_priority,
            "oldest_ticket_age_hours": oldest_hours,
            "overloaded":              len(tickets) >= threshold,
            "workload_threshold":      threshold,
        })

    # Sort by open_tickets ascending (least loaded first)
    techs.sort(key=lambda x: x["open_tickets"])

    return {
        "techs":              techs,
        "total_open":         sum(len(v) for v in buckets.values()),
        "unassigned_count":   unassigned_count,
        "workload_threshold": threshold,
        "workload_pct_limit": pct,
    }


def _reverse_member_lookup(mappings: Dict, member_id: int) -> Optional[str]:
    for k, v in (mappings.get("members") or {}).items():
        try:
            if int(v) == member_id:
                return str(k)
        except (TypeError, ValueError):
            pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Teams availability
# ─────────────────────────────────────────────────────────────────────────────

def _load_teams_mapping(data_dir: Path) -> Dict[str, str]:
    """
    Load data/teams_user_mapping.json.
    Returns {} if missing or malformed.
    Keys starting with '_' are metadata — ignored.
    """
    path = data_dir / "teams_user_mapping.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {k: v for k, v in raw.items() if not k.startswith("_")}
    except Exception as exc:
        log.warning("Failed to load teams_user_mapping.json: %s", exc)
        return {}


def _resolve_teams_user_id(
    identifier: str,
    member_id: Optional[int],
    teams_client,
    data_dir: Path,
) -> Optional[str]:
    """
    Three-tier lookup to resolve a CW identifier to a Teams Graph user ID:

    1. SQLite Technician.teams_user_id (if member_id known)
    2. data/teams_user_mapping.json
    3. Teams Graph API lookup by email (from CW member record)

    Returns None if all tiers fail.
    """
    # Tier 1: Database
    if member_id is not None:
        try:
            from src.clients.database import SessionLocal, Technician
            with SessionLocal() as session:
                tech = session.query(Technician).filter_by(cw_member_id=member_id).first()
                if tech and tech.teams_user_id:
                    log.debug("Teams user ID for %s resolved from DB", identifier)
                    return tech.teams_user_id
        except Exception as exc:
            log.debug("DB tier failed for %s: %s", identifier, exc)

    # Tier 2: JSON mapping file
    mapping = _load_teams_mapping(data_dir)
    if identifier in mapping:
        log.debug("Teams user ID for %s resolved from JSON mapping", identifier)
        return mapping[identifier]

    # Tier 3: Graph API lookup by email (requires knowing the tech's email)
    if teams_client is not None:
        # Try common email patterns: identifier@domain
        # If the CW username is an email alias, Graph can find them
        try:
            result = teams_client.get_user_by_email(identifier)
            if result and result.get("id"):
                log.debug("Teams user ID for %s resolved from Graph email lookup", identifier)
                return result["id"]
        except Exception as exc:
            log.debug("Graph email lookup failed for %s: %s", identifier, exc)

    return None


def get_tech_availability(
    teams_client,
    identifier: str,
    *,
    member_id: Optional[int] = None,
    data_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Get a technician's current Teams presence status.

    Resolves CW identifier → Teams Graph user ID via three-tier lookup
    (DB → JSON mapping file → Graph email search), then calls the
    Graph presence API.

    Args:
        teams_client: TeamsClient instance (may be None if Teams unconfigured).
        identifier:   CW member login identifier (e.g. "akloss").
        member_id:    CW member ID (helps with DB lookup).
        data_dir:     Project data directory for JSON mapping file.

    Returns:
        {
          "identifier":    str,
          "availability":  "Available"|"Busy"|"Away"|"Offline"|"Unknown",
          "activity":      str,        # raw Graph activity string
          "source":        str,        # how the user ID was resolved
          "teams_user_id": str|null
        }
    """
    _data_dir = data_dir or (Path(__file__).resolve().parent.parent.parent.parent / "data")

    base = {
        "identifier":    identifier,
        "availability":  "Unknown",
        "activity":      "Unknown",
        "source":        "none",
        "teams_user_id": None,
    }

    if teams_client is None:
        base["_note"] = "Teams client not configured (check TENANT_ID, TEAMS_CLIENT_ID, TEAMS_CLIENT_VALUE)"
        return base

    teams_user_id = _resolve_teams_user_id(identifier, member_id, teams_client, _data_dir)
    if not teams_user_id:
        base["_note"] = (
            "Could not resolve Teams user ID. "
            "Add entry to data/teams_user_mapping.json or set "
            "Technician.teams_user_id in the database."
        )
        return base

    try:
        presence = teams_client.get_user_presence(teams_user_id)
        raw_avail = (presence.get("availability") or "PresenceUnknown").lower()
        mapped = _PRESENCE_MAP.get(raw_avail, "Unknown")

        return {
            "identifier":    identifier,
            "availability":  mapped,
            "activity":      presence.get("activity", "Unknown"),
            "source":        "teams_graph",
            "teams_user_id": teams_user_id,
        }
    except Exception as exc:
        log.warning("Teams presence lookup failed for %s (user_id=%s): %s",
                    identifier, teams_user_id, exc)
        return {
            **base,
            "teams_user_id": teams_user_id,
            "source":        "error",
            "_error":        str(exc),
        }
