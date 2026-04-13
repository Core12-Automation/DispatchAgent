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
import sys
from pathlib import Path

# ── Ensure project root is on sys.path ────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _load_json(path: Path, label: str) -> dict:
    if not path.exists():
        print(f"  [WARN] {label} not found at {path} -- skipping")
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

        print(f"  [{action}] {ident!r:20s}  cw_member_id={cw_member_id}  name={display_name!r}")

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
      - notes field in reason marks them as training seeds
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
        print(f"  [SKIP] {existing_count} training examples already in DB -- skipping re-seed")
        return 0, existing_count

    # Build tech name -> DB id lookup
    tech_rows = session.query(Technician).all()
    tech_id_map: dict[str, int] = {t.name: t.id for t in tech_rows}
    # Also index by identifier (lower-cased name-prefix heuristic handled below)

    inserted = 0
    skipped_existing = existing_count

    for i, ex in enumerate(examples):
        synthetic_id = _TRAINING_TICKET_ID_START - i
        assigned_to: str = ex.get("assigned_to") or ""
        ticket_type: str = ex.get("type") or ""
        summary: str = ex.get("summary") or ""
        company: str = ex.get("company") or ""

        # Check if this synthetic ID already exists
        exists = (
            session.query(DispatchDecision)
            .filter_by(ticket_id=synthetic_id)
            .first()
        )
        if exists:
            skipped_existing += 1
            continue

        # Build a rich summary for keyword search
        full_summary = " | ".join(filter(None, [ticket_type, summary, company]))

        # Try to find the tech's DB id
        tech_db_id: int | None = tech_id_map.get(assigned_to)

        decision = DispatchDecision(
            ticket_id=synthetic_id,
            ticket_summary=full_summary[:500],
            assigned_tech_id=tech_db_id,
            assigned_tech_identifier=assigned_to,
            reason=f"Historical routing example (training seed). Type: {ticket_type}. Company: {company}.",
            confidence=0.9,
            was_dry_run=False,
        )
        decision.alternatives_considered = []
        session.add(decision)
        inserted += 1

        if inserted % 100 == 0:
            session.flush()
            print(f"    ... {inserted} examples inserted so far")

    session.commit()
    return inserted, skipped_existing


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 60)
    print("DispatchAgent -- Database Migration")
    print("=" * 60)

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
    training = _load_json(PROJECT_ROOT / "data" / "routing_training.json", "routing_training.json")

    if not mappings:
        print("\nNo mappings data available. Nothing to migrate.")
        print("Copy data/mappings.json from the original project first.")
        sys.exit(0)

    # ── Init DB ───────────────────────────────────────────────────────────────
    from src.clients.database import SessionLocal, init_db

    print("\nInitialising database tables ...")
    init_db()
    print("  [OK] Tables ready")

    # ── Migrate technicians ───────────────────────────────────────────────────
    print("\n[1/2] Migrating technicians ...")
    with SessionLocal() as session:
        created, updated = migrate_technicians(mappings, session)

    print(f"\n  Technicians created : {created}")
    print(f"  Technicians updated : {updated}")

    # ── Seed training examples ────────────────────────────────────────────────
    meta = training.get("_meta") or {}
    example_count = meta.get("examples", len(training.get("examples") or []))
    print(f"\n[2/2] Seeding routing training examples ({example_count} total) ...")
    with SessionLocal() as session:
        inserted, skipped = seed_training_examples(training, session)

    print(f"\n  Training examples inserted : {inserted}")
    print(f"  Training examples skipped  : {skipped} (already present)")

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "-" * 60)
    print("Migration complete.")
    print(f"  Database : {PROJECT_ROOT / 'data' / 'dispatcher.db'}")
    print("=" * 60)


if __name__ == "__main__":
    main()
