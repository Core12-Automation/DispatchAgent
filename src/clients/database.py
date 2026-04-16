"""
src/clients/database.py

SQLAlchemy ORM setup and table definitions for the DispatchAgent.

Database: SQLite at data/dispatcher.db  (created automatically on first use)

Tables:
    technicians         — per-tech profile data, skills, and performance stats
    dispatch_decisions  — one row per ticket routed, with reasoning and audit trail
    dispatch_runs       — one row per routing run (manual or scheduled)
    active_incidents    — repeat/storm fingerprint tracking
    operator_notes      — human-authored dispatch instructions
    agent_memory        — key-value working state across cycles
    agent_traces        — full reasoning trace per ticket
    support_types       — ConnectWise ticket type name → CW integer ID

Usage:
    from src.clients.database import SessionLocal, init_db

    # Create tables (call once at app startup):
    init_db()

    # Use a session:
    with SessionLocal() as session:
        session.add(...)
        session.commit()
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)


# ── Database location ─────────────────────────────────────────────────────────

def _db_path() -> str:
    """Resolve the SQLite file path, respecting DATABASE_URL env override."""
    env_url = os.getenv("DATABASE_URL", "").strip()
    if env_url:
        return env_url
    # Default: data/dispatcher.db relative to the project root (two levels up
    # from this file: src/clients/ → src/ → project root)
    project_root = Path(__file__).resolve().parent.parent.parent
    db_file = project_root / "data" / "dispatcher.db"
    db_file.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{db_file}"


_engine = create_engine(
    _db_path(),
    connect_args={"check_same_thread": False},  # needed for SQLite + multi-thread Flask
    echo=False,
)

SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)


# ── Base ──────────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


# ── Helper: JSON column stored as TEXT ────────────────────────────────────────
# SQLite has no native JSON type; we store as TEXT and marshal on Python side.

class _JsonText(Text):
    """Marker subclass — values are serialised/deserialised as JSON."""


def _to_json(value: Any) -> Optional[str]:
    return json.dumps(value) if value is not None else None


def _from_json(raw: Optional[str]) -> Any:
    return json.loads(raw) if raw else None


# ── Tables ────────────────────────────────────────────────────────────────────

class Technician(Base):
    """
    Per-technician profile.  Populated manually or by update_tech_profile tool.
    cw_member_id links back to the ConnectWise member record.
    """

    __tablename__ = "technicians"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cw_member_id: Mapped[Optional[int]] = mapped_column(Integer, unique=True, nullable=True)
    # CW login identifier, e.g. "jsmith" — used as the primary lookup key from the UI
    cw_identifier: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    # Graph user ID used for Teams presence lookups
    teams_user_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    # Dispatch routing fields
    routable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default="1")
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # JSON arrays stored as TEXT, e.g. ["networking", "azure_ad"]
    _skills: Mapped[Optional[str]] = mapped_column("skills", Text, nullable=True)
    _specialties: Mapped[Optional[str]] = mapped_column("specialties", Text, nullable=True)

    # Aggregated performance metrics (updated by update_tech_profile)
    avg_resolution_minutes: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_tickets_handled: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationship back-reference
    dispatch_decisions: Mapped[List["DispatchDecision"]] = relationship(
        "DispatchDecision", back_populates="technician", lazy="dynamic"
    )

    @property
    def skills(self) -> List[str]:
        return _from_json(self._skills) or []

    @skills.setter
    def skills(self, value: List[str]) -> None:
        self._skills = _to_json(value)

    @property
    def specialties(self) -> List[str]:
        return _from_json(self._specialties) or []

    @specialties.setter
    def specialties(self, value: List[str]) -> None:
        self._specialties = _to_json(value)

    def __repr__(self) -> str:
        return f"<Technician id={self.id} name={self.name!r} cw_member_id={self.cw_member_id}>"


class DispatchDecision(Base):
    """
    One row per ticket routing decision — the memory layer for the agent.
    Used by get_similar_past_tickets and for auditing.
    """

    __tablename__ = "dispatch_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    ticket_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # FK to technicians.id (nullable — might not have a local profile yet)
    assigned_tech_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("technicians.id"), nullable=True
    )
    # Human-readable identifier used by the router (e.g. "jsmith")
    assigned_tech_identifier: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 0.0–1.0 expressed confidence, if the model provides it
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # JSON list of {identifier, reason} dicts considered but not chosen
    _alternatives: Mapped[Optional[str]] = mapped_column("alternatives_considered", Text, nullable=True)

    was_dry_run: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    # Relationship
    technician: Mapped[Optional["Technician"]] = relationship(
        "Technician", back_populates="dispatch_decisions"
    )
    run_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("dispatch_runs.id"), nullable=True
    )
    run: Mapped[Optional["DispatchRun"]] = relationship(
        "DispatchRun", back_populates="decisions"
    )

    @property
    def alternatives_considered(self) -> List[Dict[str, Any]]:
        return _from_json(self._alternatives) or []

    @alternatives_considered.setter
    def alternatives_considered(self, value: List[Dict[str, Any]]) -> None:
        self._alternatives = _to_json(value)

    def __repr__(self) -> str:
        return (
            f"<DispatchDecision id={self.id} ticket_id={self.ticket_id} "
            f"tech={self.assigned_tech_identifier!r} dry_run={self.was_dry_run}>"
        )


class DispatchRun(Base):
    """
    One row per routing run — tracks aggregate stats for the history tab
    and scheduled vs manual triggers.
    """

    __tablename__ = "dispatch_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    tickets_processed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tickets_assigned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tickets_flagged: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    errors: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # "manual" | "scheduled"
    trigger: Mapped[str] = mapped_column(String(20), default="manual", nullable=False)

    decisions: Mapped[List["DispatchDecision"]] = relationship(
        "DispatchDecision", back_populates="run", lazy="dynamic"
    )

    def __repr__(self) -> str:
        return (
            f"<DispatchRun id={self.id} trigger={self.trigger!r} "
            f"assigned={self.tickets_assigned} errors={self.errors}>"
        )


class ActiveIncident(Base):
    """
    Tracks groups of tickets that share the same alert fingerprint.
    Powers repeat/storm detection and suppression.
    """

    __tablename__ = "active_incidents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Normalised SHA256 fingerprint that identifies this class of alert
    incident_key: Mapped[str] = mapped_column(String(16), unique=True, nullable=False, index=True)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    # JSON array of CW ticket IDs grouped under this incident
    _ticket_ids: Mapped[Optional[str]] = mapped_column("ticket_ids", Text, nullable=True)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # "new" | "monitoring" | "assigned" | "suppressed" | "resolved"
    status: Mapped[str] = mapped_column(String(20), default="new", nullable=False)
    assigned_tech_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    suppressed_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    suppressed_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    @property
    def ticket_ids(self) -> List[int]:
        return _from_json(self._ticket_ids) or []

    @ticket_ids.setter
    def ticket_ids(self, value: List[int]) -> None:
        self._ticket_ids = _to_json(value)

    def __repr__(self) -> str:
        return (
            f"<ActiveIncident id={self.id} key={self.incident_key!r} "
            f"status={self.status!r} count={self.occurrence_count}>"
        )


class OperatorNote(Base):
    """
    Human-authored instructions that modify dispatch behaviour.
    Loaded into every Claude prompt via the situation briefing.
    """

    __tablename__ = "operator_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    note_text: Mapped[str] = mapped_column(Text, nullable=False)
    # "global" | "client" | "tech" | "incident"
    scope: Mapped[str] = mapped_column(String(20), default="global", nullable=False)
    # e.g. client company name, tech identifier, incident key
    scope_ref: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    created_by: Mapped[str] = mapped_column(String(100), default="operator", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    # NULL = permanent (until manually deleted)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default="1")
    # JSON array of tag strings
    _tags: Mapped[Optional[str]] = mapped_column("tags", Text, nullable=True)

    @property
    def tags(self) -> List[str]:
        return _from_json(self._tags) or []

    @tags.setter
    def tags(self, value: List[str]) -> None:
        self._tags = _to_json(value)

    def __repr__(self) -> str:
        return (
            f"<OperatorNote id={self.id} scope={self.scope!r} "
            f"scope_ref={self.scope_ref!r} active={self.is_active}>"
        )


class AgentMemory(Base):
    """
    Key-value working memory for the dispatch agent.
    Persists state between dispatch cycles.
    """

    __tablename__ = "agent_memory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # e.g. "incident:abc123:last_action"
    key: Mapped[str] = mapped_column(String(500), unique=True, nullable=False, index=True)
    # JSON-encoded value
    value: Mapped[str] = mapped_column(Text, nullable=False)
    # "incident" | "suppression" | "pattern" | "cycle_state"
    category: Mapped[str] = mapped_column(String(40), default="cycle_state", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:
        return f"<AgentMemory id={self.id} key={self.key!r} category={self.category!r}>"


class AgentTrace(Base):
    """
    Full reasoning trace for a single ticket dispatch run.
    Stored as a JSON list of events (text turns, tool calls, done marker).
    """

    __tablename__ = "agent_traces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    ticket_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="ok", nullable=False)
    iterations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    elapsed_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    was_dry_run: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    _trace: Mapped[Optional[str]] = mapped_column("trace_json", Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    @property
    def trace(self) -> List[Dict[str, Any]]:
        return _from_json(self._trace) or []

    @trace.setter
    def trace(self, value: List[Dict[str, Any]]) -> None:
        self._trace = _to_json(value)

    def __repr__(self) -> str:
        return (
            f"<AgentTrace id={self.id} ticket_id={self.ticket_id} "
            f"status={self.status!r} iterations={self.iterations}>"
        )


# ── SupportType ───────────────────────────────────────────────────────────────

class SupportType(Base):
    """
    ConnectWise ticket type name → CW integer ID.
    Primary store for all ticket types; replaces the 'support types' section
    of mappings.json.  Resolver checks this table first before falling back
    to the JSON file.
    """

    __tablename__ = "support_types"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Human-readable CW type name, e.g. "Network", "Azure AD", "Hardware - Server"
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False, index=True)
    # ConnectWise type integer ID
    cw_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<SupportType name={self.name!r} cw_id={self.cw_id}>"


# ── Support-type DB helpers ────────────────────────────────────────────────────

# The full type catalogue — seeded into the DB on init_db().
SUPPORT_TYPES: Dict[str, int] = {
    "Active Directory": 1101,
    "Administrative": 1102,
    "Adobe Acrobat": 1179,
    "Alert": 1103,
    "AnyConnect VPN": 1180,
    "Application": 1104,
    "AutoCAD": 1181,
    "Axcient/Replibit Backup": 1182,
    "Azure AD": 1105,
    "Barracuda": 1183,
    "Barracuda Quarantine": 1106,
    "Break-fix": 1107,
    "CHANGE THIS": 1108,
    "Child": 1109,
    "Computer Workstation": 1110,
    "Copier": 1111,
    "Dark Web ID": 1184,
    "Datto Workplace": 1185,
    "Domain Registration": 1186,
    "Email": 1112,
    "Email (Microsoft 365)": 1113,
    "Email (Other)": 1114,
    "Entra Connect Sync (Azure AD Sync)": 1187,
    "Fax": 1115,
    "Hardware - Android Phone": 1116,
    "Hardware - Conference": 1117,
    "Hardware - Other": 1120,
    "Hardware - Other Tablet": 1121,
    "Hardware - Peripheral": 1122,
    "Hardware - Server": 1123,
    "Hardware - UPS": 1189,
    "Hardware - Workstation": 1124,
    "Hardware - iPad": 1118,
    "Hardware - iPhone": 1119,
    "Hosted": 1125,
    "Huntress": 1126,
    "In Shop Network Device": 1127,
    "In Shop Project Planning": 1128,
    "In Shop Server": 1129,
    "In Shop Workstation": 1130,
    "In-Shop": 1132,
    "Information Request": 1131,
    "KnowBe4": 1190,
    "MOVE TICKET FROM DISPATCH": 1142,
    "Managed Services": 1133,
    "Meraki": 1134,
    "Microsoft 365": 1135,
    "Mobile Application - Android": 1136,
    "Mobile Application - Other": 1139,
    "Mobile Application - iPad": 1137,
    "Mobile Application - iPhone": 1138,
    "Mobile OS - Android": 1140,
    "Mobile OS - iOS": 1141,
    "NetXtender VPN": 1191,
    "Network": 1143,
    "OS - Linux Server": 1151,
    "OS - Linux Workstation": 1152,
    "OS - Other Server": 1154,
    "OS - Other Workstation": 1155,
    "OS - Windows 10": 1156,
    "OS - Windows 11": 1157,
    "OS - Windows Server": 1158,
    "OS - macOS Workstation": 1153,
    "On-Site": 1144,
    "Onsite Mobile Phone": 1145,
    "Onsite Network Device": 1146,
    "Onsite Project Planning": 1147,
    "Onsite Server": 1148,
    "Onsite Traning": 1149,
    "Onsite Workstation": 1150,
    "Parent": 1159,
    "Peripherals": 1160,
    "Phone": 1161,
    "Printer": 1162,
    "Procurement": 1197,
    "Project": 1163,
    "Reactive": 1164,
    "Remote": 1165,
    "Remote Access": 1192,
    "Remote Desktop Connection": 1193,
    "Remote Mobile Phone": 1166,
    "Remote Network Device": 1167,
    "Remote Project Planning": 1168,
    "Remote Server": 1169,
    "Remote Training": 1170,
    "Remote Workstation": 1171,
    "Scanner": 1172,
    "Server": 1173,
    "Site Visit": 1174,
    "Software": 1175,
    "Trimble": 1194,
    "USM Site Visit": 1176,
    "VPN": 1177,
    "Windows Login": 1178,
    "Windows VPN": 1195,
    "ZoomInfo": 1196,
    "gINT": 1188,
    "Autodesk": 1198,
    "Revit": 1199,
    "Disc Space": 1200,
    "Website": 1201,
    "Pax8": 1202,
    "Offboarding/Onboarding": 1203,
    "Wireless/Wifi": 1204,
    "Ninja": 1205,
    "Antivirus": 1206,
    "Uptime Robot": 1207,
    "SSL": 1208,
    "Fortinet": 1209,
    "Sentinel One": 1210,
    "Automation": 1211,
    "Dev Work/Scripting": 1212,
    "Bluebeam": 1213,
    "IP Address": 1214,
    "Speakers/Audio": 1215,
    "Display/Video": 1216,
    "Quickbooks": 1217,
}


def seed_support_types(session=None) -> int:
    """
    Upsert all entries from SUPPORT_TYPES into the support_types table.
    Creates a temporary session if one is not provided.
    Returns the number of rows inserted or updated.
    """
    _own_session = session is None
    if _own_session:
        session = SessionLocal()
    try:
        count = 0
        for name, cw_id in SUPPORT_TYPES.items():
            row = session.query(SupportType).filter_by(name=name).first()
            if row is None:
                session.add(SupportType(name=name, cw_id=cw_id))
                count += 1
            elif row.cw_id != cw_id:
                row.cw_id = cw_id
                row.updated_at = datetime.now(timezone.utc)
                count += 1
        session.commit()
        return count
    finally:
        if _own_session:
            session.close()


def lookup_support_type(name: str) -> Optional[int]:
    """
    Look up a CW type ID by name from the database.
    Case-insensitive.  Returns None if not found.

    This is the primary type-resolution path; resolver.py falls back to
    mappings.json only when this returns None.
    """
    if not name:
        return None
    session = SessionLocal()
    try:
        # Exact match first
        row = session.query(SupportType).filter(
            SupportType.name == name
        ).first()
        if row is not None:
            return row.cw_id
        # Case-insensitive fallback
        name_lower = name.strip().lower()
        for st in session.query(SupportType).all():
            if st.name.lower() == name_lower:
                return st.cw_id
        return None
    finally:
        session.close()


def get_all_support_types(session=None) -> List[Dict[str, Any]]:
    """Return all support types as a list of {name, cw_id} dicts, sorted by name."""
    _own_session = session is None
    if _own_session:
        session = SessionLocal()
    try:
        rows = session.query(SupportType).order_by(SupportType.name).all()
        return [{"name": r.name, "cw_id": r.cw_id} for r in rows]
    finally:
        if _own_session:
            session.close()


# ── Initialiser ───────────────────────────────────────────────────────────────

def migrate_db() -> None:
    """
    Add columns introduced after the initial schema creation.
    Uses PRAGMA table_info to skip columns that already exist (idempotent).
    """
    from sqlalchemy import text

    technician_columns = [
        ("cw_identifier", "TEXT"),
        ("routable",      "INTEGER DEFAULT 1 NOT NULL"),
        ("description",   "TEXT"),
    ]

    with _engine.connect() as conn:
        rows = conn.execute(text("PRAGMA table_info(technicians)")).fetchall()
        existing = {row[1] for row in rows}
        for col_name, col_def in technician_columns:
            if col_name not in existing:
                conn.execute(text(f"ALTER TABLE technicians ADD COLUMN {col_name} {col_def}"))
        conn.commit()


def init_db() -> None:
    """Create all tables and seed reference data. Safe to call multiple times."""
    Base.metadata.create_all(bind=_engine)
    migrate_db()
    seed_support_types()
