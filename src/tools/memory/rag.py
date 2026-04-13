"""
src/tools/memory/rag.py

Retrieval-Augmented Generation helpers for the dispatch agent.

Public API:
    get_similar_past_tickets(summary, limit=5) → dict

Current implementation: simple SQLite keyword search (ilike) over
dispatch_decisions.ticket_summary.

TODO: Replace with embeddings + vector search (chromadb or pgvector) for
      semantic similarity rather than keyword overlap.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

log = logging.getLogger(__name__)


def get_similar_past_tickets(
    summary: str,
    limit: int = 5,
) -> Dict[str, Any]:
    """
    Search the dispatch_decisions table for past tickets with similar summaries.

    Strategy (current):
        1. Split the query into meaningful keywords (length > 3).
        2. Build an OR filter: ticket_summary ILIKE '%keyword%' for each keyword.
        3. Return the most recent matching rows, with their assigned tech and reason.

    TODO: Future — replace keyword ilike with embeddings + vector search
          (chromadb or pgvector) for true semantic similarity retrieval.

    Args:
        summary: Free-text description of the new ticket
        limit:   Maximum results to return (capped at 20)

    Returns:
        {
          "query": str,
          "results": [
            {
              "ticket_id": int,
              "summary": str,
              "assigned_to": str,
              "reason": str,
              "confidence": float,
              "was_dry_run": bool,
              "date": str (ISO-8601),
            },
            ...
          ]
        }
    """
    limit = min(limit, 20)
    query_lower = summary.strip().lower()

    # Extract meaningful keywords (skip stop-words and short tokens)
    _STOP = {
        "the", "and", "for", "this", "that", "with", "from", "have",
        "been", "will", "when", "they", "their", "our", "not", "can",
        "also", "are", "was", "has", "but", "its", "on", "is", "in",
        "of", "to", "a", "an",
    }
    keywords = [
        w for w in query_lower.split()
        if len(w) > 3 and w not in _STOP
    ]

    if not keywords:
        return {"query": summary, "results": []}

    try:
        from src.clients.database import SessionLocal, DispatchDecision
        from sqlalchemy import or_

        with SessionLocal() as session:
            filters = [
                DispatchDecision.ticket_summary.ilike(f"%{kw}%")
                for kw in keywords
            ]
            rows = (
                session.query(DispatchDecision)
                .filter(or_(*filters))
                .order_by(DispatchDecision.created_at.desc())
                .limit(limit)
                .all()
            )

            return {
                "query": summary,
                "results": [
                    {
                        "ticket_id":  r.ticket_id,
                        "summary":    r.ticket_summary,
                        "assigned_to": r.assigned_tech_identifier,
                        "reason":     r.reason,
                        "confidence": r.confidence,
                        "was_dry_run": r.was_dry_run,
                        "date":       r.created_at.isoformat() if r.created_at else None,
                    }
                    for r in rows
                ],
            }

    except Exception as exc:
        log.warning("get_similar_past_tickets failed: %s", exc)
        return {"query": summary, "results": [], "error": str(exc)}
