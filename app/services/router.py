"""
app/services/router.py

AI-powered ticket routing service.
Uses Claude to assign open ConnectWise tickets to the most appropriate
technician based on ticket content and the configured agent roster.
"""

from __future__ import annotations

import json
import os
import re
import time
import threading
from typing import Any, Dict, List, Optional, Tuple

import anthropic
from dotenv import find_dotenv, load_dotenv

from app.core.connectwise import (
    build_auth,
    build_headers,
    fetch_tickets,
    get_base_url,
    make_session,
    patch_ticket,
    post_note,
)
from app.core.state import broadcast, record_summary


# ── Mappings helpers ──────────────────────────────────────────────────────────

def _get_board_ids(mappings: Dict, board_names: List[str]) -> List[Tuple[str, int]]:
    boards = {str(k).lower(): int(v) for k, v in (mappings.get("boards") or {}).items()}
    if not board_names:
        return list(boards.items())
    result = []
    for name in board_names:
        bid = boards.get(name.lower())
        if bid is None:
            raise RuntimeError(f"Board '{name}' not found in mappings 'boards'.")
        result.append((name, bid))
    return result


def _get_member_id(mappings: Dict, identifier: str) -> Optional[int]:
    members = {str(k).lower(): v for k, v in (mappings.get("members") or {}).items()}
    val = members.get(identifier.lower())
    return int(val) if val is not None else None


def _get_status_id(mappings: Dict, board_name: str, status_name: str) -> Optional[int]:
    key = f"{board_name.lower()} statuses"
    statuses = {str(k).lower(): v for k, v in (mappings.get(key) or {}).items()}
    val = statuses.get(status_name.lower())
    return int(val) if val is not None else None


def _build_roster(mappings: Dict) -> List[Dict]:
    routing = mappings.get("agent_routing") or {}
    return [
        {
            "identifier":   ident,
            "display_name": info.get("display_name", ident),
            "description":  info.get("description", ""),
        }
        for ident, info in routing.items()
        if isinstance(info, dict) and info.get("routable") is True
    ]


def _build_unrouted_ids(mappings: Dict, identifiers: List[str]) -> set:
    ids: set = set()
    for ident in identifiers:
        mid = _get_member_id(mappings, ident)
        if mid is not None:
            ids.add(mid)
    return ids


def _ticket_needs_routing(ticket: Dict, unrouted_ids: set) -> bool:
    owner = ticket.get("owner")
    if owner is None:
        return True
    owner_id = owner.get("id") if isinstance(owner, dict) else None
    return owner_id is None or int(owner_id) in unrouted_ids


# ── Claude integration ────────────────────────────────────────────────────────

def _build_system_prompt(roster: List[Dict]) -> str:
    lines = "\n".join(
        f'  - {a["identifier"]} ({a["display_name"]}): {a["description"]}'
        for a in roster
    )
    return (
        "You are a help desk dispatcher for a managed IT services provider.\n"
        "Your job is to assign incoming support tickets to the most appropriate technician.\n\n"
        f"Available technicians:\n{lines}\n\n"
        "Rules:\n"
        "- Choose exactly ONE technician from the list above.\n"
        "- Match ticket content to the technician whose description best fits the work required.\n"
        "- Prefer lower-tier technicians for routine issues; use Tier 2 for networking, servers,\n"
        "  Azure AD, VPN, or complex escalations.\n"
        "- Respond ONLY with valid JSON (no markdown, no extra text):\n"
        '{"agent": "<identifier>", "reason": "<one sentence>"}'
    )


def _ask_claude(
    client: anthropic.Anthropic,
    system_prompt: str,
    ticket: Dict,
    model: str,
) -> Tuple[str, str]:
    summary     = (ticket.get("summary") or "").strip()
    description = (ticket.get("initialDescription") or ticket.get("description") or "").strip()
    t_type      = ticket.get("type", {}).get("name", "") if isinstance(ticket.get("type"), dict) else ""
    company     = ticket.get("company", {}).get("name", "") if isinstance(ticket.get("company"), dict) else ""
    board       = ticket.get("board", {}).get("name", "") if isinstance(ticket.get("board"), dict) else ""

    user_msg = (
        f"Ticket #{ticket.get('id')}\n"
        f"Board: {board}\nCompany: {company}\nType: {t_type}\n"
        f"Summary: {summary}\nDescription: {description[:800]}"
    )
    response = client.messages.create(
        model=model,
        max_tokens=150,
        system=system_prompt,
        messages=[{"role": "user", "content": user_msg}],
    )
    raw = response.content[0].text.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\n?```$", "", raw, flags=re.IGNORECASE).strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError(f"Claude returned non-JSON: {raw!r}")

    agent  = str(parsed.get("agent") or "").strip()
    reason = str(parsed.get("reason") or "").strip()
    if not agent:
        raise ValueError(f"Claude JSON missing 'agent': {raw!r}")
    return agent, reason


# ── Routing application ───────────────────────────────────────────────────────

def _apply_routing(
    sess,
    site: str,
    auth,
    headers: Dict,
    ticket: Dict,
    owner_id: int,
    status_id: Optional[int],
    dry_run: bool,
    timeout: int,
    add_note: bool,
    note_template: str,
    display_name: str,
    reason: str,
    board_id: Optional[int] = None,
) -> None:
    tid = ticket["id"]
    ops: List[Dict] = []

    owner_exists = isinstance(ticket.get("owner"), dict) and ticket["owner"].get("id") is not None
    ops.append({"op": "replace" if owner_exists else "add", "path": "/owner", "value": {"id": owner_id}})

    if status_id is not None:
        status_exists = isinstance(ticket.get("status"), dict)
        ops.append({"op": "replace" if status_exists else "add", "path": "/status", "value": {"id": status_id}})

    if board_id is not None:
        board_exists = isinstance(ticket.get("board"), dict)
        ops.append({"op": "replace" if board_exists else "add", "path": "/board", "value": {"id": board_id}})

    if dry_run:
        broadcast(f"    [DRY RUN] PATCH ticket {tid}: owner={owner_id}" +
                  (f", status={status_id}" if status_id else "") +
                  (f", board={board_id}" if board_id else ""))
    else:
        patch_ticket(sess, site, auth, headers, tid, ops, timeout)

    if add_note:
        note = note_template.format(display_name=display_name, reason=reason)
        if dry_run:
            broadcast(f"    [DRY RUN] Note: {note[:120]}")
        else:
            post_note(sess, site, auth, headers, tid, note, timeout)


# ── Main service entry point ──────────────────────────────────────────────────

def run_routing(cfg: Dict[str, Any], stop_event: threading.Event) -> None:
    """
    Full routing run. Logs progress via broadcast().
    Intended to be called from a background thread.
    """
    from app.core.config_manager import load_mappings

    load_dotenv(find_dotenv(), override=True)

    api_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        broadcast("ERROR: Missing ANTHROPIC_API_KEY. Check the Environment tab.")
        return

    err = __import__("app.core.connectwise", fromlist=["check_credentials"]).check_credentials()
    if err:
        broadcast(f"ERROR: {err}")
        return

    site    = (os.getenv("CWM_SITE") or "").rstrip("/")
    auth    = build_auth()
    headers = build_headers()
    sess    = make_session()

    dry_run             = cfg.get("dry_run", True)
    boards_to_scan      = cfg.get("boards_to_scan", [])
    route_from_statuses = cfg.get("route_from_statuses", [])
    assigned_status     = cfg.get("assigned_status", "")
    route_to_board      = cfg.get("route_to_board", "")
    unrouted_idents     = cfg.get("unrouted_owner_identifiers", [])
    add_note            = cfg.get("add_routing_note", True)
    note_template       = cfg.get("note_template", "AI Routing: assigned to {display_name} \u2014 {reason}")
    max_tickets         = cfg.get("max_tickets_to_process", 50)
    model               = cfg.get("claude_model", "claude-sonnet-4-6")
    mappings_path       = cfg.get("mappings_path", "")
    timeout             = int(cfg.get("timeout_secs", 20))
    page_size           = int(cfg.get("page_size", 200))

    try:
        mappings = load_mappings(mappings_path)
    except Exception as e:
        broadcast(f"ERROR loading mappings.json: {e}")
        return

    try:
        boards = _get_board_ids(mappings, boards_to_scan)
    except Exception as e:
        broadcast(f"ERROR resolving boards: {e}")
        return

    roster       = _build_roster(mappings)
    unrouted_ids = _build_unrouted_ids(mappings, unrouted_idents)

    if not roster:
        broadcast("ERROR: No routable agents found in mappings.json 'agent_routing'.")
        return

    roster_lookup = {a["identifier"].lower(): a for a in roster}
    claude        = anthropic.Anthropic(api_key=api_key)
    system_prompt = _build_system_prompt(roster)

    broadcast("=" * 60)
    broadcast("Ticket Router  \u2014  " + time.strftime("%Y-%m-%d %H:%M:%S"))
    broadcast(f"Boards:          {[b[0] for b in boards]}")
    broadcast(f"Route-from:      {route_from_statuses}")
    broadcast(f"After routing:   status \u2192 '{assigned_status}' | board \u2192 '{route_to_board or '(unchanged)'}'")
    broadcast(f"Routable agents: {[a['identifier'] for a in roster]}")
    broadcast(f"DRY_RUN:         {dry_run}")
    broadcast("=" * 60)

    total_routed = total_skipped = total_errors = 0

    for board_name, board_id in boards:
        if stop_event.is_set():
            broadcast("\nRun stopped by user.")
            break

        broadcast(f"\n--- Board: {board_name} (id={board_id}) ---")
        try:
            tickets = fetch_tickets(
                sess, site, auth, headers,
                board_id=board_id, statuses=route_from_statuses,
                timeout=timeout, page_size=page_size,
            )
        except Exception as e:
            broadcast(f"  ERROR fetching tickets: {e}")
            continue

        candidates = [t for t in tickets if _ticket_needs_routing(t, unrouted_ids)]
        broadcast(f"  Fetched {len(tickets)} tickets \u2014 {len(candidates)} need routing.")

        for ticket in candidates:
            if stop_event.is_set():
                broadcast("\nRun stopped by user.")
                break
            if total_routed + total_errors >= max_tickets:
                broadcast(f"\nReached MAX_TICKETS={max_tickets}. Stopping.")
                break

            tid     = ticket.get("id")
            summary = (ticket.get("summary") or "").strip()[:80]
            co_name = (ticket.get("company") or {}).get("name", "") if isinstance(ticket.get("company"), dict) else ""

            broadcast(f"\n  Ticket #{tid} | {co_name} | {summary}")

            try:
                agent_id, reason = _ask_claude(claude, system_prompt, ticket, model)
            except Exception as e:
                broadcast(f"    ERROR (Claude): {e}")
                total_errors += 1
                continue

            agent_info = roster_lookup.get(agent_id.lower())
            if agent_info is None:
                broadcast(f"    ERROR: Claude chose '{agent_id}' \u2014 not in routable roster. Skipping.")
                total_skipped += 1
                continue

            owner_id = _get_member_id(mappings, agent_info["identifier"])
            if owner_id is None:
                broadcast(f"    ERROR: No member ID for '{agent_info['identifier']}'. Skipping.")
                total_skipped += 1
                continue

            effective_board = route_to_board if route_to_board else board_name
            status_id: Optional[int] = None
            if assigned_status:
                status_id = _get_status_id(mappings, effective_board, assigned_status)
                if status_id is None:
                    status_id = _get_status_id(mappings, board_name, assigned_status)
                if status_id is None:
                    broadcast(f"    WARNING: '{assigned_status}' not found in '{effective_board}' statuses. Status unchanged.")

            target_board_id: Optional[int] = None
            if route_to_board:
                all_boards = {str(k).lower(): int(v) for k, v in (mappings.get("boards") or {}).items()}
                target_board_id = all_boards.get(route_to_board.lower())
                if target_board_id is None:
                    broadcast(f"    WARNING: '{route_to_board}' not found in mappings 'boards'. Board unchanged.")

            broadcast(f"    \u2192 {agent_info['identifier']} ({agent_info['display_name']})")
            broadcast(f"    Reason: {reason}")

            try:
                _apply_routing(
                    sess, site, auth, headers, ticket, owner_id, status_id,
                    dry_run, timeout, add_note, note_template,
                    agent_info["display_name"], reason, target_board_id,
                )
                total_routed += 1
                broadcast("    Done.")
            except Exception as e:
                broadcast(f"    ERROR (PATCH): {e}")
                total_errors += 1

            time.sleep(0.3)

    broadcast("\n" + "=" * 60)
    broadcast("Run complete")
    broadcast(f"  Routed:  {total_routed}")
    broadcast(f"  Skipped: {total_skipped}")
    broadcast(f"  Errors:  {total_errors}")
    broadcast(f"  DRY_RUN: {dry_run}")
    broadcast("=" * 60)

    record_summary({
        "routed":  total_routed,
        "skipped": total_skipped,
        "errors":  total_errors,
        "dry_run": dry_run,
    })
