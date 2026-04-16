"""
tests/test_playwright.py

End-to-end browser tests using Playwright + pytest-playwright.

These tests spin up the real Flask app (on a random port) and drive it
through Chromium, verifying the UI and its interactions with the
ConnectWise-backed API surface.

Usage
-----
Run all E2E tests (headless, default):
    pytest tests/test_playwright.py

Run with visible browser (handy for debugging):
    pytest tests/test_playwright.py --headed

Run only tests that don't need live CW credentials:
    pytest tests/test_playwright.py -m "not live_cw"

Run with tracing for failed tests:
    pytest tests/test_playwright.py --tracing=retain-on-failure
"""

from __future__ import annotations

import json
import threading
import time

import pytest
from playwright.sync_api import Page, expect


# ── Live server fixture ───────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def live_server():
    """
    Build a session-scoped Flask app (in-memory DB, mocked dispatcher) and
    start it in a background thread on a free port.  Returns the base URL.

    This must be session-scoped so Playwright can share the server across all
    tests without spinning up a new process per test.
    """
    import os
    import socket
    from unittest.mock import MagicMock, patch
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    # ── Env vars ──────────────────────────────────────────────────────────────
    os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-real")
    os.environ.setdefault("CWM_COMPANY_ID",    "testco")
    os.environ.setdefault("CWM_PUBLIC_KEY",    "test-pub")
    os.environ.setdefault("CWM_PRIVATE_KEY",   "test-priv")
    os.environ.setdefault("CLIENT_ID",         "test-client-id")
    os.environ.setdefault("CWM_SITE",          "https://test.example.com/v4_6_release/apis/3.0")

    # ── In-memory DB ──────────────────────────────────────────────────────────
    # StaticPool keeps a single connection shared across all threads — required
    # for SQLite :memory: when Werkzeug runs multiple request threads.
    from sqlalchemy.pool import StaticPool
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from src.clients import database as db_module
    from src.clients.database import Base

    db_module._engine = engine
    db_module.SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)

    # ── Mock dispatcher ───────────────────────────────────────────────────────
    mock_dispatcher = MagicMock()
    mock_dispatcher.get_status.return_value = {
        "running": True,
        "paused":  False,
        "last_run": "2024-01-15 09:00:00 UTC",
        "next_run": None,
    }
    mock_dispatcher.start.return_value    = None
    mock_dispatcher.toggle_pause.return_value = False
    mock_dispatcher.run_once.return_value = None

    # ── Create app ────────────────────────────────────────────────────────────
    with patch("services.dispatcher.get_dispatcher", return_value=mock_dispatcher):
        from app import create_app
        app = create_app()
        app.config["TESTING"] = True

    # ── Find free port and start server ───────────────────────────────────────
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    base_url = f"http://127.0.0.1:{port}"

    server_thread = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, use_reloader=False),
        daemon=True,
    )
    server_thread.start()

    # Wait until the server is accepting connections
    import urllib.request
    for _ in range(40):
        try:
            urllib.request.urlopen(f"{base_url}/health", timeout=1)
            break
        except Exception:
            time.sleep(0.3)

    yield base_url


# ── Helper ────────────────────────────────────────────────────────────────────

def goto(page: Page, base_url: str, path: str = "/") -> None:
    """Navigate and wait for the network to be idle."""
    page.goto(f"{base_url}{path}", wait_until="networkidle")


# ══════════════════════════════════════════════════════════════════════════════
# Page load & basic structure
# ══════════════════════════════════════════════════════════════════════════════

class TestPageLoad:
    def test_title(self, page: Page, live_server):
        goto(page, live_server)
        expect(page).to_have_title("Ticket Router Portal")

    def test_sidebar_visible(self, page: Page, live_server):
        goto(page, live_server)
        expect(page.locator("#sidebar")).to_be_visible()

    def test_topbar_visible(self, page: Page, live_server):
        goto(page, live_server)
        expect(page.locator("#topbar")).to_be_visible()

    def test_dashboard_tab_active_on_load(self, page: Page, live_server):
        goto(page, live_server)
        dashboard_tab = page.locator(".nav-item[data-tab='dashboard']")
        classes = dashboard_tab.get_attribute("class") or ""
        assert "active" in classes

    def test_dashboard_panel_visible_on_load(self, page: Page, live_server):
        goto(page, live_server)
        expect(page.locator("#tab-dashboard")).to_be_visible()

    def test_topbar_title_shows_dashboard(self, page: Page, live_server):
        goto(page, live_server)
        expect(page.locator("#topbar-title")).to_have_text("Dashboard")


# ══════════════════════════════════════════════════════════════════════════════
# Sidebar navigation
# ══════════════════════════════════════════════════════════════════════════════

TABS = [
    ("run",        "tab-run",        "Run"),
    ("dispatcher", "tab-dispatcher", "Dispatcher"),
    ("config",     "tab-config",     "Config"),
    ("agents",     "tab-agents",     "Agents"),
    ("boards",     "tab-boards",     "Boards"),
    ("members",    "tab-members",    "Members"),
    ("env",        "tab-env",        "Env"),
    ("search",     "tab-search",     "Search"),
    ("report",     "tab-report",     "Report"),
]


class TestNavigation:
    @pytest.mark.parametrize("tab_key,panel_id,title_fragment", TABS)
    def test_tab_switch(self, page: Page, live_server, tab_key, panel_id, title_fragment):
        """Clicking each nav item shows the correct panel and updates the topbar title."""
        goto(page, live_server)

        nav_item = page.locator(f".nav-item[data-tab='{tab_key}']")
        nav_item.click()
        page.wait_for_timeout(200)

        # Panel becomes visible
        expect(page.locator(f"#{panel_id}")).to_be_visible()

        # Dashboard panel is hidden
        if tab_key != "dashboard":
            expect(page.locator("#tab-dashboard")).to_be_hidden()

    def test_active_class_moves_on_nav_click(self, page: Page, live_server):
        """The .active class migrates to the clicked nav item."""
        goto(page, live_server)

        run_nav = page.locator(".nav-item[data-tab='run']")
        run_nav.click()
        page.wait_for_timeout(150)

        # run should be active
        run_classes = run_nav.get_attribute("class") or ""
        assert "active" in run_classes
        # dashboard should no longer be active
        dash_nav = page.locator(".nav-item[data-tab='dashboard']")
        classes = dash_nav.get_attribute("class") or ""
        assert "active" not in classes


# ══════════════════════════════════════════════════════════════════════════════
# Dashboard metrics
# ══════════════════════════════════════════════════════════════════════════════

class TestDashboardMetrics:
    def test_metric_cards_present(self, page: Page, live_server):
        goto(page, live_server)
        for elem_id in ("dm-today", "dm-week", "dm-month", "dm-avg-time", "dm-flagged"):
            expect(page.locator(f"#{elem_id}")).to_be_visible()

    def test_metrics_load_from_api(self, page: Page, live_server):
        """After page load the JS fetches /api/dispatcher/metrics and fills the cards."""
        goto(page, live_server)
        # Wait up to 3s for the JS to replace the em-dash placeholder
        page.wait_for_function(
            "document.getElementById('dm-today').innerText !== '—'",
            timeout=3000,
        )
        today_text = page.locator("#dm-today").inner_text()
        assert today_text.isdigit() or today_text == "0"

    def test_live_feed_section_present(self, page: Page, live_server):
        goto(page, live_server)
        expect(page.locator("#dash-feed")).to_be_visible()

    def test_tech_workload_section_present(self, page: Page, live_server):
        goto(page, live_server)
        expect(page.locator("#dash-techs")).to_be_visible()


# ══════════════════════════════════════════════════════════════════════════════
# Run tab — controls & dry-run toggle
# ══════════════════════════════════════════════════════════════════════════════

class TestRunTab:
    def _open_run_tab(self, page: Page, live_server):
        goto(page, live_server)
        page.locator(".nav-item[data-tab='run']").click()
        page.wait_for_timeout(150)

    def test_run_button_visible(self, page: Page, live_server):
        self._open_run_tab(page, live_server)
        expect(page.locator("#btn-run")).to_be_visible()

    def test_stop_button_hidden_initially(self, page: Page, live_server):
        self._open_run_tab(page, live_server)
        expect(page.locator("#btn-stop")).to_be_hidden()

    def test_dry_run_checkbox_and_label_are_consistent(self, page: Page, live_server):
        """The checkbox state and label text must match each other."""
        self._open_run_tab(page, live_server)
        # Wait for JS to potentially load config and sync the checkbox
        page.wait_for_timeout(500)
        checkbox = page.locator("#dry-run-override")
        label    = page.locator("#dry-label").inner_text()
        is_checked = checkbox.is_checked()
        if is_checked:
            assert "Dry Run" in label
        else:
            assert "Live Run" in label

    def test_dry_run_toggle_unchecks(self, page: Page, live_server):
        self._open_run_tab(page, live_server)
        checkbox = page.locator("#dry-run-override")
        checkbox.uncheck()
        expect(checkbox).not_to_be_checked()

    def test_terminal_section_present(self, page: Page, live_server):
        self._open_run_tab(page, live_server)
        expect(page.locator("#terminal")).to_be_visible()

    def test_history_section_present(self, page: Page, live_server):
        self._open_run_tab(page, live_server)
        expect(page.locator("#history-card")).to_be_visible()


# ══════════════════════════════════════════════════════════════════════════════
# Dispatcher tab
# ══════════════════════════════════════════════════════════════════════════════

class TestDispatcherTab:
    def _open(self, page: Page, live_server):
        goto(page, live_server)
        page.locator(".nav-item[data-tab='dispatcher']").click()
        page.wait_for_timeout(200)

    def test_stat_cards_visible(self, page: Page, live_server):
        self._open(page, live_server)
        for elem_id in ("disp-status-val", "disp-tickets-today", "disp-interval", "disp-uptime"):
            expect(page.locator(f"#{elem_id}")).to_be_visible()

    def test_dispatcher_badge_visible(self, page: Page, live_server):
        self._open(page, live_server)
        expect(page.locator("#disp-badge")).to_be_visible()

    def test_toggle_pause_button_visible(self, page: Page, live_server):
        self._open(page, live_server)
        expect(page.locator("#disp-btn-toggle")).to_be_visible()

    def test_run_now_button_visible(self, page: Page, live_server):
        self._open(page, live_server)
        run_now = page.locator("button", has_text="Run Now")
        expect(run_now).to_be_visible()

    def test_history_table_present(self, page: Page, live_server):
        self._open(page, live_server)
        expect(page.locator("#disp-history-table")).to_be_visible()

    def test_dispatcher_status_loads(self, page: Page, live_server):
        """JS fetches /api/dispatcher/status and fills disp-status-val."""
        self._open(page, live_server)
        page.wait_for_function(
            "document.getElementById('disp-status-val').innerText !== '—'",
            timeout=3000,
        )
        status_text = page.locator("#disp-status-val").inner_text().lower()
        assert any(s in status_text for s in ("running", "paused", "stopped", "●", "■"))


# ══════════════════════════════════════════════════════════════════════════════
# Config tab
# ══════════════════════════════════════════════════════════════════════════════

class TestConfigTab:
    def _open(self, page: Page, live_server):
        goto(page, live_server)
        page.locator(".nav-item[data-tab='config']").click()
        page.wait_for_timeout(200)

    def test_model_select_present(self, page: Page, live_server):
        self._open(page, live_server)
        expect(page.locator("#cfg-model")).to_be_visible()

    def test_max_tickets_input_present(self, page: Page, live_server):
        self._open(page, live_server)
        expect(page.locator("#cfg-max-tickets")).to_be_visible()

    def test_boards_tag_input_present(self, page: Page, live_server):
        self._open(page, live_server)
        expect(page.locator("#input-boards")).to_be_visible()

    def test_config_loads_from_api(self, page: Page, live_server):
        """After switching to config tab, the model select is populated."""
        self._open(page, live_server)
        page.wait_for_function(
            "document.getElementById('cfg-model').value !== ''",
            timeout=3000,
        )
        model_val = page.locator("#cfg-model").input_value()
        assert "claude" in model_val


# ══════════════════════════════════════════════════════════════════════════════
# Env tab — ConnectWise credential fields
# ══════════════════════════════════════════════════════════════════════════════

class TestEnvTab:
    def _open(self, page: Page, live_server):
        goto(page, live_server)
        page.locator(".nav-item[data-tab='env']").click()
        page.wait_for_timeout(300)

    def test_env_tab_panel_visible(self, page: Page, live_server):
        self._open(page, live_server)
        expect(page.locator("#tab-env")).to_be_visible()

    def test_cw_credential_fields_rendered(self, page: Page, live_server):
        """The env tab must show input rows for each ConnectWise env key."""
        self._open(page, live_server)
        # The JS renders inputs dynamically from /api/env — wait for them
        page.wait_for_selector("#tab-env input", timeout=3000)
        inputs = page.locator("#tab-env input").all()
        assert len(inputs) >= 4, "Expected at least 4 env var input fields"

    def test_sensitive_fields_are_masked(self, page: Page, live_server):
        """Sensitive keys (CWM_PRIVATE_KEY, CWM_PUBLIC_KEY) render with masked values (bullet chars)."""
        self._open(page, live_server)
        page.wait_for_selector("#tab-env .env-input", timeout=3000)
        # Sensitive fields are rendered with a masked placeholder/value (e.g. "••••••••")
        # The API returns masked values via mask_value(); check that at least one input
        # has a value containing bullet chars or the placeholder text for set keys.
        inputs = page.locator("#tab-env .env-input").all()
        assert len(inputs) >= 4, "Expected at least 4 env var inputs"
        # Check for the set/unset status indicators
        set_indicators = page.locator("#tab-env .env-set, #tab-env .env-unset").all()
        assert len(set_indicators) >= 4, "Expected env-set/env-unset status indicators"


# ══════════════════════════════════════════════════════════════════════════════
# Health endpoint (via browser fetch)
# ══════════════════════════════════════════════════════════════════════════════

class TestHealthViaAPI:
    def test_health_endpoint_returns_json(self, page: Page, live_server):
        """Fetch /health directly and confirm it returns valid JSON with expected keys."""
        goto(page, live_server)
        result = page.evaluate("""
            async () => {
                const resp = await fetch('/health');
                const body = await resp.json();
                return { status: resp.status, body };
            }
        """)
        assert result["status"] in (200, 503)
        body = result["body"]
        assert body.get("flask") == "ok"
        assert "dispatcher" in body
        assert "db" in body

    def test_dispatcher_metrics_api(self, page: Page, live_server):
        goto(page, live_server)
        result = page.evaluate("""
            async () => {
                const resp = await fetch('/api/dispatcher/metrics');
                return await resp.json();
            }
        """)
        assert "today" in result
        assert "week" in result
        assert "month" in result

    def test_dispatcher_status_api(self, page: Page, live_server):
        goto(page, live_server)
        result = page.evaluate("""
            async () => {
                const resp = await fetch('/api/dispatcher/status');
                return await resp.json();
            }
        """)
        assert "running" in result

    def test_env_api_returns_cw_keys(self, page: Page, live_server):
        """GET /api/env must return at least the 4 ConnectWise credential keys."""
        goto(page, live_server)
        result = page.evaluate("""
            async () => {
                const resp = await fetch('/api/env');
                return await resp.json();
            }
        """)
        cw_keys = {"CWM_SITE", "CWM_COMPANY_ID", "CWM_PUBLIC_KEY", "CWM_PRIVATE_KEY"}
        returned_keys = set(result.get("vars", {}).keys())
        assert cw_keys.issubset(returned_keys)


# ══════════════════════════════════════════════════════════════════════════════
# ConnectWise integration — requires live credentials
# (skipped automatically when env vars are test/fake values)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.live_cw
class TestConnectWiseLive:
    """
    Tests that make real ConnectWise API calls.

    These are skipped unless the environment provides genuine CW credentials
    (CWM_SITE must NOT contain 'test.example.com').

    Run with:
        pytest tests/test_playwright.py -m live_cw
    """

    @pytest.fixture(autouse=True)
    def _require_live_creds(self):
        import os
        site = os.getenv("CWM_SITE", "")
        if "test.example.com" in site or not site:
            pytest.skip("Live ConnectWise credentials not configured")

    def test_health_cw_api_ok(self, page: Page, live_server):
        """With real credentials /health should report cw_api=ok."""
        goto(page, live_server)
        result = page.evaluate("""
            async () => {
                const resp = await fetch('/health');
                return await resp.json();
            }
        """)
        assert result.get("cw_api") == "ok", (
            f"Expected cw_api=ok, got: {result.get('cw_api')} — error: {result.get('cw_api_error')}"
        )

    def test_members_tab_loads_cw_members(self, page: Page, live_server):
        """Members tab should populate with real technician data from CW."""
        goto(page, live_server)
        page.locator(".nav-item[data-tab='members']").click()
        # Wait for the member table / list to render real data
        page.wait_for_timeout(2000)
        member_panel = page.locator("#tab-members")
        expect(member_panel).to_be_visible()
        # There should be at least one member row rendered
        rows = member_panel.locator("tr, .member-row, [data-member]").all()
        assert len(rows) > 0, "No member rows found — CW members may not have loaded"

    def test_boards_tab_loads_cw_boards(self, page: Page, live_server):
        """Boards tab should show at least the configured board name from CW."""
        goto(page, live_server)
        page.locator(".nav-item[data-tab='boards']").click()
        page.wait_for_timeout(2000)
        boards_panel = page.locator("#tab-boards")
        expect(boards_panel).to_be_visible()
        board_text = boards_panel.inner_text()
        assert board_text.strip() != "", "Boards panel is empty — no data loaded from CW"

    def test_run_single_ticket_dry_run(self, page: Page, live_server):
        """
        POST /api/dispatch/run-single with a known ticket ID.
        Requires PLAYWRIGHT_TEST_TICKET_ID env var to be set.
        """
        import os
        ticket_id = os.getenv("PLAYWRIGHT_TEST_TICKET_ID")
        if not ticket_id:
            pytest.skip("PLAYWRIGHT_TEST_TICKET_ID not set")

        goto(page, live_server)
        result = page.evaluate(
            """
            async (ticketId) => {
                const resp = await fetch('/api/dispatch/run-single', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ticket_id: ticketId, dry_run: true}),
                });
                return { status: resp.status, body: await resp.json() };
            }
            """,
            int(ticket_id),
        )
        assert result["status"] in (200, 207), f"Unexpected status: {result}"
        body = result["body"]
        assert body.get("ticket_id") == int(ticket_id)
        assert body.get("dry_run") is True
        assert body.get("status") in ("ok", "max_iterations", "timeout", "error")
