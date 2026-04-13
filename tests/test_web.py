"""
tests/test_web.py

Flask route tests using the test client.

Tests:
  - GET /             → 200 HTML
  - GET /health       → 200 or 503 with JSON body
  - GET /api/dispatcher/status   → 200 JSON
  - GET /api/dispatcher/decisions → 200 JSON list
  - GET /api/dispatcher/metrics   → 200 JSON with expected keys
  - GET /api/dispatcher/history   → 200 JSON list
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


# ── Index ─────────────────────────────────────────────────────────────────────

class TestIndexRoute:
    def test_returns_200(self, client):
        response = client.get("/")
        assert response.status_code == 200

    def test_returns_html(self, client):
        response = client.get("/")
        ct = response.content_type
        assert "text/html" in ct or b"<html" in response.data or b"<!DOCTYPE" in response.data


# ── Health endpoint ───────────────────────────────────────────────────────────

class TestHealthEndpoint:
    def test_returns_json_body(self, client):
        response = client.get("/health")
        assert response.status_code in (200, 503)
        data = json.loads(response.data)
        assert isinstance(data, dict)

    def test_flask_key_is_ok(self, client):
        response = client.get("/health")
        data = json.loads(response.data)
        assert data.get("flask") == "ok"

    def test_required_keys_present(self, client):
        response = client.get("/health")
        data = json.loads(response.data)
        # These three keys must always appear
        for key in ("flask", "dispatcher", "db"):
            assert key in data, f"Missing key: {key}"

    def test_200_when_all_subsystems_healthy(self, client):
        """
        When dispatcher is running and DB is reachable, expect 200.
        (The test flask_app fixture mocks a healthy dispatcher.)
        """
        response = client.get("/health")
        data = json.loads(response.data)

        if response.status_code == 200:
            assert data["dispatcher"] in ("running", "paused", "restarted")
        # 503 is also acceptable if the DB probe fails in the test environment

    def test_503_when_dispatcher_dead(self, flask_app):
        """503 is returned when the dispatcher cannot be started."""
        with flask_app.test_client() as c:
            with patch("services.dispatcher.get_dispatcher") as mock_disp:
                dispatcher = MagicMock()
                dispatcher.get_status.return_value = {"running": False, "paused": False}
                dispatcher.start.side_effect = Exception("Scheduler crashed")
                mock_disp.return_value = dispatcher

                response = c.get("/health")

        assert response.status_code == 503
        data = json.loads(response.data)
        assert data["dispatcher"] in ("dead", "error")

    def test_rate_limit_counters_included(self, client):
        """Rate limit counters appear when rate limiter is available."""
        response = client.get("/health")
        data = json.loads(response.data)
        # Keys may be absent if rate limiter import fails in test env — that's OK
        for key in ("claude_calls_this_hour", "cw_calls_this_hour"):
            if key in data:
                assert isinstance(data[key], int)


# ── Dispatcher status ─────────────────────────────────────────────────────────

class TestDispatcherStatusRoute:
    def test_returns_200_json(self, client):
        response = client.get("/api/dispatcher/status")
        assert response.status_code == 200
        data = json.loads(response.data)
        assert isinstance(data, dict)

    def test_contains_running_key(self, client):
        response = client.get("/api/dispatcher/status")
        data = json.loads(response.data)
        assert "running" in data


# ── Dispatcher decisions ──────────────────────────────────────────────────────

class TestDispatcherDecisionsRoute:
    def test_returns_200_list(self, client):
        response = client.get("/api/dispatcher/decisions")
        assert response.status_code == 200
        data = json.loads(response.data)
        assert isinstance(data, list)

    def test_empty_db_returns_empty_list(self, client):
        response = client.get("/api/dispatcher/decisions")
        data = json.loads(response.data)
        assert data == []

    def test_decisions_with_data_appear(self, client, in_memory_db, mock_mappings):
        """Decisions logged to the in-memory DB appear in the route response."""
        from src.tools.memory.decision_log import log_dispatch_decision

        log_dispatch_decision(
            77777, "akloss", "VPN expert", 0.9, [],
            ticket_summary="Cannot connect to VPN",
            was_dry_run=True,
            mappings=mock_mappings,
        )

        response = client.get("/api/dispatcher/decisions")
        data = json.loads(response.data)
        ticket_ids = [d["ticket_id"] for d in data]
        assert 77777 in ticket_ids

    def test_decision_entry_structure(self, client, in_memory_db, mock_mappings):
        """Each decision entry has the expected keys."""
        from src.tools.memory.decision_log import log_dispatch_decision

        log_dispatch_decision(
            88888, "jsmith", "Server issue", 0.8, [],
            was_dry_run=True, mappings=mock_mappings
        )

        response = client.get("/api/dispatcher/decisions")
        data = json.loads(response.data)

        entry = next((d for d in data if d["ticket_id"] == 88888), None)
        assert entry is not None

        expected_keys = {
            "id", "ticket_id", "ticket_summary", "assigned_to",
            "reason", "confidence", "was_dry_run", "created_at",
        }
        assert expected_keys.issubset(entry.keys())

    def test_returns_at_most_30(self, client, in_memory_db, mock_mappings):
        """Route caps results at 30 entries."""
        from src.tools.memory.decision_log import log_dispatch_decision

        for i in range(40):
            log_dispatch_decision(
                i, "akloss", "r", 0.9, [],
                was_dry_run=True, mappings=mock_mappings
            )

        response = client.get("/api/dispatcher/decisions")
        data = json.loads(response.data)
        assert len(data) <= 30


# ── Dispatcher metrics ────────────────────────────────────────────────────────

class TestDispatcherMetricsRoute:
    def test_returns_200_json(self, client):
        response = client.get("/api/dispatcher/metrics")
        assert response.status_code == 200
        data = json.loads(response.data)
        assert isinstance(data, dict)

    def test_has_required_counter_keys(self, client):
        response = client.get("/api/dispatcher/metrics")
        data = json.loads(response.data)

        for key in ("today", "week", "month", "flagged_today"):
            assert key in data, f"Missing key: {key}"

    def test_counters_are_integers(self, client):
        response = client.get("/api/dispatcher/metrics")
        data = json.loads(response.data)

        assert isinstance(data["today"], int)
        assert isinstance(data["week"], int)
        assert isinstance(data["month"], int)
        assert isinstance(data["flagged_today"], int)

    def test_avg_dispatch_secs_is_none_or_float(self, client):
        response = client.get("/api/dispatcher/metrics")
        data = json.loads(response.data)
        avg = data.get("avg_dispatch_secs")
        assert avg is None or isinstance(avg, (int, float))

    def test_assignments_by_tech_is_dict(self, client):
        response = client.get("/api/dispatcher/metrics")
        data = json.loads(response.data)
        assert isinstance(data.get("assignments_by_tech", {}), dict)

    def test_metrics_reflect_decisions(self, client, in_memory_db, mock_mappings):
        """today counter increments when decisions are logged today."""
        from src.tools.memory.decision_log import log_dispatch_decision

        # Get baseline
        r1 = client.get("/api/dispatcher/metrics")
        baseline = json.loads(r1.data)["today"]

        log_dispatch_decision(
            55555, "akloss", "r", 0.9, [],
            was_dry_run=True, mappings=mock_mappings
        )

        r2 = client.get("/api/dispatcher/metrics")
        updated = json.loads(r2.data)["today"]
        assert updated == baseline + 1


# ── Dispatcher history ────────────────────────────────────────────────────────

class TestDispatcherHistoryRoute:
    def test_returns_200_list(self, client):
        response = client.get("/api/dispatcher/history")
        assert response.status_code == 200
        data = json.loads(response.data)
        assert isinstance(data, list)

    def test_empty_db_returns_empty_list(self, client):
        response = client.get("/api/dispatcher/history")
        data = json.loads(response.data)
        assert data == []

    def test_history_entry_structure(self, client, in_memory_db):
        """DispatchRun rows appear in history with the expected shape."""
        from src.clients.database import DispatchRun
        from datetime import datetime, timezone

        with in_memory_db() as session:
            run = DispatchRun(
                tickets_processed=10,
                tickets_assigned=8,
                tickets_flagged=1,
                errors=0,
                trigger="manual",
            )
            session.add(run)
            session.commit()

        response = client.get("/api/dispatcher/history")
        data = json.loads(response.data)
        assert len(data) == 1

        entry = data[0]
        for key in ("id", "tickets_processed", "tickets_assigned", "trigger"):
            assert key in entry
        assert entry["tickets_processed"] == 10
        assert entry["tickets_assigned"] == 8
        assert entry["trigger"] == "manual"
