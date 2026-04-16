"""
app/core/logging_config.py

Configures structured JSON logging for production deployment.

Log file : /var/log/ai-dispatcher/app.log  (override via LOG_DIR env var)
Format   : one JSON object per line — ingest with any log aggregator
Rotation : handled externally by logrotate (deploy/logrotate.conf)

Log levels by use case
──────────────────────
DEBUG    every tool call, inputs/outputs
INFO     dispatch decisions, service start/stop, normal lifecycle
WARNING  API retries, rate limits, slow responses
ERROR    failed dispatches, API errors
CRITICAL dispatcher thread died, DB unreachable
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict


class _JsonFormatter(logging.Formatter):
    """Emit one JSON object per log record — easy to parse with jq or any SIEM."""

    _SKIP = frozenset(logging.LogRecord.__dict__.keys()) | {
        "message", "asctime", "msg", "args",
    }

    def format(self, record: logging.LogRecord) -> str:
        obj: Dict[str, Any] = {
            "ts":     self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level":  record.levelname,
            "logger": record.name,
            "msg":    record.getMessage(),
        }
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            obj["stack"] = self.formatStack(record.stack_info)

        # Carry any extra fields attached via logging(..., extra={...})
        for key, val in record.__dict__.items():
            if key in self._SKIP or key.startswith("_"):
                continue
            try:
                json.dumps(val)
                obj[key] = val
            except (TypeError, ValueError):
                obj[key] = str(val)

        return json.dumps(obj, ensure_ascii=False)


def configure_logging() -> None:
    """
    Install JSON file handler + human-readable stderr handler on the root logger.

    Safe to call multiple times — handlers are only added once.
    Call this before creating the Flask app so that app.logger inherits the config.
    """
    root = logging.getLogger()
    if getattr(root, "_dispatch_logging_configured", False):
        return

    root.setLevel(logging.DEBUG)

    # ── JSON file handler ─────────────────────────────────────────────────────
    log_dir = Path(os.getenv("LOG_DIR", "/var/log/ai-dispatcher"))
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "app.log"
        fh = logging.FileHandler(log_path, encoding="utf-8")
    except (PermissionError, OSError):
        # Dev fallback: write to project directory
        log_path = Path("app.log")
        fh = logging.FileHandler(log_path, encoding="utf-8")

    fh.setFormatter(_JsonFormatter())
    fh.setLevel(logging.DEBUG)
    root.addHandler(fh)

    # ── Human-readable stderr handler ─────────────────────────────────────────
    ch = logging.StreamHandler(sys.stderr)
    ch.setFormatter(logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    ))
    ch.setLevel(logging.INFO)
    root.addHandler(ch)

    # ── Quiet excessively noisy third-party loggers ───────────────────────────
    for name in (
        "werkzeug",
        "urllib3.connectionpool",
        "anthropic._base_client",
        "httpx",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)

    # APScheduler "max instances reached" fires every interval when a cycle is
    # still running — this is expected behaviour (coalesce=True, max_instances=1)
    # and does not indicate a problem.  Suppress below ERROR.
    for name in (
        "apscheduler.executors.default",
        "apscheduler.scheduler",
        "apscheduler.job",
    ):
        logging.getLogger(name).setLevel(logging.ERROR)

    root._dispatch_logging_configured = True  # type: ignore[attr-defined]

    log = logging.getLogger(__name__)
    log.info("Logging configured — file=%s", log_path)
