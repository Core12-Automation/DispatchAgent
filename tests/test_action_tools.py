"""
tests/test_action_tools.py

Tests for action methods in ToolRegistry:
  - _update_ticket_notes   (dry_run vs live)
  - _message_client        (dry_run vs live)
  - _assign_ticket         (changes passed to CW, error handling)
  - _flag_for_human_review (note + channel stub)
  - call() dispatcher      (unknown tool, broadcast)
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch


def _make_registry(mock_config, mock_mappings, mock_cw_client, dry_run: bool = True,
                   broadcaster=None):
    """Helper: create a ToolRegistry with injected mock CW client."""
    from src.agent.tool_registry import ToolRegistry

    registry = ToolRegistry(
        config=mock_config,
        mappings=mock_mappings,
        dry_run=dry_run,
        broadcaster=broadcaster or (lambda _: None),
    )
    # Inject mock clients via the name-mangled private attributes
    registry._ToolRegistry__cw = mock_cw_client
    registry._ToolRegistry__teams = None
    registry._ToolRegistry__resolver = MagicMock()
    return registry


# ── update_ticket_notes ───────────────────────────────────────────────────────

class TestUpdateTicketNotes:
    def test_dry_run_returns_would_post(self, mock_config, mock_mappings, mock_cw_client):
        reg = _make_registry(mock_config, mock_mappings, mock_cw_client, dry_run=True)

        result = reg._update_ticket_notes({"ticket_id": 123, "note_text": "hello world"})

        assert result["ok"] is True
        assert result["dry_run"] is True
        assert result["would_post"] == "hello world"
        assert result["ticket_id"] == 123
        mock_cw_client.add_ticket_note.assert_not_called()

    def test_live_calls_add_ticket_note(self, mock_config, mock_mappings, mock_cw_client):
        reg = _make_registry(mock_config, mock_mappings, mock_cw_client, dry_run=False)
        mock_cw_client.add_ticket_note.return_value = {"id": 1}

        reg._update_ticket_notes({"ticket_id": 456, "note_text": "internal analysis"})

        mock_cw_client.add_ticket_note.assert_called_once_with(
            456,
            "internal analysis",
            internal_analysis_flag=True,
            detail_description_flag=False,
        )

    def test_live_returns_ok_posted(self, mock_config, mock_mappings, mock_cw_client):
        reg = _make_registry(mock_config, mock_mappings, mock_cw_client, dry_run=False)
        mock_cw_client.add_ticket_note.return_value = {"id": 42}

        result = reg._update_ticket_notes({"ticket_id": 789, "note_text": "test"})

        assert result["ok"] is True
        assert result["posted"] is True
        assert result["ticket_id"] == 789
        assert "dry_run" not in result


# ── message_client ────────────────────────────────────────────────────────────

class TestMessageClient:
    def test_dry_run_returns_would_post(self, mock_config, mock_mappings, mock_cw_client):
        reg = _make_registry(mock_config, mock_mappings, mock_cw_client, dry_run=True)

        result = reg._message_client({
            "ticket_id": 100,
            "message": "Working on your issue.",
            "send_email_notification": False,
        })

        assert result["ok"] is True
        assert result["dry_run"] is True
        assert result["ticket_id"] == 100
        mock_cw_client.add_ticket_note.assert_not_called()

    def test_live_posts_discussion_note_no_email(self, mock_config, mock_mappings, mock_cw_client):
        reg = _make_registry(mock_config, mock_mappings, mock_cw_client, dry_run=False)

        reg._message_client({
            "ticket_id": 200,
            "message": "Issue resolved.",
            "send_email_notification": False,
        })

        mock_cw_client.add_ticket_note.assert_called_once_with(
            200,
            "Issue resolved.",
            internal_analysis_flag=False,
            detail_description_flag=True,
            resolution_flag=False,
            process_notifications=False,
        )

    def test_live_posts_with_email(self, mock_config, mock_mappings, mock_cw_client):
        reg = _make_registry(mock_config, mock_mappings, mock_cw_client, dry_run=False)

        reg._message_client({
            "ticket_id": 300,
            "message": "Scheduled for Monday.",
            "send_email_notification": True,
        })

        call_kwargs = mock_cw_client.add_ticket_note.call_args
        assert call_kwargs.kwargs["process_notifications"] is True


# ── assign_ticket ─────────────────────────────────────────────────────────────

class TestAssignTicket:
    def test_assigns_correct_owner(self, mock_config, mock_mappings, mock_cw_client):
        reg = _make_registry(mock_config, mock_mappings, mock_cw_client, dry_run=True)

        reg._assign_ticket({
            "ticket_id": 12345,
            "technician_identifier": "akloss",
        })

        mock_cw_client.patch_fields.assert_called_once()
        ticket_id_arg, changes_arg = mock_cw_client.patch_fields.call_args[0][:2]
        assert ticket_id_arg == 12345
        assert changes_arg["owner"] == "akloss"

    def test_assign_with_board_and_status(self, mock_config, mock_mappings, mock_cw_client):
        reg = _make_registry(mock_config, mock_mappings, mock_cw_client, dry_run=True)

        reg._assign_ticket({
            "ticket_id": 12345,
            "technician_identifier": "jsmith",
            "new_board": "Projects",
            "new_status": "In Progress",
        })

        changes = mock_cw_client.patch_fields.call_args[0][1]
        assert changes["owner"] == "jsmith"
        assert changes["board"] == "Projects"
        assert changes["status"] == "In Progress"

    def test_assign_without_board_or_status(self, mock_config, mock_mappings, mock_cw_client):
        reg = _make_registry(mock_config, mock_mappings, mock_cw_client, dry_run=True)

        reg._assign_ticket({"ticket_id": 1, "technician_identifier": "mwilson"})

        changes = mock_cw_client.patch_fields.call_args[0][1]
        assert "board" not in changes
        assert "status" not in changes

    def test_cw_error_returns_error_dict(self, mock_config, mock_mappings, mock_cw_client):
        reg = _make_registry(mock_config, mock_mappings, mock_cw_client, dry_run=True)
        mock_cw_client.patch_fields.side_effect = Exception("CW returned 500")

        result = reg._assign_ticket({
            "ticket_id": 12345,
            "technician_identifier": "akloss",
        })

        assert result["ok"] is False
        assert "CW returned 500" in result["error"]
        assert result["ticket_id"] == 12345


# ── flag_for_human_review ─────────────────────────────────────────────────────

class TestFlagForHumanReview:
    def test_flag_returns_expected_structure(self, mock_config, mock_mappings, mock_cw_client):
        reg = _make_registry(mock_config, mock_mappings, mock_cw_client, dry_run=True)

        result = reg._flag_for_human_review({
            "ticket_id": 789,
            "reason": "Ambiguous ticket type",
            "suggested_technician": "jsmith",
        })

        assert result["ok"] is True
        assert result["ticket_id"] == 789
        assert result["flagged"] is True
        assert result["reason"] == "Ambiguous ticket type"
        assert result["suggested_technician"] == "jsmith"

    def test_flag_without_suggested_tech(self, mock_config, mock_mappings, mock_cw_client):
        reg = _make_registry(mock_config, mock_mappings, mock_cw_client, dry_run=True)

        result = reg._flag_for_human_review({
            "ticket_id": 111,
            "reason": "Unknown company",
        })

        assert result["ok"] is True
        assert result["suggested_technician"] is None

    def test_flag_adds_internal_note_dry_run(self, mock_config, mock_mappings, mock_cw_client):
        """In dry-run mode no note is actually posted to CW."""
        reg = _make_registry(mock_config, mock_mappings, mock_cw_client, dry_run=True)

        reg._flag_for_human_review({"ticket_id": 222, "reason": "test"})

        # In dry_run mode, add_ticket_note is never called
        mock_cw_client.add_ticket_note.assert_not_called()

    def test_flag_posts_note_in_live_mode(self, mock_config, mock_mappings, mock_cw_client):
        """In live mode, an internal note is posted to CW."""
        reg = _make_registry(mock_config, mock_mappings, mock_cw_client, dry_run=False)

        reg._flag_for_human_review({"ticket_id": 333, "reason": "Needs escalation"})

        # Note should have been added with FLAGGED FOR HUMAN REVIEW text
        assert mock_cw_client.add_ticket_note.called
        note_text = mock_cw_client.add_ticket_note.call_args[0][1]
        assert "FLAGGED FOR HUMAN REVIEW" in note_text
        assert "Needs escalation" in note_text


# ── tool registry dispatcher ──────────────────────────────────────────────────

class TestToolRegistryDispatcher:
    def test_unknown_tool_raises_value_error(self, mock_config, mock_mappings, mock_cw_client):
        reg = _make_registry(mock_config, mock_mappings, mock_cw_client)

        with pytest.raises(ValueError, match="Unknown tool"):
            reg.call("this_tool_does_not_exist", {})

    def test_call_broadcasts_input_and_result(self, mock_config, mock_mappings, mock_cw_client):
        """call() emits both the tool input (→) and result (←) to broadcaster."""
        msgs: list[str] = []
        reg = _make_registry(mock_config, mock_mappings, mock_cw_client,
                              broadcaster=msgs.append)

        reg.call("update_ticket_notes", {"ticket_id": 1, "note_text": "test note"})

        combined = " ".join(msgs)
        assert "update_ticket_notes" in combined

    def test_call_returns_result(self, mock_config, mock_mappings, mock_cw_client):
        """call() returns whatever the handler returns."""
        reg = _make_registry(mock_config, mock_mappings, mock_cw_client, dry_run=True)

        result = reg.call("update_ticket_notes", {"ticket_id": 99, "note_text": "dry"})

        assert isinstance(result, dict)
        assert result.get("ok") is True
        assert result.get("dry_run") is True


# ── reassign_ticket ───────────────────────────────────────────────────────────

class TestReassignTicket:
    def test_reassign_calls_assign_and_posts_note(
        self, mock_config, mock_mappings, mock_cw_client
    ):
        """Reassign calls assign, then posts a reason note (in dry_run: both are dry)."""
        reg = _make_registry(mock_config, mock_mappings, mock_cw_client, dry_run=False)
        mock_cw_client.patch_fields.return_value = {"ok": True, "ticket_id": 1}

        result = reg._reassign_ticket({
            "ticket_id": 1,
            "new_technician_identifier": "mwilson",
            "reason": "akloss is on PTO",
        })

        assert mock_cw_client.patch_fields.called
        # In live mode, add_ticket_note should also be called for the reason
        assert mock_cw_client.add_ticket_note.called
        note_text = mock_cw_client.add_ticket_note.call_args[0][1]
        assert "mwilson" in note_text
        assert "akloss is on PTO" in note_text
