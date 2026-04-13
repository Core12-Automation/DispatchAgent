"""
tests/test_agent_loop.py

Tests for src/agent/loop.py — the core agentic dispatch loop.

Key things verified:
  - Happy path: end_turn on first response
  - Tool-use → tool_result → end_turn flow populates decisions_made / tools_called
  - Missing ANTHROPIC_API_KEY returns error result immediately
  - Max-iterations guard exits after MAX_ITERATIONS
  - Timeout guard exits when elapsed > TIMEOUT_SECONDS
  - Tool errors are captured gracefully (loop continues)
  - API errors return error result immediately
  - Broadcaster callable is invoked throughout
  - Result dict always has the full set of required keys
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import make_api_response, make_text_block, make_tool_use_block

# ── Shared minimal ticket ─────────────────────────────────────────────────────

_TICKET = {
    "id": 12345,
    "summary": "VPN not working after Windows update",
    "board": {"id": 10, "name": "Support"},
    "status": {"id": 1, "name": "New"},
    "priority": {"id": 3, "name": "High"},
    "company": {"id": 100, "name": "Acme Corp"},
    "owner": None,
    "type": {"id": 5, "name": "Service Request"},
}

_LOG_DECISION_INPUT = {
    "ticket_id": 12345,
    "assigned_technician": "akloss",
    "reason": "VPN issue matches Alex Kloss's networking expertise",
    "confidence": 0.9,
    "ticket_summary": "VPN not working",
    "alternatives_considered": [
        {"identifier": "jsmith", "reason": "Less relevant skills"},
    ],
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _patch_anthropic(mock_client):
    """Return a context manager that patches anthropic.Anthropic."""
    return patch(
        "src.clients.anthropic_client.anthropic.Anthropic",
        return_value=mock_client,
    )


def _mock_client_returning(*responses):
    """Return a mock _client whose messages.create cycles through responses."""
    mc = MagicMock()
    if len(responses) == 1:
        mc.messages.create.return_value = responses[0]
    else:
        mc.messages.create.side_effect = list(responses)
    return mc


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestRunDispatchHappyPath:
    def test_end_turn_on_first_call(self, mock_config, mock_mappings):
        """Single end_turn response → status ok, summary populated."""
        final_text = "Ticket dispatched to akloss."
        response = make_api_response([make_text_block(final_text)], stop_reason="end_turn")
        mc = _mock_client_returning(response)

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            with _patch_anthropic(mc):
                from src.agent.loop import run_dispatch

                result = run_dispatch(_TICKET, config=mock_config, mappings=mock_mappings)

        assert result["status"] == "ok"
        assert result["ticket_id"] == 12345
        assert result["dry_run"] is True
        assert result["summary"] == final_text
        assert result["iterations"] == 1
        assert result["tools_called"] == []
        assert result["decisions_made"] == []

    def test_result_always_has_required_keys(self, mock_config, mock_mappings):
        """run_dispatch always returns all keys in the result dict."""
        response = make_api_response([make_text_block("Done.")], stop_reason="end_turn")
        mc = _mock_client_returning(response)

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            with _patch_anthropic(mc):
                from src.agent.loop import run_dispatch

                result = run_dispatch(_TICKET, config=mock_config, mappings=mock_mappings)

        required = {
            "status", "ticket_id", "summary", "decisions_made",
            "tools_called", "elapsed_seconds", "iterations", "dry_run",
        }
        assert required.issubset(result.keys())
        assert isinstance(result["elapsed_seconds"], float)
        assert isinstance(result["iterations"], int)
        assert isinstance(result["tools_called"], list)
        assert isinstance(result["decisions_made"], list)

    def test_dry_run_flag_passed_through(self, mock_config, mock_mappings):
        """dry_run=True is reflected in the result dict."""
        response = make_api_response([make_text_block("Done.")], stop_reason="end_turn")
        mc = _mock_client_returning(response)

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            with _patch_anthropic(mc):
                from src.agent.loop import run_dispatch

                result = run_dispatch(
                    _TICKET, config=mock_config, mappings=mock_mappings, dry_run=True
                )

        assert result["dry_run"] is True


class TestToolUseFlow:
    def test_tool_use_then_end_turn(self, mock_config, mock_mappings):
        """
        Claude emits tool_use → loop calls tool → appends result → Claude ends.
        Verifies: decisions_made populated, tools_called logged.
        """
        first = make_api_response(
            [
                make_text_block("Analyzing ticket..."),
                make_tool_use_block("log_dispatch_decision", _LOG_DECISION_INPUT, "tu_001"),
            ],
            stop_reason="tool_use",
        )
        second = make_api_response(
            [make_text_block("Assigned to akloss.")],
            stop_reason="end_turn",
        )
        mc = _mock_client_returning(first, second)

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            with _patch_anthropic(mc):
                with patch("src.agent.tool_registry.ToolRegistry.call") as mock_call:
                    mock_call.return_value = {"ok": True, "decision_id": 1, "ticket_id": 12345}

                    from src.agent.loop import run_dispatch

                    result = run_dispatch(_TICKET, config=mock_config, mappings=mock_mappings)

        assert result["status"] == "ok"
        assert result["iterations"] == 2
        assert len(result["tools_called"]) == 1
        assert result["tools_called"][0]["tool"] == "log_dispatch_decision"

        # decisions_made captures the raw input dict of every log_dispatch_decision call
        assert len(result["decisions_made"]) == 1
        d = result["decisions_made"][0]
        assert d["assigned_technician"] == "akloss"
        assert d["confidence"] == 0.9

    def test_multiple_tools_in_one_turn(self, mock_config, mock_mappings):
        """Multiple tool_use blocks in one response are all executed."""
        first = make_api_response(
            [
                make_tool_use_block("get_new_tickets", {}, "tu_a"),
                make_tool_use_block("get_technician_workload",
                                    {"technician_identifier": "akloss"}, "tu_b"),
            ],
            stop_reason="tool_use",
        )
        second = make_api_response([make_text_block("Done.")], stop_reason="end_turn")
        mc = _mock_client_returning(first, second)

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            with _patch_anthropic(mc):
                with patch("src.agent.tool_registry.ToolRegistry.call") as mock_call:
                    mock_call.return_value = {"ok": True}

                    from src.agent.loop import run_dispatch

                    result = run_dispatch(_TICKET, config=mock_config, mappings=mock_mappings)

        assert len(result["tools_called"]) == 2
        tool_names = [c["tool"] for c in result["tools_called"]]
        assert "get_new_tickets" in tool_names
        assert "get_technician_workload" in tool_names

    def test_tool_error_captured_loop_continues(self, mock_config, mock_mappings):
        """
        When a tool raises, the error is captured and the loop continues —
        Claude receives the error as tool_result content and can still end gracefully.
        """
        first = make_api_response(
            [make_tool_use_block("get_new_tickets", {}, "tu_err")],
            stop_reason="tool_use",
        )
        second = make_api_response(
            [make_text_block("Could not retrieve tickets. Flagging for review.")],
            stop_reason="end_turn",
        )
        mc = _mock_client_returning(first, second)

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            with _patch_anthropic(mc):
                with patch("src.agent.tool_registry.ToolRegistry.call") as mock_call:
                    mock_call.side_effect = Exception("CW API timeout")

                    from src.agent.loop import run_dispatch

                    result = run_dispatch(_TICKET, config=mock_config, mappings=mock_mappings)

        assert result["status"] == "ok"
        assert len(result["tools_called"]) == 1
        assert "error" in result["tools_called"][0]
        assert "CW API timeout" in result["tools_called"][0]["error"]

    def test_tool_call_logged_with_timing(self, mock_config, mock_mappings):
        """Each entry in tools_called includes a 't' timing offset."""
        first = make_api_response(
            [make_tool_use_block("get_new_tickets", {}, "tu_t")],
            stop_reason="tool_use",
        )
        second = make_api_response([make_text_block("Done.")], stop_reason="end_turn")
        mc = _mock_client_returning(first, second)

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            with _patch_anthropic(mc):
                with patch("src.agent.tool_registry.ToolRegistry.call", return_value={}):
                    from src.agent.loop import run_dispatch

                    result = run_dispatch(_TICKET, config=mock_config, mappings=mock_mappings)

        call = result["tools_called"][0]
        assert "t" in call
        assert isinstance(call["t"], float)
        assert call["t"] >= 0


class TestGuards:
    def test_missing_api_key_returns_error(self, mock_config, mock_mappings):
        """Missing ANTHROPIC_API_KEY returns an error result, no exception raised."""
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}

        with patch.dict(os.environ, env, clear=True):
            from src.agent.loop import run_dispatch

            result = run_dispatch(_TICKET, config=mock_config, mappings=mock_mappings)

        assert result["status"] == "error"
        assert "ANTHROPIC_API_KEY" in result.get("error", "")
        assert result["ticket_id"] == 12345

    def test_api_exception_returns_error(self, mock_config, mock_mappings):
        """When messages.create raises, run_dispatch returns error result."""
        mc = MagicMock()
        mc.messages.create.side_effect = Exception("Rate limit exceeded")

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            with _patch_anthropic(mc):
                from src.agent.loop import run_dispatch

                result = run_dispatch(_TICKET, config=mock_config, mappings=mock_mappings)

        assert result["status"] == "error"
        assert "Rate limit" in result["error"]

    def test_max_iterations_guard(self, mock_config, mock_mappings):
        """Loop terminates with max_iterations when Claude keeps returning tool_use."""
        infinite_response = make_api_response(
            [make_tool_use_block("get_new_tickets", {}, "tu_inf")],
            stop_reason="tool_use",
        )
        mc = MagicMock()
        mc.messages.create.return_value = infinite_response

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            with _patch_anthropic(mc):
                with patch("src.agent.tool_registry.ToolRegistry.call", return_value={}):
                    from src.agent.loop import run_dispatch, MAX_ITERATIONS

                    result = run_dispatch(_TICKET, config=mock_config, mappings=mock_mappings)

        assert result["status"] == "max_iterations"
        assert result["iterations"] == MAX_ITERATIONS
        assert len(result["tools_called"]) == MAX_ITERATIONS

    def test_timeout_guard(self, mock_config, mock_mappings):
        """Loop exits with timeout status when elapsed > TIMEOUT_SECONDS."""
        from src.agent.loop import TIMEOUT_SECONDS

        # time.time() sequence: first call is start_time=0.0, subsequent calls
        # simulate time advancing past the timeout threshold.
        _call_count = [0]

        def advancing_time():
            _call_count[0] += 1
            if _call_count[0] <= 1:
                return 0.0  # start_time
            return TIMEOUT_SECONDS + 5.0  # always over limit afterwards

        tool_response = make_api_response(
            [make_tool_use_block("get_new_tickets", {}, "tu_slow")],
            stop_reason="tool_use",
        )
        mc = MagicMock()
        mc.messages.create.return_value = tool_response

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            with _patch_anthropic(mc):
                with patch("src.agent.tool_registry.ToolRegistry.call", return_value={}):
                    with patch("src.agent.loop.time.time", side_effect=advancing_time):
                        from src.agent.loop import run_dispatch

                        result = run_dispatch(_TICKET, config=mock_config, mappings=mock_mappings)

        assert result["status"] == "timeout"

    def test_unexpected_stop_reason(self, mock_config, mock_mappings):
        """Unexpected stop_reason (not end_turn/tool_use) exits gracefully."""
        response = make_api_response(
            [make_text_block("Partial output.")],
            stop_reason="max_tokens",
        )
        mc = _mock_client_returning(response)

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            with _patch_anthropic(mc):
                from src.agent.loop import run_dispatch

                result = run_dispatch(_TICKET, config=mock_config, mappings=mock_mappings)

        assert "max_tokens" in result["status"]
        assert result["iterations"] == 1


class TestBroadcaster:
    def test_broadcaster_receives_messages(self, mock_config, mock_mappings):
        """Broadcaster callable is called with meaningful messages throughout."""
        msgs: list[str] = []
        response = make_api_response([make_text_block("Done.")], stop_reason="end_turn")
        mc = _mock_client_returning(response)

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            with _patch_anthropic(mc):
                from src.agent.loop import run_dispatch

                run_dispatch(
                    _TICKET,
                    config=mock_config,
                    mappings=mock_mappings,
                    broadcaster=msgs.append,
                )

        assert len(msgs) >= 3
        combined = " ".join(msgs)
        assert "12345" in combined  # ticket ID mentioned

    def test_broadcaster_includes_iteration_info(self, mock_config, mock_mappings):
        """Broadcaster receives iteration-level messages from the loop."""
        msgs: list[str] = []
        first = make_api_response(
            [make_tool_use_block("get_new_tickets", {}, "tu_b")],
            stop_reason="tool_use",
        )
        second = make_api_response([make_text_block("Done.")], stop_reason="end_turn")
        mc = _mock_client_returning(first, second)

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            with _patch_anthropic(mc):
                with patch("src.agent.tool_registry.ToolRegistry.call", return_value={}):
                    from src.agent.loop import run_dispatch

                    run_dispatch(
                        _TICKET,
                        config=mock_config,
                        mappings=mock_mappings,
                        broadcaster=msgs.append,
                    )

        combined = " ".join(msgs)
        # The loop emits [Agent] messages including iteration counts and the ticket ID
        assert "[Agent]" in combined
        assert "12345" in combined
