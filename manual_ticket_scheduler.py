import os
import json
import argparse
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple, List
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from requests.auth import HTTPBasicAuth
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv, find_dotenv

# ============================================================
# Manual Ticket Scheduler for ConnectWise Manage / PSA
#
# Purpose:
#   Create a schedule entry for a chosen technician, ticket number,
#   day, and time slot.
#
# Examples:
#   python manual_ticket_scheduler.py --ticket 1207957 --tech akloss \
#       --date 2026-04-21 --start 13:00 --end 14:30
#
#   python manual_ticket_scheduler.py --ticket 1207957 --member-id 42 \
#       --date 2026-04-21 --start 13:00 --end 14:30 --commit
#
# Notes:
#   - By default this runs in preview mode and DOES NOT create anything.
#   - Add --commit to actually create the schedule entry.
# ============================================================

DEFAULT_TIMEZONE = os.getenv("CW_LOCAL_TIMEZONE", "America/New_York")
DEFAULT_MAPPINGS_PATH = os.getenv("CWM_MAPPINGS_PATH", r"C:/APIScripts/mappings.json")
DEFAULT_SCHEDULE_TYPE_ID = int(os.getenv("CWM_SCHEDULE_TYPE_ID", "4"))
DEFAULT_SCHEDULE_STATUS_ID = int(os.getenv("CWM_SCHEDULE_STATUS_ID", "1"))
DEFAULT_WHERE_ID = os.getenv("CWM_DEFAULT_WHERE_ID", "").strip() or None
TIMEOUT_SECS = 30


# ============================================================
# Auth / session helpers
# ============================================================

def get_auth() -> HTTPBasicAuth:
    company = (os.getenv("CWM_COMPANY_ID") or "").strip()
    public_key = (os.getenv("CWM_PUBLIC_KEY") or "").strip()
    private_key = (os.getenv("CWM_PRIVATE_KEY") or "").strip()
    return HTTPBasicAuth(f"{company}+{public_key}", private_key)


def get_headers() -> Dict[str, str]:
    client_id = (os.getenv("CLIENT_ID") or "").strip()
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if client_id:
        headers["ClientID"] = client_id
    return headers


def api_url(path: str) -> str:
    site = (os.getenv("CWM_SITE") or "").rstrip("/")
    return urljoin(site.rstrip("/") + "/", path.lstrip("/"))


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


SESSION = make_session()


def cw_get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    response = SESSION.get(
        api_url(path),
        auth=get_auth(),
        headers=get_headers(),
        params=params,
        timeout=TIMEOUT_SECS,
    )
    if not response.ok:
        raise RuntimeError(f"GET {path} failed: HTTP {response.status_code}: {response.text[:900]}")
    return response.json()


def cw_post(path: str, body: Dict[str, Any]) -> Any:
    response = SESSION.post(
        api_url(path),
        auth=get_auth(),
        headers=get_headers(),
        json=body,
        timeout=TIMEOUT_SECS,
    )
    if response.status_code in (400, 409):
        try:
            return {"_http_status": response.status_code, "_body": response.json()}
        except Exception:
            return {"_http_status": response.status_code, "_body": response.text}
    if not response.ok:
        raise RuntimeError(f"POST {path} failed: HTTP {response.status_code}: {response.text[:900]}")
    return response.json()


# ============================================================
# General helpers
# ============================================================

def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_date_time_local(date_str: str, time_str: str, tz_name: str) -> datetime:
    naive = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    return naive.replace(tzinfo=ZoneInfo(tz_name))


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def load_mappings(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"mappings.json not found at: {path}\n"
            f"Either place it there, or set CWM_MAPPINGS_PATH to the correct path."
        )
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def build_member_maps(mappings: Dict[str, Any]) -> Tuple[Dict[str, int], Dict[int, str]]:
    members = (mappings or {}).get("members") or {}
    ident_to_id: Dict[str, int] = {}
    id_to_ident: Dict[int, str] = {}
    for ident, mid in members.items():
        if ident is None or mid is None:
            continue
        key = str(ident).strip()
        if not key:
            continue
        try:
            member_id = int(mid)
        except Exception:
            continue
        ident_to_id[key.lower()] = member_id
        id_to_ident[member_id] = key
    return ident_to_id, id_to_ident


def fetch_ticket(ticket_id: int) -> Dict[str, Any]:
    ticket = cw_get(f"service/tickets/{ticket_id}")
    if not isinstance(ticket, dict):
        raise RuntimeError("Ticket lookup did not return an object.")
    return ticket


def fetch_overlapping_schedule_entries_by_member_id(
    member_id: int,
    start_dt: datetime,
    end_dt: datetime,
) -> List[Dict[str, Any]]:
    conditions = (
        f"member/id={member_id} AND "
        f"dateStart < [{iso_z(end_dt)}] AND "
        f"dateEnd > [{iso_z(start_dt)}]"
    )
    data = cw_get(
        "schedule/entries",
        params={
            "conditions": conditions,
            "pageSize": 1000,
            "page": 1,
        },
    )
    return data if isinstance(data, list) else []


def resolve_technician(
    tech_value: Optional[str],
    member_id_value: Optional[int],
    mappings_path: str,
) -> Tuple[int, str]:
    if member_id_value is not None:
        return int(member_id_value), str(member_id_value)

    if not tech_value:
        raise ValueError("You must provide either --tech or --member-id.")

    mappings = load_mappings(mappings_path)
    ident_to_id, _ = build_member_maps(mappings)

    resolved = ident_to_id.get(tech_value.strip().lower())
    if resolved is None:
        valid = sorted((mappings.get("members") or {}).keys())
        preview = ", ".join(valid[:25])
        raise ValueError(
            f"Technician '{tech_value}' was not found in mappings.json.\n"
            f"Use one of the mapped identifiers/names, or use --member-id directly.\n"
            f"Sample mapped values: {preview}"
        )

    return resolved, tech_value.strip()


def build_schedule_payload(
    *,
    ticket_id: int,
    ticket_summary: str,
    member_id: int,
    start_dt_local: datetime,
    end_dt_local: datetime,
    schedule_type_id: int,
    schedule_status_id: int,
    where_id: Optional[int],
    label: str,
    entry_name: Optional[str],
    extra_notes: Optional[str],
) -> Dict[str, Any]:
    base_name = entry_name.strip() if entry_name else f"Ticket {ticket_id}: {ticket_summary}".strip()
    if len(base_name) > 80:
        base_name = base_name[:77] + "..."

    payload: Dict[str, Any] = {
        "objectId": int(ticket_id),
        "type": {"id": int(schedule_type_id)},
        "member": {"id": int(member_id)},
        "dateStart": iso_z(start_dt_local),
        "dateEnd": iso_z(end_dt_local),
        "status": {"id": int(schedule_status_id)},
        "name": base_name,
    }

    if where_id is not None:
        payload["where"] = {"id": int(where_id)}

    if extra_notes:
        payload["notes"] = extra_notes

    return payload


def format_overlap_summary(entries: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for entry in entries[:10]:
        entry_id = safe_int(entry.get("id"), 0)
        object_id = safe_int(entry.get("objectId"), 0)
        name = str(entry.get("name") or "").strip()
        start = str(entry.get("dateStart") or "")
        end = str(entry.get("dateEnd") or "")
        lines.append(f"  - schedule #{entry_id} | ticket/object {object_id} | {start} -> {end} | {name}")
    if len(entries) > 10:
        lines.append(f"  ... plus {len(entries) - 10} more overlap(s)")
    return "\n".join(lines)


def require_env() -> None:
    missing = []
    for key in ["CWM_SITE", "CWM_COMPANY_ID", "CWM_PUBLIC_KEY", "CWM_PRIVATE_KEY"]:
        if not (os.getenv(key) or "").strip():
            missing.append(key)
    if missing:
        raise SystemExit(
            "Missing required environment variables: " + ", ".join(missing) +
            "\nAlso recommended: CLIENT_ID"
        )


# ============================================================
# CLI
# ============================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a manual schedule entry for a chosen ConnectWise ticket and technician."
    )
    parser.add_argument("--ticket", type=int, required=True, help="ConnectWise ticket number / id")
    parser.add_argument("--tech", help="Technician identifier or mapped name from mappings.json")
    parser.add_argument("--member-id", type=int, help="Technician member ID (bypasses mappings lookup)")
    parser.add_argument("--date", required=True, help="Date in YYYY-MM-DD format")
    parser.add_argument("--start", required=True, help="Start time in HH:MM 24-hour format")
    parser.add_argument("--end", required=True, help="End time in HH:MM 24-hour format")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE, help=f"IANA timezone name (default: {DEFAULT_TIMEZONE})")
    parser.add_argument("--mappings-path", default=DEFAULT_MAPPINGS_PATH, help=f"Path to mappings.json (default: {DEFAULT_MAPPINGS_PATH})")
    parser.add_argument("--schedule-type-id", type=int, default=DEFAULT_SCHEDULE_TYPE_ID, help=f"Schedule type id (default: {DEFAULT_SCHEDULE_TYPE_ID})")
    parser.add_argument("--status-id", type=int, default=DEFAULT_SCHEDULE_STATUS_ID, help=f"Schedule status id (default: {DEFAULT_SCHEDULE_STATUS_ID})")
    parser.add_argument("--where-id", type=int, default=(int(DEFAULT_WHERE_ID) if DEFAULT_WHERE_ID is not None else None), help="Optional service location / where id")
    parser.add_argument("--name", help="Optional custom schedule entry name")
    parser.add_argument("--notes", help="Optional notes for the schedule entry")
    parser.add_argument("--allow-overlap", action="store_true", help="Attempt creation even if the technician already has an overlapping schedule entry")
    parser.add_argument("--commit", action="store_true", help="Actually create the schedule entry. Without this, the script only previews the payload.")
    return parser


def main() -> None:
    load_dotenv(find_dotenv())
    require_env()
    parser = build_parser()
    args = parser.parse_args()

    member_id, tech_label = resolve_technician(args.tech, args.member_id, args.mappings_path)

    start_dt_local = parse_date_time_local(args.date, args.start, args.timezone)
    end_dt_local = parse_date_time_local(args.date, args.end, args.timezone)
    if end_dt_local <= start_dt_local:
        raise SystemExit("End time must be later than start time.")

    ticket = fetch_ticket(args.ticket)
    ticket_summary = str(ticket.get("summary") or "").strip()

    overlaps = fetch_overlapping_schedule_entries_by_member_id(member_id, start_dt_local, end_dt_local)
    if overlaps and not args.allow_overlap:
        print("============================================================")
        print("Schedule NOT created because overlaps were found.")
        print(f"Technician:  {tech_label} (member id {member_id})")
        print(f"Ticket:      {args.ticket}")
        print(f"Time slot:   {args.date} {args.start} -> {args.end} ({args.timezone})")
        print(f"Overlaps:    {len(overlaps)}")
        print(format_overlap_summary(overlaps))
        print("\nRe-run with --allow-overlap if you want the script to try anyway.")
        print("============================================================")
        return

    payload = build_schedule_payload(
        ticket_id=args.ticket,
        ticket_summary=ticket_summary,
        member_id=member_id,
        start_dt_local=start_dt_local,
        end_dt_local=end_dt_local,
        schedule_type_id=args.schedule_type_id,
        schedule_status_id=args.status_id,
        where_id=args.where_id,
        label=tech_label,
        entry_name=args.name,
        extra_notes=args.notes,
    )

    print("============================================================")
    print("Manual Ticket Scheduler")
    print(f"Technician:      {tech_label} (member id {member_id})")
    print(f"Ticket:          {args.ticket}")
    print(f"Ticket Summary:  {ticket_summary}")
    print(f"Local Window:    {args.date} {args.start} -> {args.end} ({args.timezone})")
    print(f"UTC Window:      {payload['dateStart']} -> {payload['dateEnd']}")
    print(f"Commit Mode:     {args.commit}")
    print("Payload:")
    print(json.dumps(payload, indent=2))
    print("============================================================")

    if not args.commit:
        print("Preview only. Add --commit to actually create the schedule entry.")
        return

    created = cw_post("schedule/entries", payload)
    if isinstance(created, dict) and created.get("_http_status") in (400, 409):
        print("Create failed.")
        print(json.dumps(created, indent=2))
        raise SystemExit(1)

    created_id = safe_int(created.get("id"), 0)
    print(f"Schedule entry created successfully. New schedule entry id: {created_id}")


if __name__ == "__main__":
    main()
