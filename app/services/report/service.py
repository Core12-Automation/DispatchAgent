"""
app/services/report/service.py

Ticket report service entry point.
Fetches ticket data from ConnectWise, runs the data pipeline,
and stores the report in tool state for the route layer to serve.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, List
from urllib.parse import urljoin

from dotenv import find_dotenv, load_dotenv

from app.core.connectwise import build_auth, build_headers, get_base_url, make_session
from app.core.state import broadcast, get_lock, get_state, get_tool_state
from app.services.report.models import Filters
from app.services.report.pipeline import apply_filters, compute_metrics, extract_ticket_rows


def run_ticket_report(params: Dict[str, Any], stop_event: threading.Event) -> None:
    """
    Generate a ticket report from ConnectWise data.
    Logs progress via broadcast() and stores results in tool_state["report_data"].
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

    def _lst(key: str) -> List[str]:
        return [s.strip() for s in params.get(key, []) if str(s).strip()]

    date_from = (params.get("date_from") or "").strip()
    date_to   = (params.get("date_to")   or "").strip()

    filters = Filters(
        date_entered_after_utc=f"{date_from}T00:00:00Z"  if date_from else None,
        date_entered_before_utc=f"{date_to}T23:59:59Z"   if date_to   else None,
        min_age_days=params.get("min_age_days"),
        max_age_days=params.get("max_age_days"),
        exclude_if_closed_in_month=params.get("exclude_closed_month"),
        exclude_if_closed_in_month_over_days=params.get("exclude_closed_month_days"),
        company_include=_lst("company_include"),
        company_exclude=_lst("company_exclude"),
        assignee_include=_lst("assignee_include"),
        assignee_exclude=_lst("assignee_exclude"),
        board_include=_lst("board_include"),
        board_exclude=_lst("board_exclude") or ["Alerts"],
        status_include=_lst("status_include"),
        status_exclude=_lst("status_exclude"),
        priority_include=_lst("priority_include"),
        priority_exclude=_lst("priority_exclude"),
        summary_contains_any=_lst("summary_contains_any"),
        source_include=_lst("source_include"),
        team_include=_lst("team_include"),
    )

    broadcast("=" * 60)
    broadcast(f"Ticket Report  \u2014  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    broadcast(f"Date from:       {date_from or 'all'}")
    broadcast(f"Date to:         {date_to   or 'now'}")
    broadcast(f"Companies inc:   {filters.company_include  or '(all)'}")
    broadcast(f"Companies excl:  {filters.company_exclude  or '(none)'}")
    broadcast(f"Boards inc:      {filters.board_include    or '(all)'}")
    broadcast(f"Boards excl:     {filters.board_exclude    or '(none)'}")
    broadcast(f"Status inc:      {filters.status_include   or '(all)'}")
    broadcast(f"Status excl:     {filters.status_exclude   or '(none)'}")
    broadcast(f"Priority inc:    {filters.priority_include or '(all)'}")
    broadcast(f"Priority excl:   {filters.priority_exclude or '(none)'}")
    broadcast(f"Assignee inc:    {filters.assignee_include or '(all)'}")
    broadcast(f"Assignee excl:   {filters.assignee_exclude or '(none)'}")
    broadcast(f"Summary any:     {filters.summary_contains_any or '(any)'}")
    broadcast(f"Source inc:      {filters.source_include   or '(all)'}")
    broadcast(f"Team inc:        {filters.team_include     or '(all)'}")
    broadcast("=" * 60 + "\n")

    cond_parts = []
    if date_from:
        cond_parts.append(f"dateEntered >= [{date_from}T00:00:00Z]")
    if date_to:
        cond_parts.append(f"dateEntered <= [{date_to}T23:59:59Z]")
    conditions = " AND ".join(f"({p})" for p in cond_parts)

    all_tickets: List[Dict] = []
    page = 1
    while not stop_event.is_set():
        p: Dict = {
            "page":    page,
            "pageSize": 500,
            "orderBy": "dateEntered desc",
            "fields":  (
                "id,summary,company,board,owner,status,type,priority,team,source,"
                "dateEntered,closedDate,closedFlag,resources"
            ),
        }
        if conditions:
            p["conditions"] = conditions
        r = sess.get(
            urljoin(site + "/", "service/tickets"),
            auth=auth, headers=headers, params=p, timeout=60,
        )
        if not r.ok:
            broadcast(f"ERROR: HTTP {r.status_code}")
            break
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break
        all_tickets.extend(batch)
        broadcast(f"Fetched page {page}: {len(batch)} tickets (total {len(all_tickets)})")
        if len(batch) < 500:
            break
        page += 1
        time.sleep(0.05)

    broadcast(f"\nProcessing {len(all_tickets)} tickets...")

    raw  = {"tickets": [{"ticket": t, "auditTrail": [], "computed": {}} for t in all_tickets]}
    rows = extract_ticket_rows(raw, mappings=None)

    if filters.company_include and rows:
        sample = sorted({
            f"{r.company_name!r} / id={r.company_identifier!r} / canon={r.company_canonical!r}"
            for r in rows[:200]
        })[:10]
        broadcast(f"DEBUG sample companies (pre-filter): {sample}")

    rows    = apply_filters(rows, filters)
    metrics = compute_metrics(rows)

    broadcast(f"After filters: {len(rows)} tickets")

    import statistics as _stats
    avg_close_days = (
        _stats.mean(metrics.t_close_seconds) / 86400.0
        if metrics.t_close_seconds else None
    )

    report = {
        "total":                 metrics.total_tickets,
        "closed":                metrics.total_closed,
        "open":                  metrics.total_tickets - metrics.total_closed,
        "by_board":              metrics.by_board,
        "by_status":             metrics.by_status,
        "by_priority":           metrics.by_priority,
        "by_assignee":           metrics.by_assignee,
        "by_company":            metrics.by_company,
        "tech_priority_counts":  metrics.tech_priority_counts,
        "close_seconds":         metrics.t_close_seconds,
        "first_contact_seconds": metrics.t_first_contact_seconds,
        "date_from":             date_from,
        "date_to":               date_to,
        "generated":             time.strftime("%Y-%m-%d %H:%M:%S"),
        "params":                params,
    }
    get_tool_state()["report_data"] = report

    broadcast(
        f"Report generated \u2014 {len(rows)} tickets | "
        f"avg close {round(avg_close_days, 2) if avg_close_days is not None else 'N/A'} days"
    )
    broadcast("=" * 60)

    with get_lock():
        get_state()["summary"] = {"routed": len(rows), "skipped": 0, "errors": 0, "dry_run": False}
