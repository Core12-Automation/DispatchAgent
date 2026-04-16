"""
app/routes/notes.py

Blueprint: /api/notes/*

Operator Notes CRUD + the natural-language "chat" endpoint that lets a
dispatcher brief Claude in plain English and automatically creates a
structured OperatorNote from the parsed intent.

Endpoints:
    GET    /api/notes               — list active notes
    POST   /api/notes               — create a note (structured)
    PUT    /api/notes/<id>          — update a note
    DELETE /api/notes/<id>          — soft-delete (set is_active=false)
    POST   /api/notes/chat          — parse natural language → create note
    GET    /api/notes/briefing      — return current situation briefing
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, request

log = logging.getLogger(__name__)

bp = Blueprint("notes", __name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _note_to_dict(note) -> Dict[str, Any]:
    return {
        "id":         note.id,
        "note_text":  note.note_text,
        "scope":      note.scope,
        "scope_ref":  note.scope_ref,
        "created_by": note.created_by,
        "created_at": note.created_at.isoformat() if note.created_at else None,
        "expires_at": note.expires_at.isoformat() if note.expires_at else None,
        "is_active":  note.is_active,
        "tags":       note.tags,
    }


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


# ── LIST ──────────────────────────────────────────────────────────────────────

@bp.route("/api/notes", methods=["GET"])
def list_notes():
    """
    GET /api/notes?include_expired=true

    Returns notes ordered by scope then created_at desc.
    By default only active + non-expired notes are shown.
    """
    from src.clients.database import SessionLocal, OperatorNote
    from sqlalchemy import or_

    include_expired = request.args.get("include_expired", "false").lower() == "true"

    with SessionLocal() as session:
        q = session.query(OperatorNote).filter(OperatorNote.is_active.is_(True))
        if not include_expired:
            now = datetime.now(timezone.utc)
            q = q.filter(
                or_(
                    OperatorNote.expires_at.is_(None),
                    OperatorNote.expires_at > now,
                )
            )
        notes = (
            q.order_by(OperatorNote.scope, OperatorNote.created_at.desc())
            .all()
        )
        return jsonify([_note_to_dict(n) for n in notes])


# ── CREATE ────────────────────────────────────────────────────────────────────

@bp.route("/api/notes", methods=["POST"])
def create_note():
    """
    POST /api/notes
    Body: {note_text, scope, scope_ref?, expires_at?, tags?}
    """
    from src.clients.database import SessionLocal, OperatorNote

    body = request.get_json(silent=True) or {}
    note_text = (body.get("note_text") or "").strip()
    if not note_text:
        return jsonify({"error": "note_text is required"}), 400

    scope = body.get("scope", "global")
    if scope not in ("global", "client", "tech", "incident"):
        return jsonify({"error": "scope must be global|client|tech|incident"}), 400

    note = OperatorNote(
        note_text=note_text,
        scope=scope,
        scope_ref=body.get("scope_ref") or None,
        created_by=body.get("created_by", "operator"),
        expires_at=_parse_iso(body.get("expires_at")),
    )
    note.tags = body.get("tags") or []

    with SessionLocal() as session:
        session.add(note)
        session.commit()
        session.refresh(note)
        return jsonify(_note_to_dict(note)), 201


# ── UPDATE ────────────────────────────────────────────────────────────────────

@bp.route("/api/notes/<int:note_id>", methods=["PUT"])
def update_note(note_id: int):
    """
    PUT /api/notes/<id>
    Body: any subset of {note_text, scope, scope_ref, expires_at, tags, is_active}
    """
    from src.clients.database import SessionLocal, OperatorNote

    body = request.get_json(silent=True) or {}
    with SessionLocal() as session:
        note = session.get(OperatorNote, note_id)
        if note is None:
            return jsonify({"error": "Not found"}), 404

        if "note_text" in body:
            note.note_text = body["note_text"]
        if "scope" in body:
            note.scope = body["scope"]
        if "scope_ref" in body:
            note.scope_ref = body["scope_ref"] or None
        if "expires_at" in body:
            note.expires_at = _parse_iso(body["expires_at"])
        if "tags" in body:
            note.tags = body["tags"] or []
        if "is_active" in body:
            note.is_active = bool(body["is_active"])

        session.commit()
        session.refresh(note)
        return jsonify(_note_to_dict(note))


# ── DELETE (soft) ─────────────────────────────────────────────────────────────

@bp.route("/api/notes/<int:note_id>", methods=["DELETE"])
def delete_note(note_id: int):
    """DELETE /api/notes/<id>  — soft delete (is_active=false)"""
    from src.clients.database import SessionLocal, OperatorNote

    with SessionLocal() as session:
        note = session.get(OperatorNote, note_id)
        if note is None:
            return jsonify({"error": "Not found"}), 404
        note.is_active = False
        session.commit()
        return jsonify({"ok": True, "id": note_id})


# ── CHAT (NL → structured note) ───────────────────────────────────────────────

@bp.route("/api/notes/chat", methods=["POST"])
def chat_note():
    """
    POST /api/notes/chat
    Body: {message: "Mike is out sick today, don't assign him anything"}

    Sends the message to Claude with a parsing prompt, gets back structured
    JSON, creates the OperatorNote, and returns both the interpretation and
    the created note.
    """
    from app.core.config_manager import load_config, load_mappings
    from src.clients.database import SessionLocal, OperatorNote, Technician

    body = request.get_json(silent=True) or {}
    message = (body.get("message") or "").strip()
    if not message:
        return jsonify({"error": "message is required"}), 400

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not set"}), 500

    config = load_config()
    model = config.get("claude_model", "claude-sonnet-4-6")

    # ── Build context for the parser ──────────────────────────────────────────
    try:
        mappings_path = config.get("mappings_path", "")
        mappings = load_mappings(mappings_path)
    except Exception:
        mappings = {}

    # Gather known tech identifiers
    tech_names: List[str] = []
    try:
        with SessionLocal() as session:
            techs = session.query(Technician).filter(
                Technician.is_active.is_(True)
            ).all()
            tech_names = [t.name for t in techs if t.name]
    except Exception:
        pass

    # Gather known client names from COMPANY_ALIASES
    client_names: List[str] = []
    try:
        from config import COMPANY_ALIASES
        client_names = list(COMPANY_ALIASES.keys())
    except Exception:
        pass

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    parse_prompt = f"""You are a note parser for an MSP dispatch system. The human operator \
is giving you an instruction that should affect how IT support tickets are dispatched. \
Parse it into structured data.

Known technicians: {', '.join(tech_names) if tech_names else 'unknown'}
Known clients: {', '.join(client_names[:30]) if client_names else 'unknown'}
Current date/time: {now_str}

Rules for expiry:
- "today" → end of today (23:59:59 local → use UTC end of current day)
- "this week" → end of current week (Friday 17:00 UTC)
- "until Friday" → next or current Friday at 17:00 UTC
- "until [date]" → that date at 17:00 UTC
- No time mentioned → null (permanent until manually deleted)

Respond with JSON only (no markdown, no explanation):
{{
  "note_text": "cleaned/complete version of their instruction",
  "scope": "global|client|tech|incident",
  "scope_ref": "name or identifier if applicable, null if global",
  "expires_at": "ISO 8601 datetime string or null if permanent",
  "tags": ["tag1", "tag2"],
  "confidence": 0.0,
  "interpretation": "one sentence explaining how you understood this"
}}"""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=model,
            max_tokens=512,
            messages=[
                {"role": "user", "content": f"{parse_prompt}\n\nOperator message: {message}"}
            ],
        )
        raw_json = (resp.content[0].text if resp.content else "{}").strip()
        # Strip any accidental markdown fences
        if raw_json.startswith("```"):
            raw_json = raw_json.split("```")[1]
            if raw_json.startswith("json"):
                raw_json = raw_json[4:]
            raw_json = raw_json.strip()
        parsed = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        return jsonify({"error": f"Claude returned non-JSON: {exc}", "raw": raw_json}), 500
    except Exception as exc:
        return jsonify({"error": f"Claude API error: {exc}"}), 500

    # ── Validate parsed output ────────────────────────────────────────────────
    scope = parsed.get("scope", "global")
    if scope not in ("global", "client", "tech", "incident"):
        scope = "global"

    note_text = (parsed.get("note_text") or message).strip()
    scope_ref = parsed.get("scope_ref") or None
    expires_at = _parse_iso(parsed.get("expires_at"))
    tags = parsed.get("tags") or []

    # ── Create the note ───────────────────────────────────────────────────────
    note = OperatorNote(
        note_text=note_text,
        scope=scope,
        scope_ref=scope_ref,
        created_by="operator:chat",
        expires_at=expires_at,
    )
    note.tags = tags

    with SessionLocal() as session:
        session.add(note)
        session.commit()
        session.refresh(note)
        note_dict = _note_to_dict(note)

    return jsonify({
        "ok": True,
        "interpretation": parsed.get("interpretation", ""),
        "confidence": parsed.get("confidence", 1.0),
        "note": note_dict,
    }), 201


# ── BRIEFING (read-only) ──────────────────────────────────────────────────────

@bp.route("/api/notes/briefing", methods=["GET"])
def get_briefing():
    """GET /api/notes/briefing — return the current situation briefing text."""
    try:
        from src.agent.briefing import build_situation_briefing
        text = build_situation_briefing()
        return jsonify({"ok": True, "briefing": text})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
