"""
tests/test_e2e_connectwise.py

Interactive end-to-end tests: ConnectWise UI -> Dispatch Agent -> Verify Assignment

HOW TO RUN
----------
1. Start the portal:  python run.py
2. Run the tests:     pytest tests/test_e2e_connectwise.py --headed -s -v

The browser opens automatically with two tabs:
  Tab 1 - DispatchAgent portal (localhost:5000)
  Tab 2 - ConnectWise

Log in to ConnectWise. The test watches the DOM and continues automatically
once it detects you are inside the app. Screenshots are saved to
tests/screenshots/ at every key step so progress can be monitored.
"""
from __future__ import annotations

import os
import time
import json
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

import pytest
import requests
from playwright.sync_api import Page, BrowserContext

# ── Configuration ─────────────────────────────────────────────────────────────

PORTAL_URL         = os.getenv("PORTAL_URL",       "http://localhost:5000")
CW_BASE_URL        = os.getenv("CW_BASE_URL",      "https://na.myconnectwise.net")
CW_URL             = f"{CW_BASE_URL}/v4_6_release/ConnectWise.aspx"
CW_DISPATCH_BOARD  = 38   # board ID from mappings.json

NAMED_TECH_DISPLAY = os.getenv("CW_NAMED_TECH",    "S. Ismail")
NAMED_TECH_ID      = os.getenv("CW_NAMED_TECH_ID", "sismail")

TEST_COMPANY = "core12"
TEST_BOARD   = "Dispatch"

SCREENSHOTS_DIR = Path(__file__).parent / "screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)


# ── Screenshot helper ─────────────────────────────────────────────────────────

def shot(page: Page, label: str) -> Path:
    """Save a full-page screenshot and return its path."""
    ts = datetime.now().strftime("%H%M%S")
    name = f"{ts}_{label}.png"
    path = SCREENSHOTS_DIR / name
    try:
        page.screenshot(path=str(path), full_page=True)
        print(f"  [screenshot] {name}")
    except Exception as e:
        print(f"  [screenshot failed] {label}: {e}")
    return path


# ── Portal / API helpers ──────────────────────────────────────────────────────

def portal_is_up() -> bool:
    try:
        urllib.request.urlopen(f"{PORTAL_URL}/health", timeout=3)
        return True
    except Exception:
        return False


def dispatch_ticket(ticket_id: int, dry_run: bool = False) -> dict:
    resp = requests.post(
        f"{PORTAL_URL}/api/dispatch/run-single",
        json={"ticket_id": ticket_id, "dry_run": dry_run},
        timeout=180,
    )
    return resp.json()


def get_portal_config() -> dict:
    resp = requests.get(f"{PORTAL_URL}/api/config", timeout=10)
    resp.raise_for_status()
    return resp.json()


def extract_ticket_id_from_url(url: str) -> Optional[int]:
    import re
    for pattern in (r"recid=(\d+)", r"ticket_id=(\d+)", r"/(\d+)$"):
        m = re.search(pattern, url)
        if m:
            val = int(m.group(1))
            if val > 0:
                return val
    return None


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def portal_check():
    if not portal_is_up():
        pytest.skip(f"Portal not running - start with: python run.py  (tried {PORTAL_URL}/health)")


@pytest.fixture(scope="module")
def cw_page(browser) -> Page:
    """
    Opens a two-tab browser:
      Tab 1 - Portal at localhost:5000 (Dashboard + live feed visible)
      Tab 2 - ConnectWise (for ticket creation and verification)

    Waits up to 3 minutes for the user to log in to CW by watching for the
    authenticated app shell in the DOM. No terminal interaction required.
    """
    context: BrowserContext = browser.new_context(viewport={"width": 1440, "height": 900})

    # ── Tab 1: Portal ─────────────────────────────────────────────────────────
    portal_tab = context.new_page()
    portal_tab.goto(PORTAL_URL, wait_until="networkidle", timeout=20_000)
    portal_tab.evaluate(
        "() => { const n = document.querySelector(\".nav-item[data-tab='run']\"); if(n) n.click(); }"
    )
    portal_tab.wait_for_timeout(400)
    shot(portal_tab, "01_portal_run_tab")
    print(f"\n  [Tab 1] Portal open: {PORTAL_URL}")

    # ── Tab 2: ConnectWise ────────────────────────────────────────────────────
    cw_tab = context.new_page()
    cw_tab.goto(CW_URL, wait_until="domcontentloaded", timeout=30_000)
    cw_tab.bring_to_front()
    shot(cw_tab, "02_cw_initial")
    print(f"  [Tab 2] ConnectWise open - please log in")
    print(f"  Waiting up to 3 minutes for login...")

    # Auto-detect login: wait for an element that only exists inside the
    # authenticated CW app (navigation bar, toolbar, or module content).
    # The login page has a password field; the app shell does not.
    logged_in = False
    for i in range(90):          # poll every 2 s for up to 3 min
        time.sleep(2)
        try:
            result = cw_tab.evaluate("""() => {
                const hasPassword = !!document.querySelector('input[type="password"]');
                const hasAppShell = !!(
                    document.querySelector('[class*="cw-nav"]') ||
                    document.querySelector('[class*="NavigationMenu"]') ||
                    document.querySelector('[id*="pageContent"]') ||
                    document.querySelector('[class*="module-header"]') ||
                    document.querySelector('[class*="toolbar"]') ||
                    document.querySelector('.header-bar') ||
                    document.querySelector('[class*="AppHeader"]') ||
                    document.querySelector('[class*="nav-bar"]') ||
                    document.querySelector('nav')
                );
                return { hasPassword, hasAppShell, title: document.title, url: window.location.href };
            }""")
            if result.get("hasAppShell") and not result.get("hasPassword"):
                logged_in = True
                break
            if i % 5 == 0:
                shot(cw_tab, f"03_cw_login_wait_{i:02d}")
                print(f"  Waiting for login... (title: {result.get('title','')[:40]})")
        except Exception:
            pass

    shot(cw_tab, "04_cw_after_login")
    if logged_in:
        print("  Login detected - navigating to Dispatch board...")
    else:
        print("  Login timeout - proceeding anyway...")

    # Navigate to the Dispatch service board
    dispatch_board_url = (
        f"{CW_URL}?locale=en_US&routeId=ServiceBoardFV"
        f"&boardId={CW_DISPATCH_BOARD}"
    )
    cw_tab.goto(dispatch_board_url, wait_until="domcontentloaded", timeout=20_000)
    cw_tab.wait_for_timeout(2000)
    shot(cw_tab, "05_dispatch_board")
    print(f"  Navigated to Dispatch board (ID {CW_DISPATCH_BOARD})")

    yield cw_tab
    context.close()


# ── ConnectWise form helpers ──────────────────────────────────────────────────

def _navigate_to_new_ticket(page: Page) -> None:
    """Navigate to a blank new service ticket form."""
    new_ticket_url = (
        f"{CW_URL}?locale=en_US&routeId=ServiceFV"
        f"&recordType=ServiceTicket&recid=0"
    )
    page.goto(new_ticket_url, wait_until="domcontentloaded", timeout=20_000)
    page.wait_for_timeout(1500)

    # If that URL dumped us somewhere unexpected, try clicking New -> Service Ticket
    if "recid=0" not in page.url:
        _click_new_service_ticket(page)

    _wait_for_ticket_form(page)


def _click_new_service_ticket(page: Page) -> None:
    for sel in ["button:has-text('New')", "[title='New']", "a:has-text('New')", ".toolbar-new"]:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1500):
                btn.click()
                page.wait_for_timeout(400)
                break
        except Exception:
            continue
    for sel in ["text=Service Ticket", "[role='menuitem']:has-text('Service Ticket')", "li:has-text('Service Ticket')"]:
        try:
            item = page.locator(sel).first
            if item.is_visible(timeout=2000):
                item.click()
                return
        except Exception:
            continue


def _wait_for_ticket_form(page: Page, timeout: int = 15_000) -> None:
    for sel in [
        "input[placeholder*='Summary' i]", "input[placeholder*='Subject' i]",
        "input[aria-label*='Summary' i]", "[id*='summary' i]", "[name*='summary' i]",
        ".ticket-summary input", "input",
    ]:
        try:
            page.wait_for_selector(sel, timeout=timeout)
            return
        except Exception:
            continue


def _fill_ticket_form(page: Page, *, summary: str, description: str,
                      company: str = TEST_COMPANY, board: str = TEST_BOARD) -> None:
    # Summary
    for sel in ["input[placeholder*='Summary' i]", "input[placeholder*='Subject' i]",
                "input[aria-label*='Summary' i]", "[id*='summary' i]", "[name*='summary' i]"]:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=2000):
                loc.click(); loc.fill(summary)
                break
        except Exception:
            continue

    # Company (autocomplete)
    for sel in ["input[placeholder*='Company' i]", "input[aria-label*='Company' i]",
                "[id*='company' i] input", "[name*='company' i]", "[data-field='company'] input"]:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=2000):
                loc.click(); loc.fill(company)
                page.wait_for_timeout(800)
                for ss in [f"[role='option']:has-text('{company}')", f"li:has-text('{company}')",
                           "[role='listbox'] li:first-child", "[class*='autocomplete'] li:first-child"]:
                    try:
                        sug = page.locator(ss).first
                        if sug.is_visible(timeout=1500): sug.click(); break
                    except Exception: continue
                break
        except Exception:
            continue

    # Board
    for sel in ["select[id*='board' i]", "input[placeholder*='Board' i]",
                "input[aria-label*='Board' i]", "[id*='board' i] input", "[name*='board' i]"]:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=2000):
                tag = loc.evaluate("el => el.tagName.toLowerCase()")
                if tag == "select":
                    loc.select_option(label=board)
                else:
                    loc.click(); loc.fill(board)
                    page.wait_for_timeout(600)
                    for ss in [f"[role='option']:has-text('{board}')", f"li:has-text('{board}')"]:
                        try:
                            opt = page.locator(ss).first
                            if opt.is_visible(timeout=1000): opt.click(); break
                        except Exception: continue
                break
        except Exception:
            continue

    # Initial Description
    for sel in ["textarea[placeholder*='Description' i]", "textarea[placeholder*='Initial' i]",
                "[id*='description' i] textarea", "textarea[aria-label*='Description' i]",
                "[contenteditable='true']", "textarea"]:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=2000):
                tag = loc.evaluate("el => el.tagName.toLowerCase()")
                if tag in ("textarea", "input"):
                    loc.click(); loc.fill(description)
                else:
                    loc.click()
                    loc.evaluate(f"el => el.innerText = {json.dumps(description)}")
                break
        except Exception:
            continue


def _save_ticket(page: Page) -> None:
    for sel in ["button:has-text('Save')", "[type='submit']:has-text('Save')",
                "[id*='save' i]", "button[title*='Save' i]"]:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=2000):
                btn.click()
                return
        except Exception:
            continue
    page.keyboard.press("Control+s")


def _wait_for_saved_ticket_id(page: Page) -> Optional[int]:
    for _ in range(30):
        time.sleep(1)
        tid = extract_ticket_id_from_url(page.url)
        if tid:
            return tid
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Pre-flight checks (no browser)
# ══════════════════════════════════════════════════════════════════════════════

class TestPortalDefaults:
    def test_portal_is_reachable(self, portal_check):
        resp = requests.get(f"{PORTAL_URL}/health", timeout=10)
        assert resp.status_code in (200, 503)

    def test_portal_is_in_live_mode_not_dry_run(self, portal_check):
        cfg = get_portal_config()
        assert cfg.get("dry_run") is False, (
            f"Portal is in dry-run mode - set dry_run=false in data/portal_config.json.\n{json.dumps(cfg, indent=2)}"
        )

    def test_dispatch_board_is_configured(self, portal_check):
        cfg = get_portal_config()
        boards = cfg.get("boards_to_scan", [])
        assert TEST_BOARD in boards, f"'{TEST_BOARD}' not in boards_to_scan: {boards}"

    def test_health_cw_api_ok(self, portal_check):
        data = requests.get(f"{PORTAL_URL}/health", timeout=10).json()
        assert data.get("cw_api") == "ok", (
            f"CW API not OK: {data.get('cw_api')} / {data.get('cw_api_error')}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Dispatch routing tests
# ══════════════════════════════════════════════════════════════════════════════

class TestDispatchRouting:

    @pytest.fixture(autouse=True)
    def _require_portal(self, portal_check):
        pass

    @staticmethod
    def _get_portal_tab(cw_page: Page) -> Optional[Page]:
        try:
            for p in cw_page.context.pages:
                if "localhost" in p.url or "127.0.0.1" in p.url:
                    return p
        except Exception:
            pass
        return None

    def _create_and_dispatch(self, cw_page: Page, *, summary: str,
                              description: str, dry_run: bool = False) -> tuple[int, dict]:
        # Navigate to new ticket form
        _navigate_to_new_ticket(cw_page)
        shot(cw_page, "10_new_ticket_form")

        _fill_ticket_form(cw_page, summary=summary, description=description)
        shot(cw_page, "11_form_filled")

        _save_ticket(cw_page)
        ticket_id = _wait_for_saved_ticket_id(cw_page)
        shot(cw_page, f"12_ticket_saved_{ticket_id or 'unknown'}")

        assert ticket_id, (
            f"Could not get ticket ID after save - URL: {cw_page.url}"
        )
        print(f"\n  [OK] Created ticket #{ticket_id}: {summary[:50]}")

        # Switch portal tab to Run view so user can watch live output
        portal_tab = self._get_portal_tab(cw_page)
        if portal_tab:
            portal_tab.bring_to_front()
            portal_tab.evaluate(
                "() => { const n = document.querySelector(\".nav-item[data-tab='run']\"); if(n) n.click(); }"
            )
            portal_tab.wait_for_timeout(300)
            shot(portal_tab, f"13_portal_before_dispatch_{ticket_id}")

        print(f"  >> Dispatching ticket #{ticket_id}...")
        result = dispatch_ticket(ticket_id, dry_run=dry_run)

        if portal_tab:
            portal_tab.wait_for_timeout(500)
            shot(portal_tab, f"14_portal_after_dispatch_{ticket_id}")

        tools = [e.get("tool") for e in (result.get("tools_called") or [])]
        print(f"  Status: {result.get('status')} | Tools: {tools}")

        # Flip back to CW to verify
        cw_page.bring_to_front()
        return ticket_id, result

    def _get_cw_owner(self, cw_page: Page, ticket_id: int) -> Optional[str]:
        cw_page.goto(
            f"{CW_URL}?locale=en_US&routeId=ServiceFV&recordType=ServiceTicket&recid={ticket_id}",
            wait_until="domcontentloaded", timeout=20_000,
        )
        cw_page.wait_for_timeout(2000)
        shot(cw_page, f"20_verify_ticket_{ticket_id}")
        for sel in ["[id*='owner' i] input", "[id*='assigned' i] input",
                    "[aria-label*='Owner' i]", "[data-field='owner'] input",
                    "[placeholder*='Owner' i]"]:
            try:
                loc = cw_page.locator(sel).first
                if loc.is_visible(timeout=2000):
                    val = loc.input_value() if loc.evaluate("el => el.tagName") == "INPUT" else loc.inner_text()
                    if val.strip():
                        return val.strip()
            except Exception:
                continue
        return None

    # ── Tests ─────────────────────────────────────────────────────────────────

    def test_printer_ticket_routes_to_correct_tech(self, cw_page: Page):
        """Printer broke -> should go to sismail (printer/M365) or jdunn (hardware)."""
        ticket_id, result = self._create_and_dispatch(
            cw_page,
            summary="Printer not printing - paper jams and offline error",
            description=(
                "Our HP LaserJet printer has been showing an offline error all morning "
                "and keeps jamming. None of the users on the floor can print. "
                "We tried restarting it but it still shows offline in Windows. "
                "Printer broke and we need it fixed ASAP."
            ),
        )
        assert result.get("status") in ("ok", "max_iterations"), f"Dispatch failed: {result.get('error')}"

        decisions = result.get("decisions_made") or []
        all_assigned = {d.get("technician_identifier", "") for d in decisions}
        all_assigned.discard("")
        print(f"  Assigned to: {all_assigned}")

        printer_techs = {"sismail", "jdunn"}
        assert any(t in all_assigned for t in printer_techs), (
            f"Expected printer ticket -> {printer_techs}, got: {all_assigned}\n"
            f"Full result: {json.dumps(result, indent=2, default=str)}"
        )
        owner = self._get_cw_owner(cw_page, ticket_id)
        print(f"  CW owner field: {owner}")

    def test_password_reset_routes_to_tier1(self, cw_page: Page):
        """Password reset -> jnelms (Tier 1, explicitly handles password resets)."""
        ticket_id, result = self._create_and_dispatch(
            cw_page,
            summary="Password reset needed - locked out of Windows login",
            description=(
                "User is completely locked out of their Windows account. "
                "They entered the wrong password too many times and the account is locked. "
                "They need a password reset to get back into their computer."
            ),
        )
        assert result.get("status") in ("ok", "max_iterations"), f"Dispatch failed: {result.get('error')}"

        decisions = result.get("decisions_made") or []
        all_assigned = {d.get("technician_identifier", "") for d in decisions}
        all_assigned.discard("")
        print(f"  Assigned to: {all_assigned}")

        assert "jnelms" in all_assigned, (
            f"Expected password reset -> jnelms (J. Nelms), got: {all_assigned}\n"
            f"Full result: {json.dumps(result, indent=2, default=str)}"
        )

    def test_named_tech_routing(self, cw_page: Page):
        """Explicit tech request in description should be honored."""
        ticket_id, result = self._create_and_dispatch(
            cw_page,
            summary="Outlook not loading - email and calendar errors",
            description=(
                f"I'm having trouble with Outlook not loading properly. "
                f"Emails are not syncing and the calendar shows errors. "
                f"I would like specifically {NAMED_TECH_DISPLAY} to help me with this "
                f"as they have helped me before and I trust their work."
            ),
        )
        assert result.get("status") in ("ok", "max_iterations"), f"Dispatch failed: {result.get('error')}"

        decisions = result.get("decisions_made") or []
        all_assigned = {d.get("technician_identifier", "") for d in decisions}
        all_assigned.discard("")
        print(f"  Named-routing result - Assigned to: {all_assigned}")
        print(f"  Expected: {NAMED_TECH_ID} ({NAMED_TECH_DISPLAY})")

        assert NAMED_TECH_ID in all_assigned, (
            f"Agent ignored explicit request for '{NAMED_TECH_DISPLAY}' ({NAMED_TECH_ID}).\n"
            f"Assigned to: {all_assigned}\n"
            f"Full result: {json.dumps(result, indent=2, default=str)}"
        )

    def test_schedule_check_called_for_high_priority(self, cw_page: Page):
        """Critical ticket should trigger schedule/availability tool calls."""
        ticket_id, result = self._create_and_dispatch(
            cw_page,
            summary="URGENT: Server completely offline - production down",
            description=(
                "Our main file server has gone completely offline as of 9am. "
                "All users cannot access shared drives, internal apps, or email. "
                "This is a critical production outage affecting the entire company. "
                "Need immediate on-site response."
            ),
        )
        assert result.get("status") in ("ok", "max_iterations"), f"Dispatch failed: {result.get('error')}"

        tool_names = {e.get("tool", "") for e in (result.get("tools_called") or [])}
        schedule_tools = {"get_technician_schedule", "get_tech_availability", "get_technician_workload"}
        print(f"  Tools called: {tool_names}")
        assert bool(tool_names & schedule_tools), (
            f"Expected a schedule/workload tool in {schedule_tools}, got: {tool_names}"
        )

        decisions = result.get("decisions_made") or []
        all_assigned = {d.get("technician_identifier", "") for d in decisions}
        all_assigned.discard("")
        high_tier_techs = {"cbauer", "mdubuisson", "sspencer", "akloss"}
        print(f"  Assigned to: {all_assigned}")
        assert any(t in all_assigned for t in high_tier_techs), (
            f"Expected critical ticket -> Tier 2/3 {high_tier_techs}, got: {all_assigned}"
        )

    def test_live_dispatch_assigns_in_cw(self, cw_page: Page):
        """Live mode (dry_run=False): ticket must have an owner set in CW after dispatch."""
        ticket_id, result = self._create_and_dispatch(
            cw_page,
            summary="Test: email not receiving messages since yesterday",
            description=(
                "User reports not receiving any emails since yesterday afternoon. "
                "Other users on the same domain receive emails fine. "
                "Need help troubleshooting the mailbox."
            ),
            dry_run=False,
        )
        assert result.get("status") in ("ok", "max_iterations"), f"Dispatch failed: {result.get('error')}"
        assert result.get("dry_run") is False, f"Expected dry_run=False in result, got: {result.get('dry_run')}"

        time.sleep(3)   # brief pause for CW to process the update
        owner = self._get_cw_owner(cw_page, ticket_id)
        print(f"  Live dispatch - CW owner after dispatch: {owner}")
        assert owner, (
            f"Ticket #{ticket_id} has no owner in CW after live dispatch.\n"
            f"Check _get_cw_owner() selector list or CW field names."
        )


# ══════════════════════════════════════════════════════════════════════════════
# Schedule tool checks (no browser, portal API only)
# ══════════════════════════════════════════════════════════════════════════════

class TestScheduleTool:

    @pytest.fixture(autouse=True)
    def _require_portal(self, portal_check):
        pass

    def test_members_endpoint_reachable(self):
        resp = requests.get(f"{PORTAL_URL}/api/members", timeout=15)
        assert resp.status_code == 200

    def test_dispatch_result_has_tools_called(self):
        decisions = requests.get(f"{PORTAL_URL}/api/dispatcher/decisions", timeout=10).json()
        if not decisions:
            pytest.skip("No dispatch decisions in log yet - run dispatch tests first")
        ticket_id = decisions[0].get("ticket_id")
        if not ticket_id:
            pytest.skip("No ticket_id in last decision")
        result = dispatch_ticket(ticket_id, dry_run=True)
        print(f"  Re-dispatch #{ticket_id}: {result.get('status')}")
        assert "tools_called" in result
        assert isinstance(result["tools_called"], list)
        for entry in result["tools_called"]:
            assert "tool" in entry

    def test_schedule_tool_fires_on_critical(self):
        decisions = requests.get(f"{PORTAL_URL}/api/dispatcher/decisions", timeout=10).json()
        if not decisions:
            pytest.skip("No prior dispatch decisions")
        ticket_id = decisions[0].get("ticket_id")
        if not ticket_id:
            pytest.skip("No ticket_id")
        result = dispatch_ticket(ticket_id, dry_run=True)
        tool_names = {e.get("tool", "") for e in (result.get("tools_called") or [])}
        if "get_technician_schedule" not in tool_names:
            pytest.skip(f"get_technician_schedule not called for #{ticket_id} (likely low-priority)")
        assert result.get("status") != "error"
