"""
app/core/rate_limiter.py

Thread-safe sliding-window rate limiter for Claude and ConnectWise API calls.

When either service exceeds its per-hour threshold the limiter will:
  1. Log a CRITICAL event.
  2. Pause the background dispatcher (so no new tickets are dispatched).
  3. Post a Teams alert if credentials are configured (best-effort).

Configuration via environment variables
────────────────────────────────────────
  CLAUDE_CALLS_PER_HOUR   integer, default 200
  CW_CALLS_PER_HOUR       integer, default 2000

Both limits apply to a rolling 60-minute window (not a fixed clock hour).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import List

log = logging.getLogger(__name__)


class RateLimiter:
    """Sliding 1-hour window counter for a named external API service."""

    def __init__(self, service: str, max_per_hour: int) -> None:
        self._service = service
        self._max = max_per_hour
        self._timestamps: List[float] = []
        self._lock = threading.Lock()
        self._alerted = False   # avoid repeated Teams pings in the same window

    # ── Public ────────────────────────────────────────────────────────────────

    def record_call(self) -> None:
        """
        Record one outgoing API call.

        If the rolling-hour count exceeds the configured maximum, pause the
        dispatcher and fire a Teams alert (each only once per breach window).
        """
        now = time.monotonic()
        cutoff = now - 3600.0

        with self._lock:
            self._timestamps = [t for t in self._timestamps if t > cutoff]
            self._timestamps.append(now)
            count = len(self._timestamps)

        if count > self._max:
            log.warning(
                "Rate limit exceeded — service=%s calls_this_hour=%d max=%d",
                self._service, count, self._max,
            )
            self._handle_threshold_exceeded(count)
        elif count > self._max * 0.9:
            # Warn at 90 % so operators have some lead time
            log.warning(
                "Approaching rate limit — service=%s calls_this_hour=%d max=%d",
                self._service, count, self._max,
            )

    def calls_this_hour(self) -> int:
        now = time.monotonic()
        cutoff = now - 3600.0
        with self._lock:
            return sum(1 for t in self._timestamps if t > cutoff)

    def reset_alert(self) -> None:
        """Call when the limiter drops below threshold to re-arm the alert."""
        with self._lock:
            self._alerted = False

    # ── Internal ──────────────────────────────────────────────────────────────

    def _handle_threshold_exceeded(self, count: int) -> None:
        with self._lock:
            if self._alerted:
                return
            self._alerted = True

        log.critical(
            "RATE LIMIT BREACH — service=%s count=%d max=%d — pausing dispatcher",
            self._service, count, self._max,
        )

        # Pause dispatcher
        try:
            from services.dispatcher import get_dispatcher
            disp = get_dispatcher()
            if not disp.get_status().get("paused"):
                disp.toggle_pause()
        except Exception as exc:
            log.error("Could not pause dispatcher after rate-limit breach: %s", exc)

        # Teams alert (best-effort — missing credentials is not an error here)
        msg = (
            f"\u26a0\ufe0f AI Dispatcher PAUSED\n"
            f"Rate limit exceeded for *{self._service}*: "
            f"{count} calls in the last hour (threshold: {self._max}).\n"
            f"Resume via the portal: POST /api/dispatcher/toggle"
        )
        try:
            from src.clients.teams import TeamsClient
            TeamsClient().send_message(msg)
        except Exception:
            pass  # Teams credentials not configured — skip silently


# ── Singletons ────────────────────────────────────────────────────────────────

_claude_limiter: "RateLimiter | None" = None
_cw_limiter:     "RateLimiter | None" = None
_singleton_lock  = threading.Lock()


def get_claude_limiter() -> RateLimiter:
    global _claude_limiter
    with _singleton_lock:
        if _claude_limiter is None:
            _claude_limiter = RateLimiter(
                "claude",
                int(os.getenv("CLAUDE_CALLS_PER_HOUR", "200")),
            )
    return _claude_limiter


def get_cw_limiter() -> RateLimiter:
    global _cw_limiter
    with _singleton_lock:
        if _cw_limiter is None:
            _cw_limiter = RateLimiter(
                "connectwise",
                int(os.getenv("CW_CALLS_PER_HOUR", "2000")),
            )
    return _cw_limiter
