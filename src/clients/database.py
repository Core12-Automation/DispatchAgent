"""
src/clients/database.py

SQLAlchemy ORM setup and table definitions for the DispatchAgent.

Database: SQLite at data/dispatcher.db  (created automatically on first use)

Tables:
    technicians         — per-tech profile data, skills, and performance stats
    dispatch_decisions  — one row per ticket routed, with reasoning and audit trail
    dispatch_runs       — one row per routing run (manual or scheduled)

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
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    # Graph user ID used for Teams presence lookups
    teams_user_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    # JSON arrays stored as TEXT, e.g. ["networking", "azure_ad"]
    _skills: Mapped[Optional[str]] = mapped_column("skills", Text, nullable=True)
    _specialties: Mapped[Optional[str]] = mapped_column("specialties", Text, nullable=True)

    # Aggregated performance metrics (updated by update_tech_profile)
    avg_resolution_minutes: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_tickets_handled: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

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


# ── Initialiser ───────────────────────────────────────────────────────────────

def init_db() -> None:
    """Create all tables that don't yet exist. Safe to call multiple times."""
    Base.metadata.create_all(bind=_engine)
