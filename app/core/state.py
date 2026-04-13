"""
app/core/state.py

Global run state shared across all services and routes.
Provides thread-safe SSE broadcasting and run lifecycle management.
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Dict

# ── Locks ────────────────────────────────────────────────────────────────────

_lock = threading.Lock()

# ── Run state ────────────────────────────────────────────────────────────────

_state: Dict[str, Any] = {
    "running":     False,
    "log_lines":   [],
    "subscribers": [],   # list[queue.Queue]  — SSE consumers
    "summary":     None,
    "history":     [],
    "stop_flag":   threading.Event(),
}

# ── Tool state (search results, report data) ──────────────────────────────────

_tool_state: Dict[str, Any] = {
    "search_results": [],
    "report_data":    {},
}


# ── Public accessors ──────────────────────────────────────────────────────────

def get_lock() -> threading.Lock:
    return _lock


def get_state() -> Dict[str, Any]:
    return _state


def get_tool_state() -> Dict[str, Any]:
    return _tool_state


# ── SSE broadcasting ──────────────────────────────────────────────────────────

def broadcast(msg: str) -> None:
    """Append a log line and fan it out to all live SSE subscribers."""
    with _lock:
        _state["log_lines"].append(msg)
        for q in _state["subscribers"]:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass


def broadcast_done() -> None:
    """Signal end-of-stream to all SSE subscribers (None sentinel)."""
    with _lock:
        for q in _state["subscribers"]:
            try:
                q.put_nowait(None)
            except queue.Full:
                pass


# ── Run lifecycle helpers ─────────────────────────────────────────────────────

def start_run() -> threading.Event:
    """
    Mark a run as started; reset log lines and summary.
    Returns the fresh stop_event to pass to the service thread.
    Raises RuntimeError if a run is already in progress.
    """
    with _lock:
        if _state["running"]:
            raise RuntimeError("A run is already in progress")
        stop_event = threading.Event()
        _state["running"]   = True
        _state["log_lines"] = []
        _state["summary"]   = None
        _state["stop_flag"] = stop_event
    return stop_event


def finish_run() -> None:
    """Mark the run as finished."""
    with _lock:
        _state["running"] = False


def record_summary(summary: Dict[str, Any]) -> None:
    """Persist the run summary and prepend to history (capped at 20 entries)."""
    import time
    with _lock:
        _state["summary"] = summary
        _state["history"].insert(0, {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            **summary,
            "log_count": len(_state["log_lines"]),
        })
        _state["history"] = _state["history"][:20]
