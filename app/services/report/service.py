"""
app/services/report/service.py

Ticket report service entry point.
Fetches ticket data from ConnectWise, then delegates all report
generation to General_Ticket_Report_Final.generate_report().
"""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urljoin

from dotenv import find_dotenv, load_dotenv

import General_Ticket_Report_Final as report_script
from app.core.connectwise import build_auth, build_headers, get_base_url, make_session
from app.core.state import broadcast, get_lock, get_state, get_tool_state


def run_ticket_report(params: Dict[str, Any], stop_event: threading.Event) -> None:
    """
    Fetch tickets from ConnectWise and generate the report via
    General_Ticket_Report_Final.generate_report().
    Logs progress via broadcast() and stores the PDF path in
    tool_state["report_pdf_path"].
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

    date_from = (params.get("date_from") or "").strip()
    date_to   = (params.get("date_to")   or "").strip()

    cond_parts = []
    if date_from:
        cond_parts.append(f"dateEntered >= [{date_from}T00:00:00Z]")
    if date_to:
        cond_parts.append(f"dateEntered <= [{date_to}T23:59:59Z]")
    conditions = " AND ".join(f"({p})" for p in cond_parts)

    broadcast("=" * 60)
    broadcast(f"Ticket Report  \u2014  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    broadcast(f"Date from: {date_from or 'all'}")
    broadcast(f"Date to:   {date_to   or 'now'}")
    broadcast("=" * 60 + "\n")

    all_tickets: List[Dict] = []
    page = 1
    while not stop_event.is_set():
        p: Dict = {
            "page":     page,
            "pageSize": 500,
            "orderBy":  "dateEntered desc",
            "fields": (
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

    if stop_event.is_set():
        broadcast("Run cancelled.")
        return

    output_dir = Path(tempfile.mkdtemp(prefix="dispatch_report_"))
    pdf_path, _html = report_script.generate_report(
        tickets=all_tickets,
        params=params,
        output_dir=output_dir,
        broadcast_fn=broadcast,
    )

    get_tool_state()["report_pdf_path"] = str(pdf_path)

    with get_lock():
        get_state()["summary"] = {
            "routed": len(all_tickets),
            "skipped": 0,
            "errors": 0,
            "dry_run": False,
        }
