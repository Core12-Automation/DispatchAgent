"""
src/clients/resolver.py

Ported from cw_agent_tools/connectwise_manage_resolvers.py.

MappingResolver: resolves human-readable names (board names, status names,
member identifiers, company names) to ConnectWise numeric IDs.

Handles:
  - Case-insensitive, whitespace-normalised key matching
  - Board-scoped status and type resolution
  - Company resolution from cw_companies.json + alias table
  - Numeric pass-through (if you already have an ID, it passes straight through)
  - Reverse lookup (ID → name)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


# ── Company aliases ────────────────────────────────────────────────────────────
# Maps canonical company name → list of alternate spellings / email patterns.
# Add entries here as new clients are onboarded.

COMPANY_ALIASES: Dict[str, List[str]] = {
    "BLUR Workshop": ["blur", "BLUR", "blur workshop", "blurworkshop", "blurworkshop.com"],
    "Willmer Engineering, Inc.": ["Willmer", "willmerengineeringinc", "jcwillmer@willmerengineering.com"],
    "Mann Mechanical": ["Mann Mechanical", "mann", "gthomas@mannmechanical.com"],
}

# ── Defaults ───────────────────────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_MAPPINGS_PATH  = _PROJECT_ROOT / "data" / "mappings.json"
DEFAULT_COMPANIES_PATH = _PROJECT_ROOT / "data" / "cw_companies.json"


# ── Utilities ─────────────────────────────────────────────────────────────────

def parse_maybe_int(value: Any) -> Optional[int]:
    """Safely coerce value to int, returning None on failure."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        try:
            return int(value)
        except Exception:
            return None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    try:
        return int(value)
    except Exception:
        return None


def normalize_key(value: Any) -> str:
    """Lowercase, strip, and collapse internal whitespace."""
    s = str(value or "").strip().lower()
    return re.sub(r"\s+", " ", s)


def _load_json_file(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except Exception:
        return default


# ── MappingResolver ───────────────────────────────────────────────────────────

class MappingResolver:
    """
    Resolves names → ConnectWise numeric IDs using mappings.json and
    optionally cw_companies.json.

    All lookups are case-insensitive and whitespace-normalised so that
    "Dispatch Statuses", "dispatch statuses", and "dispatch  statuses"
    all refer to the same section.

    Usage:
        resolver = MappingResolver()
        board_id  = resolver.resolve_board_id("Support")     # → 61
        status_id = resolver.resolve_status_id("Support", "Assigned")  # → 843
        member_id = resolver.resolve_member_id("akloss")     # → 407
        company_id = resolver.resolve_company_id("Mann Mechanical")  # → int
    """

    def __init__(
        self,
        mappings_path: Path = DEFAULT_MAPPINGS_PATH,
        companies_path: Path = DEFAULT_COMPANIES_PATH,
        company_aliases: Optional[Dict[str, List[str]]] = None,
    ) -> None:
        self.mappings_path  = mappings_path
        self.companies_path = companies_path
        self.mappings       = self._load_mappings(mappings_path)
        self.company_map    = self._load_companies(companies_path)
        self.company_alias_lookup = self._build_alias_lookup(
            company_aliases if company_aliases is not None else COMPANY_ALIASES
        )

    # ── Loaders ───────────────────────────────────────────────────────────────

    def _load_mappings(self, path: Path) -> Dict[str, Dict[str, Any]]:
        data = _load_json_file(path, {})
        if not isinstance(data, dict):
            return {}
        normalized: Dict[str, Dict[str, Any]] = {}
        for section, payload in data.items():
            if not isinstance(payload, dict):
                continue
            sec_out: Dict[str, Any] = {}
            for k, v in payload.items():
                sec_out[normalize_key(k)] = v
            normalized[normalize_key(section)] = sec_out
        return normalized

    def _load_companies(self, path: Path) -> Dict[str, int]:
        raw = _load_json_file(path, {})
        out: Dict[str, int] = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                candidate = v.get("id") if isinstance(v, dict) else v
                cid = parse_maybe_int(candidate)
                if cid is not None:
                    out[normalize_key(k)] = cid
        elif isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                cid = parse_maybe_int(
                    item.get("id") or item.get("companyId") or item.get("company_id")
                )
                if cid is None:
                    continue
                for name_key in ("name", "identifier", "company", "companyName"):
                    if item.get(name_key):
                        out[normalize_key(item[name_key])] = cid
        return out

    def _build_alias_lookup(self, aliases: Dict[str, List[str]]) -> Dict[str, str]:
        """Build { normalized_alias → canonical_name } lookup."""
        lookup: Dict[str, str] = {}
        for canonical, alias_list in aliases.items():
            lookup[normalize_key(canonical)] = canonical
            for alias in (alias_list or []):
                lookup[normalize_key(alias)] = canonical
        return lookup

    # ── Core resolution helpers ───────────────────────────────────────────────

    def resolve_from_section(self, section_key: str, value: Any, item_type: str) -> int:
        """
        Resolve value against a named section in mappings.json.
        Passes through numeric values unchanged.
        Raises RuntimeError if the name cannot be resolved.
        """
        nid = parse_maybe_int(value)
        if nid is not None:
            return nid
        mapping = self.mappings.get(normalize_key(section_key)) or {}
        key = normalize_key(value)
        resolved = parse_maybe_int(mapping.get(key))
        if resolved is not None:
            return resolved
        raise RuntimeError(
            f"Could not resolve {item_type} {value!r} in section {section_key!r}. "
            f"Available: {list(mapping.keys())}"
        )

    def reverse_lookup_name(self, section_key: str, target_id: Any) -> Optional[str]:
        """Return the name for a given ID within a mappings section, or None."""
        target_num = parse_maybe_int(target_id)
        if target_num is None:
            return None
        mapping = self.mappings.get(normalize_key(section_key)) or {}
        for k, v in mapping.items():
            if parse_maybe_int(v) == target_num:
                return k
        return None

    # ── Public resolution methods ─────────────────────────────────────────────

    def resolve_board_id(self, value: Any) -> int:
        return self.resolve_from_section("boards", value, "board")

    def resolve_member_id(self, value: Any) -> int:
        return self.resolve_from_section("members", value, "member")

    def resolve_status_id(self, board_name_or_id: Any, status_value: Any) -> int:
        """
        Resolve a status name within the correct board-scoped section.
        e.g. resolve_status_id("Support", "Assigned") → 843
        """
        nid = parse_maybe_int(status_value)
        if nid is not None:
            return nid
        # Resolve board name if given an ID
        board_name = str(board_name_or_id or "").strip()
        if parse_maybe_int(board_name_or_id) is not None:
            board_name = (
                self.reverse_lookup_name("boards", board_name_or_id) or board_name
            )
        section = f"{normalize_key(board_name)} statuses"
        return self.resolve_from_section(section, status_value, "status")

    def resolve_type_id(self, board_name_or_id: Any, type_value: Any) -> int:
        """
        Resolve a ticket type name within the correct board-scoped section.
        e.g. resolve_type_id("Support", "Network") → 1143
        """
        nid = parse_maybe_int(type_value)
        if nid is not None:
            return nid
        board_name = str(board_name_or_id or "").strip()
        if parse_maybe_int(board_name_or_id) is not None:
            board_name = (
                self.reverse_lookup_name("boards", board_name_or_id) or board_name
            )
        section = f"{normalize_key(board_name)} types"
        return self.resolve_from_section(section, type_value, "type")

    def resolve_company_id(self, value: Any) -> int:
        """
        Resolve a company name to its CW ID.
        Checks: numeric pass-through → cw_companies.json → alias table.
        """
        cid = parse_maybe_int(value)
        if cid is not None:
            return cid
        raw_name = str(value or "").strip()
        if not raw_name:
            raise RuntimeError("Company value is blank.")
        norm = normalize_key(raw_name)

        direct = self.company_map.get(norm)
        if direct is not None:
            return int(direct)

        canonical = self.company_alias_lookup.get(norm)
        if canonical:
            cid2 = self.company_map.get(normalize_key(canonical))
            if cid2 is not None:
                return int(cid2)

        raise RuntimeError(
            f"Could not resolve company {value!r}. "
            f"Add it to data/cw_companies.json or COMPANY_ALIASES in src/clients/resolver.py."
        )

    def resolve_priority_id(self, value: Any) -> int:
        return self.resolve_from_section("priorities", value, "priority")

    # ── Convenience: raw section access ───────────────────────────────────────

    def get_section(self, section_key: str) -> Dict[str, Any]:
        """Return a normalized mappings section dict (empty dict if absent)."""
        return self.mappings.get(normalize_key(section_key)) or {}
