"""
app/core/config_manager.py

Handles reading and writing the portal configuration file (portal_config.json),
the mappings file (mappings.json), and the .env file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict

from dotenv import dotenv_values, find_dotenv, set_key

import config as cfg


# ── Portal config ─────────────────────────────────────────────────────────────

def load_config() -> Dict:
    """Load portal config from disk, merging with defaults for missing keys."""
    if cfg.CONFIG_FILE.exists():
        with open(cfg.CONFIG_FILE, encoding="utf-8") as f:
            return {**cfg.DEFAULT_CONFIG, **json.load(f)}
    return cfg.DEFAULT_CONFIG.copy()


def save_config(data: Dict) -> None:
    """Persist the portal config to disk."""
    cfg.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg.CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ── Mappings ──────────────────────────────────────────────────────────────────

def load_mappings(path: str) -> Dict:
    """Load the mappings JSON file from the given path."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_mappings(path: str, data: Dict) -> None:
    """Write the mappings JSON file to the given path."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ── .env management ───────────────────────────────────────────────────────────

def get_env_path() -> Path:
    """Return the path to the .env file, defaulting to BASE_DIR/.env."""
    p = find_dotenv(usecwd=True)
    return Path(p) if p else cfg.BASE_DIR / ".env"


def read_env() -> Dict[str, str]:
    """Read all values from the .env file (without affecting os.environ)."""
    env_path = get_env_path()
    return dict(dotenv_values(str(env_path))) if env_path.exists() else {}


def mask_value(key: str, value: str) -> str:
    """Partially mask sensitive env var values for display."""
    if key in cfg.SENSITIVE and value and len(value) > 4:
        return value[:4] + "\u2022" * min(len(value) - 4, 20)
    return value
