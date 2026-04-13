#!/usr/bin/env python
"""
scripts/test_dispatch.py

CLI tool for testing the AI Dispatcher against a live ConnectWise ticket.

Usage:
    python scripts/test_dispatch.py --ticket-id 12345
    python scripts/test_dispatch.py --ticket-id 12345 --verbose

Always runs in dry-run mode — never modifies ConnectWise.
Prints each tool call and its return value, Claude's reasoning per step,
the final dispatch decision, total API calls, and elapsed time.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# ── Ensure project root is on sys.path ────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ── Output helpers ─────────────────────────────────────────────────────────────

def _ansi(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


def _heading(text: str) -> None:
    bar = "─" * 62
    print(f"\n{_ansi(bar, '34')}")
    print(_ansi(f"  {text}", "1;34"))
    print(_ansi(bar, "34"))


def _ok(text: str) -> None:
    print(_ansi(f"  ✓ {text}", "32"))


def _err(text: str) -> None:
    print(_ansi(f"  ✗ {text}", "31"), file=sys.stderr)


def _fmt(obj: object, indent: int = 2) -> str:
    return json.dumps(obj, indent=indent, default=str, ensure_ascii=False)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test the AI Dispatcher against a live CW ticket (always dry-run).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="This tool NEVER modifies ConnectWise — dry-run is always enabled.",
    )
    parser.add_argument(
        "--ticket-id", required=True, type=int,
        help="ConnectWise ticket ID to dispatch",
    )
    parser.add_argument(
        "--model", default=None,
        help="Claude model override (e.g. claude-opus-4-6)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print full JSON for every tool input and result",
    )
    args = parser.parse_args()

    # ── Load .env ─────────────────────────────────────────────────────────────
    try:
        from dotenv import load_dotenv, find_dotenv
        load_dotenv(find_dotenv(usecwd=True), override=False)
    except ImportError:
        pass

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        _err("ANTHROPIC_API_KEY is not set — copy .env from the project root.")
        sys.exit(1)

    # ── Load config and mappings ──────────────────────────────────────────────
    _heading("Loading configuration")
    try:
        from app.core.config_manager import load_config, load_mappings
        config = load_config()
        mappings_path = config.get("mappings_path", str(PROJECT_ROOT / "data" / "mappings.json"))
        mappings = load_mappings(mappings_path)
    except Exception as exc:
        _err(f"Config load failed: {exc}")
        sys.exit(1)

    # Force dry-run and apply model override
    config["dry_run"] = True
    if args.model:
        config["claude_model"] = args.model

    model_name = config.get("claude_model", "default")
    print(f"  Model   : {model_name}")
    print(f"  Dry-run : True (forced — this tool never writes to CW)")
    print(f"  Ticket  : #{args.ticket_id}")

    # ── Fetch ticket from ConnectWise ─────────────────────────────────────────
    _heading(f"Fetching ticket #{args.ticket_id} from ConnectWise")
    try:
        from src.clients.connectwise import CWManageClient
        cw = CWManageClient(dry_run=True)
        ticket = cw.get_ticket(args.ticket_id)
    except Exception as exc:
        _err(f"Failed to fetch ticket: {exc}")
        sys.exit(1)

    def _field(ticket: dict, *keys: str, default: str = "N/A") -> str:
        for k in keys:
            v = ticket.get(k)
            if isinstance(v, dict):
                v = v.get("name") or v.get("identifier") or v.get("id")
            if v:
                return str(v)
        return default

    print(f"  Summary  : {_field(ticket, 'summary')}")
    print(f"  Board    : {_field(ticket, 'board')}")
    print(f"  Status   : {_field(ticket, 'status')}")
    print(f"  Priority : {_field(ticket, 'priority')}")
    print(f"  Company  : {_field(ticket, 'company')}")
    print(f"  Owner    : {_field(ticket, 'owner', default='unassigned')}")
    print(f"  Type     : {_field(ticket, 'type')}")

    # ── Set up broadcaster for real-time output ───────────────────────────────
    progress_lines: list[str] = []

    def broadcaster(msg: str) -> None:
        progress_lines.append(msg)
        # Emit agent-level progress immediately so the user can see it unfold
        if any(tag in msg for tag in ("[Agent]", "tool:", "← ", "✓", "✗", "─" * 20)):
            print(f"  {_ansi(msg, '2')}")

    # ── Run dispatch loop ─────────────────────────────────────────────────────
    _heading("Running agent dispatch loop")
    t0 = time.time()

    try:
        from src.agent.loop import run_dispatch
        result = run_dispatch(
            ticket,
            config=config,
            mappings=mappings,
            dry_run=True,
            broadcaster=broadcaster,
        )
    except Exception as exc:
        _err(f"run_dispatch raised unexpectedly: {exc}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    wall_elapsed = time.time() - t0

    # ── Tool call trace ───────────────────────────────────────────────────────
    calls = result.get("tools_called", [])
    _heading(f"Tool Call Trace  ({len(calls)} call{'s' if len(calls) != 1 else ''})")

    for i, call in enumerate(calls, 1):
        tool_name = call.get("tool", "unknown")
        t_offset = call.get("t", 0.0)
        had_error = "error" in call

        print(f"\n  [{i}] {_ansi(tool_name, '1;36')}  (+{t_offset:.2f}s)"
              + (_ansi("  ERROR", "31") if had_error else ""))

        inp = call.get("input", {})
        if args.verbose:
            print(f"      Input  :\n{_fmt(inp, 6)}")
        else:
            preview = ", ".join(f"{k}={v!r}" for k, v in list(inp.items())[:3])
            if len(inp) > 3:
                preview += ", …"
            print(f"      Input  : {preview}")

        if had_error:
            print(f"      Error  : {_ansi(call['error'], '31')}")
        elif "result" in call:
            res = call["result"]
            if args.verbose:
                print(f"      Result :\n{_fmt(res, 6)}")
            else:
                if isinstance(res, dict):
                    preview = {k: res[k] for k in list(res.keys())[:4]}
                    print(f"      Result : {str(preview)[:120]}")
                else:
                    print(f"      Result : {str(res)[:120]}")

    # ── Dispatch decisions ────────────────────────────────────────────────────
    decisions = result.get("decisions_made", [])
    _heading(f"Dispatch Decision{'s' if len(decisions) != 1 else ''}  ({len(decisions)} logged)")

    if decisions:
        for d in decisions:
            tech = d.get("assigned_technician", "?")
            conf = float(d.get("confidence", 0.0))
            reason = d.get("reason", "")
            alts = d.get("alternatives_considered", [])
            conf_str = f"{conf:.0%}"
            conf_code = "32" if conf >= 0.8 else ("33" if conf >= 0.5 else "31")

            print(f"\n  Assigned to : {_ansi(tech, '1;32')}")
            print(f"  Confidence  : {_ansi(conf_str, conf_code)}")
            if reason:
                # Wrap at 78 chars
                wrapped = (reason[:300] + "…") if len(reason) > 300 else reason
                print(f"  Reason      : {wrapped}")
            if alts:
                print(f"  Alternatives considered ({len(alts)}):")
                for alt in alts:
                    ident = alt.get("identifier", str(alt))
                    alt_reason = alt.get("reason", "")
                    print(f"    - {ident}: {alt_reason[:100]}")
    else:
        print("  No dispatch decisions were logged.")
        print("  (Claude may have flagged for human review or encountered an error)")

    # ── Claude's final text ───────────────────────────────────────────────────
    if result.get("summary"):
        _heading("Claude's Final Response")
        summary = result["summary"]
        # Wrap at 78 chars per line for readability
        for line in summary.split("\n"):
            while len(line) > 78:
                print(f"  {line[:78]}")
                line = "  " + line[78:]
            print(f"  {line}")

    # ── Summary stats ─────────────────────────────────────────────────────────
    _heading("Summary")
    status = result.get("status", "unknown")
    status_code = "32" if status == "ok" else "31"

    print(f"  Status      : {_ansi(status, status_code)}")
    print(f"  Ticket      : #{result.get('ticket_id')}")
    print(f"  Iterations  : {result.get('iterations', 0)}")
    print(f"  Tool calls  : {len(calls)}")
    print(f"  Elapsed     : {wall_elapsed:.2f}s  (loop: {result.get('elapsed_seconds', 0):.2f}s)")
    print(f"  Dry-run     : True  — no changes were made to ConnectWise")

    if result.get("error"):
        print()
        _err(f"Loop error: {result['error']}")

    print()

    if result.get("status") not in ("ok", "max_iterations"):
        sys.exit(1)


if __name__ == "__main__":
    main()
