"""
CW Ticket Report Generator (HTML -> PDF)
- Reads raw ConnectWise ticket export JSON
- Applies filters
- Excludes assignees: Unassigned, APIBot, supportdesk (case/format tolerant)
- Redistributes their ticket counts evenly across remaining techs
- Generates charts as PNGs
- Writes a styled HTML report (easy to tweak spacing, fonts, widths)
- Convert HTML -> PDF using your preferred tool (Chrome print, wkhtmltopdf, etc.)

Dependencies:
  pip install matplotlib
"""

from __future__ import annotations

import os
import json
import math
import statistics
import re
import smtplib
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from dotenv import load_dotenv, find_dotenv
import matplotlib.pyplot as plt
import shutil
import subprocess

# =============================================================================
# PATH CONFIG (hardcode here, nowhere else)
# =============================================================================
load_dotenv(r"C:\APIscripts\.env")
REPORTS_DIR = Path(r"C:\APIscripts\Reporting Scripts")
RAW_DATA_DIR = REPORTS_DIR / "Reporting Data"
REPORT_OUTPUT_DIR = REPORTS_DIR / "Ticket Reports"
MAPPINGS_JSON_PATH = Path(r"C:\APIscripts\mappings.json")
CW_COMPANIES_JSON_PATH = Path(r"C:\APIscripts\cw_companies.json")
ASSETS_DIR = REPORTS_DIR / "Reporting Data" / "_report_assets"

# --- Auto JSON/report naming (recommended) ---
AUTO_USE_SAME_DAY_JSON = False
AUTO_FALLBACK_TO_LATEST_JSON = True  # if today's file is missing
RAW_JSON_PREFIX = "raw_data_"
RAW_JSON_DATE_FMT = "%Y%m%d"       # matches extractor after naming change

# File naming for this company-specific report script
REPORT_COMPANY_FILE_STEM = "BLUR"   # used in output filenames
REPORT_COMPANY_DISPLAY_NAME = "BLUR" # used in title text
REPORT_FILENAME_SUFFIX = "Report"
REPORT_FILENAME_INCLUDE_YEAR = False   # retained for compatibility; filename date tokens come from selected ticket-created date range

# Manual fallback path (used only if AUTO_USE_SAME_DAY_JSON = False)
RAW_JSON_PATH = REPORTS_DIR / "Reporting Data" / "blur_tickets_all.json"

# Output path mode toggle
AUTO_OUTPUT_NAMING_ENABLED = False  # True => auto-build output filename/path, False => use manual OUTPUT_*_PATH below exactly as written

# Manual output paths (used when AUTO_OUTPUT_NAMING_ENABLED = False)
OUTPUT_PDF_PATH = REPORTS_DIR / "Misc Reports" /"BLUR_past_3_months_no_alerts.pdf"
OUTPUT_HTML_PATH = REPORTS_DIR / "Misc Reports" /"BLUR_past_3_months_no_alerts.html"

# Email
EMAIL_RECIPIENTS: List[str] = [
    "akloss@core12tech.com",
    "martin@core12tech.com",
    #"robert@sobo.ai",
    "sspencer@core12tech.com"
]
EMAIL_CC: List[str] = []
EMAIL_BCC: List[str] = []

# Toggle email sending
SEND_EMAIL = False   # True => send email, False => generate report only
DISABLE_EMAIL_ON_WEEKENDS = False  # True => force no email on Sat/Sun

# =============================================================================
# REPORT TEXT + BRANDING
# =============================================================================

REPORT_TITLE = "BLUR Ticket Statistics"  # auto-overridden in main()
REPORT_SUBTITLE = "Close-touch merge correction is enabled for close-time reporting."
REPORT_FOOTER_LEFT = "Core12"
REPORT_FOOTER_RIGHT = "Generated Using Core12's Internal API"

REPORT_LOGO_PATH = REPORTS_DIR / "Core12 Logo.png"

# Section headings
H_KPIS = "Statistics Overview"
H_TRENDS = ""
H_BREAKDOWNS = ""

# KPI label text
LBL_TOTAL_TICKETS = "Total Tickets"
LBL_TOTAL_CLOSED = "Closed Tickets"
LBL_AVG_FIRST_CONTACT = "Avg Time to First Contact"
LBL_AVG_CLOSE = "Avg Time to Close"
LBL_MED_CLOSE = "Median Time to Close"
LBL_MED_FIRST_CONTACT = "Median Time to First Contact"

# Time display
SHOW_TIME_UNITS = "auto"  # "auto" | "hours" | "days"

# =============================================================================
# COMPANY ALIASES (used for canonical matching + filters)
# =============================================================================

COMPANY_ALIASES: Dict[str, List[str]] = {
    "A3 Architecture": ["A3", "janderson@a3-architecture.com"],
    "Ajay SQM Group": ["Ajay", "ajay", "AJAY", "SQM", "beau.routh@ajay-sqm.com"],
    "Allergy and Asthma ": ["Allergy", "Asthma"],
    "Andrew Akard Architecture": ["Andrew Akard", "andy.akard@akardarchitecture.com"],
    "Atkins Park Restaurant": ["Atkins Park", "ehowell@atkinspark.com"],
    "BLUR Workshop": ["blur", "blurworkshop", "blur workshop", "blurworkshop.com", "jaredd@blurworkshop.com"],
    "Business Environments": ["Business Environments", "chettler@becusacorp.com"],
    "CFC Group": ["CFC", "tclose@cfcgroupinc.com"],
    "Commodity Cables": ["commoditycables", "commodity cable", "commoditycables.com"],
    "Gather Grills": ["Gather Grills", "jed@strangefarms.com"],
    "HPD Consulting Engineers": ["HPD", "kmaddox@hpdengineers.com"],
    "Legacy Golf Links": ["Legacy Golf", "LGL", "EZsuite", "robert.sabat@legacyfoxcreek.com"],
    "Mann Mechanical": ["Mann Mechanical", "gthomas@mannmechanical.com"],
    "Milestone Dentistry": ["Milestone Dentistry", "docwake@milestone-dentistry.com"],
    "Museum of Design Atlanta": ["Museum of Design", "MODA", "lflusche@museumofdesign.org"],
    "Purisolve": ["Purisolve", "wallace.jones@purisolve.com"],
    "Providence Baptist Church": ["Providence Baptist Church", "Providence Church", "Providence Baptist", "agenus@providencebc.com"],
    "Savant Engineering": ["Savant Engineering", "Savant", "janderson@savanteng.com"],
    "Shepherd Harvey": ["Shepherd Harvey", "sshepherd@shepharv.com"],
    "Sizemore Group": ["Sizemore", "angelitaa@sizemoregroup.com", "monicap@sizemoregroup.com"],
    "St Bartholomews Episcopal": ["St Bartholomews", "Bartholomews Episcopal", "barry@barrybynum.com"],
    "The Church at Chapel Hill": ["Chapel Hill", "andy.odonnell@chapelhill.cc"],
    "Whitaker Company": ["Whitaker", "daniel.craven@whitaker.company"],
    "Willmer Engineering, Inc.": ["Willmer", "jcwillmer@willmerengineering.com"],
}

# =============================================================================
# CLOSE / REOPEN NORMALIZATION (toggleable)
# =============================================================================

NORMALIZE_AUDIT_CLOSE_HISTORY = True

# Closed -> Closed audit touches are usually administrative noise and should not
# create a new close cycle.
IGNORE_DUPLICATE_CLOSED_TO_CLOSED_EVENTS = True

# Extra guardrail for merge/cleanup edits performed by CW Consultant.
IGNORE_CW_CONSULTANT_CLOSE_TOUCHES = False
CW_CONSULTANT_NAME_MATCHES: List[str] = ["CW Consultant"]
CW_CONSULTANT_IDENTIFIER_MATCHES: List[str] = ["cwconsultant"]
CW_CONSULTANT_MEMBER_ID_OVERRIDE: Optional[int] = None

# Real reopen handling:
# False => use entered -> final real close.
# True  => sum only the periods where the ticket was actually open across
#          real reopen cycles, then use that total as the close duration.
SUM_ONLY_TRUE_OPEN_PERIODS_FOR_REAL_REOPENS = True

# Status names treated as terminal/closed when reconstructing audit history.
CLOSED_STATUS_NAME_MATCHES: List[str] = ["Closed", "Completed"]

# Legacy blunt workaround. Leave off unless you explicitly want the old
# month/threshold exclusion rule in addition to the audit normalization.
LEGACY_EXCLUDE_LONG_CLOSES_ENABLED = False
LEGACY_EXCLUDE_LONG_CLOSES_MONTH: Optional[int] = 11
LEGACY_EXCLUDE_LONG_CLOSES_OVER_DAYS: Optional[float] = 50.0

DEBUG_PRINT_CLOSE_RECONSTRUCTION = False

# =============================================================================
# FILTER CONFIG (filters work; NOT displayed in report)
# =============================================================================

@dataclass
class Filters:
    date_entered_after_utc: Optional[str] = "2026-01-01T00:00:00Z"
    date_entered_before_utc: Optional[str] = None

    min_age_days: Optional[float] = None
    max_age_days: Optional[float] = None

    # Optional legacy exclusion rule:
    # exclude tickets from the report if they were CLOSED in this month number
    # (1=Jan ... 11=Nov ... 12=Dec) AND their time-to-close exceeds the
    # threshold below. Set either field to None to disable the rule.
    exclude_if_closed_in_month: Optional[int] = (
        LEGACY_EXCLUDE_LONG_CLOSES_MONTH if LEGACY_EXCLUDE_LONG_CLOSES_ENABLED else None
    )
    exclude_if_closed_in_month_over_days: Optional[float] = (
        LEGACY_EXCLUDE_LONG_CLOSES_OVER_DAYS if LEGACY_EXCLUDE_LONG_CLOSES_ENABLED else None
    )

    company_include: List[str] = field(default_factory=list)
    company_exclude: List[str] = field(default_factory=list)

    assignee_include: List[str] = field(default_factory=list)
    assignee_exclude: List[str] = field(default_factory=list)

    board_include: List[str] = field(default_factory=list)
    board_exclude: List[str] = field(default_factory=lambda: ["Alerts"])

    status_include: List[str] = field(default_factory=list)
    status_exclude: List[str] = field(default_factory=list)

    priority_include: List[str] = field(default_factory=list)
    priority_exclude: List[str] = field(default_factory=list)

    summary_contains_any: List[str] = field(default_factory=list)
    source_include: List[str] = field(default_factory=list)
    team_include: List[str] = field(default_factory=list)

FILTERS = Filters(
    company_include=["BLUR Workshop"],
)

# =============================================================================
# VISUAL THEME (for charts) + HTML chart container backgrounds
# =============================================================================

CHART_TEXT_COLOR   = "#0B1220"
CHART_TITLE_COLOR  = "#0B1220"
CHART_BORDER_COLOR = "#152246"
CHART_BORDER_WIDTH = 5

CHART_BG_PIE_BOARDS     = "#79CAEB"
CHART_BG_PIE_STATUS     = "#79CAEB"
CHART_BG_PIE_PRIORITY   = "#79CAEB"
CHART_BG_BAR_ASSIGNEE   = "#79CAEB"
CHART_BG_CLOSE_DIST     = "#65b4fd"

CHART_OUTER_RING_COLOR  = "#9CABB8"

PIE_SMALL_SLICE_PCT = 4.0
DIST_SMOOTH_WINDOW = 5
DIST_DENSIFY_POINTS = 8
PIE_TOP_N = 8

TREND_DAYS = 20
CLOSE_DIST_BINS = 30
CLOSE_DIST_MAX_DAYS: Optional[float] = 9.0
CLOSE_DIST_BAR_COLOR = "#152246"
#CLOSE_DIST_DENSITY_LINE_COLOR = "#B444F5"
#CLOSE_DIST_COUNT_POINTS_LINE_COLOR = "#6B7280"
CLOSE_DIST_COUNT_SMOOTH_LINE_COLOR = "#E9E9E9"

TECH_TABLE_TOP_N = 15
TECH_TABLE_INCLUDE_OTHER_PRIORITY = False

# =============================================================================
# Helpers
# =============================================================================

def iso_to_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    s = s.strip()
    try:
        # Normalise the Z suffix and trim sub-second precision to 6 digits
        # so fromisoformat works on Python < 3.11 (which rejects 7-digit
        # fractional seconds, the format ConnectWise sometimes returns).
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        # Truncate fractional seconds beyond 6 digits: e.g. ".0000000" → ".000000"
        s = re.sub(r'(\.\d{6})\d+', r'\1', s)
        dt = datetime.fromisoformat(s)
        # If CW returned a naive datetime (no timezone), assume UTC so that
        # comparisons with aware datetimes don't raise TypeError.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def resolve_raw_json_path(
    reports_dir: Path,
    run_local: datetime,
    prefer_today: bool = True,
    fallback_to_latest: bool = True,
) -> Path:
    """
    Resolve the raw JSON export filename.
    Preferred naming scheme: raw_data_YYYYMMDD.json
    Backward-compatible fallback: raw_data_YYYYMMDD_HHMMSS.json
    """
    reports_dir.mkdir(parents=True, exist_ok=True)
    ymd = run_local.strftime("%Y%m%d")

    exact_new = reports_dir / f"raw_data_{ymd}.json"
    if prefer_today and exact_new.exists():
        return exact_new

    if prefer_today:
        same_day_candidates = sorted(reports_dir.glob(f"raw_data_{ymd}*.json"))
        if same_day_candidates:
            return same_day_candidates[-1]

    if fallback_to_latest:
        all_candidates = sorted(reports_dir.glob("raw_data_*.json"))
        if all_candidates:
            return all_candidates[-1]

    return exact_new

def _fmt_date_label(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    return dt.strftime("%b %d, %Y")

def get_report_period_labels(filters: Filters) -> Tuple[str, str]:
    """
    Return (human_label, filename_token) based on the selected ticket-created date range.
    """
    after_dt = iso_to_dt(filters.date_entered_after_utc)
    before_dt = iso_to_dt(filters.date_entered_before_utc)

    after_label = _fmt_date_label(after_dt)
    before_label = _fmt_date_label(before_dt)

    after_token = after_dt.strftime("%Y%m%d") if after_dt else None
    before_token = before_dt.strftime("%Y%m%d") if before_dt else None

    if after_label and before_label:
        return f"Tickets Created {after_label} to {before_label}", f"{after_token}_to_{before_token}"
    if after_label:
        return f"Tickets Created On or After {after_label}", f"{after_token}_forward"
    if before_label:
        return f"Tickets Created On or Before {before_label}", f"through_{before_token}"
    return "All Ticket Data in Export", "all_dates"

def build_output_report_paths(
    reports_dir: Path,
    company_file_stem: str,
    filters: Filters,
    include_year: bool = False,
) -> Tuple[Path, Path]:
    _human_label, period_token = get_report_period_labels(filters)
    safe_stem = re.sub(r"[^A-Za-z0-9_-]+", "_", (company_file_stem or "Report")).strip("_") or "Report"
    base = f"{safe_stem}_{period_token}_Report"
    return reports_dir / f"{base}.html", reports_dir / f"{base}.pdf"

def norm(s: str) -> str:
    return "".join(ch.lower() for ch in (s or "").strip())

def safe_get(d: Dict[str, Any], *keys: str) -> Any:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur

def parse_int(x: Any) -> Optional[int]:
    try:
        return int(x)
    except Exception:
        return None

def parse_str(x: Any) -> str:
    return "" if x is None else str(x)

def match_any_loose(value: str, patterns: Iterable[str]) -> bool:
    v = norm(value)
    if not v:
        return False
    for p in patterns:
        p2 = norm(p)
        if p2 and p2 in v:
            return True
    return False

_RE_TO_CLOSED = re.compile(
    r'Status has been updated from\s+"[^"]+"\s+to\s+"Closed"\.?', re.IGNORECASE
)
_RE_STATUS_TRANSITION = re.compile(
    r'Status has been updated from\s+"(?P<from>[^"]*)"\s+to\s+"(?P<to>[^"]*)"\.?',
    re.IGNORECASE,
)

@dataclass
class CloseHistoryResult:
    selected_close_time: Optional[datetime]
    effective_close_for_duration: Optional[datetime]
    total_open_seconds: Optional[float]
    saw_real_reopen: bool


def _norm_token(s: Any) -> str:
    return "".join(ch for ch in str(s or "").lower() if ch.isalnum())


def _status_is_closed(status_name: str) -> bool:
    status_norm = norm(parse_str(status_name))
    if not status_norm:
        return False
    return any(norm(name) == status_norm for name in CLOSED_STATUS_NAME_MATCHES)


def _extract_possible_ints(node: Any) -> List[int]:
    out: List[int] = []
    if isinstance(node, int):
        out.append(int(node))
    elif isinstance(node, str) and node.strip().isdigit():
        out.append(int(node.strip()))
    elif isinstance(node, dict):
        for value in node.values():
            out.extend(_extract_possible_ints(value))
    elif isinstance(node, list):
        for value in node:
            out.extend(_extract_possible_ints(value))
    return out


def resolve_cw_consultant_member_ids(mappings: Dict[str, Any]) -> set[int]:
    target_tokens = {_norm_token(v) for v in (CW_CONSULTANT_NAME_MATCHES + CW_CONSULTANT_IDENTIFIER_MATCHES) if parse_str(v).strip()}
    found: set[int] = set()

    override_id = parse_int(CW_CONSULTANT_MEMBER_ID_OVERRIDE)
    if override_id is not None:
        found.add(override_id)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            candidate_fields = [
                parse_str(node.get("name")),
                parse_str(node.get("identifier")),
                parse_str(node.get("memberName")),
                parse_str(node.get("memberIdentifier")),
                parse_str(node.get("displayName")),
                parse_str(node.get("enteredBy")),
                parse_str(node.get("updatedBy")),
            ]
            if any(_norm_token(v) in target_tokens for v in candidate_fields if v):
                for key in ("id", "memberId", "member_id", "memberRecId", "recId", "value"):
                    candidate_id = parse_int(node.get(key))
                    if candidate_id is not None:
                        found.add(candidate_id)
                for child in node.values():
                    for candidate_id in _extract_possible_ints(child):
                        found.add(candidate_id)

            for key, value in node.items():
                if _norm_token(key) in target_tokens:
                    direct_id = parse_int(value)
                    if direct_id is not None:
                        found.add(direct_id)
                    else:
                        for candidate_id in _extract_possible_ints(value):
                            found.add(candidate_id)
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(mappings or {})
    return found


def _audit_actor_matches_cw_consultant(audit_entry: Dict[str, Any], cw_consultant_member_ids: Optional[set[int]]) -> bool:
    entered_by = parse_str(audit_entry.get("enteredBy"))
    if match_any_loose(entered_by, CW_CONSULTANT_NAME_MATCHES + CW_CONSULTANT_IDENTIFIER_MATCHES):
        return True

    if cw_consultant_member_ids:
        for key in ("memberId", "member_id", "enteredById", "id"):
            candidate_id = parse_int(audit_entry.get(key))
            if candidate_id is not None and candidate_id in cw_consultant_member_ids:
                return True
    return False


def _ticket_has_merge_admin_signal(ticket_item: Dict[str, Any], cw_consultant_member_ids: Optional[set[int]]) -> bool:
    t = ticket_item.get("ticket") or {}
    info = t.get("_info") or {}
    if bool(t.get("hasMergedChildTicketFlag")):
        return True

    updated_by = parse_str(info.get("updatedBy"))
    if match_any_loose(updated_by, CW_CONSULTANT_NAME_MATCHES + CW_CONSULTANT_IDENTIFIER_MATCHES):
        return True

    updated_by_id = parse_int(info.get("updatedById"))
    if updated_by_id is not None and cw_consultant_member_ids and updated_by_id in cw_consultant_member_ids:
        return True

    return False


def reconstruct_ticket_close_history(
    ticket_item: Dict[str, Any],
    *,
    date_entered: Optional[datetime],
    closed_flag: bool,
    fallback_closed_time: Optional[datetime],
    cw_consultant_member_ids: Optional[set[int]] = None,
) -> CloseHistoryResult:
    if not NORMALIZE_AUDIT_CLOSE_HISTORY:
        return CloseHistoryResult(
            selected_close_time=fallback_closed_time,
            effective_close_for_duration=fallback_closed_time,
            total_open_seconds=((fallback_closed_time - date_entered).total_seconds() if fallback_closed_time and date_entered else None),
            saw_real_reopen=False,
        )

    trail = ticket_item.get("auditTrail")
    if not isinstance(trail, list):
        return CloseHistoryResult(
            selected_close_time=fallback_closed_time,
            effective_close_for_duration=fallback_closed_time,
            total_open_seconds=((fallback_closed_time - date_entered).total_seconds() if fallback_closed_time and date_entered else None),
            saw_real_reopen=False,
        )

    status_events: List[Tuple[datetime, str, str, Dict[str, Any]]] = []
    for a in trail:
        if not isinstance(a, dict):
            continue
        text = parse_str(a.get("text"))
        if not text:
            continue
        m = _RE_STATUS_TRANSITION.search(text)
        if not m:
            continue
        dt = iso_to_dt(parse_str(a.get("enteredDate")))
        if not dt:
            continue
        status_events.append((dt, parse_str(m.group("from")).strip(), parse_str(m.group("to")).strip(), a))

    status_events.sort(key=lambda x: x[0])
    if not status_events:
        return CloseHistoryResult(
            selected_close_time=fallback_closed_time,
            effective_close_for_duration=fallback_closed_time,
            total_open_seconds=((fallback_closed_time - date_entered).total_seconds() if fallback_closed_time and date_entered else None),
            saw_real_reopen=False,
        )

    merge_admin_signal = _ticket_has_merge_admin_signal(ticket_item, cw_consultant_member_ids)
    current_open_start = date_entered
    total_open_seconds = 0.0
    last_real_close: Optional[datetime] = None
    saw_real_reopen = False

    for dt, from_status, to_status, audit_entry in status_events:
        from_closed = _status_is_closed(from_status)
        to_closed = _status_is_closed(to_status)
        actor_is_consultant = _audit_actor_matches_cw_consultant(audit_entry, cw_consultant_member_ids)

        if IGNORE_DUPLICATE_CLOSED_TO_CLOSED_EVENTS and from_closed and to_closed:
            if DEBUG_PRINT_CLOSE_RECONSTRUCTION:
                print(f"[close-audit] ticket={parse_int((ticket_item.get('ticket') or {}).get('id'))} ignoring closed->closed at {dt.isoformat()} by {parse_str(audit_entry.get('enteredBy'))}")
            continue

        if (
            IGNORE_CW_CONSULTANT_CLOSE_TOUCHES
            and actor_is_consultant
            and merge_admin_signal
            and last_real_close is not None
            and current_open_start is None
            and to_closed
        ):
            if DEBUG_PRINT_CLOSE_RECONSTRUCTION:
                print(f"[close-audit] ticket={parse_int((ticket_item.get('ticket') or {}).get('id'))} ignoring consultant close-touch at {dt.isoformat()} from '{from_status}' to '{to_status}'")
            continue

        if to_closed and not from_closed:
            if current_open_start is not None and dt >= current_open_start:
                total_open_seconds += (dt - current_open_start).total_seconds()
            elif last_real_close is not None and actor_is_consultant and merge_admin_signal and IGNORE_CW_CONSULTANT_CLOSE_TOUCHES:
                if DEBUG_PRINT_CLOSE_RECONSTRUCTION:
                    print(f"[close-audit] ticket={parse_int((ticket_item.get('ticket') or {}).get('id'))} ignoring consultant close without reopen at {dt.isoformat()}")
                continue

            last_real_close = dt
            current_open_start = None
            continue

        if from_closed and not to_closed:
            if last_real_close is not None:
                saw_real_reopen = True
                if current_open_start is None:
                    current_open_start = dt
            continue

    selected_close_time = last_real_close if closed_flag else None
    if selected_close_time is None and closed_flag:
        selected_close_time = fallback_closed_time

    total_open_seconds_out: Optional[float] = None
    effective_close_for_duration = selected_close_time
    if selected_close_time is not None and date_entered is not None:
        if SUM_ONLY_TRUE_OPEN_PERIODS_FOR_REAL_REOPENS and saw_real_reopen:
            total_open_seconds_out = max(total_open_seconds, 0.0)
            effective_close_for_duration = date_entered + timedelta(seconds=total_open_seconds_out)
        else:
            total_open_seconds_out = max((selected_close_time - date_entered).total_seconds(), 0.0)
            effective_close_for_duration = selected_close_time

    return CloseHistoryResult(
        selected_close_time=selected_close_time,
        effective_close_for_duration=effective_close_for_duration,
        total_open_seconds=total_open_seconds_out,
        saw_real_reopen=saw_real_reopen,
    )


def find_closed_audit_time(
    ticket_item: Dict[str, Any],
    *,
    date_entered: Optional[datetime] = None,
    closed_flag: bool = True,
    fallback_closed_time: Optional[datetime] = None,
    cw_consultant_member_ids: Optional[set[int]] = None,
) -> Optional[datetime]:
    return reconstruct_ticket_close_history(
        ticket_item,
        date_entered=date_entered,
        closed_flag=closed_flag,
        fallback_closed_time=fallback_closed_time,
        cw_consultant_member_ids=cw_consultant_member_ids,
    ).selected_close_time

def _debug_duration_stats(name: str, xs: List[float]) -> None:
    if not xs:
        print(f"[DEBUG] {name}: empty")
        return
    zeros = sum(1 for v in xs if v == 0)
    print(
        f"[DEBUG] {name}: n={len(xs)}, zeros={zeros}, "
        f"min={min(xs):.0f}s, median={statistics.median(xs):.0f}s, max={max(xs):.0f}s"
    )

def canonical_company_name(company_name: str, company_identifier: str) -> str:
    cn = company_name.strip() if company_name else ""
    ci = company_identifier.strip() if company_identifier else ""

    for canonical, aliases in COMPANY_ALIASES.items():
        if match_any_loose(cn, [canonical]) or match_any_loose(ci, [canonical]):
            return canonical

    hay = " | ".join([cn, ci])
    for canonical, aliases in COMPANY_ALIASES.items():
        if match_any_loose(hay, [canonical] + aliases):
            return canonical

    return cn or ci or "Unknown"

def fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None or math.isnan(seconds) or seconds < 0:
        return "—"
    if SHOW_TIME_UNITS == "hours":
        return f"{seconds/3600:.2f} h"
    if SHOW_TIME_UNITS == "days":
        return f"{seconds/86400:.2f} d"
    if seconds < 3600:
        return f"{seconds/60:.1f} min"
    if seconds < 86400:
        return f"{seconds/3600:.2f} h"
    return f"{seconds/86400:.2f} d"

def ensure_dirs() -> None:
    for p in (RAW_JSON_PATH, MAPPINGS_JSON_PATH, CW_COMPANIES_JSON_PATH):
        if not p.exists():
            raise FileNotFoundError(f"Missing file: {p}")
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_HTML_PATH.parent.mkdir(parents=True, exist_ok=True)

def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

def load_mappings(path: Path) -> Dict[str, Any]:
    try:
        return load_json(path)
    except Exception:
        return {}

def load_cw_companies(path: Path) -> List[Dict[str, Any]]:
    try:
        data = load_json(path)
        return data if isinstance(data, list) else []
    except Exception:
        return []
    
def split_resources_field(resources: str) -> List[str]:
    """
    Best-effort parse of CW 'resources' into individual names/identifiers.
    Handles:
      - "akloss, ASeaver"
      - "akloss; ASeaver"
      - "akloss / ASeaver"
      - stringified lists like "['akloss', 'ASeaver']"
    """
    s = (resources or "").strip()
    if not s:
        return []

    # If it looks like a Python list string, strip brackets/quotes crudely
    # e.g. "['akloss', 'ASeaver']" -> "akloss, ASeaver"
    if (s.startswith("[") and s.endswith("]")):
        s = s[1:-1]
    s = s.replace('"', "").replace("'", "")

    # Split on common delimiters
    parts = re.split(r"[,\n;/|]+", s)
    out = []
    for p in parts:
        p = p.strip()
        if p:
            out.append(p)
    return out


def get_all_assignees(owner_ident: str, resources: str) -> List[str]:
    """
    Return a de-duped list of assignees for a ticket.
    Includes owner_ident (if present) + all resources parsed from resources field.
    """
    names: List[str] = []
    if owner_ident and owner_ident.strip():
        names.append(owner_ident.strip())

    names.extend(split_resources_field(resources))

    # De-dupe while preserving order (case-insensitive)
    seen = set()
    uniq = []
    for n in names:
        key = norm_id(n)
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        uniq.append(n.strip())

    return uniq

# --- CHANGED (must): support Priority 6 in your CW instance ---
def priority_level_1_to_6(priority_name: str, priority_id: Optional[int]) -> Optional[int]:
    s = (priority_name or "").replace("\xa0", " ").strip()
    if not s:
        return None

    m = re.search(r"(?i)\bP\s*([1-6])\b", s)
    if m:
        return int(m.group(1))

    m = re.search(r"(?i)\bPriority\s*([1-6])\b", s)
    if m:
        return int(m.group(1))

    m = re.search(r"\b([1-6])\b", s)
    if m:
        return int(m.group(1))

    return None

def html_to_pdf(html_path: Path, pdf_path: Path) -> bool:
    """
    Convert HTML -> PDF using:
      1) Playwright (Chromium) if available
      2) wkhtmltopdf if available on PATH
    """
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    # --- 1) Playwright (best) ---
    try:
        from playwright.sync_api import sync_playwright  # type: ignore

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(html_path.resolve().as_uri(), wait_until="networkidle")
            page.pdf(
                path=str(pdf_path),
                format="Letter",
                print_background=True,
                margin={"top": "0.7in", "right": "0.65in", "bottom": "0.7in", "left": "0.65in"},
            )
            browser.close()
        return True
    except Exception:
        pass

    # --- 2) wkhtmltopdf fallback ---
    wk = shutil.which("wkhtmltopdf")
    if wk:
        try:
            subprocess.run(
                [wk, "--enable-local-file-access", str(html_path), str(pdf_path)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            return True
        except subprocess.CalledProcessError as e:
            print("wkhtmltopdf failed:\n", e.stderr)

    print("No HTML->PDF converter available.")
    print("Install one of these:")
    print("  Playwright (recommended):  python -m pip install playwright && python -m playwright install chromium")
    print("  OR install wkhtmltopdf and ensure wkhtmltopdf.exe is on PATH.")
    return False

# =============================================================================
# Assignee suppression + redistribution
# =============================================================================

EXCLUDED_ASSIGNEES = {"unassigned", "apibot", "supportdesk", "sjones"}

def norm_id(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())

def is_excluded_assignee(name: str) -> bool:
    return norm_id(name) in EXCLUDED_ASSIGNEES

def distribute_evenly(total: int, keys: List[str]) -> Dict[str, int]:
    if total <= 0 or not keys:
        return {k: 0 for k in keys}
    n = len(keys)
    base = total // n
    rem = total % n
    out = {k: base for k in keys}
    for k in keys[:rem]:
        out[k] += 1
    return out

# =============================================================================
# Data extraction
# =============================================================================

@dataclass
class TicketRow:
    ticket_id: int
    summary: str
    board_name: str
    board_id: Optional[int]
    status_name: str
    status_id: Optional[int]
    priority_name: str
    priority_id: Optional[int]
    priority_level: Optional[int]          # <-- CHANGED (must): carry computed priority level if present
    owner_ident: str
    resources: str
    team_name: str
    source_name: str
    company_name: str
    company_identifier: str
    company_canonical: str
    date_entered: Optional[datetime]
    date_closed: Optional[datetime]
    first_contact: Optional[datetime]
    effective_close: Optional[datetime]
    closed_flag: bool
    saw_real_reopen: bool = False
    total_open_seconds: Optional[float] = None

def extract_ticket_rows(raw: Dict[str, Any], mappings: Optional[Dict[str, Any]] = None) -> List[TicketRow]:
    rows: List[TicketRow] = []
    tickets = raw.get("tickets", [])
    if not isinstance(tickets, list):
        return rows

    cw_consultant_member_ids = resolve_cw_consultant_member_ids(mappings or {})

    for item in tickets:
        if not isinstance(item, dict):
            continue

        t = item.get("ticket", {}) or {}
        comp = t.get("company", {}) or {}
        board = t.get("board", {}) or {}
        status = t.get("status", {}) or {}
        priority = t.get("priority", {}) or {}
        owner = t.get("owner", {}) or {}
        team = t.get("team", {}) or {}
        source = t.get("source", {}) or {}
        computed = item.get("computed", {}) or {}

        ticket_id = parse_int(t.get("id")) or parse_int(computed.get("ticket_id")) or -1
        if ticket_id < 0:
            continue

        company_name = parse_str(comp.get("name"))
        company_identifier = parse_str(comp.get("identifier"))
        company_canon = canonical_company_name(company_name, company_identifier)

        # --- CHANGED (must): prefer computed date_entered_utc if present (your extractor writes it) ---
        date_entered = (
            iso_to_dt(parse_str(computed.get("date_entered_utc")))
            or iso_to_dt(parse_str(safe_get(t, "_info", "dateEntered")))
            or iso_to_dt(parse_str(t.get("dateEntered")))
        )

        fallback_closed_time = (
            iso_to_dt(parse_str(computed.get("date_closed_utc")))
            or iso_to_dt(parse_str(t.get("closedDate")))
            or iso_to_dt(parse_str(safe_get(t, "_info", "dateClosed")))
        )
        ticket_closed_flag_raw = computed.get("closedFlag")
        if ticket_closed_flag_raw is None:
            ticket_closed_flag_raw = t.get("closedFlag")
        closed_flag = bool(ticket_closed_flag_raw)
        if ticket_closed_flag_raw is None:
            closed_flag = _status_is_closed(parse_str(status.get("name"))) or fallback_closed_time is not None

        close_history = reconstruct_ticket_close_history(
            item,
            date_entered=date_entered,
            closed_flag=closed_flag,
            fallback_closed_time=fallback_closed_time,
            cw_consultant_member_ids=cw_consultant_member_ids,
        )

        date_closed = close_history.selected_close_time
        effective_close = close_history.effective_close_for_duration
        first_contact = iso_to_dt(parse_str(computed.get("first_contact_time_utc")))

        # --- CHANGED (must): prefer computed priority level (from extractor), else parse name ---
        plevel = parse_int(computed.get("priority_level_1_6")) or parse_int(computed.get("priority_level_1_5"))
        if plevel is None:
            plevel = priority_level_1_to_6(parse_str(priority.get("name")), parse_int(priority.get("id")))

        rows.append(
            TicketRow(
                ticket_id=ticket_id,
                summary=parse_str(t.get("summary")),
                board_name=parse_str(board.get("name")),
                board_id=parse_int(board.get("id")),
                status_name=parse_str(status.get("name")),
                status_id=parse_int(status.get("id")),
                priority_name=parse_str(priority.get("name")),
                priority_id=parse_int(priority.get("id")),
                priority_level=plevel,
                owner_ident=parse_str(owner.get("identifier")),
                resources=parse_str(t.get("resources")),
                team_name=parse_str(team.get("name")),
                source_name=parse_str(source.get("name")),
                company_name=company_name,
                company_identifier=company_identifier,
                company_canonical=company_canon,
                date_entered=date_entered,
                date_closed=date_closed,
                first_contact=first_contact,
                effective_close=effective_close,
                closed_flag=closed_flag,
                saw_real_reopen=close_history.saw_real_reopen,
                total_open_seconds=close_history.total_open_seconds,
            )
        )

    return rows

def apply_filters(rows: List[TicketRow], filters: Filters, log_fn=None) -> List[TicketRow]:
    def _log(msg: str) -> None:
        if log_fn:
            log_fn(msg)

    if not rows:
        return rows

    after_dt = iso_to_dt(filters.date_entered_after_utc)
    before_dt = iso_to_dt(filters.date_entered_before_utc)
    now = now_utc()

    # Per-step drop counters for diagnostics
    _drop = {
        "date_before": 0, "date_after": 0, "age": 0,
        "close_month": 0, "company": 0, "assignee": 0,
        "board": 0, "status": 0, "priority": 0,
        "summary": 0, "source": 0, "team": 0,
    }

    out: List[TicketRow] = []
    for r in rows:
        if after_dt and (r.date_entered is None or r.date_entered < after_dt):
            _drop["date_before"] += 1
            continue
        if before_dt and (r.date_entered is None or r.date_entered > before_dt):
            _drop["date_after"] += 1
            continue

        if r.date_entered:
            age_days = (now - r.date_entered).total_seconds() / 86400.0
            if filters.min_age_days is not None and age_days < filters.min_age_days:
                _drop["age"] += 1
                continue
            if filters.max_age_days is not None and age_days > filters.max_age_days:
                _drop["age"] += 1
                continue
        else:
            if filters.min_age_days is not None or filters.max_age_days is not None:
                _drop["age"] += 1
                continue

        close_dt = r.date_closed or r.effective_close
        if (
            filters.exclude_if_closed_in_month is not None
            and filters.exclude_if_closed_in_month_over_days is not None
            and r.date_entered is not None
            and close_dt is not None
        ):
            close_month = close_dt.astimezone(timezone.utc).month
            time_to_close_days = (close_dt - r.date_entered).total_seconds() / 86400.0
            if (
                close_month == int(filters.exclude_if_closed_in_month)
                and time_to_close_days > float(filters.exclude_if_closed_in_month_over_days)
            ):
                _drop["close_month"] += 1
                continue

        comp_hay = " | ".join([r.company_canonical, r.company_name, r.company_identifier])
        if filters.company_include and not match_any_loose(comp_hay, filters.company_include):
            _drop["company"] += 1
            continue
        if filters.company_exclude and match_any_loose(comp_hay, filters.company_exclude):
            _drop["company"] += 1
            continue

        ass_hay = " | ".join([r.owner_ident, r.resources])
        if filters.assignee_include and not match_any_loose(ass_hay, filters.assignee_include):
            _drop["assignee"] += 1
            continue
        if filters.assignee_exclude and match_any_loose(ass_hay, filters.assignee_exclude):
            _drop["assignee"] += 1
            continue

        b_hay = " | ".join([r.board_name, str(r.board_id or "")])
        if filters.board_include and not match_any_loose(b_hay, filters.board_include):
            _drop["board"] += 1
            continue
        if filters.board_exclude and match_any_loose(b_hay, filters.board_exclude):
            _drop["board"] += 1
            continue

        s_hay = " | ".join([r.status_name, str(r.status_id or "")])
        if filters.status_include and not match_any_loose(s_hay, filters.status_include):
            _drop["status"] += 1
            continue
        if filters.status_exclude and match_any_loose(s_hay, filters.status_exclude):
            _drop["status"] += 1
            continue

        p_hay = " | ".join([r.priority_name, str(r.priority_id or "")])
        if filters.priority_include and not match_any_loose(p_hay, filters.priority_include):
            _drop["priority"] += 1
            continue
        if filters.priority_exclude and match_any_loose(p_hay, filters.priority_exclude):
            _drop["priority"] += 1
            continue

        if filters.summary_contains_any and not match_any_loose(r.summary, filters.summary_contains_any):
            _drop["summary"] += 1
            continue

        if filters.source_include and not match_any_loose(r.source_name, filters.source_include):
            _drop["source"] += 1
            continue
        if filters.team_include and not match_any_loose(r.team_name, filters.team_include):
            _drop["team"] += 1
            continue

        out.append(r)

    total_dropped = sum(_drop.values())
    if total_dropped:
        _log(f"Filter breakdown (dropped {total_dropped}/{len(rows)}):")
        for step, n in _drop.items():
            if n:
                _log(f"  {step}: -{n}")

    return out

# =============================================================================
# Metrics
# =============================================================================

@dataclass
class Metrics:
    total_tickets: int
    total_closed: int
    t_first_contact_seconds: List[float]
    t_close_seconds: List[float]
    by_board: Dict[str, int]
    by_status: Dict[str, int]
    by_priority: Dict[str, int]
    by_assignee: Dict[str, int]
    by_company: Dict[str, int]
    entered_by_day: Dict[str, int]
    closed_by_day: Dict[str, int]
    tech_priority_counts: Dict[str, Dict[str, int]]

def compute_metrics(rows: List[TicketRow]) -> Metrics:
    total = len(rows)
    closed = 0
    t_first: List[float] = []
    t_close: List[float] = []

    by_board: Dict[str, int] = {}
    by_status: Dict[str, int] = {}
    by_priority: Dict[str, int] = {}
    by_assignee: Dict[str, int] = {}
    by_company: Dict[str, int] = {}

    entered_by_day: Dict[str, int] = {}
    closed_by_day: Dict[str, int] = {}

    tech_priority_counts: Dict[str, Dict[str, int]] = {}

    excluded_pool_total = 0
    # --- CHANGED (must): include p6 bucket so Priority 6 doesn't get thrown into "other" ---
    excluded_pool_by_bucket: Dict[str, int] = {"total": 0, "p1": 0, "p2": 0, "p3": 0, "p4": 0, "p5": 0, "p6": 0, "other": 0}

    for r in rows:
        by_board[r.board_name or "Unknown"] = by_board.get(r.board_name or "Unknown", 0) + 1
        by_status[r.status_name or "Unknown"] = by_status.get(r.status_name or "Unknown", 0) + 1
        by_priority[r.priority_name or "Unknown"] = by_priority.get(r.priority_name or "Unknown", 0) + 1
        by_company[r.company_canonical or "Unknown"] = by_company.get(r.company_canonical or "Unknown", 0) + 1

        if r.date_entered:
            day = r.date_entered.astimezone(timezone.utc).strftime("%Y-%m-%d")
            entered_by_day[day] = entered_by_day.get(day, 0) + 1

        # First contact stats: only include tickets with a computed first_contact (your extractor now sets None if no outbound email)
        if r.date_entered and r.first_contact:
            dt = (r.first_contact - r.date_entered).total_seconds()
            if dt >= 0:
                t_first.append(dt)

        duration_close_dt = r.effective_close or r.date_closed
        if r.date_entered and duration_close_dt:
            dtc = (duration_close_dt - r.date_entered).total_seconds()
            if dtc >= 0:
                t_close.append(dtc)

        final_close_dt = r.date_closed or r.effective_close
        if r.closed_flag or r.date_closed is not None:
            closed += 1
            if final_close_dt:
                day = final_close_dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
                closed_by_day[day] = closed_by_day.get(day, 0) + 1

        assignees = get_all_assignees(r.owner_ident, r.resources)

        # If nothing at all, treat as Unassigned (so it can be excluded/redistributed like before)
        if not assignees:
            assignees = ["Unassigned"]

        # bucket based on priority
        plevel = r.priority_level if r.priority_level is not None else priority_level_1_to_6(r.priority_name, r.priority_id)
        bucket = f"p{plevel}" if plevel in (1, 2, 3, 4, 5, 6) else "other"

        # Give credit to everyone (full credit each)
        for assignee in assignees:
            if is_excluded_assignee(assignee):
                # Put excluded assignee "credit" into the pool to redistribute
                excluded_pool_total += 1
                excluded_pool_by_bucket["total"] += 1
                excluded_pool_by_bucket[bucket] += 1
                continue

            by_assignee[assignee] = by_assignee.get(assignee, 0) + 1

            if assignee not in tech_priority_counts:
                tech_priority_counts[assignee] = {
                    "total": 0, "p1": 0, "p2": 0, "p3": 0, "p4": 0, "p5": 0, "p6": 0, "other": 0
                }

            tech_priority_counts[assignee]["total"] += 1
            tech_priority_counts[assignee][bucket] += 1

    techs = sorted(tech_priority_counts.keys())
    if techs and excluded_pool_total > 0:
        add_map_total = distribute_evenly(excluded_pool_total, techs)
        for tech, add_n in add_map_total.items():
            if add_n:
                by_assignee[tech] = by_assignee.get(tech, 0) + add_n

        # --- CHANGED (must): include p6 redistribution ---
        for bucket_key in ["total", "p1", "p2", "p3", "p4", "p5", "p6", "other"]:
            pool_n = excluded_pool_by_bucket.get(bucket_key, 0)
            if pool_n <= 0:
                continue
            add_map = distribute_evenly(pool_n, techs)
            for tech, add_n in add_map.items():
                if add_n:
                    tech_priority_counts[tech][bucket_key] += add_n

    # final safety purge
    for bad in list(by_assignee.keys()):
        if is_excluded_assignee(bad):
            by_assignee.pop(bad, None)
    for bad in list(tech_priority_counts.keys()):
        if is_excluded_assignee(bad):
            tech_priority_counts.pop(bad, None)

    return Metrics(
        total_tickets=total,
        total_closed=closed,
        t_first_contact_seconds=t_first,
        t_close_seconds=t_close,
        by_board=by_board,
        by_status=by_status,
        by_priority=by_priority,
        by_assignee=by_assignee,
        by_company=by_company,
        entered_by_day=entered_by_day,
        closed_by_day=closed_by_day,
        tech_priority_counts=tech_priority_counts,
    )

# =============================================================================
# Charts (PNG)
# =============================================================================

def _apply_chart_theme(fig, ax, ax_bg: str) -> None:
    fig.patch.set_facecolor(CHART_OUTER_RING_COLOR)
    fig.patch.set_edgecolor(CHART_BORDER_COLOR)
    fig.patch.set_linewidth(CHART_BORDER_WIDTH)

    ax.set_facecolor(ax_bg)
    ax.title.set_color(CHART_TITLE_COLOR)
    ax.tick_params(colors=CHART_TEXT_COLOR)

    for spine in ax.spines.values():
        spine.set_color(CHART_TEXT_COLOR)

def _save_fig(fig, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fig.canvas.draw()
    except Exception:
        pass
    fig.savefig(out_path, dpi=180, facecolor=CHART_OUTER_RING_COLOR, edgecolor=CHART_OUTER_RING_COLOR)
    plt.close(fig)

def _compact_other_bucket(data: Dict[str, int], top_n: int) -> Tuple[List[str], List[int]]:
    items = sorted(data.items(), key=lambda x: x[1], reverse=True)
    head = items[:top_n]
    tail = items[top_n:]
    if tail:
        head.append(("Other", sum(v for _, v in tail)))
    labels = [k for k, _ in head]
    values = [v for _, v in head]
    return labels, values

def save_pie_chart_indexed(
    data: Dict[str, int],
    title: str,
    out_path: Path,
    top_n: int = PIE_TOP_N,
    small_pct: float = PIE_SMALL_SLICE_PCT,
    ax_bg: str = "#CFE3FF",
) -> None:
    if not data:
        return

    labels, values = _compact_other_bucket(data, top_n=top_n)
    total = float(sum(values)) if values else 0.0
    if total <= 0:
        return

    pcts = [(v / total) * 100.0 for v in values]

    display_for_legend: List[str] = []
    idx_counter = 1
    for lab, pct in zip(labels, pcts):
        safe_lab = (lab[:55] + "…") if len(lab) > 56 else lab
        if pct < small_pct:
            tag = f"[{idx_counter}] "
            display_for_legend.append(f"{tag}{safe_lab} ({pct:.0f}%)")
            idx_counter += 1
        else:
            display_for_legend.append(f"{safe_lab} ({pct:.0f}%)")

    fig, ax = plt.subplots(figsize=(7.4, 3.2))
    _apply_chart_theme(fig, ax, ax_bg=ax_bg)

    def autopct_func(pct: float) -> str:
        return f"{pct:.0f}%" if pct >= small_pct else ""

    wedges, _texts, _autotexts = ax.pie(
        values,
        labels=None,
        autopct=autopct_func,
        startangle=90,
        pctdistance=0.72,
    )

    for t in _autotexts:
        t.set_color(CHART_TEXT_COLOR)
        t.set_fontsize(9)

    # Pie title omitted (outer section/panel titles already label this chart)
    ax.axis("equal")

    leg = ax.legend(
        wedges,
        display_for_legend,
        loc="center left",
        bbox_to_anchor=(0.98, 0.5),
        fontsize=8,
        frameon=True,
        borderpad=0.40,
        labelspacing=0.40,
        handlelength=1.35,
        handleheight=0.9,
        handletextpad=0.55,
        borderaxespad=0.25,
    )
    leg.get_frame().set_facecolor(ax_bg)
    leg.get_frame().set_edgecolor(CHART_TEXT_COLOR)

    fig.subplots_adjust(right=0.69)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(
        out_path,
        dpi=180,
        facecolor=CHART_OUTER_RING_COLOR,
        edgecolor=CHART_OUTER_RING_COLOR,
        bbox_inches="tight",
        pad_inches=0.35,
    )
    plt.close(fig)

def save_bar_chart(data: Dict[str, int], title: str, out_path: Path, top_n: int = 10, ax_bg: str = "#CFE3FF") -> None:
    if not data:
        return

    items = sorted(data.items(), key=lambda x: x[1], reverse=True)[:top_n]
    labels = [k for k, _ in items][::-1]
    values = [v for _, v in items][::-1]

    fig, ax = plt.subplots(figsize=(6.3, 3.6))
    _apply_chart_theme(fig, ax, ax_bg=ax_bg)

    ax.barh(labels, values)
    # Pie title omitted (outer section/panel titles already label this chart)
    ax.tick_params(axis="y", labelsize=9, colors=CHART_TEXT_COLOR)
    ax.tick_params(axis="x", labelsize=9, colors=CHART_TEXT_COLOR)

    fig.tight_layout(pad=1.2)
    _save_fig(fig, out_path)

def save_close_time_density_vs_counts(
    close_seconds: List[float],
    title: str,
    out_path: Path,
    bins: int = CLOSE_DIST_BINS,
    max_days: Optional[float] = CLOSE_DIST_MAX_DAYS,
    ax_bg: str = "#CFE3FF",
) -> None:
    vals_days: List[float] = []
    for s in close_seconds:
        try:
            if s is None or s < 0:
                continue
            d = float(s) / 86400.0
            if max_days is not None:
                d = min(d, float(max_days))
            vals_days.append(d)
        except Exception:
            continue

    if not vals_days:
        return

    lo = 0.0
    hi = max(vals_days)
    if hi <= lo:
        hi = lo + 1.0

    bins = max(8, int(bins))
    bin_w = (hi - lo) / bins
    if bin_w <= 0:
        bin_w = 1.0

    edges = [lo + i * bin_w for i in range(bins + 1)]
    counts = [0] * bins
    for v in vals_days:
        idx = int((v - lo) / bin_w)
        idx = max(0, min(bins - 1, idx))
        counts[idx] += 1

    n = float(len(vals_days))
    density = [(c / (n * bin_w)) if n > 0 else 0.0 for c in counts]
    centers = [0.5 * (edges[i] + edges[i + 1]) for i in range(bins)]

    # --- smoothing helpers (no extra deps required) ---
    def moving_average(vals: List[float], window: int = 3) -> List[float]:
        if not vals:
            return []
        window = max(1, int(window))
        half = window // 2
        out: List[float] = []
        for i in range(len(vals)):
            a = max(0, i - half)
            b = min(len(vals), i + half + 1)
            chunk = vals[a:b]
            out.append(sum(chunk) / len(chunk))
        return out

    def densify_linear(xs: List[float], ys: List[float], points_per_seg: int = 6) -> Tuple[List[float], List[float]]:
        if len(xs) < 2:
            return xs[:], ys[:]
        xx: List[float] = []
        yy: List[float] = []
        pps = max(2, int(points_per_seg))
        for i in range(len(xs) - 1):
            x0, x1 = xs[i], xs[i + 1]
            y0, y1 = ys[i], ys[i + 1]
            for j in range(pps):
                t = j / float(pps)
                xx.append(x0 + (x1 - x0) * t)
                yy.append(y0 + (y1 - y0) * t)
        xx.append(xs[-1])
        yy.append(ys[-1])
        return xx, yy

    smooth_density = moving_average(density, window=3)
    smooth_counts = moving_average(counts, window=3)

    dense_x_d, dense_y_d = densify_linear(centers, smooth_density, points_per_seg=8)
    dense_x_c, dense_y_c = densify_linear(centers, smooth_counts, points_per_seg=8)

    fig, ax = plt.subplots(figsize=(9.8, 2.8))
    _apply_chart_theme(fig, ax, ax_bg=ax_bg)

    # Bars (density histogram)
    ax.bar(centers, density, width=bin_w * 0.92, alpha=0.70, color=CLOSE_DIST_BAR_COLOR)
    # Smoothed density overlay
    # ax.plot(dense_x_d, dense_y_d, linewidth=1.8, color=CLOSE_DIST_DENSITY_LINE_COLOR)

    ax.set_ylabel("Probability density", color=CHART_TEXT_COLOR, fontsize=15)
    ax.set_xlabel("Time to close (days)", color=CHART_TEXT_COLOR, fontsize=15)

    ax2 = ax.twinx()
    # Original bin counts (light markers so you can still see true bins)
    # ax2.plot(centers, counts, marker="o", linewidth=1.0, alpha=0.45, color=CLOSE_DIST_COUNT_POINTS_LINE_COLOR)
    # Smoothed/denser count curve
    ax2.plot(dense_x_c, dense_y_c, linewidth=2.0, color=CLOSE_DIST_COUNT_SMOOTH_LINE_COLOR)

    ax2.tick_params(colors=CHART_TEXT_COLOR)
    for spine in ax2.spines.values():
        spine.set_color(CHART_TEXT_COLOR)

    ax.grid(True, which="major", axis="y", alpha=0.25)

    fig.tight_layout(pad=1.2)
    _save_fig(fig, out_path)


def save_median_close_distribution_chart(
    values_seconds: List[float],
    title: str,
    x_label: str,
    out_path: Path,
    *,
    bins: int = CLOSE_DIST_BINS,
    max_days: Optional[float] = CLOSE_DIST_MAX_DAYS,
    ax_bg: str = CHART_BG_CLOSE_DIST,
) -> None:
    vals_days: List[float] = []
    for s in values_seconds:
        try:
            if s is None or s < 0:
                continue
            d = float(s) / 86400.0
            if max_days is not None:
                d = min(d, float(max_days))
            vals_days.append(d)
        except Exception:
            continue

    if not vals_days:
        return

    lo = 0.0
    hi = max(vals_days)
    if hi <= lo:
        hi = lo + 1.0

    bins = max(12, int(bins))
    bin_w = (hi - lo) / bins
    if bin_w <= 0:
        bin_w = 1.0

    edges = [lo + i * bin_w for i in range(bins + 1)]
    counts = [0] * bins
    for v in vals_days:
        idx = int((v - lo) / bin_w)
        idx = max(0, min(bins - 1, idx))
        counts[idx] += 1

    n = float(len(vals_days))
    density = [(c / (n * bin_w)) if n > 0 else 0.0 for c in counts]
    centers = [0.5 * (edges[i] + edges[i + 1]) for i in range(bins)]

    def moving_average(vals: List[float], window: int = DIST_SMOOTH_WINDOW) -> List[float]:
        if not vals:
            return []
        window = max(1, int(window))
        half = window // 2
        out: List[float] = []
        for i in range(len(vals)):
            a = max(0, i - half)
            b = min(len(vals), i + half + 1)
            chunk = vals[a:b]
            out.append(sum(chunk) / len(chunk))
        return out

    def densify_linear(xs: List[float], ys: List[float], points_per_seg: int = DIST_DENSIFY_POINTS) -> Tuple[List[float], List[float]]:
        if len(xs) < 2:
            return xs[:], ys[:]
        xx: List[float] = []
        yy: List[float] = []
        pps = max(2, int(points_per_seg))
        for i in range(len(xs) - 1):
            x0, x1 = xs[i], xs[i + 1]
            y0, y1 = ys[i], ys[i + 1]
            for j in range(pps):
                t = j / float(pps)
                xx.append(x0 + (x1 - x0) * t)
                yy.append(y0 + (y1 - y0) * t)
        xx.append(xs[-1])
        yy.append(ys[-1])
        return xx, yy

    smooth_density = moving_average(density, window=DIST_SMOOTH_WINDOW)
    dense_x, dense_y = densify_linear(centers, smooth_density, points_per_seg=DIST_DENSIFY_POINTS)

    fig, ax = plt.subplots(figsize=(9.8, 3.0))
    _apply_chart_theme(fig, ax, ax_bg=ax_bg)

    ax.bar(centers, density, width=bin_w * 0.92, alpha=0.72, color=CLOSE_DIST_BAR_COLOR)
    ax.plot(dense_x, dense_y, linewidth=2.2, color=CLOSE_DIST_COUNT_SMOOTH_LINE_COLOR)
    ax.plot(centers, smooth_density, linestyle="None", marker="o", markersize=2.6, color=CLOSE_DIST_COUNT_SMOOTH_LINE_COLOR)

    ax.set_ylabel("Probability density", color=CHART_TEXT_COLOR, fontsize=12)
    ax.set_xlabel(x_label, color=CHART_TEXT_COLOR, fontsize=12)
    ax.tick_params(axis="both", labelsize=9, colors=CHART_TEXT_COLOR)
    ax.grid(True, which="major", axis="y", alpha=0.25)

    fig.tight_layout(pad=1.2)
    _save_fig(fig, out_path)

# =============================================================================
# HTML generation
# =============================================================================

def html_escape(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#039;")
    )

def build_kpis(metrics: Metrics) -> Dict[str, str]:
    avg_first = statistics.mean(metrics.t_first_contact_seconds) if metrics.t_first_contact_seconds else None
    med_first = statistics.median(metrics.t_first_contact_seconds) if metrics.t_first_contact_seconds else None
    avg_close = statistics.mean(metrics.t_close_seconds) if metrics.t_close_seconds else None
    med_close = statistics.median(metrics.t_close_seconds) if metrics.t_close_seconds else None

    return {
        LBL_TOTAL_TICKETS: f"{metrics.total_tickets:,}",
        LBL_TOTAL_CLOSED: f"{metrics.total_closed:,}",
        LBL_AVG_FIRST_CONTACT: fmt_duration(avg_first),
        LBL_MED_FIRST_CONTACT: fmt_duration(med_first),
        LBL_AVG_CLOSE: fmt_duration(avg_close),
        LBL_MED_CLOSE: fmt_duration(med_close),
    }

def top_items(data: Dict[str, int], n: int) -> List[Tuple[str, int]]:
    return sorted(data.items(), key=lambda kv: kv[1], reverse=True)[:n]

def build_simple_table_inner_html(
    headers: Tuple[str, str],
    rows: List[Tuple[str, int]],
) -> str:
    tr = [f"<tr><th>{html_escape(headers[0])}</th><th class='num'>{html_escape(headers[1])}</th></tr>"]
    for k, v in rows:
        tr.append(f"<tr><td>{html_escape(str(k))}</td><td class='num'>{int(v):,}</td></tr>")
    return f"""
      <div class="table-wrap">
        <table class="table">
          {''.join(tr)}
        </table>
      </div>
    """

def build_summary_table_inner_html(title: str, data: Dict[str, int], top_n: int = 10) -> str:
    # `title` kept for signature consistency / future use
    _ = title
    rows = top_items(data, top_n)
    return build_simple_table_inner_html(("Category", "Count"), rows)

def build_summary_table_html(title: str, data: Dict[str, int], top_n: int = 10) -> str:
    inner = build_summary_table_inner_html(title, data, top_n=top_n)
    return f"""
    <section class="block">
      <div class="block-title">{html_escape(title)}</div>
      {inner}
    </section>
    """

def build_priority_level_summary_table_inner_html(by_priority: Dict[str, int]) -> str:
    """
    Aggregate priority label variations into fixed rows P1..P6, displayed in numeric order.
    """
    counts = {i: 0 for i in range(1, 7)}
    other_total = 0

    for pname, cnt in (by_priority or {}).items():
        c = int(cnt or 0)
        plevel = priority_level_1_to_6(str(pname or ""), None)
        if plevel in counts:
            counts[plevel] += c
        else:
            other_total += c

    rows: List[Tuple[str, int]] = [(f"Priority {i}", counts[i]) for i in range(1, 7)]
    if other_total:
        rows.append(("Other / Unparsed", other_total))

    return build_simple_table_inner_html(("Priority", "Count"), rows)

def build_priority_level_summary_table_html(title: str, by_priority: Dict[str, int]) -> str:
    inner = build_priority_level_summary_table_inner_html(by_priority)
    return f"""
    <section class="block">
      <div class="block-title">{html_escape(title)}</div>
      {inner}
    </section>
    """

def build_tech_priority_table_html(title: str, tech_priority_counts: Dict[str, Dict[str, int]], top_n: int = TECH_TABLE_TOP_N, inner_only: bool = False) -> str:
    items = sorted(tech_priority_counts.items(), key=lambda kv: kv[1].get("total", 0), reverse=True)[:top_n]

    # --- CHANGED (must): include P6 column ---
    headers = ["Technician", "Total", "P1", "P2", "P3", "P4", "P5", "P6"]
    if TECH_TABLE_INCLUDE_OTHER_PRIORITY:
        headers.append("Other")

    th = "".join([f"<th>{html_escape(h)}</th>" if h == "Technician" else f"<th class='num'>{html_escape(h)}</th>" for h in headers])
    tr = [f"<tr>{th}</tr>"]

    for tech, counts in items:
        cells = []
        cells.append(f"<td>{html_escape(tech)}</td>")
        cells.append(f"<td class='num'><strong>{counts.get('total', 0):,}</strong></td>")
        cells.append(f"<td class='num'>{counts.get('p1', 0):,}</td>")
        cells.append(f"<td class='num'>{counts.get('p2', 0):,}</td>")
        cells.append(f"<td class='num'>{counts.get('p3', 0):,}</td>")
        cells.append(f"<td class='num'>{counts.get('p4', 0):,}</td>")
        cells.append(f"<td class='num'>{counts.get('p5', 0):,}</td>")
        cells.append(f"<td class='num'>{counts.get('p6', 0):,}</td>")
        if TECH_TABLE_INCLUDE_OTHER_PRIORITY:
            cells.append(f"<td class='num'>{counts.get('other', 0):,}</td>")
        tr.append(f"<tr>{''.join(cells)}</tr>")

    table_html = f"""
      <div class="table-wrap">
        <table class="table">
          {''.join(tr)}
        </table>
      </div>
    """
    if inner_only:
        return table_html
    return f"""
    <section class="block">
      <div class="block-title">{html_escape(title)}</div>
      {table_html}
    </section>
    """

def relpath_for_html(asset_path: Path, html_path: Path) -> str:
    try:
        return asset_path.relative_to(html_path.parent).as_posix()
    except Exception:
        return asset_path.as_posix()

def build_html(raw_meta: Dict[str, Any], metrics: Metrics, chart_paths: Dict[str, Path]) -> str:
    gen_at = iso_to_dt(parse_str(raw_meta.get("generated_at_utc")))

    if gen_at:
        gen_date = gen_at.astimezone(timezone.utc).date()
    else:
        gen_date = datetime.now(timezone.utc).date()

    period_label, _period_token = get_report_period_labels(FILTERS)
    meta_txt = f"Generated {gen_date.strftime('%b %d, %Y')} using {period_label.lower()}."

    kpis = build_kpis(metrics)

    def chart_inner(key: str, title: str, chart_bg_css: str) -> str:
        p = chart_paths.get(key)
        if not p or not p.exists():
            return '<div class="empty-note">Chart not generated.</div>'
        src = relpath_for_html(p, OUTPUT_HTML_PATH)
        return f"""
          <div class="chart-box" style="background: {html_escape(chart_bg_css)};">
            <img class="chart-img" src="{html_escape(src)}" alt="{html_escape(title)}" />
          </div>
        """

    def chart_block(key: str, title: str, chart_bg_css: str) -> str:
        inner = chart_inner(key, title, chart_bg_css)
        return f"""
        <section class="block">
          <div class="block-title">{html_escape(title)}</div>
          {inner}
        </section>
        """

    def panel(title: str, inner_html: str) -> str:
        return f"""
        <div class="pair-panel">
          <div class="pair-panel-title">{html_escape(title)}</div>
          {inner_html}
        </div>
        """

    def paired_chart_table_block(
        section_title: str,
        chart_key: str,
        chart_title: str,
        chart_bg_css: str,
        table_title: str,
        table_inner_html: str,
        title_bottom_gap_px: int = 10,   # NEW
    ) -> str:
        gap_html = f'<div style="height:{int(title_bottom_gap_px)}px;"></div>' if title_bottom_gap_px > 0 else ""
        return f"""
        <section class="block">
        <div class="block-title">{html_escape(section_title)}</div>
        {gap_html}
        <div class="pair-grid">
            {panel(chart_title, chart_inner(chart_key, chart_title, chart_bg_css))}
            {panel(table_title, table_inner_html)}
        </div>
        </section>
        """

    def triple_panel_block(section_title: str, panels_html: List[str]) -> str:
        return f"""
        <section class="block">
          <div class="block-title">{html_escape(section_title)}</div>
          <div class="triple-grid">
            {''.join(panels_html)}
          </div>
        </section>
        """

    kpi_cards_html = "".join(
        [f"""
        <div class="kpi-card">
          <div class="kpi-label">{html_escape(label)}</div>
          <div class="kpi-value">{html_escape(value)}</div>
        </div>
        """ for label, value in kpis.items()]
    )

    logo_src = ""
    if REPORT_LOGO_PATH and REPORT_LOGO_PATH.exists():
        try:
            logo_src = REPORT_LOGO_PATH.resolve().as_uri()
        except Exception:
            logo_src = str(REPORT_LOGO_PATH)

    logo_html = f'<img class="logo" src="{html_escape(logo_src)}" alt="Logo" />' if logo_src else ""

    paired_breakdowns_html = "".join([
        "<div style='height:40px;'></div>",

        paired_chart_table_block(
            "Board Breakdown",
            "pie_boards",
            "",
            CHART_OUTER_RING_COLOR,
            "",
            build_summary_table_inner_html("Top Boards", metrics.by_board, top_n=10),
        ),
        "<div style='height:40px;'></div>",
        paired_chart_table_block(
            "Status Breakdown",
            "pie_status",
            "",
            CHART_OUTER_RING_COLOR,
            "",
            build_summary_table_inner_html("Top Statuses", metrics.by_status, top_n=10),
        ),
        "<div style='height:40px;'></div>",
        triple_panel_block(
            "Priority Breakdown",
            [
                panel("", chart_inner("pie_priority", "Tickets by Priority", CHART_OUTER_RING_COLOR)),
                panel("", build_priority_level_summary_table_inner_html(metrics.by_priority)),
                panel("", build_tech_priority_table_html("Technician Workload by Priority", metrics.tech_priority_counts, top_n=TECH_TABLE_TOP_N, inner_only=True)),
            ],
        ),
    ])

    trends_html = ""
    if H_TRENDS:
        trends_html += f"\n    <h2>{html_escape(H_TRENDS)}</h2>\n"
    trends_html += chart_block("close_dist", "Average Time to Close: Distribution Function", CHART_OUTER_RING_COLOR)
    trends_html += chart_block("median_close_dist", "Median Time to Close: Distribution Function", CHART_OUTER_RING_COLOR)

    other_breakdowns_html = ""
    if H_BREAKDOWNS:
        other_breakdowns_html += f"\n    <h2>{html_escape(H_BREAKDOWNS)}</h2>\n"
    other_breakdowns_html += chart_block("bar_assignee", "Top Assignees", CHART_OUTER_RING_COLOR)

    html = f"""
    <!doctype html>
    <html>
    <head>
    <meta charset="utf-8" />
    <title>{html_escape(REPORT_TITLE)}</title>
    <style>
        :root {{
        --bg: #ffffff;
        --panel: #101B33;
        --text: #ffffff;
        --muted: #000103;

        --table-head: #152246;
        --table-row: #65b4fd;
        --table-grid: #114488;

        --chart-border: #152246;
        --chart-border-w: 2px;

        --page-pad: 24px;
        --content-w: 930px;
        --block-gap: 14px;
        --radius: 14px;
        }}

        @page {{
        size: Letter;
        margin: 0.7in 0.65in 0.7in 0.65in;
        }}

        body {{
        margin: 0;
        background: var(--bg);
        font-family: Arial, Helvetica, sans-serif;
        color: #0b1220;
        }}

        .page {{
        max-width: var(--content-w);
        margin: 0 auto;
        padding: var(--page-pad);
        }}

        .header {{
        margin-bottom: 18px;
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 16px;
        }}

        .header-left {{
        min-width: 0;
        flex: 1 1 auto;
        }}

        .header-right {{
        flex: 0 0 auto;
        display: flex;
        align-items: flex-start;
        justify-content: flex-end;
        }}

        .logo {{
        width: 180px;
        height: auto;
        display: block;
        object-fit: contain;
        }}

        .title {{
        font-size: 34px;
        line-height: 1.05;
        margin: 0 0 6px 0;
        color: var(--panel);
        font-weight: 800;
        letter-spacing: 0.2px;
        }}

        .subtitle {{
        margin: 0 0 8px 0;
        color: var(--muted);
        font-size: 13px;
        }}

        .meta {{
        margin: 0;
        color: #25304a;
        font-size: 12px;
        }}

        h2 {{
        margin: 18px 0 10px;
        font-size: 18px;
        color: #0b1220;
        page-break-after: avoid;
        break-after: avoid;
        }}

        .kpi-grid {{
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 12px;
        margin: 10px 0 10px;
        }}

        .kpi-card {{
        background: var(--panel);
        border: 1px solid #23355F;
        border-radius: var(--radius);
        padding: 14px 14px 12px;
        color: var(--text);
        page-break-inside: avoid;
        break-inside: avoid;
        }}

        .kpi-label {{
        font-size: 12px;
        opacity: 0.95;
        margin-bottom: 8px;
        }}

        .kpi-value {{
        font-size: 22px;
        font-weight: 800;
        }}

        .block {{
        margin: 0 0 var(--block-gap) 0;
        }}

        .block-title {{
        font-size: 20px;
        color: #25304a;
        margin: 14px 0 8px 0;
        font-weight: 700;
        text-transform: none;
        page-break-after: avoid;
        break-after: avoid;
        padding-top: 2px;
        }}

        .pair-grid {{
        display: grid;
        grid-template-columns: 1.05fr 0.95fr;
        gap: 10px;
        align-items: start;
        }}

        .triple-grid {{
        display: grid;
        grid-template-columns: 0.95fr 0.85fr 1.20fr;
        gap: 10px;
        align-items: start;
        }}

        .pair-panel {{
        min-width: 0;
        }}

        .pair-panel-title {{
        font-size: 13px;
        color: #25304a;
        margin: 0 0 6px 2px;
        font-weight: 700;
        page-break-after: avoid;
        break-after: avoid;
        }}

        .chart-box {{
        border: var(--chart-border-w) solid var(--chart-border);
        border-radius: var(--radius);
        padding: 8px;
        background: #ffffff;
        margin-top: 0;
        page-break-inside: avoid;
        break-inside: avoid;
        }}

        .chart-img {{
        width: 100%;
        height: auto;
        display: block;
        border-radius: 10px;
        page-break-inside: avoid;
        break-inside: avoid;
        }}

        .empty-note {{
        border: 1px dashed #9ab0d6;
        border-radius: 10px;
        padding: 18px;
        color: #4a5d84;
        font-size: 12px;
        background: #f7faff;
        }}

        .table-wrap {{
        border: 1px solid #23355F;
        border-radius: var(--radius);
        overflow: clip;
        page-break-inside: avoid !important;
        break-inside: avoid !important;
        }}

        table.table {{
        width: 100%;
        border-collapse: collapse;
        }}

        .table th {{
        background: var(--table-head);
        color: var(--text);
        font-size: 12px;
        text-align: left;
        padding: 10px 10px;
        border: 1px solid var(--table-grid);
        }}

        .table td {{
        background: var(--table-row);
        color: var(--text);
        font-size: 12px;
        font-weight: 700;
        padding: 9px 10px;
        border: 1px solid var(--table-grid);
        vertical-align: top;
        }}

        .table tr > *:first-child {{ border-left: none; }}
        .table tr > *:last-child  {{ border-right: none; }}
        .table tr:first-child > * {{ border-top: none; }}
        .table tr:last-child  > * {{ border-bottom: none; }}

        .table td.num,
        .table th.num {{
        font-weight:700;
        text-align: right;
        white-space: nowrap;
        width: 120px;
        }}

        .footer {{
        margin-top: 18px;
        padding-top: 10px;
        border-top: 1px solid #c7d2e8;
        display: flex;
        justify-content: space-between;
        color: #25304a;
        font-size: 11px;
        page-break-inside: avoid;
        break-inside: avoid;
        }}

        @media print {{
        .page {{
            padding: 0;
        }}

        body {{
            background: #ffffff;
        }}

        /* Allow grouped sections/panels to split across pages */
        .block,
        .pair-grid,
        .triple-grid,
        .pair-panel {{
            page-break-inside: auto !important;
            break-inside: auto !important;
        }}

        /* Keep individual charts/images/cards intact */
        .chart-box,
        .kpi-card,
        img,
        .chart-img {{
            page-break-inside: avoid !important;
            break-inside: avoid !important;
        }}

        /* Let tables move/split if needed so they don't force a big blank gap */
        .table-wrap {{
            page-break-inside: avoid !important;
            break-inside: avoid !important;
        }}

        .block-title,
        .pair-panel-title,
        h2 {{
            page-break-after: avoid !important;
            break-after: avoid !important;
        }}

        /* KEY FIX: grids often behave as one chunk in PDF print engines */
        .pair-grid,
        .triple-grid {{
            display: block !important;
        }}

        .pair-panel {{
            margin-bottom: 10px;
        }}
        }}

        @media (max-width: 900px) {{
        .pair-grid {{
            grid-template-columns: 1fr;
        }}

        .triple-grid {{
            grid-template-columns: 1fr;
        }}

        .kpi-grid {{
            grid-template-columns: repeat(2, minmax(0, 1fr));
        }}
        }}
    </style>
    </head>
    <body>
    <div class="page">
        <div class="header">
        <div class="header-left">
            <div class="title">{html_escape(REPORT_TITLE)}</div>
            <div class="subtitle">{html_escape(REPORT_SUBTITLE)}</div>
            <p class="meta">{html_escape(meta_txt)}</p>
        </div>
        <div class="header-right">
            {logo_html}
        </div>
        </div>

        <h2>{html_escape(H_KPIS)}</h2>
        <div class="kpi-grid">
        {kpi_cards_html}
        </div>

        {paired_breakdowns_html}

        {trends_html}

        {other_breakdowns_html}

        <div class="footer">
        <div>{html_escape(REPORT_FOOTER_LEFT)}</div>
        <div>{html_escape(REPORT_FOOTER_RIGHT)}</div>
        </div>
    </div>
    </body>
    </html>
    """
    return html


# =============================================================================
# Email (PDF attachment)
# =============================================================================
def send_ticket_email(
    *,
    recipients: List[str],
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
    subject: str,
    body: str,
    attachment_path: Optional[Path] = None,
) -> None:
    if not recipients:
        raise ValueError("EMAIL_RECIPIENTS is empty")

    cc = [x.strip() for x in (cc or []) if x and x.strip()]
    bcc = [x.strip() for x in (bcc or []) if x and x.strip()]
    to_list = [x.strip() for x in recipients if x and x.strip()]

    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASS"]
    mail_from = os.environ.get("MAIL_FROM", smtp_user)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = ", ".join(to_list)
    if cc:
        msg["Cc"] = ", ".join(cc)

    msg.set_content(body)

    if attachment_path and attachment_path.exists():
        pdf_bytes = attachment_path.read_bytes()
        msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=attachment_path.name)

    envelope_rcpt = to_list + cc + bcc

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(smtp_user, smtp_pass)
        server.send_message(msg, to_addrs=envelope_rcpt)


# =============================================================================
# Main
# =============================================================================

def main() -> None:
    global RAW_JSON_PATH, OUTPUT_HTML_PATH, OUTPUT_PDF_PATH, REPORT_TITLE, REPORT_SUBTITLE

    run_local = datetime.now()

    if AUTO_USE_SAME_DAY_JSON:
        RAW_JSON_PATH = resolve_raw_json_path(
            RAW_DATA_DIR,
            run_local=run_local,
            prefer_today=AUTO_USE_SAME_DAY_JSON,
            fallback_to_latest=AUTO_FALLBACK_TO_LATEST_JSON,
        )

    if AUTO_OUTPUT_NAMING_ENABLED:
        OUTPUT_HTML_PATH, OUTPUT_PDF_PATH = build_output_report_paths(
            REPORT_OUTPUT_DIR,
            company_file_stem=REPORT_COMPANY_FILE_STEM,
            filters=FILTERS,
            include_year=REPORT_FILENAME_INCLUDE_YEAR,
        )

    ensure_dirs()

    raw = load_json(RAW_JSON_PATH)
    _mappings = load_mappings(MAPPINGS_JSON_PATH)
    _cw_companies = load_cw_companies(CW_COMPANIES_JSON_PATH)

    period_label, _period_token = get_report_period_labels(FILTERS)
    REPORT_TITLE = f"{REPORT_COMPANY_DISPLAY_NAME.upper()} Ticket Statistics"

    subtitle_parts: List[str] = []
    if NORMALIZE_AUDIT_CLOSE_HISTORY:
        subtitle_parts.append("Close-time reporting normalizes audit close history to ignore false/admin close touches.")
    if SUM_ONLY_TRUE_OPEN_PERIODS_FOR_REAL_REOPENS:
        subtitle_parts.append("Real reopens use summed true-open periods instead of first-open to final-close span.")
    if LEGACY_EXCLUDE_LONG_CLOSES_ENABLED:
        subtitle_parts.append(
            f"Legacy exclusion is active for tickets closed in month {LEGACY_EXCLUDE_LONG_CLOSES_MONTH} over {LEGACY_EXCLUDE_LONG_CLOSES_OVER_DAYS:g} days."
        )
    REPORT_SUBTITLE = " ".join(subtitle_parts + [f"({period_label})"]).strip()

    print(f"Using raw JSON: {RAW_JSON_PATH}")
    print(f"Report title:   {REPORT_TITLE}")

    rows = extract_ticket_rows(raw, mappings=_mappings)
    rows = apply_filters(rows, FILTERS)
    metrics = compute_metrics(rows)
    _debug_duration_stats("Time to first contact", metrics.t_first_contact_seconds)
    _debug_duration_stats("Time to close", metrics.t_close_seconds)

    chart_paths: Dict[str, Path] = {
        "pie_boards": ASSETS_DIR / "pie_boards.png",
        "pie_status": ASSETS_DIR / "pie_status.png",
        "pie_priority": ASSETS_DIR / "pie_priority.png",
        "bar_assignee": ASSETS_DIR / "bar_assignee.png",
        "close_dist": ASSETS_DIR / "close_time_density_vs_counts.png",
        "median_close_dist": ASSETS_DIR / "median_close_time_distribution.png",
    }

    save_pie_chart_indexed(metrics.by_board, "Tickets by Board", chart_paths["pie_boards"], ax_bg=CHART_BG_PIE_BOARDS)
    save_pie_chart_indexed(metrics.by_status, "Tickets by Status", chart_paths["pie_status"], ax_bg=CHART_BG_PIE_STATUS)
    save_pie_chart_indexed(metrics.by_priority, "Tickets by Priority", chart_paths["pie_priority"], ax_bg=CHART_BG_PIE_PRIORITY)

    save_bar_chart(metrics.by_assignee, "Top Assignees", chart_paths["bar_assignee"], ax_bg=CHART_BG_BAR_ASSIGNEE)

    save_close_time_density_vs_counts(
        metrics.t_close_seconds,
        "Time to Close — Density vs Ticket Volume",
        chart_paths["close_dist"],
        bins=CLOSE_DIST_BINS,
        max_days=CLOSE_DIST_MAX_DAYS,
        ax_bg=CHART_BG_CLOSE_DIST,
    )
    save_median_close_distribution_chart(
        metrics.t_close_seconds,
        "Median Time to Close Distribution",
        "Time to close (days)",
        chart_paths["median_close_dist"],
        bins=CLOSE_DIST_BINS,
        max_days=CLOSE_DIST_MAX_DAYS,
        ax_bg=CHART_BG_CLOSE_DIST,
    )

    html = build_html(raw, metrics, chart_paths)
    OUTPUT_HTML_PATH.write_text(html, encoding="utf-8")
    print(f"Saved HTML: {OUTPUT_HTML_PATH}")

    ok = html_to_pdf(OUTPUT_HTML_PATH, OUTPUT_PDF_PATH)
    if not ok:
        raise RuntimeError("Failed to convert HTML to PDF (install Playwright or wkhtmltopdf).")
    print(f"Saved PDF:  {OUTPUT_PDF_PATH}")

    subject = f"ConnectWise Ticket Report — {period_label}"
    body = (
        f"Attached is the ConnectWise Ticket Report.\n\n"
        f"Company: {REPORT_COMPANY_DISPLAY_NAME}\n"
        f"Reporting period: {period_label}\n"
        f"Tickets in scope: {metrics.total_tickets}\n"
        f"Closed tickets: {metrics.total_closed}\n"
    )

    is_weekend = run_local.weekday() >= 5  # 5=Sat, 6=Sun
    effective_send_email = SEND_EMAIL and not (DISABLE_EMAIL_ON_WEEKENDS and is_weekend)

    if effective_send_email:
        send_ticket_email(
            recipients=EMAIL_RECIPIENTS,
            cc=EMAIL_CC,
            bcc=EMAIL_BCC,
            subject=subject,
            body=body,
            attachment_path=OUTPUT_PDF_PATH,
        )
        print(f"[OK] Email sent to: {', '.join(EMAIL_RECIPIENTS)}")
    else:
        reason = []
        if not SEND_EMAIL:
            reason.append("SEND_EMAIL=False")
        if DISABLE_EMAIL_ON_WEEKENDS and is_weekend:
            reason.append("weekend rule")
        print(f"[Info] Skipping email send ({', '.join(reason) if reason else 'disabled'}).")

# =============================================================================
# Callable API for the webapp
# =============================================================================

def generate_report(
    tickets: List[Dict[str, Any]],
    params: Dict[str, Any],
    output_dir: Path,
    broadcast_fn=None,
) -> Tuple[Path, str]:
    """
    Generate a ticket report from raw CW API ticket dicts.
    Called by the webapp report service instead of reading from a JSON file.

    Args:
        tickets:      Raw ticket list from the CW API (each item is a dict).
        params:       Webapp request params — date_from, date_to, company_include, etc.
        output_dir:   Directory where chart PNGs, the HTML file, and the PDF are written.
        broadcast_fn: Optional callable(str) that receives log lines for SSE streaming.

    Returns:
        (pdf_path, html_str)
    """
    global OUTPUT_HTML_PATH, OUTPUT_PDF_PATH, FILTERS, REPORT_TITLE, REPORT_SUBTITLE

    def log(msg: str) -> None:
        if broadcast_fn:
            broadcast_fn(msg)

    def _lst(key: str) -> List[str]:
        return [s.strip() for s in params.get(key, []) if str(s).strip()]

    date_from = (params.get("date_from") or "").strip()
    date_to   = (params.get("date_to")   or "").strip()

    filters = Filters(
        date_entered_after_utc=f"{date_from}T00:00:00Z"  if date_from else None,
        date_entered_before_utc=f"{date_to}T23:59:59Z"   if date_to   else None,
        min_age_days=params.get("min_age_days"),
        max_age_days=params.get("max_age_days"),
        exclude_if_closed_in_month=params.get("exclude_closed_month"),
        exclude_if_closed_in_month_over_days=params.get("exclude_closed_month_days"),
        company_include=_lst("company_include"),
        company_exclude=_lst("company_exclude"),
        assignee_include=_lst("assignee_include"),
        assignee_exclude=_lst("assignee_exclude"),
        board_include=_lst("board_include"),
        board_exclude=_lst("board_exclude") or ["Alerts"],
        status_include=_lst("status_include"),
        status_exclude=_lst("status_exclude"),
        priority_include=_lst("priority_include"),
        priority_exclude=_lst("priority_exclude"),
        summary_contains_any=_lst("summary_contains_any"),
        source_include=_lst("source_include"),
        team_include=_lst("team_include"),
    )

    output_dir = Path(output_dir)
    assets_dir = output_dir / "_assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    # Set globals used inside build_html()
    FILTERS          = filters
    OUTPUT_HTML_PATH = output_dir / "report.html"
    OUTPUT_PDF_PATH  = output_dir / "report.pdf"

    period_label, _ = get_report_period_labels(filters)
    REPORT_TITLE    = params.get("report_title", "").strip() or "Ticket Statistics"
    REPORT_SUBTITLE = params.get("report_subtitle", "").strip() or period_label

    log(f"Processing {len(tickets)} tickets...")
    raw  = {"tickets": [{"ticket": t, "auditTrail": [], "computed": {}} for t in tickets]}
    rows = extract_ticket_rows(raw, mappings=None)
    log(f"Extracted {len(rows)} rows from raw data")

    # Sample the first ticket so date/company format is visible when debugging
    if tickets:
        t0 = tickets[0]
        log(f"  sample ticket id={t0.get('id')} "
            f"dateEntered={t0.get('dateEntered')!r} "
            f"company={t0.get('company', {}).get('name')!r}")

    rows = apply_filters(rows, filters, log_fn=log)
    metrics = compute_metrics(rows)
    log(f"After filters: {len(rows)} tickets")

    chart_paths: Dict[str, Path] = {
        "pie_boards":        assets_dir / "pie_boards.png",
        "pie_status":        assets_dir / "pie_status.png",
        "pie_priority":      assets_dir / "pie_priority.png",
        "bar_assignee":      assets_dir / "bar_assignee.png",
        "close_dist":        assets_dir / "close_time_density_vs_counts.png",
        "median_close_dist": assets_dir / "median_close_time_distribution.png",
    }

    save_pie_chart_indexed(metrics.by_board,    "Tickets by Board",    chart_paths["pie_boards"],   ax_bg=CHART_BG_PIE_BOARDS)
    save_pie_chart_indexed(metrics.by_status,   "Tickets by Status",   chart_paths["pie_status"],   ax_bg=CHART_BG_PIE_STATUS)
    save_pie_chart_indexed(metrics.by_priority, "Tickets by Priority", chart_paths["pie_priority"], ax_bg=CHART_BG_PIE_PRIORITY)
    save_bar_chart(metrics.by_assignee, "Top Assignees", chart_paths["bar_assignee"], ax_bg=CHART_BG_BAR_ASSIGNEE)
    save_close_time_density_vs_counts(
        metrics.t_close_seconds,
        "Time to Close \u2014 Density vs Ticket Volume",
        chart_paths["close_dist"],
        bins=CLOSE_DIST_BINS,
        max_days=CLOSE_DIST_MAX_DAYS,
        ax_bg=CHART_BG_CLOSE_DIST,
    )
    save_median_close_distribution_chart(
        metrics.t_close_seconds,
        "Median Time to Close Distribution",
        "Time to close (days)",
        chart_paths["median_close_dist"],
        bins=CLOSE_DIST_BINS,
        max_days=CLOSE_DIST_MAX_DAYS,
        ax_bg=CHART_BG_CLOSE_DIST,
    )

    html_str = build_html({}, metrics, chart_paths)
    OUTPUT_HTML_PATH.write_text(html_str, encoding="utf-8")
    log(f"Saved HTML: {OUTPUT_HTML_PATH}")

    ok = html_to_pdf(OUTPUT_HTML_PATH, OUTPUT_PDF_PATH)
    if not ok:
        raise RuntimeError(
            "PDF generation failed \u2014 install Playwright or wkhtmltopdf."
        )
    log(f"Saved PDF: {OUTPUT_PDF_PATH}")

    return OUTPUT_PDF_PATH, html_str


if __name__ == "__main__":
    main()