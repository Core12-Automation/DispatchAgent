"""
scripts/migrate_to_db.py

Migration: reads mappings.json + routing_training.json and populates
the SQLite database (data/dispatcher.db).

  - technicians table  : seeded from mappings members + agent_routing
  - dispatch_decisions : seeded from routing_training.json examples
                         (flagged as training data so they don't pollute
                          production audit logs)

Usage:
    python scripts/migrate_to_db.py

Safe to re-run:
  - Technicians: matched by cw_member_id, updated not duplicated.
  - Training examples: only inserted once (checked via training_seed flag).
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

# ── Ensure project root is on sys.path ────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── Standalone logging (simple console output — this is a CLI script) ─────────
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-8s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


def _load_json(path: Path, label: str) -> dict:
    if not path.exists():
        log.warning("%s not found at %s — skipping", label, path)
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ── Step 1: Migrate technicians ───────────────────────────────────────────────

def migrate_technicians(mappings: dict, session) -> tuple[int, int]:
    from src.clients.database import Technician

    members: dict = mappings.get("members") or {}
    roster: dict = mappings.get("agent_routing") or {}
    all_identifiers: set[str] = set(members.keys()) | set(roster.keys())

    created = updated = 0

    for ident in sorted(all_identifiers):
        cw_member_id: int | None = None
        raw_id = members.get(ident)
        if raw_id is not None:
            try:
                cw_member_id = int(raw_id)
            except (TypeError, ValueError):
                pass

        roster_info: dict = roster.get(ident) or {}
        display_name: str = roster_info.get("display_name") or ident

        tech: Technician | None = None
        if cw_member_id is not None:
            tech = session.query(Technician).filter_by(cw_member_id=cw_member_id).first()
        if tech is None:
            tech = session.query(Technician).filter_by(name=display_name).first()

        if tech is None:
            tech = Technician(
                cw_member_id=cw_member_id,
                name=display_name,
                skills=[],
                specialties=[],
            )
            session.add(tech)
            action = "CREATE"
            created += 1
        else:
            if cw_member_id is not None and tech.cw_member_id is None:
                tech.cw_member_id = cw_member_id
            if tech.name != display_name:
                tech.name = display_name
            action = "UPDATE"
            updated += 1

        log.info("  [%s] %-20s  cw_member_id=%-6s  name=%r",
                 action, ident, cw_member_id, display_name)

    session.commit()
    return created, updated


# ── Step 2: Seed routing training examples ────────────────────────────────────

# Sentinel ticket_id range for training data (well below any real CW ticket IDs)
_TRAINING_TICKET_ID_START = -100000


def seed_training_examples(training: dict, session) -> tuple[int, int]:
    """
    Insert routing_training.json examples into dispatch_decisions.

    Each example is stored with:
      - ticket_id  : synthetic negative ID (unique per example)
      - ticket_summary : "<type> | <summary> | <company>"
      - assigned_tech_identifier : assigned_to
      - reason     : "Historical routing example (training data)"
      - confidence : 0.9
      - was_dry_run : False  (these were real decisions)
    """
    from src.clients.database import DispatchDecision, Technician

    examples: list[dict] = training.get("examples") or []
    if not examples:
        return 0, 0

    # Check how many training rows already exist (avoid re-seeding)
    existing_count = (
        session.query(DispatchDecision)
        .filter(DispatchDecision.ticket_id < 0)
        .count()
    )
    if existing_count >= len(examples):
        log.info("  [SKIP] %d training examples already in DB — skipping re-seed",
                 existing_count)
        return 0, existing_count

    # Build tech name -> DB id lookup
    tech_rows = session.query(Technician).all()
    tech_id_map: dict[str, int] = {t.name: t.id for t in tech_rows}

    inserted = 0
    skipped_existing = existing_count

    for i, ex in enumerate(examples):
        synthetic_id = _TRAINING_TICKET_ID_START - i
        assigned_to: str = ex.get("assigned_to") or ""
        ticket_type: str = ex.get("type") or ""
        summary: str = ex.get("summary") or ""
        company: str = ex.get("company") or ""

        exists = (
            session.query(DispatchDecision)
            .filter_by(ticket_id=synthetic_id)
            .first()
        )
        if exists:
            skipped_existing += 1
            continue

        full_summary = " | ".join(filter(None, [ticket_type, summary, company]))
        tech_db_id: int | None = tech_id_map.get(assigned_to)

        decision = DispatchDecision(
            ticket_id=synthetic_id,
            ticket_summary=full_summary[:500],
            assigned_tech_id=tech_db_id,
            assigned_tech_identifier=assigned_to,
            reason=(
                f"Historical routing example (training seed). "
                f"Type: {ticket_type}. Company: {company}."
            ),
            confidence=0.9,
            was_dry_run=False,
        )
        decision.alternatives_considered = []
        session.add(decision)
        inserted += 1

        if inserted % 100 == 0:
            session.flush()
            log.info("    ... %d examples inserted so far", inserted)

    session.commit()
    return inserted, skipped_existing


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("=" * 60)
    log.info("DispatchAgent -- Database Migration")
    log.info("=" * 60)

    # ── Load config to find mappings path ─────────────────────────────────────
    config_path = PROJECT_ROOT / "data" / "portal_config.json"
    config = _load_json(config_path, "portal_config.json")
    mappings_rel = config.get("mappings_path", "data/mappings.json")
    mappings_path = (
        Path(mappings_rel) if Path(mappings_rel).is_absolute()
        else PROJECT_ROOT / mappings_rel
    )

    # ── Load source files ─────────────────────────────────────────────────────
    mappings = _load_json(mappings_path, "mappings.json")
    training = _load_json(
        PROJECT_ROOT / "data" / "routing_training.json",
        "routing_training.json",
    )

    if not mappings:
        log.error("No mappings data available — nothing to migrate.")
        log.error("Copy data/mappings.json from the original project first.")
        sys.exit(0)

    # ── Init DB ───────────────────────────────────────────────────────────────
    from src.clients.database import SessionLocal, init_db

    log.info("Initialising database tables ...")
    init_db()
    log.info("  [OK] Tables ready")

    # ── Migrate technicians ───────────────────────────────────────────────────
    log.info("[1/2] Migrating technicians ...")
    with SessionLocal() as session:
        created, updated = migrate_technicians(mappings, session)

    log.info("  Technicians created : %d", created)
    log.info("  Technicians updated : %d", updated)

    # ── Seed training examples ────────────────────────────────────────────────
    meta = training.get("_meta") or {}
    example_count = meta.get("examples", len(training.get("examples") or []))
    log.info("[2/2] Seeding routing training examples (%d total) ...", example_count)
    with SessionLocal() as session:
        inserted, skipped = seed_training_examples(training, session)

    log.info("  Training examples inserted : %d", inserted)
    log.info("  Training examples skipped  : %d (already present)", skipped)

    # ── Summary ───────────────────────────────────────────────────────────────
    log.info("-" * 60)
    log.info("Migration complete.")
    log.info("  Database : %s", PROJECT_ROOT / "data" / "dispatcher.db")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
