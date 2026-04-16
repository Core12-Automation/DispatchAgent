"""
tests/test_members_db.py

Playwright tests for the Members tab.  Each test:
  1. Drives the real UI at http://localhost:5000
  2. After any save / delete, queries the live SQLite DB directly to confirm
     the change was persisted — the DB is always the source of truth.

Prerequisites
-------------
    pip install pytest pytest-playwright
    playwright install chromium
    Flask app must be running:  python -m flask run --port 5000

Run:
    pytest tests/test_members_db.py -v --headed        # visible browser
    pytest tests/test_members_db.py -v                  # headless
"""

from __future__ import annotations

import sqlite3
import time
import os
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, expect

# ── Config ────────────────────────────────────────────────────────────────────

BASE_URL = "http://localhost:5000"
DB_PATH  = Path(__file__).resolve().parents[1] / "data" / "dispatcher.db"

# Identifier guaranteed to exist after sync (from mappings.json)
KNOWN_IDENT = "akloss"
KNOWN_CW_ID = 407


# ── DB helpers ────────────────────────────────────────────────────────────────

def db_row(cw_identifier: str) -> dict[str, Any] | None:
    """Fetch a single technician row by cw_identifier."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM technicians WHERE cw_identifier = ?",
            (cw_identifier,),
        ).fetchone()
    return dict(row) if row else None


def db_row_by_cw_id(cw_member_id: int) -> dict[str, Any] | None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM technicians WHERE cw_member_id = ?",
            (cw_member_id,),
        ).fetchone()
    return dict(row) if row else None


def db_all_identifiers() -> list[str]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT cw_identifier FROM technicians WHERE cw_identifier IS NOT NULL"
        ).fetchall()
    return [r[0] for r in rows]


# ── Page helpers ──────────────────────────────────────────────────────────────

def open_members(page: Page) -> None:
    """Navigate to the portal and switch to the Members tab."""
    page.goto(BASE_URL, wait_until="networkidle")
    page.locator(".nav-item[data-tab='members']").click()
    # Wait for the member table to populate from the DB
    page.wait_for_selector("#members-tbody tr", timeout=8000)


def save_members(page: Page) -> None:
    """Click Save Members and wait for the success toast."""
    page.locator("button", has_text="Save Members").click()
    page.wait_for_function(
        "document.querySelector('.toast') !== null",
        timeout=5000,
    )
    page.wait_for_timeout(400)   # let the PUT calls settle


def row_for(page: Page, ident: str):
    """Return the <tr> locator whose first input matches the identifier."""
    return page.locator(
        f"#members-tbody tr:has(input.mem-name[value='{ident}'])"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Sanity: portal reachable and members tab renders
# ══════════════════════════════════════════════════════════════════════════════

class TestMembersTabRenders:
    def test_portal_reachable(self, page: Page):
        page.goto(BASE_URL, wait_until="networkidle")
        expect(page).to_have_title("Ticket Router Portal")

    def test_members_tab_exists(self, page: Page):
        page.goto(BASE_URL, wait_until="networkidle")
        expect(page.locator(".nav-item[data-tab='members']")).to_be_visible()

    def test_members_table_renders_rows(self, page: Page):
        open_members(page)
        rows = page.locator("#members-tbody tr").all()
        assert len(rows) > 0, "Members table is empty — DB may be empty"

    def test_all_columns_present(self, page: Page):
        open_members(page)
        tr = page.locator("#members-tbody tr").first
        assert tr.locator("input.mem-name").count()     == 1
        assert tr.locator("input.mem-fullname").count() == 1
        assert tr.locator("input.mem-id").count()       == 1
        assert tr.locator(".pill").count()               >= 1
        assert tr.locator(".mem-status-pill").count()    == 1
        assert tr.locator("textarea.agent-desc").count() == 1

    def test_db_rows_match_ui_rows(self, page: Page):
        """Every cw_identifier in the DB must appear as a row in the table."""
        open_members(page)
        db_idents = set(db_all_identifiers())
        ui_idents = set(
            page.locator("#members-tbody input.mem-name").evaluate_all(
                "els => els.map(e => e.value)"
            )
        )
        missing = db_idents - ui_idents
        assert not missing, f"DB identifiers not shown in UI: {missing}"


# ══════════════════════════════════════════════════════════════════════════════
# Full Name  →  DB name column
# ══════════════════════════════════════════════════════════════════════════════

class TestFullNamePersists:
    def test_edit_full_name_updates_db(self, page: Page):
        open_members(page)
        original = db_row(KNOWN_IDENT)["name"]
        new_name = f"Test Name {int(time.time()) % 10000}"

        row = row_for(page, KNOWN_IDENT)
        fn = row.locator("input.mem-fullname")
        fn.click(click_count=3)
        fn.fill(new_name)

        save_members(page)

        after = db_row(KNOWN_IDENT)
        assert after is not None, "Row disappeared from DB"
        assert after["name"] == new_name, (
            f"Expected name={new_name!r}, got {after['name']!r}"
        )

        # Restore
        fn.click(click_count=3)
        fn.fill(original)
        save_members(page)

    def test_full_name_shown_from_db_on_reload(self, page: Page):
        """After save, reloading the tab reads the updated name from DB."""
        open_members(page)
        new_name = f"ReloadTest {int(time.time()) % 10000}"

        row = row_for(page, KNOWN_IDENT)
        row.locator("input.mem-fullname").click(click_count=3)
        row.locator("input.mem-fullname").fill(new_name)
        save_members(page)

        # Re-open the tab to force a fresh DB fetch
        page.locator(".nav-item[data-tab='dashboard']").click()
        page.wait_for_timeout(300)
        open_members(page)

        displayed = row_for(page, KNOWN_IDENT).locator("input.mem-fullname").input_value()
        assert displayed == new_name, (
            f"UI shows {displayed!r} after reload but DB was set to {new_name!r}"
        )

        # Restore
        row_for(page, KNOWN_IDENT).locator("input.mem-fullname").click(click_count=3)
        row_for(page, KNOWN_IDENT).locator("input.mem-fullname").fill("Aaron Kloss")
        save_members(page)


# ══════════════════════════════════════════════════════════════════════════════
# Routable pill  →  DB routable column
# ══════════════════════════════════════════════════════════════════════════════

class TestRoutablePersists:
    def test_toggle_routable_updates_db(self, page: Page):
        open_members(page)
        before = db_row(KNOWN_IDENT)["routable"]

        row   = row_for(page, KNOWN_IDENT)
        pill  = row.locator(".pill")
        pill.click()
        save_members(page)

        after = db_row(KNOWN_IDENT)["routable"]
        assert after != before, (
            f"Routable did not change in DB (still {after})"
        )

        # Restore
        pill.click()
        save_members(page)

    def test_routable_false_persists_across_reload(self, page: Page):
        open_members(page)
        # Ensure routable is True first
        row  = row_for(page, KNOWN_IDENT)
        pill = row.locator(".pill")
        if "pill-no" in (pill.get_attribute("class") or ""):
            pill.click()
            save_members(page)
            open_members(page)

        # Toggle to False
        row_for(page, KNOWN_IDENT).locator(".pill").click()
        save_members(page)

        db_val = db_row(KNOWN_IDENT)["routable"]
        assert db_val == 0, f"Expected routable=0 in DB, got {db_val}"

        # Reload and check it stayed False
        page.locator(".nav-item[data-tab='dashboard']").click()
        page.wait_for_timeout(300)
        open_members(page)
        pill_class = row_for(page, KNOWN_IDENT).locator(".pill").get_attribute("class") or ""
        assert "pill-no" in pill_class, "Routable pill did not stay toggled after reload"

        # Restore
        row_for(page, KNOWN_IDENT).locator(".pill").click()
        save_members(page)


# ══════════════════════════════════════════════════════════════════════════════
# Active pill  →  DB is_active column
# ══════════════════════════════════════════════════════════════════════════════

class TestActivePersists:
    def test_toggle_active_updates_db(self, page: Page):
        open_members(page)
        before = db_row(KNOWN_IDENT)["is_active"]

        row  = row_for(page, KNOWN_IDENT)
        pill = row.locator(".mem-status-pill")
        pill.click()
        save_members(page)

        after = db_row(KNOWN_IDENT)["is_active"]
        assert after != before, f"is_active did not change (still {after})"

        # Restore
        pill.click()
        save_members(page)

    def test_inactive_stays_after_reload(self, page: Page):
        open_members(page)
        row  = row_for(page, KNOWN_IDENT)
        pill = row.locator(".mem-status-pill")

        # Ensure currently active
        if "inactive" in (pill.get_attribute("class") or ""):
            pill.click()
            save_members(page)
            open_members(page)

        # Toggle to inactive
        row_for(page, KNOWN_IDENT).locator(".mem-status-pill").click()
        save_members(page)

        assert db_row(KNOWN_IDENT)["is_active"] == 0

        # Reload
        page.locator(".nav-item[data-tab='dashboard']").click()
        page.wait_for_timeout(300)
        open_members(page)
        cls = row_for(page, KNOWN_IDENT).locator(".mem-status-pill").get_attribute("class") or ""
        assert "inactive" in cls, "Active pill did not stay toggled after reload"

        # Restore
        row_for(page, KNOWN_IDENT).locator(".mem-status-pill").click()
        save_members(page)


# ══════════════════════════════════════════════════════════════════════════════
# Description  →  DB description column
# ══════════════════════════════════════════════════════════════════════════════

class TestDescriptionPersists:
    def test_edit_description_updates_db(self, page: Page):
        open_members(page)
        original = db_row(KNOWN_IDENT)["description"] or ""
        new_desc = f"Updated description {int(time.time()) % 10000}"

        row = row_for(page, KNOWN_IDENT)
        ta  = row.locator("textarea.agent-desc")
        ta.click(click_count=3)
        ta.fill(new_desc)
        save_members(page)

        after = db_row(KNOWN_IDENT)["description"]
        assert after == new_desc, f"Expected description={new_desc!r}, got {after!r}"

        # Restore
        ta.click(click_count=3)
        ta.fill(original)
        save_members(page)

    def test_description_shown_from_db_on_reload(self, page: Page):
        open_members(page)
        new_desc = f"PersistTest {int(time.time()) % 10000}"

        row_for(page, KNOWN_IDENT).locator("textarea.agent-desc").click(click_count=3)
        row_for(page, KNOWN_IDENT).locator("textarea.agent-desc").fill(new_desc)
        save_members(page)

        page.locator(".nav-item[data-tab='dashboard']").click()
        page.wait_for_timeout(300)
        open_members(page)

        displayed = row_for(page, KNOWN_IDENT).locator("textarea.agent-desc").input_value()
        assert displayed == new_desc, f"Description {new_desc!r} not shown after reload"

        # Restore
        row_for(page, KNOWN_IDENT).locator("textarea.agent-desc").click(click_count=3)
        row_for(page, KNOWN_IDENT).locator("textarea.agent-desc").fill(
            db_row(KNOWN_IDENT)["description"] or ""
        )
        save_members(page)


# ══════════════════════════════════════════════════════════════════════════════
# Delete member  →  row removed from DB
# ══════════════════════════════════════════════════════════════════════════════

class TestDeletePersists:
    # Use a throwaway member to avoid deleting real data
    TEMP_IDENT  = "pw_test_delete"
    TEMP_CW_ID  = 99901

    def _seed_temp(self):
        """Insert a throwaway technician directly into the DB."""
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "DELETE FROM technicians WHERE cw_identifier = ? OR cw_member_id = ?",
                (self.TEMP_IDENT, self.TEMP_CW_ID),
            )
            conn.execute(
                """INSERT INTO technicians
                   (cw_identifier, cw_member_id, name, routable, is_active,
                    total_tickets_handled, created_at, updated_at)
                   VALUES (?, ?, 'PW Temp Delete', 1, 1, 0,
                           datetime('now'), datetime('now'))""",
                (self.TEMP_IDENT, self.TEMP_CW_ID),
            )
            conn.commit()

    def _cleanup(self):
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "DELETE FROM technicians WHERE cw_identifier = ? OR cw_member_id = ?",
                (self.TEMP_IDENT, self.TEMP_CW_ID),
            )
            conn.commit()

    def test_delete_removes_row_from_db(self, page: Page):
        self._seed_temp()
        assert db_row(self.TEMP_IDENT) is not None, "Seed failed"

        open_members(page)
        page.wait_for_selector(
            f"#members-tbody tr:has(input.mem-name[value='{self.TEMP_IDENT}'])",
            timeout=5000,
        )

        row = row_for(page, self.TEMP_IDENT)
        page.on("dialog", lambda d: d.accept())
        row.locator("button.btn-danger").click()
        page.wait_for_function(
            f"document.querySelector('.toast') !== null",
            timeout=5000,
        )
        page.wait_for_timeout(500)

        assert db_row(self.TEMP_IDENT) is None, (
            "Row still exists in DB after delete"
        )

    def test_deleted_member_not_shown_after_reload(self, page: Page):
        self._seed_temp()
        open_members(page)
        page.wait_for_selector(
            f"#members-tbody tr:has(input.mem-name[value='{self.TEMP_IDENT}'])",
            timeout=5000,
        )

        page.on("dialog", lambda d: d.accept())
        row_for(page, self.TEMP_IDENT).locator("button.btn-danger").click()
        page.wait_for_timeout(600)

        # Reload tab
        page.locator(".nav-item[data-tab='dashboard']").click()
        page.wait_for_timeout(300)
        open_members(page)

        ui_idents = page.locator("#members-tbody input.mem-name").evaluate_all(
            "els => els.map(e => e.value)"
        )
        assert self.TEMP_IDENT not in ui_idents, (
            "Deleted member reappeared in UI after tab reload"
        )

    def teardown_method(self, method):
        self._cleanup()


# ══════════════════════════════════════════════════════════════════════════════
# Add new member  →  row created in DB
# ══════════════════════════════════════════════════════════════════════════════

class TestAddMemberPersists:
    NEW_IDENT  = "pw_test_new"
    NEW_CW_ID  = 99902
    NEW_NAME   = "PW New Member"

    def _cleanup(self):
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "DELETE FROM technicians WHERE cw_identifier = ? OR cw_member_id = ?",
                (self.NEW_IDENT, self.NEW_CW_ID),
            )
            conn.commit()

    def test_add_member_creates_db_row(self, page: Page):
        self._cleanup()
        open_members(page)

        page.locator("button", has_text="+ Add Member").click()
        page.wait_for_timeout(200)

        # Fill the new (last) row
        new_rows = page.locator("#members-tbody tr").all()
        last = new_rows[-1]
        last.locator("input.mem-name").fill(self.NEW_IDENT)
        last.locator("input.mem-fullname").fill(self.NEW_NAME)
        last.locator("input.mem-id").fill(str(self.NEW_CW_ID))
        last.locator("textarea.agent-desc").fill("Playwright test member")

        save_members(page)

        row = db_row(self.NEW_IDENT)
        assert row is not None, "New member not found in DB after save"
        assert row["name"] == self.NEW_NAME
        assert row["cw_member_id"] == self.NEW_CW_ID
        assert row["cw_identifier"] == self.NEW_IDENT

    def test_new_member_survives_reload(self, page: Page):
        self._cleanup()
        open_members(page)

        page.locator("button", has_text="+ Add Member").click()
        page.wait_for_timeout(200)
        last = page.locator("#members-tbody tr").all()[-1]
        last.locator("input.mem-name").fill(self.NEW_IDENT)
        last.locator("input.mem-fullname").fill(self.NEW_NAME)
        last.locator("input.mem-id").fill(str(self.NEW_CW_ID))
        save_members(page)

        page.locator(".nav-item[data-tab='dashboard']").click()
        page.wait_for_timeout(300)
        open_members(page)

        ui_idents = page.locator("#members-tbody input.mem-name").evaluate_all(
            "els => els.map(e => e.value)"
        )
        assert self.NEW_IDENT in ui_idents, (
            f"New member {self.NEW_IDENT!r} not shown after reload"
        )

    def teardown_method(self, method):
        self._cleanup()


# ══════════════════════════════════════════════════════════════════════════════
# Routable count badge updates in sync with DB
# ══════════════════════════════════════════════════════════════════════════════

class TestRoutableCountBadge:
    def test_badge_matches_db_routable_count(self, page: Page):
        open_members(page)
        page.wait_for_timeout(500)

        with sqlite3.connect(DB_PATH) as conn:
            db_count = conn.execute(
                "SELECT COUNT(*) FROM technicians WHERE routable = 1 AND cw_identifier IS NOT NULL"
            ).fetchone()[0]

        badge_text = page.locator("#routable-count").inner_text()
        ui_count   = int(badge_text.split()[0]) if badge_text.split() else -1
        assert ui_count == db_count, (
            f"Badge shows {ui_count} routable but DB has {db_count}"
        )
