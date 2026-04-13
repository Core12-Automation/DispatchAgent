"""
services/dispatcher.py

Background dispatcher service — runs the agentic ticket loop on a schedule.

This is "auto mode": the dispatcher wakes up every N seconds, fetches
unrouted tickets from ConnectWise, and runs the agent loop on each new one.
The existing /api/run/start endpoint ("manual mode") is completely independent.

Key design points:
  - One APScheduler BackgroundScheduler, single job.
  - Processed ticket IDs are tracked in memory and reset at midnight.
  - Errors in a single ticket are logged and skipped; the run continues.
  - CW API failures trigger a 5-minute pause.
  - Anthropic rate-limit errors trigger exponential backoff.
  - All progress is broadcast to the shared SSE channel.

Configuration (via .env):
  DISPATCH_INTERVAL_SECONDS   How often to poll (default: 60)
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone, date
from typing import Any, Dict, Optional, Set

log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

_DEFAULT_INTERVAL = 60          # seconds between scheduler ticks
_CW_PAUSE_SECONDS = 300         # 5 min pause on CW API failure
_RATE_LIMIT_BASE  = 30          # base back-off for Claude rate limit (seconds)
_RATE_LIMIT_MAX   = 600         # cap for exponential back-off
_MAX_TICKETS_PER_CYCLE = 20     # safety cap so one cycle doesn't run forever


# ─────────────────────────────────────────────────────────────────────────────
# Singleton accessor
# ─────────────────────────────────────────────────────────────────────────────

_instance: Optional["DispatcherService"] = None
_instance_lock = threading.Lock()


def get_dispatcher() -> "DispatcherService":
    """Return the process-wide DispatcherService singleton (created on first call)."""
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = DispatcherService()
        return _instance


# ─────────────────────────────────────────────────────────────────────────────
# Service
# ─────────────────────────────────────────────────────────────────────────────

class DispatcherService:
    """
    Long-running background dispatcher that runs the agent loop on new tickets.

    Lifecycle::

        svc = DispatcherService()
        svc.start()       # starts APScheduler, begins polling
        svc.stop()        # shuts down cleanly
        svc.run_once()    # trigger one cycle immediately (non-blocking)
        svc.get_status()  # dict suitable for JSON response
    """

    def __init__(self) -> None:
        self._scheduler = None          # APScheduler BackgroundScheduler
        self._lock       = threading.Lock()
        self._paused     = False
        self._paused_until: Optional[float] = None   # epoch seconds
        self._started_at: Optional[float]  = None
        self._last_run:   Optional[float]  = None
        self._next_run:   Optional[float]  = None
        self._last_error: Optional[str]    = None

        # Processed ticket IDs — reset at midnight
        self._processed_ids: Set[int] = set()
        self._processed_date: date     = date.today()

        # Stats for today
        self._tickets_today: int = 0
        self._today_date: date   = date.today()

        # Exponential back-off state for Claude rate limits
        self._rate_limit_backoff: float = 0.0
        self._rate_limit_until:   float = 0.0

        # Interval (loaded once at construction; re-read from env each cycle)
        self._interval: int = int(os.getenv("DISPATCH_INTERVAL_SECONDS", _DEFAULT_INTERVAL))

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background scheduler.  Safe to call multiple times."""
        with self._lock:
            if self._scheduler is not None and self._scheduler.running:
                log.info("[Dispatcher] Already running — ignoring start()")
                return

            try:
                from apscheduler.schedulers.background import BackgroundScheduler
            except ImportError:
                log.error("[Dispatcher] APScheduler not installed — cannot start")
                return

            interval = int(os.getenv("DISPATCH_INTERVAL_SECONDS", self._interval))
            self._interval = interval

            scheduler = BackgroundScheduler(timezone="UTC")
            scheduler.add_job(
                self._cycle,
                trigger="interval",
                seconds=interval,
                id="dispatch_cycle",
                max_instances=1,
                coalesce=True,
                next_run_time=None,   # don't fire immediately on start
            )
            scheduler.start()
            self._scheduler = scheduler
            self._started_at = time.time()

            # Schedule the first run after one full interval
            job = scheduler.get_job("dispatch_cycle")
            if job:
                import datetime as _dt
                job.modify(
                    next_run_time=_dt.datetime.now(_dt.timezone.utc)
                    + _dt.timedelta(seconds=interval)
                )
                self._next_run = job.next_run_time.timestamp() if job.next_run_time else None

            log.info("[Dispatcher] Started — interval=%ds", interval)
            self._broadcast(f"[Dispatcher] Started — polling every {interval}s")

    def stop(self) -> None:
        """Shut down the scheduler gracefully."""
        with self._lock:
            if self._scheduler is None or not self._scheduler.running:
                return
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
            log.info("[Dispatcher] Stopped")
            self._broadcast("[Dispatcher] Stopped")

    def run_once(self) -> None:
        """Trigger one dispatch cycle immediately (runs in background thread)."""
        t = threading.Thread(target=self._cycle, daemon=True, name="dispatcher-manual")
        t.start()

    def toggle_pause(self) -> bool:
        """Pause if running, resume if paused. Returns new paused state."""
        with self._lock:
            self._paused = not self._paused
            state = "Paused" if self._paused else "Resumed"
            log.info("[Dispatcher] %s", state)
            self._broadcast(f"[Dispatcher] {state}")
            # Clear any CW-error-induced pause when user manually resumes
            if not self._paused:
                self._paused_until = None
        return self._paused

    # ── Status ─────────────────────────────────────────────────────────────────

    def get_status(self) -> Dict[str, Any]:
        """Return a JSON-serialisable status dict."""
        with self._lock:
            running = bool(self._scheduler and self._scheduler.running)

            # Determine effective pause state
            paused = self._paused
            if self._paused_until and time.time() < self._paused_until:
                paused = True
                pause_reason = "cw_error"
            elif self._rate_limit_until and time.time() < self._rate_limit_until:
                paused = True
                pause_reason = "rate_limit"
            else:
                pause_reason = "user" if self._paused else None

            self._reset_daily_counters()

            interval = int(os.getenv("DISPATCH_INTERVAL_SECONDS", self._interval))

            return {
                "running":       running,
                "paused":        paused,
                "pause_reason":  pause_reason,
                "interval_secs": interval,
                "last_run":      _fmt_ts(self._last_run),
                "next_run":      _next_run_str(self._scheduler),
                "tickets_today": self._tickets_today,
                "uptime_secs":   round(time.time() - self._started_at, 0) if self._started_at else None,
                "last_error":    self._last_error,
            }

    # ── Internal: one dispatch cycle ───────────────────────────────────────────

    def _cycle(self) -> None:
        """Execute one full dispatch cycle (fetch → filter → dispatch → record)."""
        # ── Guard: paused? ────────────────────────────────────────────────────
        if self._paused:
            log.debug("[Dispatcher] Paused — skipping cycle")
            return

        now = time.time()
        if self._paused_until and now < self._paused_until:
            log.debug("[Dispatcher] CW error pause — skipping cycle")
            return
        if self._rate_limit_until and now < self._rate_limit_until:
            log.debug("[Dispatcher] Rate-limit back-off — skipping cycle")
            return

        self._reset_daily_counters()
        self._last_run = now
        self._broadcast("[Dispatcher] ─── Starting dispatch cycle ───")

        # ── Load config & mappings ─────────────────────────────────────────────
        try:
            from app.core.config_manager import load_config, load_mappings
            config   = load_config()
            mappings = load_mappings(config.get("mappings_path", ""))
        except Exception as exc:
            msg = f"[Dispatcher] Failed to load config/mappings: {exc}"
            log.error(msg)
            self._last_error = str(exc)
            self._broadcast(msg)
            return

        # ── Fetch new tickets from CW ──────────────────────────────────────────
        try:
            from src.clients.connectwise import CWManageClient
            from src.tools.perception.tickets import get_new_tickets

            cw      = CWManageClient(dry_run=config.get("dry_run", True))
            result  = get_new_tickets(cw, config, mappings, limit=_MAX_TICKETS_PER_CYCLE)
            tickets = result.get("tickets", [])
        except Exception as exc:
            # CW unreachable → pause for 5 minutes
            msg = f"[Dispatcher] CW API error — pausing {_CW_PAUSE_SECONDS}s: {exc}"
            log.error(msg)
            self._last_error = str(exc)
            self._broadcast(msg)
            with self._lock:
                self._paused_until = time.time() + _CW_PAUSE_SECONDS
            return

        if not tickets:
            self._broadcast("[Dispatcher] No new tickets to dispatch")
            return

        # ── Filter already-processed tickets ──────────────────────────────────
        with self._lock:
            processed = set(self._processed_ids)

        new_tickets = [t for t in tickets if t.get("id") not in processed]
        if not new_tickets:
            self._broadcast(f"[Dispatcher] {len(tickets)} ticket(s) found — all already processed today")
            return

        self._broadcast(f"[Dispatcher] {len(new_tickets)} new ticket(s) to dispatch")

        # ── Create DispatchRun record ──────────────────────────────────────────
        run_id = self._create_run_record()

        # ── Dispatch each ticket ───────────────────────────────────────────────
        tickets_processed = 0
        tickets_assigned  = 0
        tickets_flagged   = 0
        errors            = 0

        for ticket in new_tickets:
            ticket_id = ticket.get("id")
            if ticket_id is None:
                continue

            # Mark as processed immediately (so a crash doesn't cause re-processing)
            with self._lock:
                self._processed_ids.add(ticket_id)

            try:
                outcome = self._dispatch_ticket(ticket, config, mappings, cw, run_id)
            except _RateLimitError as exc:
                # Claude rate-limit — back off exponentially, then continue later
                backoff = min(
                    max(self._rate_limit_backoff * 2, _RATE_LIMIT_BASE),
                    _RATE_LIMIT_MAX
                )
                self._rate_limit_backoff = backoff
                self._rate_limit_until   = time.time() + backoff
                msg = f"[Dispatcher] Claude rate limit — backing off {backoff:.0f}s"
                log.warning(msg)
                self._broadcast(msg)
                errors += 1
                break  # stop processing this batch; will retry remaining next cycle
            except Exception as exc:
                msg = f"[Dispatcher] Error dispatching ticket #{ticket_id}: {exc}"
                log.error(msg, exc_info=True)
                self._last_error = str(exc)
                self._broadcast(msg)
                errors += 1
                continue  # skip this ticket, keep going

            # Reset rate-limit back-off counter on success
            self._rate_limit_backoff = 0.0

            tickets_processed += 1
            status = outcome.get("status", "error")
            if status == "timeout":
                # Flag the ticket for human review
                self._flag_for_human_review(ticket_id, cw, config, mappings)
                tickets_flagged += 1
            elif status == "ok":
                if outcome.get("decisions_made"):
                    tickets_assigned += 1
            elif status == "error":
                errors += 1

            with self._lock:
                self._tickets_today += 1

        # ── Close DispatchRun record ───────────────────────────────────────────
        self._close_run_record(
            run_id,
            tickets_processed=tickets_processed,
            tickets_assigned=tickets_assigned,
            tickets_flagged=tickets_flagged,
            errors=errors,
        )

        self._broadcast(
            f"[Dispatcher] Cycle complete — "
            f"processed={tickets_processed} assigned={tickets_assigned} "
            f"flagged={tickets_flagged} errors={errors}"
        )
        self._last_error = None

    # ── Internal: dispatch one ticket ──────────────────────────────────────────

    def _dispatch_ticket(
        self,
        slim_ticket: Dict[str, Any],
        config: Dict[str, Any],
        mappings: Dict[str, Any],
        cw: Any,
        run_id: Optional[int],
    ) -> Dict[str, Any]:
        """
        Fetch the full ticket record and run the agent loop.
        Raises _RateLimitError on Claude 429.
        """
        from src.agent.loop import run_dispatch
        from app.core.state import broadcast

        ticket_id = slim_ticket["id"]
        self._broadcast(f"[Dispatcher] → Fetching ticket #{ticket_id}: {slim_ticket.get('summary','')[:60]}")

        # Get full ticket object (slim ticket from get_new_tickets is missing some fields)
        try:
            full_ticket = cw.get_ticket(ticket_id)
        except Exception as exc:
            raise  # propagates to caller

        # Inject run_id into config so loop.py's log_dispatch_decision can link it
        cfg = {**config, "_run_id": run_id}

        try:
            result = run_dispatch(
                full_ticket,
                config=cfg,
                mappings=mappings,
                broadcaster=broadcast,
            )
        except Exception as exc:
            exc_str = str(exc).lower()
            if "rate_limit" in exc_str or "429" in exc_str or "rate limit" in exc_str:
                raise _RateLimitError(str(exc)) from exc
            raise

        return result

    # ── Internal: DB helpers ───────────────────────────────────────────────────

    def _create_run_record(self) -> Optional[int]:
        """Insert a new DispatchRun row and return its id."""
        try:
            from src.clients.database import SessionLocal, DispatchRun, init_db
            from datetime import datetime, timezone
            init_db()
            with SessionLocal() as session:
                run = DispatchRun(
                    started_at=datetime.now(timezone.utc),
                    trigger="scheduled",
                )
                session.add(run)
                session.commit()
                session.refresh(run)
                return run.id
        except Exception as exc:
            log.warning("[Dispatcher] Could not create DispatchRun record: %s", exc)
            return None

    def _close_run_record(
        self,
        run_id: Optional[int],
        *,
        tickets_processed: int,
        tickets_assigned: int,
        tickets_flagged: int,
        errors: int,
    ) -> None:
        if run_id is None:
            return
        try:
            from src.clients.database import SessionLocal, DispatchRun
            from datetime import datetime, timezone
            with SessionLocal() as session:
                run = session.get(DispatchRun, run_id)
                if run:
                    run.ended_at          = datetime.now(timezone.utc)
                    run.tickets_processed = tickets_processed
                    run.tickets_assigned  = tickets_assigned
                    run.tickets_flagged   = tickets_flagged
                    run.errors            = errors
                    session.commit()
        except Exception as exc:
            log.warning("[Dispatcher] Could not close DispatchRun record: %s", exc)

    # ── Internal: flag for human review ───────────────────────────────────────

    def _flag_for_human_review(
        self,
        ticket_id: int,
        cw: Any,
        config: Dict[str, Any],
        mappings: Dict[str, Any],
    ) -> None:
        """Add a ⚠️ note to the ticket indicating it needs human attention."""
        try:
            if config.get("dry_run", True):
                self._broadcast(f"[Dispatcher][DRY RUN] Would flag ticket #{ticket_id} for human review")
                return
            cw.add_ticket_note(
                ticket_id,
                "⚠️ Automated dispatch timed out for this ticket — please review manually.",
                internal_analysis_flag=True,
            )
            self._broadcast(f"[Dispatcher] Flagged ticket #{ticket_id} for human review")
        except Exception as exc:
            log.warning("[Dispatcher] Could not flag ticket #%s: %s", ticket_id, exc)

    # ── Internal: daily counter reset ─────────────────────────────────────────

    def _reset_daily_counters(self) -> None:
        """Reset per-day counters if the date has changed (called under lock or at start of cycle)."""
        today = date.today()
        if self._processed_date != today:
            self._processed_ids   = set()
            self._processed_date  = today
        if self._today_date != today:
            self._tickets_today   = 0
            self._today_date      = today

    # ── Internal: broadcasting ─────────────────────────────────────────────────

    def _broadcast(self, msg: str) -> None:
        """Fan out a log line to the shared SSE channel (best-effort)."""
        try:
            from app.core.state import broadcast
            broadcast(msg)
        except Exception:
            log.debug("[Dispatcher] %s", msg)


# ─────────────────────────────────────────────────────────────────────────────
# Sentinel exception
# ─────────────────────────────────────────────────────────────────────────────

class _RateLimitError(Exception):
    """Raised internally when Claude returns a rate-limit response."""


# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_ts(epoch: Optional[float]) -> Optional[str]:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _next_run_str(scheduler) -> Optional[str]:
    if scheduler is None or not scheduler.running:
        return None
    try:
        job = scheduler.get_job("dispatch_cycle")
        if job and job.next_run_time:
            return job.next_run_time.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        pass
    return None
