"""
tests/conftest.py

Shared pytest fixtures for the DispatchAgent test suite.

Fixtures
--------
mock_ticket          Minimal CW ticket dict (no owner)
mock_ticket_full     Full ticket with all nested fields
mock_ticket_critical Critical-priority incident ticket
mock_mappings        Mappings dict: boards / members / statuses / agent_routing
mock_config          Portal config dict matching portal_config.json structure
mock_cw_client       Mock CWManageClient with pre-wired return values
in_memory_db         In-memory SQLite: patches db_module._engine + SessionLocal
db_session           Live SQLAlchemy session on the in-memory DB
flask_app            Flask test app (in-memory DB + mocked dispatcher)
client               Flask test client
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ── Ticket fixtures ───────────────────────────────────────────────────────────

@pytest.fixture
def mock_ticket():
    """Minimal CW ticket — same nested-dict structure as the REST API returns."""
    return {
        "id": 12345,
        "summary": "Cannot connect to VPN after Windows update",
        "board": {"id": 10, "name": "Support"},
        "status": {"id": 1, "name": "New"},
        "priority": {"id": 3, "name": "High", "sort": 2},
        "company": {"id": 100, "name": "Acme Corp"},
        "contact": {"id": 50, "name": "Jane Doe"},
        "owner": None,
        "type": {"id": 5, "name": "Service Request"},
        "dateEntered": "2024-01-15T09:30:00Z",
        "closedFlag": False,
        "initialDescription": (
            "User is unable to connect to VPN after the latest "
            "Windows update was applied overnight."
        ),
    }


@pytest.fixture
def mock_ticket_full(mock_ticket):
    """Full CW ticket with owner, location, and _info metadata populated."""
    full = dict(mock_ticket)
    full.update(
        {
            "owner": {"id": 200, "identifier": "akloss", "name": "Alex Kloss"},
            "location": {"id": 1, "name": "Main"},
            "department": {"id": 2, "name": "IT"},
            "source": {"id": 3, "name": "Email"},
            "serviceLocation": {"id": 1, "name": "Remote"},
            "customFields": [],
            "_info": {
                "dateEntered": "2024-01-15T09:30:00Z",
                "lastUpdated": "2024-01-15T09:31:00Z",
                "notes_href": "/service/tickets/12345/notes",
                "activities_href": "/service/tickets/12345/activities",
            },
        }
    )
    return full


@pytest.fixture
def mock_ticket_critical():
    """High-urgency incident ticket for priority-dispatch tests."""
    return {
        "id": 99999,
        "summary": "CRITICAL: Production server completely down",
        "board": {"id": 10, "name": "Support"},
        "status": {"id": 1, "name": "New"},
        "priority": {"id": 1, "name": "Critical", "sort": 0},
        "company": {"id": 101, "name": "BigBank Inc"},
        "contact": None,
        "owner": None,
        "type": {"id": 1, "name": "Incident"},
        "dateEntered": "2024-01-15T08:00:00Z",
        "closedFlag": False,
        "initialDescription": "All production systems are offline. Revenue impact.",
    }


# ── Config / mapping fixtures ─────────────────────────────────────────────────

@pytest.fixture
def mock_mappings():
    """Realistic mappings dict matching data/mappings.json structure."""
    return {
        "boards": {
            "Support": 10,
            "Projects": 20,
        },
        "members": {
            "akloss": 200,
            "jsmith": 201,
            "mwilson": 202,
        },
        "Support statuses": {
            "New": 1,
            "Assigned": 2,
            "In Progress": 3,
            "Closed": 4,
        },
        "agent_routing": {
            "akloss": {
                "display_name": "Alex Kloss",
                "description": "Senior network engineer — VPN and firewalls",
                "routable": True,
                "skills": ["networking", "vpn", "firewall"],
                "specialties": ["SonicWall", "Cisco"],
            },
            "jsmith": {
                "display_name": "John Smith",
                "description": "Server and virtualization specialist",
                "routable": True,
                "skills": ["vmware", "windows_server", "azure"],
                "specialties": ["VMware", "Hyper-V"],
            },
            "mwilson": {
                "display_name": "Mike Wilson",
                "description": "Endpoint support and Microsoft 365 admin",
                "routable": True,
                "skills": ["m365", "endpoint", "intune"],
                "specialties": ["Microsoft 365", "Intune"],
            },
        },
        "unrouted_owner_identifiers": ["DispatchBot", "AutoAssign"],
    }


@pytest.fixture
def mock_config():
    """Portal config dict matching data/portal_config.json structure."""
    return {
        "dry_run": True,
        "boards_to_scan": ["Support"],
        "route_from_statuses": ["New", "New (Email connector)"],
        "unrouted_owner_identifiers": ["DispatchBot", "AutoAssign"],
        "claude_model": "claude-sonnet-4-6",
        "max_tech_workload": 5,
        "dispatch_interval_seconds": 60,
        "mappings_path": "data/mappings.json",
    }


# ── CW client mock ────────────────────────────────────────────────────────────

@pytest.fixture
def mock_cw_client(mock_ticket_full):
    """Mock CWManageClient with realistic return values."""
    client = MagicMock()
    client.dry_run = True

    client.get_ticket.return_value = mock_ticket_full
    client.fetch_all_tickets.return_value = []
    client.get_ticket_notes.return_value = []
    client.get_audit_trail.return_value = []
    client.get.return_value = []

    client.patch_fields.return_value = {
        "ok": True,
        "dry_run": True,
        "ticket_id": 12345,
        "ops": [],
    }
    client.add_ticket_note.return_value = {"id": 999, "text": "note added"}

    return client


# ── Anthropic mock helpers (used by test modules directly) ────────────────────

def make_text_block(text: str):
    """Create a mock text content block."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def make_tool_use_block(tool_name: str, tool_input: dict, block_id: str = "tu_001"):
    """Create a mock tool_use content block."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = tool_name
    block.id = block_id
    block.input = tool_input
    return block


def make_api_response(content_blocks: list, stop_reason: str = "end_turn"):
    """Create a mock Anthropic API response object."""
    resp = MagicMock()
    resp.content = content_blocks
    resp.stop_reason = stop_reason
    return resp


# ── In-memory database ────────────────────────────────────────────────────────

@pytest.fixture
def in_memory_db():
    """
    Replace the real SQLite dispatcher.db with an in-memory database.

    Patches db_module._engine and db_module.SessionLocal so that all code
    importing from src.clients.database uses the in-memory engine for the
    duration of the test.

    Yields the patched SessionLocal callable.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )

    from src.clients import database as db_module
    from src.clients.database import Base

    original_engine = db_module._engine
    original_session = db_module.SessionLocal

    db_module._engine = engine
    db_module.SessionLocal = sessionmaker(
        bind=engine, autoflush=False, autocommit=False
    )

    # Create all tables on the in-memory engine
    Base.metadata.create_all(bind=engine)

    yield db_module.SessionLocal

    # Restore originals
    db_module._engine = original_engine
    db_module.SessionLocal = original_session


@pytest.fixture
def db_session(in_memory_db):
    """Open SQLAlchemy session on the in-memory database."""
    with in_memory_db() as session:
        yield session


# ── Flask test app ────────────────────────────────────────────────────────────

@pytest.fixture
def flask_app(in_memory_db):
    """
    Flask test application with:
      - In-memory SQLite via in_memory_db
      - Background dispatcher mocked out (no APScheduler)
      - Minimum env vars set so CW config validation doesn't crash
    """
    import os
    from unittest.mock import patch

    os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-real")
    os.environ.setdefault("CWM_COMPANY_ID", "testco")
    os.environ.setdefault("CWM_PUBLIC_KEY", "test-pub")
    os.environ.setdefault("CWM_PRIVATE_KEY", "test-priv")
    os.environ.setdefault("CLIENT_ID", "test-client-id")
    os.environ.setdefault("CWM_SITE", "https://test.example.com/v4_6_release/apis/3.0")

    mock_dispatcher = MagicMock()
    mock_dispatcher.get_status.return_value = {
        "running": True,
        "paused": False,
        "last_run": "2024-01-15 09:00:00 UTC",
        "next_run": None,
    }
    mock_dispatcher.start.return_value = None
    mock_dispatcher.toggle_pause.return_value = False
    mock_dispatcher.run_once.return_value = None

    with patch("services.dispatcher.get_dispatcher", return_value=mock_dispatcher):
        from app import create_app

        app = create_app()
        app.config["TESTING"] = True

        yield app


@pytest.fixture
def client(flask_app):
    """Flask test client."""
    with flask_app.test_client() as c:
        yield c
