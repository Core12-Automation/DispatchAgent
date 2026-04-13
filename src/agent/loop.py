"""
src/agent/loop.py

Core agentic dispatch loop.

Entry point: run_dispatch(ticket, *, config, mappings, dry_run)

Workflow:
  1. Build a ToolRegistry wired to the real (or stubbed) tool implementations
  2. Build the system prompt with the live technician roster
  3. Call the Anthropic API in a tool-use loop:
       Claude → tool_use blocks → execute via registry → tool_result → Claude → …
  4. Stop when: Claude returns end_turn, max_iterations reached, or 120s elapsed
  5. Stream every tool call / result to the SSE broadcaster (visible in web UI)
  6. Return a structured result dict

The existing router.py is NOT touched — this is an additive parallel path.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

MAX_ITERATIONS = 15
TIMEOUT_SECONDS = 120


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def run_dispatch(
    ticket: Dict[str, Any],
    *,
    config: Dict[str, Any],
    mappings: Dict[str, Any],
    dry_run: Optional[bool] = None,
    broadcaster: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """
    Run the agentic dispatch loop for a single ticket.

    Args:
        ticket:       Full ticket object from ConnectWise (or a minimal dict
                      with at least 'id' and 'summary').
        config:       Portal config dict (from load_config()).
        mappings:     Mappings dict (from load_mappings()).
        dry_run:      Override config's dry_run flag.  Defaults to config value.
        broadcaster:  Callable(str) that fans output to the SSE stream.
                      If omitted, falls back to logging.

    Returns:
        {
          "status":          "ok" | "error" | "timeout" | "max_iterations",
          "ticket_id":       int,
          "summary":         str,   # Final text from Claude
          "decisions_made":  [...], # All log_dispatch_decision calls
          "tools_called":    [...], # Log of every tool invocation
          "elapsed_seconds": float,
          "iterations":      int,
          "dry_run":         bool,
        }
    """
    from src.clients.anthropic_client import AnthropicClient
    from src.agent.tool_definitions import TOOL_DEFINITIONS
    from src.agent.prompts import build_dispatch_system_prompt
    from src.agent.tool_registry import ToolRegistry

    _broadcast = broadcaster or (lambda msg: log.info("[dispatch] %s", msg))
    effective_dry_run = dry_run if dry_run is not None else config.get("dry_run", True)
    model: str = config.get("claude_model", "claude-sonnet-4-6")

    ticket_id = ticket.get("id", "unknown")
    ticket_summary = (ticket.get("summary") or "").strip()

    _broadcast("─" * 60)
    _broadcast(f"[Agent] Dispatching ticket #{ticket_id}: {ticket_summary[:80]}")
    _broadcast(f"[Agent] Model: {model}  |  dry_run={effective_dry_run}")
    _broadcast("─" * 60)

    # ── Build roster from mappings ────────────────────────────────────────────
    routing = mappings.get("agent_routing") or {}
    roster = [
        {
            "identifier":   ident,
            "display_name": info.get("display_name", ident),
            "description":  info.get("description", ""),
        }
        for ident, info in routing.items()
        if isinstance(info, dict) and info.get("routable") is True
    ]

    # ── Build system prompt ───────────────────────────────────────────────────
    system_prompt = build_dispatch_system_prompt(roster, {**config, "dry_run": effective_dry_run})

    # ── Build tool registry ───────────────────────────────────────────────────
    registry = ToolRegistry(
        config=config,
        mappings=mappings,
        dry_run=effective_dry_run,
        broadcaster=_broadcast,
    )

    # ── Metrics ───────────────────────────────────────────────────────────────
    tools_called: List[Dict[str, Any]] = []
    decisions_made: List[Dict[str, Any]] = []
    start_time = time.time()

    # ── Tool executor (called by the loop on each tool_use block) ────────────
    def execute_tool(tool_name: str, tool_input: Dict[str, Any]) -> Any:
        entry = {
            "tool": tool_name,
            "input": tool_input,
            "t": round(time.time() - start_time, 2),
        }
        try:
            result = registry.call(tool_name, tool_input)
            entry["result"] = result
            if tool_name == "log_dispatch_decision":
                decisions_made.append(tool_input)
        except Exception as exc:
            result = {"error": str(exc)}
            entry["error"] = str(exc)
            _broadcast(f"  ✗ {tool_name} raised: {exc}")
        finally:
            tools_called.append(entry)
        return result

    # ── Build user message ────────────────────────────────────────────────────
    company = (ticket.get("company") or {}).get("name", "") if isinstance(ticket.get("company"), dict) else ""
    board = (ticket.get("board") or {}).get("name", "") if isinstance(ticket.get("board"), dict) else ""
    t_type = (ticket.get("type") or {}).get("name", "") if isinstance(ticket.get("type"), dict) else ""
    priority = (ticket.get("priority") or {}).get("name", "") if isinstance(ticket.get("priority"), dict) else ""
    description = (ticket.get("initialDescription") or ticket.get("description") or "").strip()

    user_message = (
        f"Please dispatch the following ticket:\n\n"
        f"Ticket #{ticket_id}\n"
        f"Board: {board}\n"
        f"Company: {company}\n"
        f"Type: {t_type}\n"
        f"Priority: {priority}\n"
        f"Summary: {ticket_summary}\n"
        f"Description: {description[:800]}\n"
    )

    # ── Agentic loop ──────────────────────────────────────────────────────────
    api_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        return _error_result(ticket_id, "ANTHROPIC_API_KEY not set", tools_called, decisions_made, start_time, effective_dry_run)

    try:
        client = AnthropicClient(api_key=api_key, default_model=model)
    except ValueError as exc:
        return _error_result(ticket_id, str(exc), tools_called, decisions_made, start_time, effective_dry_run)

    messages: List[Dict[str, Any]] = [{"role": "user", "content": user_message}]
    final_text = ""
    iterations = 0
    stop_reason = "ok"

    while iterations < MAX_ITERATIONS:
        # ── Timeout guard ─────────────────────────────────────────────────────
        if time.time() - start_time > TIMEOUT_SECONDS:
            _broadcast(f"[Agent] ⏱ Timeout after {TIMEOUT_SECONDS}s at iteration {iterations}")
            stop_reason = "timeout"
            break

        iterations += 1
        _broadcast(f"[Agent] Iteration {iterations}/{MAX_ITERATIONS}")

        try:
            response = client._client.messages.create(
                model=model,
                max_tokens=2048,
                system=system_prompt,
                messages=messages,
                tools=TOOL_DEFINITIONS,
            )
        except Exception as exc:
            _broadcast(f"[Agent] ✗ API error: {exc}")
            return _error_result(ticket_id, str(exc), tools_called, decisions_made, start_time, effective_dry_run)

        # Append assistant turn
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            # Collect final text
            for block in response.content:
                if hasattr(block, "text"):
                    final_text = block.text
                    break
            _broadcast(f"[Agent] ✓ Complete after {iterations} iteration(s)")
            stop_reason = "ok"
            break

        if response.stop_reason != "tool_use":
            _broadcast(f"[Agent] Unexpected stop_reason: {response.stop_reason!r}")
            stop_reason = f"unexpected_stop:{response.stop_reason}"
            break

        # Execute all tool calls in this turn
        tool_results: List[Dict[str, Any]] = []
        for block in response.content:
            if not hasattr(block, "type") or block.type != "tool_use":
                continue
            result = execute_tool(block.name, dict(block.input))
            tool_results.append({
                "type":        "tool_result",
                "tool_use_id": block.id,
                "content":     json.dumps(result, default=str),
            })

        messages.append({"role": "user", "content": tool_results})

    else:
        _broadcast(f"[Agent] Reached max_iterations={MAX_ITERATIONS}")
        stop_reason = "max_iterations"

    elapsed = round(time.time() - start_time, 2)
    _broadcast(f"[Agent] Done — {len(tools_called)} tool call(s), {elapsed}s elapsed")
    _broadcast("─" * 60)

    return {
        "status":          stop_reason,
        "ticket_id":       ticket_id,
        "summary":         final_text,
        "decisions_made":  decisions_made,
        "tools_called":    tools_called,
        "elapsed_seconds": elapsed,
        "iterations":      iterations,
        "dry_run":         effective_dry_run,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _error_result(
    ticket_id: Any,
    error: str,
    tools_called: List,
    decisions_made: List,
    start_time: float,
    dry_run: bool,
) -> Dict[str, Any]:
    return {
        "status":          "error",
        "ticket_id":       ticket_id,
        "error":           error,
        "summary":         "",
        "decisions_made":  decisions_made,
        "tools_called":    tools_called,
        "elapsed_seconds": round(time.time() - start_time, 2),
        "iterations":      0,
        "dry_run":         dry_run,
    }
