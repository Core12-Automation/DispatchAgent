"""
tests/test_perception_tools.py

Tests for perception tools:
  - src/tools/perception/tickets.py  (get_new_tickets, get_single_ticket_history)
  - src/tools/perception/technicians.py  (get_technician_workload)
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock


# ── Helpers ───────────────────────────────────────────────────────────────────

def _raw_ticket(
    id_: int,
    priority: str = "Medium",
    owner: dict | None = None,
    summary: str = "Test ticket",
) -> dict:
    return {
        "id": id_,
        "summary": summary,
        "priority": {"name": priority},
        "company": {"name": "Acme Corp"},
        "board": {"name": "Support"},
        "status": {"name": "New"},
        "owner": owner,
        "dateEntered": "2024-01-15T09:00:00Z",
        "type": {"name": "Service Request"},
    }


# ═════════════════════════════════════════════════════════════════════════════
# get_new_tickets
# ═════════════════════════════════════════════════════════════════════════════

class TestGetNewTickets:
    def test_returns_expected_structure(self, mock_cw_client, mock_config, mock_mappings):
        from src.tools.perception.tickets import get_new_tickets

        mock_cw_client.fetch_all_tickets.return_value = [_raw_ticket(1)]

        result = get_new_tickets(mock_cw_client, mock_config, mock_mappings)

        assert "tickets" in result
        assert "total_unrouted" in result
        assert "boards_scanned" in result
        assert isinstance(result["tickets"], list)
        assert isinstance(result["total_unrouted"], int)

    def test_slim_ticket_fields(self, mock_cw_client, mock_config, mock_mappings):
        """Each returned ticket contains exactly the slimmed-down fields."""
        from src.tools.perception.tickets import get_new_tickets

        mock_cw_client.fetch_all_tickets.return_value = [_raw_ticket(42, "High")]

        result = get_new_tickets(mock_cw_client, mock_config, mock_mappings)
        t = result["tickets"][0]

        assert t["id"] == 42
        assert t["priority"] == "High"
        assert t["company"] == "Acme Corp"
        assert t["board"] == "Support"
        assert t["status"] == "New"
        assert t["owner"] is None
        assert t["type"] == "Service Request"
        assert "date_entered" in t

    def test_summary_truncated_to_120(self, mock_cw_client, mock_config, mock_mappings):
        """Summaries longer than 120 chars are truncated."""
        from src.tools.perception.tickets import get_new_tickets

        mock_cw_client.fetch_all_tickets.return_value = [
            _raw_ticket(1, summary="A" * 200)
        ]
        result = get_new_tickets(mock_cw_client, mock_config, mock_mappings)
        assert len(result["tickets"][0]["summary"]) <= 120

    def test_unowned_tickets_included(self, mock_cw_client, mock_config, mock_mappings):
        """Tickets with owner=None are included as unrouted."""
        from src.tools.perception.tickets import get_new_tickets

        mock_cw_client.fetch_all_tickets.return_value = [_raw_ticket(1, owner=None)]

        result = get_new_tickets(mock_cw_client, mock_config, mock_mappings)
        assert result["total_unrouted"] == 1

    def test_owned_tickets_excluded(self, mock_cw_client, mock_config, mock_mappings):
        """Tickets owned by a real tech (not in unrouted_ids) are excluded."""
        from src.tools.perception.tickets import get_new_tickets

        mock_cw_client.fetch_all_tickets.return_value = [
            _raw_ticket(1, owner=None),                            # unrouted → included
            _raw_ticket(2, owner={"id": 999, "identifier": "jdoe"}),  # real tech → excluded
        ]

        result = get_new_tickets(mock_cw_client, mock_config, mock_mappings)
        assert result["total_unrouted"] == 1
        assert result["tickets"][0]["id"] == 1

    def test_priority_sort_critical_first(self, mock_cw_client, mock_config, mock_mappings):
        """Critical tickets appear before Low tickets in results."""
        from src.tools.perception.tickets import get_new_tickets

        mock_cw_client.fetch_all_tickets.return_value = [
            _raw_ticket(1, priority="Low"),
            _raw_ticket(2, priority="Critical"),
            _raw_ticket(3, priority="High"),
        ]

        result = get_new_tickets(mock_cw_client, mock_config, mock_mappings)
        ids = [t["id"] for t in result["tickets"]]
        assert ids[0] == 2  # Critical first
        assert ids[1] == 3  # High second
        assert ids[2] == 1  # Low last

    def test_limit_caps_results(self, mock_cw_client, mock_config, mock_mappings):
        """limit= parameter caps the number of returned tickets."""
        from src.tools.perception.tickets import get_new_tickets

        mock_cw_client.fetch_all_tickets.return_value = [
            _raw_ticket(i) for i in range(30)
        ]

        result = get_new_tickets(mock_cw_client, mock_config, mock_mappings, limit=5)
        assert len(result["tickets"]) == 5

    def test_limit_max_500(self, mock_cw_client, mock_config, mock_mappings):
        """limit= is capped at 500 even if a larger value is passed."""
        from src.tools.perception.tickets import get_new_tickets

        mock_cw_client.fetch_all_tickets.return_value = [
            _raw_ticket(i) for i in range(600)
        ]

        result = get_new_tickets(mock_cw_client, mock_config, mock_mappings, limit=9999)
        assert len(result["tickets"]) <= 500

    def test_priority_filter_passed_to_cw(self, mock_cw_client, mock_config, mock_mappings):
        """priority_filter is included in the conditions passed to fetch_all_tickets."""
        from src.tools.perception.tickets import get_new_tickets

        mock_cw_client.fetch_all_tickets.return_value = []

        get_new_tickets(
            mock_cw_client, mock_config, mock_mappings, priority_filter="Critical"
        )

        assert mock_cw_client.fetch_all_tickets.called
        call_kwargs = mock_cw_client.fetch_all_tickets.call_args
        conditions = call_kwargs.kwargs.get("conditions", "") or str(call_kwargs)
        assert "Critical" in conditions

    def test_cw_api_error_returns_empty_list(self, mock_cw_client, mock_config, mock_mappings):
        """CW API failure returns empty tickets list without raising."""
        from src.tools.perception.tickets import get_new_tickets

        mock_cw_client.fetch_all_tickets.side_effect = Exception("Connection timeout")

        result = get_new_tickets(mock_cw_client, mock_config, mock_mappings)

        assert result["tickets"] == []
        assert result["total_unrouted"] == 0

    def test_missing_board_in_mappings_is_skipped(self, mock_cw_client, mock_config, mock_mappings):
        """A board in config but not in mappings is skipped (no crash)."""
        from src.tools.perception.tickets import get_new_tickets

        config = {**mock_config, "boards_to_scan": ["Support", "NonExistentBoard"]}
        mock_cw_client.fetch_all_tickets.return_value = []

        result = get_new_tickets(mock_cw_client, config, mock_mappings)

        # Only "Support" is in mock_mappings boards, "NonExistentBoard" is skipped
        assert result["boards_scanned"] == ["Support"]


# ═════════════════════════════════════════════════════════════════════════════
# get_single_ticket_history
# ═════════════════════════════════════════════════════════════════════════════

class TestGetSingleTicketHistory:
    def test_returns_full_history_structure(self, mock_cw_client):
        from src.tools.perception.tickets import get_single_ticket_history

        mock_cw_client.get_ticket.return_value = {
            "id": 100,
            "summary": "Server down",
            "status": {"name": "In Progress"},
            "owner": {"identifier": "jsmith"},
            "dateEntered": "2024-01-15T09:00:00Z",
        }
        mock_cw_client.get_ticket_notes.return_value = [
            {
                "id": 1,
                "text": "Started investigation.",
                "internalAnalysisFlag": True,
                "resolutionFlag": False,
                "createdBy": {"identifier": "jsmith"},
                "dateCreated": "2024-01-15T10:00:00Z",
            }
        ]
        mock_cw_client.get_audit_trail.return_value = []

        result = get_single_ticket_history(mock_cw_client, 100)

        assert result["ticket_id"] == 100
        assert result["summary"] == "Server down"
        assert result["status"] == "In Progress"
        assert result["owner"] == "jsmith"
        assert len(result["notes"]) == 1
        note = result["notes"][0]
        assert note["text"] == "Started investigation."
        assert note["internal"] is True
        assert note["created_by"] == "jsmith"

    def test_audit_trail_included(self, mock_cw_client):
        from src.tools.perception.tickets import get_single_ticket_history

        mock_cw_client.get_ticket.return_value = {
            "id": 200, "summary": "T", "status": {"name": "New"},
            "owner": None, "dateEntered": "2024-01-15T09:00:00Z",
        }
        mock_cw_client.get_ticket_notes.return_value = []
        mock_cw_client.get_audit_trail.return_value = [
            {"text": "Status changed to In Progress", "memberIdentifier": "jsmith",
             "auditDate": "2024-01-15T10:00:00Z"},
        ]

        result = get_single_ticket_history(mock_cw_client, 200, include_audit=True)
        assert len(result["audit_trail"]) == 1
        assert result["audit_trail"][0]["action"] == "Status changed to In Progress"

    def test_audit_api_failure_returns_empty_trail(self, mock_cw_client):
        """Audit trail API failure is logged and returns empty list (no crash)."""
        from src.tools.perception.tickets import get_single_ticket_history

        mock_cw_client.get_ticket.return_value = {
            "id": 300, "summary": "T", "status": {"name": "New"},
            "owner": None, "dateEntered": "2024-01-15T09:00:00Z",
        }
        mock_cw_client.get_ticket_notes.return_value = []
        mock_cw_client.get_audit_trail.side_effect = Exception("Audit API unavailable")

        result = get_single_ticket_history(mock_cw_client, 300, include_audit=True)

        assert result["audit_trail"] == []
        assert result["ticket_id"] == 300

    def test_note_text_truncated_at_600(self, mock_cw_client):
        """Notes longer than 600 chars are truncated."""
        from src.tools.perception.tickets import get_single_ticket_history

        mock_cw_client.get_ticket.return_value = {
            "id": 400, "summary": "T", "status": {"name": "New"},
            "owner": None, "dateEntered": "2024-01-15T09:00:00Z",
        }
        mock_cw_client.get_ticket_notes.return_value = [
            {"id": 1, "text": "X" * 1000, "internalAnalysisFlag": False,
             "resolutionFlag": False, "createdBy": None, "dateCreated": None}
        ]
        mock_cw_client.get_audit_trail.return_value = []

        result = get_single_ticket_history(mock_cw_client, 400)
        assert len(result["notes"][0]["text"]) <= 600

    def test_audit_trail_capped_at_30_entries(self, mock_cw_client):
        """At most 30 audit entries are returned even if the API returns more."""
        from src.tools.perception.tickets import get_single_ticket_history

        mock_cw_client.get_ticket.return_value = {
            "id": 500, "summary": "T", "status": {"name": "New"},
            "owner": None, "dateEntered": "2024-01-15T09:00:00Z",
        }
        mock_cw_client.get_ticket_notes.return_value = []
        mock_cw_client.get_audit_trail.return_value = [
            {"text": f"Event {i}", "memberIdentifier": "sys", "auditDate": "2024-01-15T09:00:00Z"}
            for i in range(50)
        ]

        result = get_single_ticket_history(mock_cw_client, 500, include_audit=True)
        assert len(result["audit_trail"]) <= 30


# ═════════════════════════════════════════════════════════════════════════════
# get_technician_workload
# ═════════════════════════════════════════════════════════════════════════════

class TestGetTechnicianWorkload:
    def test_single_tech_open_ticket_count(self, mock_cw_client, mock_mappings):
        from src.tools.perception.technicians import get_technician_workload

        mock_cw_client.fetch_all_tickets.return_value = [
            {"id": 1, "priority": {"name": "High"}, "dateEntered": "2024-01-10T09:00:00Z"},
            {"id": 2, "priority": {"name": "Medium"}, "dateEntered": "2024-01-12T09:00:00Z"},
        ]

        result = get_technician_workload(mock_cw_client, mock_mappings, member_id=200)

        assert result["member_id"] == 200
        assert result["open_tickets"] == 2
        assert result["by_priority"]["High"] == 1
        assert result["by_priority"]["Medium"] == 1
        assert result["oldest_ticket_age_hours"] is not None

    def test_overloaded_flag_set_above_threshold(self, mock_cw_client, mock_mappings):
        from src.tools.perception.technicians import get_technician_workload

        mock_cw_client.fetch_all_tickets.return_value = [
            {"id": i, "priority": {"name": "Low"}, "dateEntered": "2024-01-10T09:00:00Z"}
            for i in range(6)
        ]

        result = get_technician_workload(
            mock_cw_client, mock_mappings, member_id=200, max_workload_threshold=5
        )
        assert result["overloaded"] is True

    def test_overloaded_flag_clear_below_threshold(self, mock_cw_client, mock_mappings):
        from src.tools.perception.technicians import get_technician_workload

        mock_cw_client.fetch_all_tickets.return_value = [
            {"id": i, "priority": {"name": "Low"}, "dateEntered": "2024-01-10T09:00:00Z"}
            for i in range(3)
        ]

        result = get_technician_workload(
            mock_cw_client, mock_mappings, member_id=200, max_workload_threshold=5
        )
        assert result["overloaded"] is False

    def test_all_techs_mode_returns_techs_list(self, mock_cw_client, mock_mappings):
        """all_techs=True fetches all open tickets once and groups by owner."""
        from src.tools.perception.technicians import get_technician_workload

        mock_cw_client.fetch_all_tickets.return_value = [
            {"id": 1, "priority": {"name": "High"}, "dateEntered": "2024-01-10T09:00:00Z",
             "owner": {"id": 200}},
            {"id": 2, "priority": {"name": "Medium"}, "dateEntered": "2024-01-12T09:00:00Z",
             "owner": {"id": 200}},
            {"id": 3, "priority": {"name": "Low"}, "dateEntered": "2024-01-14T09:00:00Z",
             "owner": {"id": 201}},
        ]

        result = get_technician_workload(mock_cw_client, mock_mappings, all_techs=True)

        assert "techs" in result
        assert "total_open" in result
        member_ids = {t["member_id"] for t in result["techs"]}
        assert 200 in member_ids
        assert 201 in member_ids

        akloss = next(t for t in result["techs"] if t["member_id"] == 200)
        assert akloss["open_tickets"] == 2
        assert akloss["identifier"] == "akloss"

    def test_all_techs_sorted_by_workload_ascending(self, mock_cw_client, mock_mappings):
        """Techs are sorted by open_tickets ascending (least loaded first)."""
        from src.tools.perception.technicians import get_technician_workload

        # akloss (200) has 3 tickets, jsmith (201) has 1
        mock_cw_client.fetch_all_tickets.return_value = [
            {"id": i, "priority": {"name": "Low"}, "dateEntered": "2024-01-10T09:00:00Z",
             "owner": {"id": 200}}
            for i in range(3)
        ] + [
            {"id": 99, "priority": {"name": "High"}, "dateEntered": "2024-01-10T09:00:00Z",
             "owner": {"id": 201}},
        ]

        result = get_technician_workload(mock_cw_client, mock_mappings, all_techs=True)

        # First tech should have fewer tickets
        techs = result["techs"]
        for i in range(len(techs) - 1):
            assert techs[i]["open_tickets"] <= techs[i + 1]["open_tickets"]

    def test_single_tech_cw_error_returns_error_dict(self, mock_cw_client, mock_mappings):
        """CW API failure returns error dict without raising."""
        from src.tools.perception.technicians import get_technician_workload

        mock_cw_client.fetch_all_tickets.side_effect = Exception("Connection refused")

        result = get_technician_workload(mock_cw_client, mock_mappings, member_id=200)

        assert "error" in result
        assert result["open_tickets"] == 0

    def test_all_techs_cw_error_returns_error_dict(self, mock_cw_client, mock_mappings):
        """all_techs=True CW failure returns error dict."""
        from src.tools.perception.technicians import get_technician_workload

        mock_cw_client.fetch_all_tickets.side_effect = Exception("API down")

        result = get_technician_workload(mock_cw_client, mock_mappings, all_techs=True)

        assert "error" in result
        assert result.get("techs", []) == []
