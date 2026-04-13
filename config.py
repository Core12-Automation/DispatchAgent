"""
config.py

App-level settings and constants. All tuneable values live here so the
rest of the codebase stays free of magic strings and numbers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Set

# ── Paths ────────────────────────────────────────────────────────────────────

BASE_DIR      = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
DATA_DIR      = BASE_DIR / "data"
CONFIG_FILE   = DATA_DIR / "portal_config.json"

# ── ConnectWise environment keys ─────────────────────────────────────────────

ENV_KEYS: List[str] = [
    "CWM_SITE",
    "CWM_COMPANY_ID",
    "CWM_PUBLIC_KEY",
    "CWM_PRIVATE_KEY",
    "CLIENT_ID",
    "ANTHROPIC_API_KEY",
]

SENSITIVE: Set[str] = {"CWM_PRIVATE_KEY", "CWM_PUBLIC_KEY", "ANTHROPIC_API_KEY"}

# ── Default portal configuration ─────────────────────────────────────────────

DEFAULT_CONFIG: Dict[str, Any] = {
    "boards_to_scan":             ["Dispatch"],
    "route_from_statuses":        ["New", "New (Email connector)", "New (email connector)"],
    "assigned_status":            "Assigned",
    "route_to_board":             "Support",
    "unrouted_owner_identifiers": ["supportdesk", "APIBot", "AARON_API"],
    "dry_run":                    True,
    "add_routing_note":           True,
    "note_template":              "AI Routing: assigned to {display_name} \u2014 {reason}",
    "max_tickets_to_process":     50,
    "claude_model":               "claude-sonnet-4-6",
    "mappings_path":              str(DATA_DIR / "mappings.json"),
    "timeout_secs":               20,
    "page_size":                  200,
}

# ── Report constants ──────────────────────────────────────────────────────────

COMPANY_ALIASES: Dict[str, List[str]] = {
    "A3 Architecture":             ["A3", "janderson@a3-architecture.com"],
    "Ajay SQM Group":              ["Ajay", "ajay", "AJAY", "SQM", "beau.routh@ajay-sqm.com"],
    "Allergy and Asthma ":         ["Allergy", "Asthma"],
    "Andrew Akard Architecture":   ["Andrew Akard", "andy.akard@akardarchitecture.com"],
    "Atkins Park Restaurant":      ["Atkins Park", "ehowell@atkinspark.com"],
    "BLUR Workshop":               ["blur", "blurworkshop", "blur workshop", "blurworkshop.com", "jaredd@blurworkshop.com"],
    "Business Environments":       ["Business Environments", "chettler@becusacorp.com"],
    "CFC Group":                   ["CFC", "tclose@cfcgroupinc.com"],
    "Commodity Cables":            ["commoditycables", "commodity cable", "commoditycables.com"],
    "Gather Grills":               ["Gather Grills", "jed@strangefarms.com"],
    "HPD Consulting Engineers":    ["HPD", "kmaddox@hpdengineers.com"],
    "Legacy Golf Links":           ["Legacy Golf", "LGL", "EZsuite", "robert.sabat@legacyfoxcreek.com"],
    "Mann Mechanical":             ["Mann Mechanical", "gthomas@mannmechanical.com"],
    "Milestone Dentistry":         ["Milestone Dentistry", "docwake@milestone-dentistry.com"],
    "Museum of Design Atlanta":    ["Museum of Design", "MODA", "lflusche@museumofdesign.org"],
    "Purisolve":                   ["Purisolve", "wallace.jones@purisolve.com"],
    "Providence Baptist Church":   ["Providence Baptist Church", "Providence Church", "Providence Baptist", "agenus@providencebc.com"],
    "Savant Engineering":          ["Savant Engineering", "Savant", "janderson@savanteng.com"],
    "Shepherd Harvey":             ["Shepherd Harvey", "sshepherd@shepharv.com"],
    "Sizemore Group":              ["Sizemore", "angelitaa@sizemoregroup.com", "monicap@sizemoregroup.com"],
    "St Bartholomews Episcopal":   ["St Bartholomews", "Bartholomews Episcopal", "barry@barrybynum.com"],
    "The Church at Chapel Hill":   ["Chapel Hill", "andy.odonnell@chapelhill.cc"],
    "Whitaker Company":            ["Whitaker", "daniel.craven@whitaker.company"],
    "Willmer Engineering, Inc.":   ["Willmer", "jcwillmer@willmerengineering.com"],
}

# Chart styling
CHART_TEXT_COLOR   = "#0B1220"
CHART_TITLE_COLOR  = "#0B1220"
CHART_BORDER_COLOR = "#152246"
CHART_BORDER_WIDTH = 5
CHART_BG_PIE_BOARDS   = "#79CAEB"
CHART_BG_PIE_STATUS   = "#79CAEB"
CHART_BG_PIE_PRIORITY = "#79CAEB"
CHART_BG_BAR_ASSIGNEE = "#79CAEB"
CHART_BG_CLOSE_DIST   = "#65b4fd"
CHART_OUTER_RING_COLOR = "#9CABB8"

PIE_SMALL_SLICE_PCT = 4.0
DIST_SMOOTH_WINDOW  = 5
DIST_DENSIFY_POINTS = 8
PIE_TOP_N_RPT       = 8
CLOSE_DIST_BINS     = 30
CLOSE_DIST_MAX_DAYS: Optional[float] = 9.0
CLOSE_DIST_BAR_COLOR               = "#152246"
CLOSE_DIST_COUNT_SMOOTH_LINE_COLOR = "#E9E9E9"
TECH_TABLE_TOP_N                   = 15
TECH_TABLE_INCLUDE_OTHER_PRIORITY  = False

# Close history reconstruction flags
NORMALIZE_AUDIT_CLOSE_HISTORY             = True
IGNORE_DUPLICATE_CLOSED_TO_CLOSED_EVENTS  = True
IGNORE_CW_CONSULTANT_CLOSE_TOUCHES        = False
CW_CONSULTANT_NAME_MATCHES: List[str]        = ["CW Consultant"]
CW_CONSULTANT_IDENTIFIER_MATCHES: List[str]  = ["cwconsultant"]
CW_CONSULTANT_MEMBER_ID_OVERRIDE: Optional[int] = None
SUM_ONLY_TRUE_OPEN_PERIODS_FOR_REAL_REOPENS = True
CLOSED_STATUS_NAME_MATCHES: List[str]        = ["Closed", "Completed"]
DEBUG_PRINT_CLOSE_RECONSTRUCTION             = False

EXCLUDED_ASSIGNEES = {"unassigned", "apibot", "supportdesk", "sjones"}

# Report labels
SHOW_TIME_UNITS       = "auto"
H_KPIS                = "Statistics Overview"
H_TRENDS              = ""
H_BREAKDOWNS          = ""
LBL_TOTAL_TICKETS     = "Total Tickets"
LBL_TOTAL_CLOSED      = "Closed Tickets"
LBL_AVG_FIRST_CONTACT = "Avg Time to First Contact"
LBL_AVG_CLOSE         = "Avg Time to Close"
LBL_MED_CLOSE         = "Median Time to Close"
LBL_MED_FIRST_CONTACT = "Median Time to First Contact"
REPORT_FOOTER_LEFT    = "Core12"
REPORT_FOOTER_RIGHT   = "Generated Using Core12's Internal API"
