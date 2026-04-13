"""
app/services/search.py

Deep ticket search service.
Searches ConnectWise ticket summaries, descriptions, notes, and optionally
the audit trail for a given phrase.
"""

from __future__ import annotations

import html as _html_lib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List
from urllib.parse import urljoin

from dotenv import find_dotenv, load_dotenv

from app.core.connectwise import build_auth, build_headers, get_base_url, make_session
from app.core.state import broadcast, get_tool_state


def _clean(text: Any) -> str:
    """Strip HTML and normalise whitespace from arbitrary text."""
    if not text:
        return ""
    s = str(text)
    s = _html_lib.unescape(s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def run_deep_search(params: Dict[str, Any], stop_event: threading.Event) -> None:
    """
    Deep-search ConnectWise tickets for a phrase.
    Logs progress via broadcast() and stores results in _tool_state.
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

    phrase         = _clean(params.get("phrase", "")).casefold()
    company_filter = _clean(params.get("company_filter", "")).casefold()
    days_back      = int(params.get("days_back") or 0) or None
    only_open      = bool(params.get("only_open", False))
    search_audit   = bool(params.get("search_audit", False))
    max_results    = int(params.get("max_results") or 200)

    if not phrase:
        broadcast("ERROR: Search phrase cannot be empty.")
        return

    broadcast("=" * 60)
    broadcast(f"Deep Ticket Search  \u2014  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    broadcast(f"Phrase:        {phrase!r}")
    broadcast(f"Company:       {company_filter or '(all)'}")
    broadcast(f"Days back:     {days_back or 'all history'}")
    broadcast(f"Only open:     {only_open}")
    broadcast(f"Search audit:  {search_audit}")
    broadcast("=" * 60 + "\n")

    cond_parts = []
    if only_open:
        cond_parts.append("closedFlag = false")
    if days_back:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%SZ")
        cond_parts.append(f"_info/lastUpdated >= [{cutoff}]")
    conditions = " AND ".join(f"({p})" for p in cond_parts)

    results: List[Dict] = []
    seen_ids: set = set()
    page = 1
    total_fetched = 0
    deep_candidates: List[Dict] = []

    while not stop_event.is_set():
        api_params: Dict = {
            "page":      page,
            "pageSize":  500,
            "orderBy":   "_info/lastUpdated desc",
            "fields":    "id,summary,company,initialDescription,_info",
        }
        if conditions:
            api_params["conditions"] = conditions

        r = sess.get(
            urljoin(site + "/", "service/tickets"),
            auth=auth, headers=headers, params=api_params, timeout=60,
        )
        if not r.ok:
            broadcast(f"ERROR: HTTP {r.status_code} fetching tickets")
            break
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break

        total_fetched += len(batch)
        broadcast(f"[Page {page}] {len(batch)} tickets fetched (total {total_fetched})")

        for t in batch:
            tid = t.get("id")
            if not tid or tid in seen_ids:
                continue
            co_name = _clean((t.get("company") or {}).get("name") or "")
            if company_filter and company_filter not in co_name.casefold():
                continue

            summary = _clean(t.get("summary") or "")
            desc    = _clean(t.get("initialDescription") or "")
            if phrase in summary.casefold() or phrase in desc.casefold():
                seen_ids.add(tid)
                results.append({"id": tid, "summary": summary, "company": co_name, "source": "summary/desc"})
                broadcast(f"  MATCH #{tid} | {co_name} | {summary[:60]}")
            else:
                deep_candidates.append({"id": tid, "summary": summary, "company": co_name})

        if len(results) >= max_results:
            broadcast(f"\nReached max_results={max_results}. Stopping paging.")
            break
        if len(batch) < 500:
            break
        page += 1

    # Deep search: check notes (and optionally audit trail)
    if deep_candidates and not stop_event.is_set():
        broadcast(f"\nDeep searching {len(deep_candidates)} tickets (notes)...")
        checked = 0
        for t in deep_candidates:
            if stop_event.is_set():
                break
            if t["id"] in seen_ids:
                continue
            checked += 1
            if checked % 50 == 0:
                broadcast(f"  Deep: {checked}/{len(deep_candidates)}")

            r = sess.get(
                urljoin(site + "/", f"service/tickets/{t['id']}/notes"),
                auth=auth, headers=headers, timeout=30,
                params={"pageSize": 50, "page": 1},
            )
            if r.ok:
                for note in (r.json() or []):
                    if phrase in _clean(note.get("text") or "").casefold():
                        seen_ids.add(t["id"])
                        results.append({"id": t["id"], "summary": t["summary"], "company": t["company"], "source": "note"})
                        broadcast(f"  MATCH #{t['id']} | {t['company']} | {t['summary'][:60]} [note]")
                        break

            if search_audit and t["id"] not in seen_ids:
                r2 = sess.get(
                    urljoin(site + "/", "system/audittrail"),
                    auth=auth, headers=headers, timeout=30,
                    params={"type": "Ticket", "id": t["id"], "pageSize": 50},
                )
                if r2.ok:
                    for entry in (r2.json() or []):
                        text = _clean(json.dumps(entry))
                        if phrase in text.casefold():
                            seen_ids.add(t["id"])
                            results.append({"id": t["id"], "summary": t["summary"], "company": t["company"], "source": "audit"})
                            broadcast(f"  MATCH #{t['id']} | {t['company']} | {t['summary'][:60]} [audit]")
                            break

            if len(results) >= max_results:
                break
            time.sleep(0.05)

    get_tool_state()["search_results"] = results

    broadcast("\n" + "=" * 60)
    broadcast(f"Search complete \u2014 {len(results)} matches found across {total_fetched} tickets scanned")
    broadcast("=" * 60)

    from app.core.state import get_lock, get_state
    with get_lock():
        get_state()["summary"] = {"routed": len(results), "skipped": 0, "errors": 0, "dry_run": False}
