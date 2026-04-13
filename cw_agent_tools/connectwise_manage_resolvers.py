from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Optional


DEFAULT_MAPPINGS_PATH = Path("C:/APIscripts/mappings.json")
DEFAULT_COMPANIES_PATH = Path("C:/APIscripts/cw_companies.json")


def parse_maybe_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return int(value)
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
    s = str(value or "").strip().lower()
    return re.sub(r"\s+", " ", s)


def load_json_file(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default


class MappingResolver:
    def __init__(
        self,
        mappings_path: Path = DEFAULT_MAPPINGS_PATH,
        companies_path: Path = DEFAULT_COMPANIES_PATH,
        company_aliases: Optional[Dict[str, list[str]]] = None,
    ) -> None:
        self.mappings_path = mappings_path
        self.companies_path = companies_path
        self.mappings = self._load_mappings(mappings_path)
        self.company_map = self._load_companies(companies_path)
        self.company_alias_lookup = self._build_company_alias_lookup(company_aliases or {})

    def _load_mappings(self, path: Path) -> Dict[str, Dict[str, Any]]:
        data = load_json_file(path, {"boards": {}, "statuses": {}, "members": {}, "companies": {}})
        if not isinstance(data, dict):
            return {"boards": {}, "statuses": {}, "members": {}, "companies": {}}

        normalized: Dict[str, Dict[str, Any]] = {}
        for section, payload in data.items():
            if not isinstance(section, str) or not isinstance(payload, dict):
                continue
            sec_out: Dict[str, Any] = {}
            for k, v in payload.items():
                sec_out[normalize_key(k)] = v
            normalized[normalize_key(section)] = sec_out
        for section in ("boards", "statuses", "members", "companies"):
            normalized.setdefault(section, {})
        return normalized

    def _load_companies(self, path: Path) -> Dict[str, int]:
        raw = load_json_file(path, {})
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
                cid = parse_maybe_int(item.get("id") or item.get("companyId") or item.get("company_id"))
                if cid is None:
                    continue
                for name_key in ("name", "identifier", "company", "companyName"):
                    if item.get(name_key):
                        out[normalize_key(item[name_key])] = cid
        return out

    def _build_company_alias_lookup(self, company_aliases: Dict[str, list[str]]) -> Dict[str, str]:
        alias_lookup: Dict[str, str] = {}
        for canonical_name, aliases in company_aliases.items():
            alias_lookup[normalize_key(canonical_name)] = canonical_name
            for alias in aliases or []:
                alias_lookup[normalize_key(alias)] = canonical_name
        return alias_lookup

    def reverse_lookup_name(self, mapping: Dict[str, Any], target_id: Any) -> Optional[str]:
        target_num = parse_maybe_int(target_id)
        if target_num is None:
            return None
        for k, v in mapping.items():
            if parse_maybe_int(v) == target_num:
                return k
        return None

    def resolve_from_mapping(self, prefer: Any, mapping: Dict[str, Any], item_type: str) -> int:
        nid = parse_maybe_int(prefer)
        if nid is not None:
            return nid
        key = normalize_key(prefer)
        if key in mapping:
            resolved = parse_maybe_int(mapping[key])
            if resolved is not None:
                return resolved
            raise RuntimeError(f"Mapping for {item_type!r} '{prefer}' is present but not numeric.")
        raise RuntimeError(f"Could not resolve {item_type!r} from {prefer!r}. Add it to mappings or use a numeric id.")

    def resolve_board_id(self, value: Any) -> int:
        return self.resolve_from_mapping(value, self.mappings.get("boards", {}), "board")

    def resolve_member_id(self, value: Any) -> int:
        return self.resolve_from_mapping(value, self.mappings.get("members", {}), "member")

    def resolve_company_id(self, value: Any) -> int:
        cid = parse_maybe_int(value)
        if cid is not None:
            return cid
        raw_name = str(value or "").strip()
        if not raw_name:
            raise RuntimeError("Company value is blank.")
        normalized_input = normalize_key(raw_name)
        direct = self.company_map.get(normalized_input)
        if direct is not None:
            return int(direct)
        canonical_name = self.company_alias_lookup.get(normalized_input)
        if canonical_name:
            canonical_id = self.company_map.get(normalize_key(canonical_name))
            if canonical_id is not None:
                return int(canonical_id)
        raise RuntimeError(
            f"Could not resolve company {value!r}. Add it to {self.companies_path}, aliases, or use a numeric id."
        )

    def resolve_status_id(self, board_name_or_id: Any, status_value: Any) -> int:
        nid = parse_maybe_int(status_value)
        if nid is not None:
            return nid
        board_name = str(board_name_or_id or "").strip()
        if not board_name:
            raise RuntimeError("Board name or id is required to resolve a status by name.")
        if parse_maybe_int(board_name_or_id) is not None:
            board_name = self.reverse_lookup_name(self.mappings.get("boards", {}), board_name_or_id) or board_name
        section = f"{normalize_key(board_name)} statuses"
        return self.resolve_from_mapping(status_value, self.mappings.get(section, {}), "status")

    def resolve_type_id(self, board_name_or_id: Any, type_value: Any) -> int:
        nid = parse_maybe_int(type_value)
        if nid is not None:
            return nid
        board_name = str(board_name_or_id or "").strip()
        if not board_name:
            raise RuntimeError("Board name or id is required to resolve a type by name.")
        if parse_maybe_int(board_name_or_id) is not None:
            board_name = self.reverse_lookup_name(self.mappings.get("boards", {}), board_name_or_id) or board_name
        section = f"{normalize_key(board_name)} types"
        return self.resolve_from_mapping(type_value, self.mappings.get(section, {}), "type")
