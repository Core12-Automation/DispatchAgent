"""
app/services/report/pipeline.py

Data pipeline for the ticket report:
  - extract_ticket_rows()   — map raw CW API dicts → TicketRow objects
  - apply_filters()         — filter rows by the Filters spec
  - compute_metrics()       — aggregate into a Metrics object

Also contains all close-history reconstruction logic (audit trail parsing).
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import config as cfg
from app.services.report.models import (
    CloseHistoryResult,
    Filters,
    Metrics,
    TicketRow,
)


# ── Utility functions ─────────────────────────────────────────────────────────

def iso_to_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    s = s.strip()
    try:
        if s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        return datetime.fromisoformat(s)
    except Exception:
        return None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def norm(s: str) -> str:
    return "".join(ch.lower() for ch in (s or "").strip())


def safe_get(d: Dict[str, Any], *keys: str) -> Any:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def parse_int(x: Any) -> Optional[int]:
    try:
        return int(x)
    except Exception:
        return None


def parse_str(x: Any) -> str:
    return "" if x is None else str(x)


def match_any_loose(value: str, patterns) -> bool:
    v = norm(value)
    if not v:
        return False
    for p in patterns:
        p2 = norm(p)
        if p2 and p2 in v:
            return True
    return False


def norm_id(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def is_excluded_assignee(name: str) -> bool:
    return norm_id(name) in cfg.EXCLUDED_ASSIGNEES


def distribute_evenly(total: int, keys: List[str]) -> Dict[str, int]:
    if total <= 0 or not keys:
        return {k: 0 for k in keys}
    n    = len(keys)
    base = total // n
    rem  = total % n
    out  = {k: base for k in keys}
    for k in keys[:rem]:
        out[k] += 1
    return out


def split_resources_field(resources: str) -> List[str]:
    s = (resources or "").strip()
    if not s:
        return []
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    s = s.replace('"', "").replace("'", "")
    parts = re.split(r"[,\n;/|]+", s)
    return [p.strip() for p in parts if p.strip()]


def get_all_assignees(owner_ident: str, resources: str) -> List[str]:
    names: List[str] = []
    if owner_ident and owner_ident.strip():
        names.append(owner_ident.strip())
    names.extend(split_resources_field(resources))
    seen: set = set()
    uniq: List[str] = []
    for n in names:
        key = norm_id(n)
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(n.strip())
    return uniq


def priority_level_1_to_6(priority_name: str, priority_id: Optional[int]) -> Optional[int]:
    s = (priority_name or "").replace("\xa0", " ").strip()
    if not s:
        return None
    for pattern in (r"(?i)\bP\s*([1-6])\b", r"(?i)\bPriority\s*([1-6])\b", r"\b([1-6])\b"):
        m = re.search(pattern, s)
        if m:
            return int(m.group(1))
    return None


def fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None or math.isnan(seconds) or seconds < 0:
        return "\u2014"
    if cfg.SHOW_TIME_UNITS == "hours":
        return f"{seconds/3600:.2f} h"
    if cfg.SHOW_TIME_UNITS == "days":
        return f"{seconds/86400:.2f} d"
    if seconds < 3600:
        return f"{seconds/60:.1f} min"
    if seconds < 86400:
        return f"{seconds/3600:.2f} h"
    return f"{seconds/86400:.2f} d"


def canonical_company_name(company_name: str, company_identifier: str) -> str:
    cn = company_name.strip() if company_name else ""
    ci = company_identifier.strip() if company_identifier else ""
    for canonical, aliases in cfg.COMPANY_ALIASES.items():
        if match_any_loose(cn, [canonical]) or match_any_loose(ci, [canonical]):
            return canonical
    hay = " | ".join([cn, ci])
    for canonical, aliases in cfg.COMPANY_ALIASES.items():
        if match_any_loose(hay, [canonical] + aliases):
            return canonical
    return cn or ci or "Unknown"


# ── Close history reconstruction ──────────────────────────────────────────────

_RE_STATUS_TRANSITION = re.compile(
    r'Status has been updated from\s+"(?P<from>[^"]*)"\s+to\s+"(?P<to>[^"]*)"\.?',
    re.IGNORECASE,
)


def _norm_token(s: Any) -> str:
    return "".join(ch for ch in str(s or "").lower() if ch.isalnum())


def _status_is_closed(status_name: str) -> bool:
    status_norm = norm(parse_str(status_name))
    if not status_norm:
        return False
    return any(norm(name) == status_norm for name in cfg.CLOSED_STATUS_NAME_MATCHES)


def _extract_possible_ints(node: Any) -> List[int]:
    out: List[int] = []
    if isinstance(node, int):
        out.append(int(node))
    elif isinstance(node, str) and node.strip().isdigit():
        out.append(int(node.strip()))
    elif isinstance(node, dict):
        for value in node.values():
            out.extend(_extract_possible_ints(value))
    elif isinstance(node, list):
        for value in node:
            out.extend(_extract_possible_ints(value))
    return out


def resolve_cw_consultant_member_ids(mappings: Dict[str, Any]) -> set:
    target_tokens = {
        _norm_token(v)
        for v in (cfg.CW_CONSULTANT_NAME_MATCHES + cfg.CW_CONSULTANT_IDENTIFIER_MATCHES)
        if parse_str(v).strip()
    }
    found: set = set()
    override_id = parse_int(cfg.CW_CONSULTANT_MEMBER_ID_OVERRIDE)
    if override_id is not None:
        found.add(override_id)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            candidate_fields = [
                parse_str(node.get("name")),
                parse_str(node.get("identifier")),
                parse_str(node.get("memberName")),
                parse_str(node.get("memberIdentifier")),
                parse_str(node.get("displayName")),
                parse_str(node.get("enteredBy")),
                parse_str(node.get("updatedBy")),
            ]
            if any(_norm_token(v) in target_tokens for v in candidate_fields if v):
                for key in ("id", "memberId", "member_id", "memberRecId", "recId", "value"):
                    candidate_id = parse_int(node.get(key))
                    if candidate_id is not None:
                        found.add(candidate_id)
                for child in node.values():
                    for candidate_id in _extract_possible_ints(child):
                        found.add(candidate_id)
            for key, value in node.items():
                if _norm_token(key) in target_tokens:
                    direct_id = parse_int(value)
                    if direct_id is not None:
                        found.add(direct_id)
                    else:
                        for candidate_id in _extract_possible_ints(value):
                            found.add(candidate_id)
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(mappings or {})
    return found


def _audit_actor_matches_cw_consultant(
    audit_entry: Dict[str, Any], cw_consultant_member_ids: Optional[set]
) -> bool:
    entered_by = parse_str(audit_entry.get("enteredBy"))
    if match_any_loose(
        entered_by,
        cfg.CW_CONSULTANT_NAME_MATCHES + cfg.CW_CONSULTANT_IDENTIFIER_MATCHES,
    ):
        return True
    if cw_consultant_member_ids:
        for key in ("memberId", "member_id", "enteredById", "id"):
            candidate_id = parse_int(audit_entry.get(key))
            if candidate_id is not None and candidate_id in cw_consultant_member_ids:
                return True
    return False


def _ticket_has_merge_admin_signal(
    ticket_item: Dict[str, Any], cw_consultant_member_ids: Optional[set]
) -> bool:
    t    = ticket_item.get("ticket") or {}
    info = t.get("_info") or {}
    if bool(t.get("hasMergedChildTicketFlag")):
        return True
    updated_by = parse_str(info.get("updatedBy"))
    if match_any_loose(
        updated_by,
        cfg.CW_CONSULTANT_NAME_MATCHES + cfg.CW_CONSULTANT_IDENTIFIER_MATCHES,
    ):
        return True
    updated_by_id = parse_int(info.get("updatedById"))
    if (
        updated_by_id is not None
        and cw_consultant_member_ids
        and updated_by_id in cw_consultant_member_ids
    ):
        return True
    return False


def reconstruct_ticket_close_history(
    ticket_item: Dict[str, Any],
    *,
    date_entered: Optional[datetime],
    closed_flag: bool,
    fallback_closed_time: Optional[datetime],
    cw_consultant_member_ids: Optional[set] = None,
) -> CloseHistoryResult:
    if not cfg.NORMALIZE_AUDIT_CLOSE_HISTORY:
        total = (
            (fallback_closed_time - date_entered).total_seconds()
            if fallback_closed_time and date_entered
            else None
        )
        return CloseHistoryResult(
            selected_close_time=fallback_closed_time,
            effective_close_for_duration=fallback_closed_time,
            total_open_seconds=total,
            saw_real_reopen=False,
        )

    trail = ticket_item.get("auditTrail")
    if not isinstance(trail, list):
        total = (
            (fallback_closed_time - date_entered).total_seconds()
            if fallback_closed_time and date_entered
            else None
        )
        return CloseHistoryResult(
            selected_close_time=fallback_closed_time,
            effective_close_for_duration=fallback_closed_time,
            total_open_seconds=total,
            saw_real_reopen=False,
        )

    status_events: List[Tuple[datetime, str, str, Dict[str, Any]]] = []
    for a in trail:
        if not isinstance(a, dict):
            continue
        text = parse_str(a.get("text"))
        if not text:
            continue
        m = _RE_STATUS_TRANSITION.search(text)
        if not m:
            continue
        dt = iso_to_dt(parse_str(a.get("enteredDate")))
        if not dt:
            continue
        status_events.append(
            (dt, parse_str(m.group("from")).strip(), parse_str(m.group("to")).strip(), a)
        )
    status_events.sort(key=lambda x: x[0])

    if not status_events:
        total = (
            (fallback_closed_time - date_entered).total_seconds()
            if fallback_closed_time and date_entered
            else None
        )
        return CloseHistoryResult(
            selected_close_time=fallback_closed_time,
            effective_close_for_duration=fallback_closed_time,
            total_open_seconds=total,
            saw_real_reopen=False,
        )

    merge_admin_signal = _ticket_has_merge_admin_signal(ticket_item, cw_consultant_member_ids)
    current_open_start = date_entered
    total_open_seconds = 0.0
    last_real_close: Optional[datetime] = None
    saw_real_reopen = False

    for dt, from_status, to_status, audit_entry in status_events:
        from_closed       = _status_is_closed(from_status)
        to_closed         = _status_is_closed(to_status)
        actor_is_consultant = _audit_actor_matches_cw_consultant(audit_entry, cw_consultant_member_ids)

        if cfg.IGNORE_DUPLICATE_CLOSED_TO_CLOSED_EVENTS and from_closed and to_closed:
            continue

        if (
            cfg.IGNORE_CW_CONSULTANT_CLOSE_TOUCHES
            and actor_is_consultant
            and merge_admin_signal
            and last_real_close is not None
            and current_open_start is None
            and to_closed
        ):
            continue

        if to_closed and not from_closed:
            if current_open_start is not None and dt >= current_open_start:
                total_open_seconds += (dt - current_open_start).total_seconds()
            elif (
                last_real_close is not None
                and actor_is_consultant
                and merge_admin_signal
                and cfg.IGNORE_CW_CONSULTANT_CLOSE_TOUCHES
            ):
                continue
            last_real_close    = dt
            current_open_start = None
            continue

        if from_closed and not to_closed:
            if last_real_close is not None:
                saw_real_reopen = True
                if current_open_start is None:
                    current_open_start = dt
            continue

    selected_close_time = last_real_close if closed_flag else None
    if selected_close_time is None and closed_flag:
        selected_close_time = fallback_closed_time

    total_open_seconds_out: Optional[float] = None
    effective_close_for_duration = selected_close_time

    if selected_close_time is not None and date_entered is not None:
        if cfg.SUM_ONLY_TRUE_OPEN_PERIODS_FOR_REAL_REOPENS and saw_real_reopen:
            total_open_seconds_out = max(total_open_seconds, 0.0)
            effective_close_for_duration = date_entered + timedelta(seconds=total_open_seconds_out)
        else:
            total_open_seconds_out = max(
                (selected_close_time - date_entered).total_seconds(), 0.0
            )
            effective_close_for_duration = selected_close_time

    return CloseHistoryResult(
        selected_close_time=selected_close_time,
        effective_close_for_duration=effective_close_for_duration,
        total_open_seconds=total_open_seconds_out,
        saw_real_reopen=saw_real_reopen,
    )


# ── Data pipeline ─────────────────────────────────────────────────────────────

def extract_ticket_rows(
    raw: Dict[str, Any], mappings: Optional[Dict[str, Any]] = None
) -> List[TicketRow]:
    rows: List[TicketRow] = []
    tickets = raw.get("tickets", [])
    if not isinstance(tickets, list):
        return rows

    cw_consultant_member_ids = resolve_cw_consultant_member_ids(mappings or {})

    for item in tickets:
        if not isinstance(item, dict):
            continue
        t        = item.get("ticket", {}) or {}
        comp     = t.get("company",  {}) or {}
        board    = t.get("board",    {}) or {}
        status   = t.get("status",   {}) or {}
        priority = t.get("priority", {}) or {}
        owner    = t.get("owner",    {}) or {}
        team     = t.get("team",     {}) or {}
        source   = t.get("source",   {}) or {}
        computed = item.get("computed", {}) or {}

        ticket_id = parse_int(t.get("id")) or parse_int(computed.get("ticket_id")) or -1
        if ticket_id < 0:
            continue

        company_name       = parse_str(comp.get("name"))
        company_identifier = parse_str(comp.get("identifier"))
        company_canon      = canonical_company_name(company_name, company_identifier)

        date_entered = (
            iso_to_dt(parse_str(computed.get("date_entered_utc")))
            or iso_to_dt(parse_str(safe_get(t, "_info", "dateEntered")))
            or iso_to_dt(parse_str(t.get("dateEntered")))
        )
        fallback_closed_time = (
            iso_to_dt(parse_str(computed.get("date_closed_utc")))
            or iso_to_dt(parse_str(t.get("closedDate")))
            or iso_to_dt(parse_str(safe_get(t, "_info", "dateClosed")))
        )

        ticket_closed_flag_raw = computed.get("closedFlag")
        if ticket_closed_flag_raw is None:
            ticket_closed_flag_raw = t.get("closedFlag")
        closed_flag = bool(ticket_closed_flag_raw)
        if ticket_closed_flag_raw is None:
            closed_flag = (
                _status_is_closed(parse_str(status.get("name")))
                or fallback_closed_time is not None
            )

        close_history = reconstruct_ticket_close_history(
            item,
            date_entered=date_entered,
            closed_flag=closed_flag,
            fallback_closed_time=fallback_closed_time,
            cw_consultant_member_ids=cw_consultant_member_ids,
        )

        first_contact = iso_to_dt(parse_str(computed.get("first_contact_time_utc")))
        plevel = (
            parse_int(computed.get("priority_level_1_6"))
            or parse_int(computed.get("priority_level_1_5"))
        )
        if plevel is None:
            plevel = priority_level_1_to_6(
                parse_str(priority.get("name")), parse_int(priority.get("id"))
            )

        rows.append(
            TicketRow(
                ticket_id=ticket_id,
                summary=parse_str(t.get("summary")),
                board_name=parse_str(board.get("name")),
                board_id=parse_int(board.get("id")),
                status_name=parse_str(status.get("name")),
                status_id=parse_int(status.get("id")),
                priority_name=parse_str(priority.get("name")),
                priority_id=parse_int(priority.get("id")),
                priority_level=plevel,
                owner_ident=parse_str(owner.get("identifier")),
                resources=parse_str(t.get("resources")),
                team_name=parse_str(team.get("name")),
                source_name=parse_str(source.get("name")),
                company_name=company_name,
                company_identifier=company_identifier,
                company_canonical=company_canon,
                date_entered=date_entered,
                date_closed=close_history.selected_close_time,
                first_contact=first_contact,
                effective_close=close_history.effective_close_for_duration,
                closed_flag=closed_flag,
                saw_real_reopen=close_history.saw_real_reopen,
                total_open_seconds=close_history.total_open_seconds,
            )
        )
    return rows


def apply_filters(rows: List[TicketRow], filters: Filters) -> List[TicketRow]:
    if not rows:
        return rows

    after_dt  = iso_to_dt(filters.date_entered_after_utc)
    before_dt = iso_to_dt(filters.date_entered_before_utc)
    now = now_utc()
    out: List[TicketRow] = []

    for r in rows:
        if after_dt  and (r.date_entered is None or r.date_entered < after_dt):
            continue
        if before_dt and (r.date_entered is None or r.date_entered > before_dt):
            continue

        if r.date_entered:
            age_days = (now - r.date_entered).total_seconds() / 86400.0
            if filters.min_age_days is not None and age_days < filters.min_age_days:
                continue
            if filters.max_age_days is not None and age_days > filters.max_age_days:
                continue
        elif filters.min_age_days is not None or filters.max_age_days is not None:
            continue

        close_dt = r.date_closed or r.effective_close
        if (
            filters.exclude_if_closed_in_month is not None
            and filters.exclude_if_closed_in_month_over_days is not None
            and r.date_entered is not None
            and close_dt is not None
        ):
            close_month        = close_dt.astimezone(timezone.utc).month
            time_to_close_days = (close_dt - r.date_entered).total_seconds() / 86400.0
            if (
                close_month == int(filters.exclude_if_closed_in_month)
                and time_to_close_days > float(filters.exclude_if_closed_in_month_over_days)
            ):
                continue

        comp_hay = " | ".join([r.company_canonical, r.company_name, r.company_identifier])
        if filters.company_include  and not match_any_loose(comp_hay, filters.company_include):  continue
        if filters.company_exclude  and     match_any_loose(comp_hay, filters.company_exclude):  continue

        ass_hay = " | ".join([r.owner_ident, r.resources])
        if filters.assignee_include and not match_any_loose(ass_hay, filters.assignee_include):  continue
        if filters.assignee_exclude and     match_any_loose(ass_hay, filters.assignee_exclude):  continue

        b_hay = " | ".join([r.board_name, str(r.board_id or "")])
        if filters.board_include    and not match_any_loose(b_hay, filters.board_include):       continue
        if filters.board_exclude    and     match_any_loose(b_hay, filters.board_exclude):       continue

        s_hay = " | ".join([r.status_name, str(r.status_id or "")])
        if filters.status_include   and not match_any_loose(s_hay, filters.status_include):     continue
        if filters.status_exclude   and     match_any_loose(s_hay, filters.status_exclude):     continue

        p_hay = " | ".join([r.priority_name, str(r.priority_id or "")])
        if filters.priority_include and not match_any_loose(p_hay, filters.priority_include):   continue
        if filters.priority_exclude and     match_any_loose(p_hay, filters.priority_exclude):   continue

        if filters.summary_contains_any and not match_any_loose(r.summary, filters.summary_contains_any): continue
        if filters.source_include       and not match_any_loose(r.source_name, filters.source_include):   continue
        if filters.team_include         and not match_any_loose(r.team_name, filters.team_include):       continue

        out.append(r)
    return out


def compute_metrics(rows: List[TicketRow]) -> Metrics:
    total  = len(rows)
    closed = 0
    t_first: List[float] = []
    t_close: List[float] = []
    by_board:    Dict[str, int] = {}
    by_status:   Dict[str, int] = {}
    by_priority: Dict[str, int] = {}
    by_assignee: Dict[str, int] = {}
    by_company:  Dict[str, int] = {}
    entered_by_day: Dict[str, int] = {}
    closed_by_day:  Dict[str, int] = {}
    tech_priority_counts: Dict[str, Dict[str, int]] = {}

    excluded_pool_total = 0
    excluded_pool_by_bucket: Dict[str, int] = {
        "total": 0, "p1": 0, "p2": 0, "p3": 0, "p4": 0, "p5": 0, "p6": 0, "other": 0
    }

    for r in rows:
        by_board[r.board_name or "Unknown"]         = by_board.get(r.board_name or "Unknown", 0) + 1
        by_status[r.status_name or "Unknown"]       = by_status.get(r.status_name or "Unknown", 0) + 1
        by_priority[r.priority_name or "Unknown"]   = by_priority.get(r.priority_name or "Unknown", 0) + 1
        by_company[r.company_canonical or "Unknown"] = by_company.get(r.company_canonical or "Unknown", 0) + 1

        if r.date_entered:
            day = r.date_entered.astimezone(timezone.utc).strftime("%Y-%m-%d")
            entered_by_day[day] = entered_by_day.get(day, 0) + 1

        if r.date_entered and r.first_contact:
            dt = (r.first_contact - r.date_entered).total_seconds()
            if dt >= 0:
                t_first.append(dt)

        duration_close_dt = r.effective_close or r.date_closed
        if r.date_entered and duration_close_dt:
            dtc = (duration_close_dt - r.date_entered).total_seconds()
            if dtc >= 0:
                t_close.append(dtc)

        final_close_dt = r.date_closed or r.effective_close
        if r.closed_flag or r.date_closed is not None:
            closed += 1
            if final_close_dt:
                day = final_close_dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
                closed_by_day[day] = closed_by_day.get(day, 0) + 1

        assignees = get_all_assignees(r.owner_ident, r.resources)
        if not assignees:
            assignees = ["Unassigned"]

        plevel = r.priority_level if r.priority_level is not None else priority_level_1_to_6(
            r.priority_name, r.priority_id
        )
        bucket = f"p{plevel}" if plevel in (1, 2, 3, 4, 5, 6) else "other"

        for assignee in assignees:
            if is_excluded_assignee(assignee):
                excluded_pool_total += 1
                excluded_pool_by_bucket["total"] += 1
                excluded_pool_by_bucket[bucket]  += 1
                continue
            by_assignee[assignee] = by_assignee.get(assignee, 0) + 1
            if assignee not in tech_priority_counts:
                tech_priority_counts[assignee] = {
                    "total": 0, "p1": 0, "p2": 0, "p3": 0, "p4": 0, "p5": 0, "p6": 0, "other": 0
                }
            tech_priority_counts[assignee]["total"] += 1
            tech_priority_counts[assignee][bucket]  += 1

    techs = sorted(tech_priority_counts.keys())
    if techs and excluded_pool_total > 0:
        add_map_total = distribute_evenly(excluded_pool_total, techs)
        for tech, add_n in add_map_total.items():
            if add_n:
                by_assignee[tech] = by_assignee.get(tech, 0) + add_n
        for bucket_key in ["total", "p1", "p2", "p3", "p4", "p5", "p6", "other"]:
            pool_n = excluded_pool_by_bucket.get(bucket_key, 0)
            if pool_n <= 0:
                continue
            add_map = distribute_evenly(pool_n, techs)
            for tech, add_n in add_map.items():
                if add_n:
                    tech_priority_counts[tech][bucket_key] += add_n

    for bad in list(by_assignee.keys()):
        if is_excluded_assignee(bad):
            by_assignee.pop(bad, None)
    for bad in list(tech_priority_counts.keys()):
        if is_excluded_assignee(bad):
            tech_priority_counts.pop(bad, None)

    return Metrics(
        total_tickets=total,
        total_closed=closed,
        t_first_contact_seconds=t_first,
        t_close_seconds=t_close,
        by_board=by_board,
        by_status=by_status,
        by_priority=by_priority,
        by_assignee=by_assignee,
        by_company=by_company,
        entered_by_day=entered_by_day,
        closed_by_day=closed_by_day,
        tech_priority_counts=tech_priority_counts,
    )
