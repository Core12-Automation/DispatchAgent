# ConnectWise AI Dispatch Router — Technical Overview

**Project:** DispatchAgent  
**Organization:** Core12 Technology  
**Version:** Active Development (as of April 2026)  
**Author Reference:** api@core12tech.com  

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [System Architecture](#2-system-architecture)
3. [Directory Structure](#3-directory-structure)
4. [Configuration & Environment](#4-configuration--environment)
5. [Database Schema](#5-database-schema)
6. [ConnectWise API Integration](#6-connectwise-api-integration)
7. [The AI Agent](#7-the-ai-agent)
8. [Routing Decision Logic](#8-routing-decision-logic)
9. [Flask HTTP API](#9-flask-http-api)
10. [Background Dispatcher Service](#10-background-dispatcher-service)
11. [Technician Profiles](#11-technician-profiles)
12. [Operator Notes System](#12-operator-notes-system)
13. [Pattern Detection & Incident Management](#13-pattern-detection--incident-management)
14. [JSON Data Files](#14-json-data-files)
15. [Rate Limiting & Observability](#15-rate-limiting--observability)
16. [Testing Infrastructure](#16-testing-infrastructure)
17. [Stubbed & Planned Features](#17-stubbed--planned-features)
18. [End-to-End Workflow Walkthroughs](#18-end-to-end-workflow-walkthroughs)

---

## 1. Executive Summary

The **ConnectWise AI Dispatch Router** (codename: DispatchAgent) is an AI-powered service desk automation platform that automatically routes incoming support tickets in ConnectWise Manage to the most appropriate available technician. It runs continuously as a Flask web server with a background scheduling engine, and at its core it uses Anthropic's Claude language model to reason about ticket content, technician skills, workload, and availability before making a dispatch decision.

The system is designed for a Managed Service Provider (MSP) operating ConnectWise Manage as their PSA (Professional Services Automation) platform. The dispatcher monitors one or more ConnectWise boards for newly-created tickets that have no assigned technician, evaluates each ticket against a roster of routable technicians, and either assigns the ticket automatically or flags it for human review.

### What it does, end-to-end:

1. Every 30–60 seconds, the background scheduler polls ConnectWise Manage for unassigned tickets on configured dispatch boards.
2. Each ticket is run through a **pattern detector** that identifies whether it is a repeat alert, part of an alert storm, or currently suppressed.
3. The ticket (with context flags injected) is handed to a **Claude-powered agent** that reasons through the full 10-step dispatch workflow using a suite of 19 tool calls.
4. The agent writes its assignment back to ConnectWise Manage, adds an internal note explaining its reasoning, and logs the decision to a local SQLite database.
5. A web portal (Flask + vanilla HTML/JS) provides operators with a real-time dashboard, manual dispatch controls, technician profile management, and a natural-language note system for communicating special instructions to the agent.

---

## 2. System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                          Flask Web Server                           │
│  ┌────────────┐  ┌──────────────┐  ┌────────────┐  ┌───────────┐  │
│  │  /dispatch  │  │ /dispatcher  │  │  /members  │  │  /notes   │  │
│  │  (single   │  │ (scheduler   │  │ (tech      │  │ (operator │  │
│  │  ticket)   │  │  control)    │  │  profiles) │  │  notes)   │  │
│  └─────┬──────┘  └──────┬───────┘  └─────┬──────┘  └─────┬─────┘  │
└────────┼────────────────┼────────────────┼────────────────┼────────┘
         │                │                │                │
         ▼                ▼                │                │
┌─────────────────────────────────┐        │                │
│      Background Scheduler       │        │                │
│  DispatcherService (APScheduler)│        │                │
│  • Polls CW every 30–60s        │        │                │
│  • Filters already-processed    │        │                │
│  • Triggers batch dispatch      │        │                │
└───────────────┬─────────────────┘        │                │
                │                          │                │
                ▼                          ▼                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        Agent Core (src/agent/)                      │
│                                                                     │
│  PatternDetector ──► build_situation_briefing ──► build_prompt      │
│                                                          │          │
│                                                          ▼          │
│                              ┌───────────────────────────────────┐  │
│                              │     Claude API (Anthropic)        │  │
│                              │   Model: claude-sonnet-4-6        │  │
│                              │   Tools: 19 total                 │  │
│                              │   Max iterations: 15              │  │
│                              │   Timeout: 120s                   │  │
│                              └──────────────┬────────────────────┘  │
│                                             │                       │
│                              Tool Registry dispatches tool calls:   │
│  ┌──────────────────┐  ┌─────────────────┐  ┌────────────────────┐  │
│  │  PERCEPTION      │  │  ACTION         │  │  MEMORY            │  │
│  │  - get_tickets   │  │  - assign_ticket│  │  - get_tech_profile│  │
│  │  - get_workload  │  │  - update_notes │  │  - log_decision    │  │
│  │  - get_schedule  │  │  - escalate     │  │  - similar_tickets │  │
│  │  - get_board     │  │  - flag_human   │  │  - update_profile  │  │
│  │  - get_history   │  │  - message_tech │  │                    │  │
│  │  - get_presence  │  │  - suppress_alert│ │                    │  │
│  └──────────────────┘  └─────────────────┘  └────────────────────┘  │
└──────────┬──────────────────────────┬─────────────────┬─────────────┘
           │                          │                 │
           ▼                          ▼                 ▼
┌──────────────────┐      ┌───────────────────┐  ┌────────────────────┐
│  ConnectWise     │      │   SQLite Database  │  │  Microsoft Teams   │
│  Manage API      │      │   (dispatcher.db)  │  │  Graph API         │
│  (CW REST v2025) │      │  7 tables          │  │  (presence only)   │
└──────────────────┘      └───────────────────┘  └────────────────────┘
```

### Key design principles:

- **Claude is the reasoning layer.** No hard-coded routing rules. All skill matching, workload balancing, and priority handling is expressed as natural language in the system prompt — Claude interprets it.
- **ConnectWise is the source of truth.** Ticket data, member IDs, board IDs, and schedule entries always come from the CW API. The local DB stores *decisions*, not ticket state.
- **JSON files are the configuration layer.** Board/member/status ID mappings and the technician roster are stored in `data/mappings.json` and are human-editable and hot-reloaded each dispatch cycle.
- **Dry-run mode is first-class.** Every write operation checks a `dry_run` flag before touching CW. The entire agent loop can be run in observation mode.
- **Pattern detection runs before Claude.** Repeats, storms, and suppressed alerts are identified before the AI ever sees the ticket, and that context is injected into Claude's input.

---

## 3. Directory Structure

```
DispatchAgent/
│
├── app/                              # Flask application package
│   ├── __init__.py                   # App factory: creates Flask app, registers blueprints,
│   │                                 # starts DispatcherService, initializes DB tables
│   ├── core/
│   │   ├── logging_config.py         # JSON structured logging (file + console)
│   │   ├── connectwise.py            # Legacy CW client (deprecated; see src/clients/)
│   │   ├── config_manager.py         # load_config(), save_config(), load_mappings(),
│   │   │                             # save_mappings() — reads/writes JSON files
│   │   ├── rate_limiter.py           # Sliding-window rate limiter (Claude + CW API)
│   │   └── state.py                  # Global SSE broadcaster state (shared across threads)
│   └── routes/
│       ├── __init__.py               # register_blueprints() — wires all route modules
│       ├── dispatch.py               # POST /api/dispatch/run-single, GET traces
│       ├── dispatcher_routes.py      # GET/POST /api/dispatcher/* (scheduler control)
│       ├── members.py                # CRUD + sync + workload for /api/members/*
│       ├── notes.py                  # CRUD + chat parsing for /api/notes/*
│       ├── config.py                 # Read/write portal_config.json via API
│       ├── env.py                    # Read/write .env variables via API (values masked)
│       ├── bulk_edit.py              # Bulk ticket field patching
│       ├── search.py                 # CW ticket search proxy
│       ├── report.py                 # Dispatch summary reports
│       ├── health.py                 # GET /api/health
│       └── run.py                    # Legacy /api/run/start (superseded)
│
├── src/                              # Core domain logic (agent, clients, tools)
│   ├── agent/
│   │   ├── loop.py                   # run_dispatch(), run_dispatch_batch()
│   │   │                             # Main entry point for all dispatch operations.
│   │   │                             # Manages Anthropic API loop + tool execution.
│   │   ├── prompts.py                # build_dispatch_system_prompt()
│   │   │                             # Dynamically builds Claude's full system prompt.
│   │   ├── tool_definitions.py       # 19 tool JSON schemas for Anthropic tool-use API
│   │   ├── tool_registry.py          # ToolRegistry: maps tool names → Python handlers
│   │   └── briefing.py               # build_situation_briefing()
│   │                                 # Builds context summary injected at dispatch time
│   │
│   ├── clients/
│   │   ├── database.py               # SQLAlchemy ORM: 7 model classes + DB init
│   │   ├── connectwise.py            # CWManageClient: canonical CW REST client
│   │   ├── anthropic_client.py       # Anthropic SDK wrapper (messages.create)
│   │   ├── teams.py                  # Microsoft Teams Graph API (presence queries)
│   │   └── resolver.py               # ID↔name resolution helpers (board, member, status)
│   │
│   └── tools/
│       ├── perception/
│       │   ├── tickets.py            # get_new_tickets(), get_single_ticket_history()
│       │   ├── technicians.py        # get_technician_workload(), get_technician_schedule(),
│       │   │                         # get_tech_availability()
│       │   ├── dispatch_board.py     # get_dispatch_board() — board-wide snapshot
│       │   └── pattern_detector.py   # PatternDetector: fingerprint, incident tracking,
│       │                             # storm/repeat/suppression detection
│       └── memory/
│           ├── tech_profiles.py      # get_tech_profile(), update_tech_profile()
│           ├── decision_log.py       # log_dispatch_decision(), get_recent_decisions()
│           └── rag.py                # get_similar_past_tickets() (full-text SQL search)
│
├── services/
│   └── dispatcher.py                 # DispatcherService (APScheduler background thread)
│                                     # Owns the polling loop, rate limiting, cycle tracking
│
├── cw_agent_tools/                   # Legacy monolithic CW tool implementations
│   ├── connectwise_manage_client.py  # Original HTTP client (superseded by src/clients/)
│   ├── connectwise_manage_actions.py # Field-action tools (patch, set fields, etc.)
│   ├── connectwise_manage_resolvers.py  # ID/name resolution
│   └── connectwise_manage_agent_runtime.py  # Legacy agent execution context
│
├── tests/
│   ├── conftest.py                   # Shared fixtures (mock client, in-memory DB, app)
│   ├── test_agent_loop.py
│   ├── test_perception_tools.py
│   ├── test_e2e_connectwise.py       # Live CW API integration tests
│   ├── test_members_db.py
│   ├── test_memory_tools.py
│   ├── test_action_tools.py
│   ├── test_web.py
│   ├── test_playwright.py            # Browser-level UI tests (Playwright)
│   └── screenshots/                  # Debug screenshots from Playwright tests
│
├── data/
│   ├── dispatcher.db                 # SQLite database (auto-created; never commit)
│   ├── mappings.json                 # ConnectWise ID lookups + technician roster
│   ├── portal_config.json            # Application settings
│   ├── routing_training.json         # Placeholder (unused currently)
│   ├── teams_user_mapping.json       # CW identifier → Microsoft Graph user ID
│   └── teams_user_mapping_redacted.json  # Public-safe version for docs
│
├── templates/
│   └── index.html                    # Single-page portal UI (vanilla HTML/CSS/JS)
│
├── config.py                         # App-level Python constants (paths, defaults)
├── run.py                            # Entrypoint: python run.py
├── requirements.txt                  # Production dependencies
├── requirements-dev.txt              # Test dependencies (pytest, playwright, etc.)
├── pytest.ini                        # Pytest configuration
├── .env                              # Environment variables (credentials — never commit)
└── .gitignore
```

---

## 4. Configuration & Environment

### 4.1 Load Order

The system reads configuration from four sources at startup, in this order:

| Priority | Source | How loaded |
|----------|--------|------------|
| 1 | `.env` file | `python-dotenv` → `os.environ` |
| 2 | `data/portal_config.json` | `load_config()` from `app/core/config_manager.py` |
| 3 | `data/mappings.json` | `load_mappings()` from `app/core/config_manager.py` |
| 4 | SQLite database | `src/clients/database.py` (auto-creates tables) |

The dispatcher service re-reads `portal_config.json` and `mappings.json` fresh at the start of every dispatch cycle. No server restart is needed when these files change.

### 4.2 Environment Variables (`.env`)

```bash
# ── Anthropic / Claude ──────────────────────────────────────────────────────
ANTHROPIC_API_KEY=sk-ant-api03-...
# The API key for Anthropic's Messages API. Used by anthropic_client.py.

# ── ConnectWise Manage ──────────────────────────────────────────────────────
CWM_SITE=https://api-na.myconnectwise.net/v2025_1/apis/3.0/
# Full base URL for the ConnectWise API. Region must match the CW instance.

CWM_COMPANY_ID=Core12
# The short company ID string used in HTTP Basic Auth username.

CWM_PUBLIC_KEY=N4NPb4rHEbJzn8RG
CWM_PRIVATE_KEY=r7S0eVr89Sc8S7yf
# API key pair. Auth header = "Core12+N4NPb4rHEbJzn8RG:r7S0eVr89Sc8S7yf" (Base64)

CLIENT_ID=85e19fbf-cd7a-4598-b6db-7b386d6ee0c2
# ConnectWise ClientID header (identifies this integration to CW).

CWM_REQUEST_TIMEOUT=20         # Seconds before CW API request times out
CWM_RETRY_TOTAL=3              # Number of retries on 5xx / 429 responses

# ── Dispatch Scheduler ──────────────────────────────────────────────────────
DISPATCH_INTERVAL_SECONDS=30   # How often the background scheduler polls CW
CLAUDE_CALLS_PER_HOUR=200      # Rate limit ceiling for Anthropic API calls
CW_CALLS_PER_HOUR=2000         # Rate limit ceiling for ConnectWise API calls

# ── Flask ───────────────────────────────────────────────────────────────────
FLASK_HOST=0.0.0.0
FLASK_PORT=5000
LOG_DIR=/var/log/ai-dispatcher  # Log file directory; falls back to project root

# ── Database ────────────────────────────────────────────────────────────────
DATABASE_URL=sqlite:///data/dispatcher.db
# Optional override. Defaults to this SQLite path relative to project root.

# ── Microsoft Teams (Optional) ──────────────────────────────────────────────
TEAMS_TENANT_ID=b8fbdd69-...
TEAMS_CLIENT_ID=f1001935-...
TEAMS_CLIENT_SECRET=1a508038-...
TEAMS_USERNAME=akloss@core12tech.com
TEAMS_PASSWORD=...
# Used only for presence queries (Available/Busy/Away). Messaging is stubbed.

# ── Email (SMTP2GO) ─────────────────────────────────────────────────────────
SMTP_HOST=mail.smtp2go.com
SMTP_PORT=2525
SMTP_USER=reporting@core12tech.com
SMTP_PASS=...
# Not yet actively used; reserved for future email-based reports/alerts.
```

### 4.3 `data/portal_config.json`

This file drives the operational behavior of the dispatcher. It is editable at runtime through the portal UI (Settings tab) or directly.

```json
{
  "boards_to_scan": ["Dispatch"],
  "route_from_statuses": ["New", "New (Email connector)"],
  "unrouted_owner_identifiers": ["supportdesk", "APIBot"],
  "dry_run": false,
  "claude_model": "claude-sonnet-4-6",
  "max_tech_workload_pct": 0.40,
  "mappings_path": "C:\\Users\\Guest User\\Documents\\DispatchAgent\\data\\mappings.json",
  "timeout_secs": 20,
  "page_size": 200
}
```

| Key | Purpose |
|-----|---------|
| `boards_to_scan` | List of CW board names the scheduler monitors for new tickets. Resolved to board IDs via `mappings.json`. |
| `route_from_statuses` | Only tickets whose current status matches one of these strings are considered "unrouted" and eligible for dispatch. |
| `unrouted_owner_identifiers` | CW member identifiers (e.g., bot accounts, shared inboxes) that count as "no assigned technician." Any ticket owned by these accounts is treated as unowned. |
| `dry_run` | When `true`, all CW write operations return a mock response — no ticket is actually modified. Decisions are still logged to the local DB. |
| `claude_model` | Which Claude model to call. Default: `claude-sonnet-4-6`. |
| `max_tech_workload_pct` | Fraction of total open tickets at which a technician is considered overloaded (default 40%). Claude never assigns to an overloaded tech. |
| `timeout_secs` | CW API request timeout in seconds. |
| `page_size` | Number of tickets per CW API page when fetching lists. |

---

## 5. Database Schema

**Engine:** SQLite (auto-created at `data/dispatcher.db`)  
**ORM:** SQLAlchemy (declarative base)  
**Location:** `src/clients/database.py`

All tables are created on app startup via `init_db()`. The schema uses SQLite-compatible types: `String`, `Text`, `Integer`, `Float`, `Boolean`, `DateTime`.

### 5.1 `technicians`

Stores one record per known technician. This is the primary source of truth for the agent roster.

| Column | Type | Notes |
|--------|------|-------|
| `id` | Integer PK | Auto-increment |
| `cw_member_id` | Integer | Unique. ConnectWise `members.id` — used in API PATCH calls |
| `cw_identifier` | String (indexed) | CW login name, e.g. `"akloss"` |
| `name` | String | Display name, e.g. `"Aaron Kloss"` |
| `email` | String | Corporate email |
| `teams_user_id` | String | Microsoft Graph object ID (for Teams presence queries) |
| `routable` | Boolean | If `false`, agent never assigns this tech. Operator can toggle. |
| `description` | Text | Free-text description injected into the system prompt. Claude reads this when evaluating fit. |
| `skills` | Text (JSON) | Array of skill tags, e.g. `["networking", "vpn", "azure_ad"]` |
| `specialties` | Text (JSON) | Array of vendor/product specialties, e.g. `["SonicWall", "Cisco"]` |
| `avg_resolution_minutes` | Float | Computed from dispatch history |
| `total_tickets_handled` | Integer | Incremented each time agent assigns a ticket to this tech |
| `notes` | Text | Operator-authored free notes. Can include PTO, preferences, restrictions. |
| `is_active` | Boolean | Soft-delete flag |
| `created_at` | DateTime | Row creation timestamp |
| `updated_at` | DateTime | Last modification timestamp |

**Relationships:** `dispatch_decisions` (one-to-many via FK)

### 5.2 `dispatch_decisions`

One row per ticket routed by the agent. This is the primary audit log and training signal for routing quality.

| Column | Type | Notes |
|--------|------|-------|
| `id` | Integer PK | |
| `ticket_id` | Integer (indexed) | CW ticket ID |
| `ticket_summary` | Text | The ticket summary text at time of dispatch |
| `assigned_tech_id` | Integer (FK → technicians) | NULL if ticket was flagged rather than assigned |
| `assigned_tech_identifier` | String | Human-readable identifier (`"akloss"`) |
| `reason` | Text | Claude's stated rationale for the assignment |
| `confidence` | Float | 0.0–1.0. Below 0.6 → flag for human review instead of auto-assign |
| `alternatives_considered` | Text (JSON) | `[{"identifier": "jsmith", "reason": "lower workload but wrong skill set"}, ...]` |
| `was_dry_run` | Boolean | Whether this was a preview run |
| `created_at` | DateTime | When the decision was made |
| `run_id` | Integer (FK → dispatch_runs) | Which batch run produced this decision |

### 5.3 `dispatch_runs`

One row per scheduler cycle. Used for metrics and history views.

| Column | Type | Notes |
|--------|------|-------|
| `id` | Integer PK | |
| `started_at` | DateTime | Cycle start time |
| `ended_at` | DateTime | Cycle end time (NULL while running) |
| `tickets_processed` | Integer | Tickets evaluated this cycle |
| `tickets_assigned` | Integer | Successfully assigned |
| `tickets_flagged` | Integer | Escalated to human review |
| `errors` | Integer | Tickets that caused exceptions |
| `trigger` | String | `"manual"` or `"scheduled"` |

### 5.4 `active_incidents`

Tracks clusters of related tickets (same alert, same fingerprint). Core data structure for repeat/storm suppression.

| Column | Type | Notes |
|--------|------|-------|
| `id` | Integer PK | |
| `incident_key` | String (unique, indexed) | 16-character hex SHA-256 fingerprint of normalized ticket summary |
| `first_seen` | DateTime | When the first ticket with this fingerprint was created |
| `last_seen` | DateTime | When the most recent matching ticket was created |
| `ticket_ids` | Text (JSON) | Array of all CW ticket IDs with this fingerprint |
| `occurrence_count` | Integer | Total number of times this alert has appeared |
| `status` | String | `"new"` / `"monitoring"` / `"assigned"` / `"suppressed"` / `"resolved"` |
| `assigned_tech_id` | Integer | Which tech is handling this incident (if status=assigned) |
| `suppressed_until` | DateTime | NULL = not suppressed. If in the future, new tickets with this fingerprint are auto-skipped. |
| `suppressed_reason` | Text | Why it was suppressed (operator note, agent action, etc.) |
| `notes` | Text | Free-form notes about this incident |
| `created_at` | DateTime | |
| `updated_at` | DateTime | |

### 5.5 `operator_notes`

Human-authored instructions to the agent. Injected into Claude's situation briefing at dispatch time.

| Column | Type | Notes |
|--------|------|-------|
| `id` | Integer PK | |
| `note_text` | Text | The instruction, e.g. `"Mike is out sick today, do not assign him any tickets"` |
| `scope` | String | `"global"` / `"client"` / `"tech"` / `"incident"` |
| `scope_ref` | String | When scope is not global: company name, tech identifier, or incident key |
| `created_by` | String | Operator name |
| `created_at` | DateTime | |
| `expires_at` | DateTime | NULL = permanent. Expired notes are excluded from briefings. |
| `is_active` | Boolean | Soft-delete flag |
| `tags` | Text (JSON) | Array of string tags for filtering/searching |

### 5.6 `agent_memory`

Key-value working memory for the agent. Used for incident tracking, suppression state, and cycle state that needs to survive across dispatch cycles.

| Column | Type | Notes |
|--------|------|-------|
| `id` | Integer PK | |
| `key` | String (unique, indexed) | e.g. `"incident:abc123:last_action"` |
| `value` | Text (JSON) | Arbitrary JSON value |
| `category` | String | `"incident"` / `"suppression"` / `"pattern"` / `"cycle_state"` |
| `created_at` | DateTime | |
| `updated_at` | DateTime | |
| `expires_at` | DateTime | NULL = never expires |

### 5.7 `agent_traces`

Full reasoning trace for each single-ticket dispatch run. Used for debugging, audit, and future model fine-tuning.

| Column | Type | Notes |
|--------|------|-------|
| `id` | Integer PK | |
| `ticket_id` | Integer (indexed) | CW ticket ID |
| `ticket_summary` | Text | Summary at time of dispatch |
| `status` | String | `"ok"` / `"error"` / `"timeout"` / `"max_iterations"` |
| `iterations` | Integer | How many Anthropic API calls were made |
| `elapsed_seconds` | Float | Wall-clock time for the full agent run |
| `was_dry_run` | Boolean | |
| `trace_json` | Text (JSON) | Array of trace events: `[{"type": "text"/"tool_call"/"done", "tool": "assign_ticket", "input": {...}, "result": {...}, "t": 1.23}, ...]` |
| `created_at` | DateTime | |

---

## 6. ConnectWise API Integration

**Client class:** `CWManageClient` in `src/clients/connectwise.py`  
**API version:** ConnectWise Manage REST v2025_1  
**Base URL:** `https://api-na.myconnectwise.net/v2025_1/apis/3.0/`

### 6.1 Authentication

All requests use HTTP Basic Authentication. The username is constructed as:

```
{CWM_COMPANY_ID}+{CWM_PUBLIC_KEY}:{CWM_PRIVATE_KEY}
```

Example: `Core12+N4NPb4rHEbJzn8RG:r7S0eVr89Sc8S7yf` (Base64 encoded in the `Authorization` header).

The `ClientID` header is also sent on every request: `CLIENT_ID=85e19fbf-...`

### 6.2 API Endpoints Called

#### Ticket Endpoints

| Method | Endpoint | Purpose | Parameters |
|--------|----------|---------|------------|
| `GET` | `/service/tickets/{ticket_id}` | Fetch a single ticket's full object | — |
| `GET` | `/service/tickets` | Fetch a paginated list of tickets | `conditions`, `orderBy`, `pageSize`, `page` |
| `PATCH` | `/service/tickets/{ticket_id}` | Modify ticket fields | JSON Patch operations array |
| `POST` | `/service/tickets/{ticket_id}/notes` | Add a note to a ticket | `{text, detailDescriptionFlag, internalAnalysisFlag, resolutionFlag}` |
| `GET` | `/service/tickets/{ticket_id}/notes` | Fetch all notes on a ticket | — |
| `GET` | `/service/tickets/{ticket_id}/activities` | Fetch ticket audit trail | (via `_info.activities_href` from ticket object) |

**Common `conditions` used when fetching unrouted tickets:**

```python
conditions = (
  f"board/id IN ({board_ids}) "
  f"AND status/name IN ({status_names}) "
  f"AND (owner/identifier IS NULL "
  f"  OR owner/identifier IN ({unrouted_identifiers}))"
  f"AND closedFlag = false"
)
```

This query selects all open tickets on the monitored boards whose status is "New" (or similar) and that are either unowned or owned by a known bot account.

#### Schedule Endpoints

| Method | Endpoint | Purpose | Parameters |
|--------|----------|---------|------------|
| `GET` | `/schedule/entries` | Fetch a technician's scheduled calendar entries | `conditions` filtering by member ID and date range, `orderBy=dateStart`, `pageSize` |

**Conditions pattern:**
```python
conditions = (
  f"member/id = {member_id} "
  f"AND dateStart >= [{start_date}] "
  f"AND dateEnd <= [{end_date}]"
)
```

Returns entries including meeting types, time blocks, and out-of-office markers.

#### Member Endpoints

The client resolves member IDs from `mappings.json["members"]` (identifier → CW member ID). Member profile data (email, etc.) is stored locally in the `technicians` DB table rather than queried from CW's `/system/members` API on every dispatch.

### 6.3 HTTP Client Configuration

```python
session = requests.Session()
session.auth = HTTPBasicAuth(f"{company}+{public_key}", private_key)
session.headers.update({
    "Content-Type": "application/json",
    "ClientID": client_id
})
```

**Retry logic:** Up to `CWM_RETRY_TOTAL` (default 3) retries on HTTP status codes `429`, `500`, `502`, `503`, `504`. Wait: 0.8 seconds between retries (configurable).

**Pagination:** Automatic. Client loops pages until a response returns fewer items than `pageSize`. A 0.05s inter-page delay is applied to avoid hitting CW rate limits.

**Timeout:** `CWM_REQUEST_TIMEOUT` seconds (default 20s).

### 6.4 ID Resolution

`src/clients/resolver.py` provides helper functions that translate human-readable names to CW integer IDs using `mappings.json`:

- `resolve_board_id(board_name, mappings)` → `int`
- `resolve_member_id(identifier, mappings)` → `int`
- `resolve_status_id(board_name, status_name, mappings)` → `int`
- `resolve_type_id(type_name, mappings)` → `int`

These are called internally when the agent specifies an assignment by name (e.g., `assign_ticket(identifier="akloss", ...)`).

---

## 7. The AI Agent

The agent is the cognitive core of the system. It is a multi-turn tool-use Claude conversation that runs to completion for each batch of tickets.

### 7.1 Entry Points (`src/agent/loop.py`)

#### `run_dispatch(ticket, *, config, mappings, dry_run, broadcaster)`

Dispatches a **single ticket**. Called by the `/api/dispatch/run-single` endpoint.

- Max iterations: **15**
- Timeout: **120 seconds**
- Returns: `AgentResult` with `{status, decisions, tools_called, iterations, elapsed_seconds, trace}`

#### `run_dispatch_batch(tickets, *, config, mappings, briefing, broadcaster)`

Dispatches a **list of tickets** in one extended conversation. Called by the background scheduler's cycle function.

- Max iterations: **15 + 3 × num_tickets**
- Timeout: **240 seconds**
- Returns: aggregate `{status, tickets_processed, tickets_assigned, tickets_flagged, decisions, errors}`

### 7.2 The Conversation Loop

Both entry points share this inner loop:

```
1. Build system prompt (roster + briefing + rules)
2. Format ticket(s) as user message
3. Send to Claude API (tools enabled)
   │
   ├── Claude returns stop_reason = "tool_use"
   │   ├── Execute each requested tool via ToolRegistry
   │   ├── Append tool results to message history
   │   └── Loop → back to step 3
   │
   └── Claude returns stop_reason = "end_turn"
       └── Extract final text, return success
```

**Max iterations and timeout** guard against runaway loops. If either limit is hit, the function returns with status `"max_iterations"` or `"timeout"` and the partial trace is still saved.

**Tool execution** is synchronous within a single iteration. If Claude requests multiple tool calls in one response, they are executed sequentially (in the order Claude requested them) and all results are returned in the next message.

### 7.3 System Prompt (`src/agent/prompts.py`)

The system prompt is rebuilt dynamically for every dispatch cycle. It contains:

#### Section 1: Role Definition
Identifies Claude as "an AI dispatch agent for an MSP." Sets the expectation that Claude must be precise, data-driven, and always explain its reasoning.

#### Section 2: Technician Roster

Built from `mappings.json["agent_routing"]`, filtered to `routable=True` techs only. Each entry looks like:

```
- akloss (Aaron Kloss): Tier 2 networking specialist. Skills: networking, vpn, firewall, azure_ad. Specialties: SonicWall, Cisco, Microsoft 365.
- jsmith (Jane Smith): Tier 1 generalist. Skills: desktop_support, m365, basic_networking.
```

Only techs explicitly marked `routable=True` in both `mappings.json` and the DB are listed. If a tech is marked `routable=False`, Claude cannot even see them as an option.

#### Section 3: The 10-Step Dispatch Workflow

Claude is instructed to follow these steps in order for every ticket:

1. **ASSESS** — Read the ticket (summary, description, priority, company). If the description is empty, call `get_ticket_history` to read notes that may contain more context.
2. **CONTEXT** — Call `get_similar_past_tickets(keywords)` to find how identical or similar issues were resolved previously.
3. **CANDIDATE SELECTION** — Identify 2–3 technicians whose skills match the ticket type. Consider Tier-1 vs Tier-2, vendor specialties, and any named technician requests.
4. **WORKLOAD CHECK** — Call `get_technician_workload(identifier)` for each candidate. Never proceed with an overloaded tech.
5. **AVAILABILITY CHECK** — For Critical and High priority tickets only, call `get_tech_availability(identifier)` to check Teams presence. Prefer `Available` over `Busy` or `Away`.
6. **DECIDE** — Select the best match. If confidence ≥ 0.6, proceed to assignment. If below 0.6, flag for human review.
7. **ACT** — Call `assign_ticket(identifier, ticket_id)` or `flag_for_human_review(ticket_id, reason)`.
8. **NOTE** — Always call `update_ticket_notes(ticket_id, note)` with a human-readable explanation of the routing decision (for audit trail).
9. **NOTIFY** — For Critical and High tickets, call `message_technician(identifier, message, ticket_id)` to send a Teams alert.
10. **LOG** — **Mandatory.** Always call `log_dispatch_decision(ticket_id, identifier, reason, confidence, alternatives)`. This is enforced as a hard rule.

#### Section 4: SLA Priority Rules

| Priority | Action |
|----------|--------|
| Critical | Assign immediately to most skilled available tech. Skip workload check if nobody else qualifies. Notify via Teams. |
| High | Assign to skilled tech who is not overloaded. Prefer Available presence. Notify via Teams. |
| Medium | Normal assignment. No Teams notification required. |
| Low | Assign to least-loaded matching tech. No urgency. |

#### Section 5: Skill Matching Guidance

- **Tier-1 issues** (password resets, basic M365, printer issues, desktop support) → route to Tier-1 techs.
- **Tier-2 issues** (networking, firewall, server, Azure AD, domain, complex VPN) → route to Tier-2 techs.
- Escalation is appropriate when ticket content reveals higher severity than stated priority.
- If a client specifically names a technician in the ticket text, honor that request unless the tech is overloaded or not routable.

#### Section 6: Hard Rules

- Never assign to a technician with `overloaded=True` (≥40% of all open tickets).
- Never skip `log_dispatch_decision` — it is required on every ticket, even flagged ones.
- Never assign to a technician with `routable=False`.
- If an operator note says a specific tech is unavailable, follow it unconditionally.
- If a ticket is suppressed (`is_suppressed=True`), acknowledge and skip — do not route.
- Confidence threshold for auto-assignment is 0.6. Below this, always call `flag_for_human_review`.

#### Section 7: Situation Briefing (Injected at Dispatch Time)

The briefing (`src/agent/briefing.py`) is built just before each dispatch cycle and injected into the system prompt. It contains:

- **Active operator notes** — e.g., "Mike is out sick today (expires 5pm)"
- **Recent dispatch history** — Last 4 hours of decisions (who was assigned what)
- **Active incidents** — All `ActiveIncident` records with status `new`, `monitoring`, or `assigned`
- **Suppressed alerts** — Which incident fingerprints are currently suppressed and until when
- **Current tech workload** — Quick snapshot of open ticket counts per tech

This gives Claude a "situational awareness" layer beyond the individual ticket.

### 7.4 Tool Definitions (`src/agent/tool_definitions.py`)

The file defines 19 tool schemas in Anthropic's tool-use JSON format. Each schema specifies the tool name, description, and parameter types/constraints that Claude must respect when calling a tool.

### 7.5 Tool Registry (`src/agent/tool_registry.py`)

`ToolRegistry` is instantiated once per dispatch run, wired to the live CW client, DB session, and config. When the agent returns a `tool_use` block, `registry.call(tool_name, tool_input)` looks up the handler function and executes it.

```python
registry = ToolRegistry(
    cw_client=cw_client,
    db_session=session,
    config=config,
    mappings=mappings,
    dry_run=dry_run,
    broadcaster=broadcaster
)
result = registry.call("assign_ticket", {"identifier": "akloss", "ticket_id": 12345})
```

### 7.6 Complete Tool Reference

#### Perception Tools (read-only, never modify CW)

| Tool | Implementation | What it returns |
|------|----------------|-----------------|
| `get_new_tickets` | `src/tools/perception/tickets.py` | Array of unrouted ticket objects from the configured boards. Includes summary, company, priority, status, board, and any `_context` flags injected by PatternDetector. |
| `get_dispatch_board` | `src/tools/perception/dispatch_board.py` | Snapshot of all open tickets on a board. Used for overall workload visualization. |
| `get_technician_schedule` | `src/tools/perception/technicians.py` | CW schedule entries for a tech for the next N days. Returns meeting types, time blocks, dates. |
| `get_technician_workload` | `src/tools/perception/technicians.py` | `{"open_tickets": N, "by_priority": {"Critical": 0, "High": 2, ...}, "overloaded": bool, "workload_threshold": T}` |
| `get_ticket_history` | `src/tools/perception/tickets.py` | All notes (internal + external) and the audit activity trail for a single ticket. |
| `get_tech_availability` | `src/tools/perception/technicians.py` | Teams presence via Microsoft Graph: `{"availability": "Available"/"Busy"/"Away"/"Offline"}` |

#### Action Tools (write operations; respect `dry_run`)

| Tool | What it does | CW API calls |
|------|-------------|-------------|
| `assign_ticket` | Sets `owner` on a CW ticket. Optionally changes `status` and/or `board`. | `PATCH /service/tickets/{id}` |
| `reassign_ticket` | Changes `owner` + adds an internal note explaining the reassignment. | `PATCH /service/tickets/{id}` + `POST /service/tickets/{id}/notes` |
| `escalate_ticket` | Raises priority, optionally moves to a different board, adds internal note. | `PATCH /service/tickets/{id}` + `POST notes` |
| `message_technician` | Sends a Teams direct message to the tech. (Currently stubbed — chat IDs not yet linked.) | Teams Graph API (stub) |
| `message_team_channel` | Posts to a Teams channel. (Stubbed — team/channel IDs not yet configured.) | Teams Graph API (stub) |
| `send_reminder` | Posts an SLA/idle-warning note to the ticket + attempts Teams message. | `POST /service/tickets/{id}/notes` |
| `message_client` | Adds a customer-visible discussion note to the ticket. | `POST /service/tickets/{id}/notes` |
| `update_ticket_notes` | Adds an internal analyst note to the ticket. Primary audit-trail mechanism. | `POST /service/tickets/{id}/notes` |
| `flag_for_human_review` | Marks the ticket with a special note indicating human review is needed. Optionally sends Teams alert. | `POST /service/tickets/{id}/notes` |

#### Memory Tools (read/write local DB)

| Tool | What it does |
|------|-------------|
| `get_tech_profile` | Reads a `Technician` record from DB by `cw_identifier`. Returns skills, specialties, notes, avg resolution time, total tickets. |
| `update_tech_profile` | Upserts a `Technician` record. Used to accumulate stats after each dispatch. |
| `log_dispatch_decision` | Creates a `DispatchDecision` record. Required call at the end of every ticket. Stores ticket ID, assigned tech, reason, confidence, and alternatives considered. |
| `get_similar_past_tickets` | Searches `DispatchDecision` table for past decisions matching the provided keywords. Returns up to 5 prior decisions with their tech assignments and reasoning. Used as few-shot context. |

#### Incident Management Tools

| Tool | What it does |
|------|-------------|
| `suppress_alert` | Sets `suppressed_until` on an `ActiveIncident` record. Future tickets with the same fingerprint will be auto-skipped for the suppression duration. |
| `group_with_incident` | Links a new ticket ID to an existing `ActiveIncident`. Updates `occurrence_count` and `last_seen`. |
| `get_active_incidents` | Returns all `ActiveIncident` records not in status `"resolved"`. |
| `resolve_incident` | Sets an `ActiveIncident` to `status="resolved"`. Clears suppression. |

---

## 8. Routing Decision Logic

### 8.1 Pre-Processing: Pattern Detection (`src/tools/perception/pattern_detector.py`)

Before Claude sees any ticket, the `PatternDetector` class analyzes it for patterns:

#### Step 1: Fingerprinting

The ticket summary is normalized to a canonical form by:
1. Lowercasing the full text
2. Stripping IP addresses (IPv4 and IPv6 patterns)
3. Stripping dates and times
4. Stripping UUIDs and GUID strings
5. Stripping ticket reference numbers
6. Replacing all remaining digit sequences with the placeholder `"N"`
7. Stripping punctuation and collapsing whitespace
8. Prepending the company name to the normalized summary

The resulting string is hashed with SHA-256 and the first 16 hex characters are used as the `incident_key`.

**Example:**
- Raw summary: `"Server 192.168.1.10 unreachable - ticket #4521 opened 2026-04-14"`
- Normalized: `"acme server N N unreachable ticket N opened N N N"`
- Fingerprint: `"a3f8c2d1e4b90f56"`

This means functionally identical alerts — differing only in IP, date, or ticket number — will produce the same fingerprint and be tracked as one incident.

#### Step 2: Incident Lookup & Update

- If an `ActiveIncident` with this fingerprint exists and is not resolved:
  - Increment `occurrence_count`
  - Add current ticket ID to `ticket_ids`
  - Update `last_seen`
- If no incident exists:
  - Create new `ActiveIncident` (status=`"new"`)

#### Step 3: Context Flags Injected into Ticket

The following `_context` dict is attached to the ticket object before it reaches Claude:

```python
ticket["_context"] = {
    "is_repeat": bool,                 # True if occurrence_count > 1
    "incident_id": int,                # DB ID of the ActiveIncident
    "incident_key": str,               # Fingerprint hex string
    "occurrence_count": int,           # How many times this alert has appeared
    "first_seen": "2026-04-14T10:00Z", # ISO datetime
    "already_assigned_to": str | None, # Tech currently handling the incident
    "is_storm": bool,                  # True if 3+ tickets in < 1 hour
    "is_suppressed": bool,             # True if suppressed_until > now
    "suppressed_until": str | None,    # ISO datetime or null
    "suppressed_reason": str,          # Why suppressed
    "matching_operator_notes": [str]   # Active notes matching this ticket
}
```

### 8.2 Claude's Decision Process

With the enriched ticket in hand, Claude follows the 10-step workflow. Routing outcomes:

#### Automatic Assignment
- Skills match, workload ≤ 40%, availability acceptable (for Critical/High), confidence ≥ 0.6
- Agent calls `assign_ticket()` + `update_ticket_notes()` + `log_dispatch_decision()`

#### Human Review Flag
- No qualifying tech found, confidence < 0.6, or all candidates are overloaded
- Agent calls `flag_for_human_review()` + `log_dispatch_decision()` with reason
- Ticket remains unassigned; a note is added indicating it needs manual dispatch

#### Suppression Skip
- `is_suppressed=True` in context
- Agent acknowledges the ticket, skips routing, does not call `assign_ticket()`
- `log_dispatch_decision()` is still called (with reason=`"suppressed"`)

#### Incident Grouping
- `is_repeat=True` and `already_assigned_to` is not null
- Agent calls `group_with_incident()` to link the new ticket to the existing incident
- May also call `assign_ticket()` to the same tech already handling the incident
- No new routing decision needed

#### Storm Handling
- `is_storm=True` (3+ identical tickets in 1 hour)
- Agent groups all storm tickets under one incident
- Routes only to one tech
- May call `suppress_alert()` for a defined window to stop further noise

#### Named Tech Request
- If the ticket text explicitly names a technician ("please assign to Aaron")
- Agent skips normal candidate selection
- Checks only that the named tech is routable and not overloaded
- Assigns directly if conditions met; flags if not

### 8.3 Workload Calculation

`get_technician_workload` queries the CW API for all open tickets across all boards and counts how many are owned by the requested technician. The overload flag is computed as:

```
overloaded = (tech_open_tickets / total_open_tickets) >= max_tech_workload_pct
```

Where `max_tech_workload_pct` defaults to `0.40` (40%).

This is a relative measure — if total open tickets are low, a tech can hold more without being flagged. If total open tickets are high, the threshold keeps any one tech from being buried.

---

## 9. Flask HTTP API

**App factory:** `app/__init__.py` → `create_app()`  
**Blueprint registration:** `app/routes/__init__.py` → `register_blueprints(app)`

### 9.1 Dispatch Routes (`app/routes/dispatch.py`) — `/api/dispatch/`

#### `POST /api/dispatch/run-single`
Dispatch a single ticket through the full agent workflow. Streams progress via SSE and returns the complete result.

**Request body:**
```json
{
  "ticket_id": 12345,
  "dry_run": false
}
```

**Response:**
```json
{
  "status": "ok",
  "ticket_id": 12345,
  "summary": "Cannot connect to VPN",
  "decisions_made": 1,
  "tools_called": 8,
  "elapsed_seconds": 14.3,
  "iterations": 4,
  "dry_run": false,
  "agent_result": { ... }
}
```

#### `GET /api/dispatch/traces`
List agent trace records. Query params: `limit` (default 20), `ticket_id` (filter).

#### `GET /api/dispatch/traces/<id>`
Full trace for one run, including the complete `trace_json` array with every tool call and result.

### 9.2 Dispatcher Control Routes (`app/routes/dispatcher_routes.py`) — `/api/dispatcher/`

#### `GET /api/dispatcher/status`
Returns current scheduler state.
```json
{
  "running": true,
  "paused": false,
  "last_run": "2026-04-15T15:30:00Z",
  "next_run": "2026-04-15T15:31:00Z",
  "tickets_today": 42,
  "uptime_secs": 3600,
  "last_error": null
}
```

#### `POST /api/dispatcher/toggle`
Pause or resume the background scheduler.

#### `POST /api/dispatcher/run-once`
Immediately trigger one dispatch cycle (non-blocking; runs in background thread).

#### `GET /api/dispatcher/history`
Last 20 `DispatchRun` records (started_at, ended_at, counts, trigger).

#### `GET /api/dispatcher/decisions`
Last 30 `DispatchDecision` records with tech names and reasons.

#### `GET /api/dispatcher/metrics`
Aggregated stats:
```json
{
  "today": {"processed": 12, "assigned": 10, "flagged": 2},
  "this_week": {"processed": 87, "assigned": 71, "flagged": 16},
  "this_month": {"processed": 340, "assigned": 275, "flagged": 65},
  "avg_dispatch_time_seconds": 11.2,
  "assignments_by_tech": {"akloss": 45, "jsmith": 30, ...}
}
```

### 9.3 Member Routes (`app/routes/members.py`) — `/api/members/`

#### `GET /api/members`
List all technician records from DB.

#### `GET /api/members/<ident>`
Single technician by `cw_identifier` or DB `id`.

#### `PUT /api/members/<ident>`
Partial update. Accepts any subset of: `skills`, `specialties`, `email`, `teams_user_id`, `notes`, `routable`, `description`, `avg_resolution_minutes`.

#### `DELETE /api/members/<ident>`
Soft-deletes from DB (`is_active=False`) and removes from `mappings.json["agent_routing"]`.

#### `GET /api/members/<ident>/schedule`
Fetches the technician's CW schedule. Query params: `days_ahead` (default 3, max 14).

#### `GET /api/members/presence`
Fetches Teams presence for all routable technicians in one batch call.

#### `POST /api/members/sync`
Synchronizes the DB `technicians` table from `mappings.json["agent_routing"]`. Creates new records for any techs in the JSON file that are not in the DB. Idempotent.

#### `GET /api/members/workload`
Current open ticket counts per tech (5-minute cache). Calls `get_technician_workload` for all routable techs.

### 9.4 Operator Notes Routes (`app/routes/notes.py`) — `/api/notes/`

#### `GET /api/notes`
List active, non-expired operator notes. Query param: `include_expired=true` to include all.

#### `POST /api/notes`
Create a structured note.
```json
{
  "note_text": "Aaron is unavailable after 3pm today",
  "scope": "tech",
  "scope_ref": "akloss",
  "expires_at": "2026-04-15T23:59:59Z",
  "tags": ["availability"]
}
```

#### `PUT /api/notes/<id>`
Partial update any field of an existing note.

#### `DELETE /api/notes/<id>`
Soft-delete (sets `is_active=False`).

#### `POST /api/notes/chat`
**Natural language → structured note.** Send a plain English message and the system parses it into a structured operator note.

**Request:**
```json
{ "message": "Mike is out sick today" }
```

**Response:**
```json
{
  "parsed": {
    "note_text": "Mike is out sick today",
    "scope": "tech",
    "scope_ref": "akloss",
    "expires_at": "2026-04-15T23:59:59Z"
  },
  "created_note": { ... },
  "confidence": 0.95,
  "interpretation": "Identified 'Mike' as technician 'akloss' (Aaron Kloss). Set scope to tech, expires end of today."
}
```

This endpoint uses Claude to interpret the message and identify the relevant technician, time scope, and intent.

#### `GET /api/notes/briefing`
Returns the current formatted situation briefing text (what the agent sees at dispatch time).

### 9.5 Supporting Routes

| Route | Purpose |
|-------|---------|
| `GET/POST /api/config` | Read or write `portal_config.json` |
| `GET/POST /api/env` | Read or write `.env` variables (sensitive values are masked in GET responses) |
| `GET/POST /api/mappings` | Read or write `mappings.json` |
| `POST /api/search` | Proxy ConnectWise ticket search with conditions |
| `GET /api/report` | Generate dispatch summary report |
| `POST /api/bulk_edit` | Bulk-patch CW ticket fields |
| `GET /api/health` | `{"status": "ok", "db": "ok", "cw": "ok"}` |
| `GET /` | Render `templates/index.html` portal UI |

---

## 10. Background Dispatcher Service

**File:** `services/dispatcher.py`  
**Class:** `DispatcherService`  
**Scheduler:** APScheduler `BackgroundScheduler` (thread-based)

### 10.1 Lifecycle

The dispatcher is created as a module-level singleton and started when the Flask app is initialized:

```python
# app/__init__.py
dispatcher = get_dispatcher()
dispatcher.start()
```

On shutdown (SIGTERM or Flask teardown), `dispatcher.stop()` is called to cleanly shut down the APScheduler thread.

### 10.2 The Dispatch Cycle

The job `dispatch_cycle` runs every `DISPATCH_INTERVAL_SECONDS` (default 30s). APScheduler is configured with `max_instances=1` and `coalesce=True`, meaning if a cycle runs longer than the interval, the next cycle waits instead of overlapping.

One cycle:

```
1. Check: paused? error-backoff? rate-limited?
   └── If any true: skip this cycle, log reason

2. Load config = load_config()
   Load mappings = load_mappings()
   (Both files re-read from disk every cycle — no restart needed for config changes)

3. Fetch unrouted tickets from CW
   - For each board in config["boards_to_scan"]:
     - Query: open tickets, status in route_from_statuses,
              owner is null OR in unrouted_owner_identifiers
   - Filter out ticket IDs in processed_today set

4. PatternDetector.analyze_ticket(ticket) for each ticket
   - Assigns _context flags (is_repeat, is_storm, is_suppressed, etc.)
   - Updates ActiveIncident records in DB

5. build_situation_briefing()
   - Pulls active operator notes
   - Pulls recent dispatch history
   - Pulls active incidents
   - Builds the text block injected into Claude's prompt

6. Create DispatchRun record (trigger="scheduled")

7. run_dispatch_batch(tickets, config=config, mappings=mappings, briefing=briefing)
   - Returns aggregate result

8. Update DispatchRun record with counts (processed, assigned, flagged, errors)

9. Add all processed ticket IDs to processed_today set
   - processed_today resets at midnight (checked at step 3 via date comparison)
```

### 10.3 Error Handling

| Error type | Behavior |
|------------|---------|
| CW API failure (5xx, connection error) | Pause dispatcher for 5 minutes. Log CRITICAL. Set `last_error`. |
| Claude rate limit (429) | Exponential backoff: 30s → 60s → 120s → 300s → 600s. Auto-resume. |
| Individual ticket exception | Logged as error, ticket skipped. Count incremented in `DispatchRun.errors`. Cycle continues. |
| DB connection error | Log CRITICAL. Pause dispatcher. Alert via Teams (best-effort). |
| Timeout (120s per ticket) | Ticket status saved as `"timeout"` in agent trace. Counted as error. |

### 10.4 State Tracking

```python
dispatcher.status = {
    "running": bool,
    "paused": bool,
    "error_paused_until": datetime | None,
    "last_run": datetime,
    "next_run": datetime,
    "tickets_today": int,
    "uptime_secs": float,
    "last_error": str | None,
    "cycle_count": int
}
```

---

## 11. Technician Profiles

### 11.1 Sources of Truth (in priority order)

1. **SQLite `technicians` table** — primary. Full profile including skills, specialties, Teams ID, metrics.
2. **`data/mappings.json["agent_routing"]`** — secondary. Contains `display_name`, `routable`, `description`. Used to seed the DB and as fallback.
3. **`data/mappings.json["members"]`** — maps identifier → CW member ID. Required for PATCH calls.

### 11.2 Profile Sync

`POST /api/members/sync` — runs `sync_db_from_mappings()`:
- For each entry in `mappings.json["agent_routing"]`:
  - Look up existing `Technician` by `cw_identifier`
  - If not found: create with data from JSON
  - If found: update `display_name`, `routable`, `description` if changed
- Does not delete records not in JSON (soft-delete must be explicit)

This sync is idempotent and can be run any time the JSON is updated.

### 11.3 What the Agent Reads From a Profile

When Claude calls `get_tech_profile(identifier)`:

```json
{
  "identifier": "akloss",
  "name": "Aaron Kloss",
  "routable": true,
  "description": "Tier 2 tech - networking specialist",
  "skills": ["networking", "vpn", "firewall", "azure_ad"],
  "specialties": ["SonicWall", "Cisco", "Microsoft 365"],
  "avg_resolution_minutes": 120,
  "total_tickets_handled": 245,
  "notes": "Prefers morning tickets. On PTO July 10–15."
}
```

The `description`, `skills`, `specialties`, and `notes` fields are the most important. Claude uses them to assess fit for a given ticket type.

### 11.4 What the Agent Updates

After a dispatch decision, Claude calls `update_tech_profile` to increment `total_tickets_handled`. Over time, `avg_resolution_minutes` can also be updated (currently manual/operator-updated).

---

## 12. Operator Notes System

Operator notes are the primary human-to-agent communication channel. When an operator needs to influence routing behavior — e.g., "Bob is on vacation," "Client X should only get Tier-2 tickets," "We're suppressing the Barracuda backup alert until Monday" — they create an operator note.

### 12.1 Note Scopes

| Scope | `scope_ref` | Effect |
|-------|------------|--------|
| `global` | null | Applies to every ticket this cycle. |
| `tech` | cw_identifier (e.g., `"akloss"`) | Applies when this tech is being considered. |
| `client` | Company name (e.g., `"Acme Corp"`) | Applies to all tickets from this company. |
| `incident` | Incident key (16-char hex) | Applies to tickets matching this fingerprint. |

### 12.2 How Notes Are Injected

At the start of each dispatch cycle, `build_situation_briefing()` queries all active, non-expired notes and formats them:

```
OPERATOR NOTES (active):
[GLOBAL] Do not assign more than 3 tickets to any one tech today - system maintenance window 6pm-8pm.
[TECH: akloss] Aaron is out sick. Do not assign any tickets to him.
[CLIENT: Acme Corp] Acme has an emergency - prioritize all their tickets as Critical regardless of stated priority.
```

This text appears at the top of Claude's context window, before any ticket-specific content.

### 12.3 Natural Language Note Creation

`POST /api/notes/chat` uses Claude to parse free-text operator input:

```
Input: "Mike won't be in today"
```

The system:
1. Searches the technician roster for a tech named "Mike"
2. Resolves to `cw_identifier="msmith"` (or whichever "Mike" is in the roster)
3. Determines scope: `"tech"` with `scope_ref="msmith"`
4. Sets `expires_at` to end of current business day
5. Creates the structured note automatically

---

## 13. Pattern Detection & Incident Management

### 13.1 The Fingerprinting Algorithm

**File:** `src/tools/perception/pattern_detector.py`

```python
def _generate_fingerprint(summary: str, company: str) -> str:
    # 1. Normalize
    text = summary.lower()
    text = re.sub(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', '', text)  # IPv4
    text = re.sub(r'[0-9a-f]{8}-[0-9a-f]{4}-...', '', text)              # UUIDs
    text = re.sub(r'\b\d+\b', 'N', text)                                 # Numbers → N
    text = re.sub(r'[^\w\s]', '', text)                                  # Punctuation
    text = re.sub(r'\s+', ' ', text).strip()                             # Whitespace
    # 2. Prepend company
    canonical = f"{company.lower()} {text}"
    # 3. Hash
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]
```

### 13.2 Storm Detection

A **storm** is defined as: `occurrence_count >= 3` AND `first_seen` was less than 1 hour ago.

When a storm is detected:
- `is_storm=True` is set in `_context`
- Claude is expected to: route all storm tickets to one tech, call `group_with_incident()` for each, and optionally call `suppress_alert()` to stop routing further duplicates

### 13.3 Suppression

When `suppress_alert(incident_key, hours=N)` is called:
- `active_incidents.suppressed_until = now + timedelta(hours=N)` is set
- All future tickets with this fingerprint have `is_suppressed=True` in their `_context`
- Claude skips routing and logs the skip with reason=`"suppressed"`

Suppression can be set by the agent automatically (when a storm is detected) or manually by an operator through the portal UI.

---

## 14. JSON Data Files

### 14.1 `data/mappings.json`

**Purpose:** Bridge between human-readable names and ConnectWise integer IDs. Also serves as the canonical technician roster definition.

**Full structure:**

```json
{
  "agent_routing": {
    "akloss": {
      "display_name": "Aaron Kloss",
      "routable": true,
      "description": "Tier 2 networking specialist. Skills: VPN, firewall, SonicWall, Azure AD."
    },
    "jsmith": {
      "display_name": "Jane Smith",
      "routable": true,
      "description": "Tier 1 generalist. Desktop support, M365, basic networking."
    },
    "supportdesk": {
      "display_name": "Support Desk (Bot)",
      "routable": false,
      "description": "Shared inbox bot. Tickets assigned here are considered unrouted."
    }
  },
  "boards": {
    "Support": 10,
    "Dispatch": 38,
    "Tier 1": 39,
    "Tier 2": 40
  },
  "members": {
    "akloss": 407,
    "jsmith": 382,
    "supportdesk": 100
  },
  "dispatch statuses": {
    "New": 538,
    "Assigned": 542,
    "In Progress": 545,
    "Waiting": 548,
    "Resolved": 551
  },
  "support statuses": {
    "New": 841,
    "New (Email connector)": 842,
    "Assigned": 843,
    "In Progress": 846
  },
  "support types": {
    "Email": 1112,
    "Phone": 1120,
    "Network": 1143,
    "Hardware": 1144,
    "Software": 1145
  }
}
```

**How it is used:**

| Consumer | Keys used |
|----------|----------|
| Agent system prompt | `agent_routing` → build technician roster |
| Ticket fetching | `boards` → get board IDs for query conditions |
| Ticket assignment | `members[identifier]` → get CW member ID for PATCH |
| Status changes | `{board} statuses[status_name]` → get status ID for PATCH |
| Config UI | All keys → display dropdowns in portal |

**How it is maintained:**
- Hand-edited by the MSP admin (adds/removes techs, updates IDs when CW changes)
- Readable/writable via `GET/POST /api/mappings`
- Synced to DB via `POST /api/members/sync`

### 14.2 `data/portal_config.json`

See [Section 4.3](#43-dataportal_configjson) above.

### 14.3 `data/teams_user_mapping.json`

**Purpose:** Maps ConnectWise member identifiers to Microsoft Graph object IDs. Required to query Teams presence.

```json
{
  "_metadata": "CW identifier → Microsoft Graph user object ID. Do not edit via API.",
  "akloss": "12345678-1234-1234-1234-123456789abc",
  "jsmith": "87654321-4321-4321-4321-cba987654321"
}
```

**How it is used:** When `get_tech_availability(identifier)` is called, the Teams client looks up the Graph user ID here, then calls the Microsoft Presence API. If an identifier is not in this file, presence falls back to `"Unknown"`.

**How it is maintained:** Manually edited by the admin. A `teams_user_mapping_redacted.json` version with IDs masked is kept for documentation purposes.

### 14.4 `data/routing_training.json`

**Status:** Placeholder. Not yet used.  
**Intended purpose:** Labeled examples of ticket → tech assignments for potential future fine-tuning or RAG augmentation.

---

## 15. Rate Limiting & Observability

### 15.1 Rate Limiting (`app/core/rate_limiter.py`)

The system tracks API call counts in two sliding 1-hour windows:

| API | Ceiling | Env var |
|-----|---------|---------|
| Anthropic (Claude) | 200 calls/hour | `CLAUDE_CALLS_PER_HOUR` |
| ConnectWise Manage | 2000 calls/hour | `CW_CALLS_PER_HOUR` |

**Implementation:** A `RateLimiter` instance holds a deque of UNIX timestamps. On each call to `record_call()`:
1. Prune entries older than 3600 seconds
2. Count remaining entries
3. If count > ceiling: trigger rate-limit pause
4. If count > 90% of ceiling: log WARNING

**When the ceiling is hit:**
- `dispatcher.toggle_pause()` is called (pauses the scheduler)
- CRITICAL log entry is written
- Teams alert is sent (best-effort)
- The dispatcher stays paused until manually resumed via `/api/dispatcher/toggle`

### 15.2 Structured Logging (`app/core/logging_config.py`)

Every log line is a single JSON object, making it compatible with any log aggregator (Splunk, Datadog, CloudWatch, etc.):

```json
{
  "ts": "2026-04-15T15:30:45.123",
  "level": "INFO",
  "logger": "src.agent.loop",
  "msg": "[Agent] Dispatching ticket #12345 — Cannot connect to VPN",
  "exc": null
}
```

**Log file location:** `LOG_DIR/app.log` (defaults to project root `app.log` if `LOG_DIR` is not set).

**Log levels used:**

| Level | When |
|-------|------|
| DEBUG | Every tool call input/output during dispatch |
| INFO | Dispatch decisions, service start/stop, cycle completions |
| WARNING | API retries, 90% rate-limit threshold, slow responses |
| ERROR | Failed dispatches, CW API errors, individual ticket exceptions |
| CRITICAL | Dispatcher thread died, DB unreachable, rate limit exceeded |

### 15.3 SSE Streaming

The `/api/run/stream` endpoint streams Server-Sent Events to the portal UI while a dispatch run is in progress. The global `app/core/state.py` broadcaster holds the SSE queue and fan-outs to all connected clients. This gives the portal its live "Dispatch Feed" view.

---

## 16. Testing Infrastructure

**Framework:** pytest  
**Config:** `pytest.ini`

### 16.1 Shared Fixtures (`tests/conftest.py`)

| Fixture | What it provides |
|---------|-----------------|
| `mock_ticket` | Minimal unassigned CW ticket dict |
| `mock_ticket_full` | Full ticket with notes, activities, all fields |
| `mock_mappings` | Complete `mappings.json` structure in memory |
| `mock_config` | `portal_config.json` structure (dry_run=True) |
| `mock_cw_client` | `MagicMock` of `CWManageClient` with common return values pre-configured |
| `in_memory_db` | SQLAlchemy in-memory SQLite session (patches `DATABASE_URL`). All 7 tables created. Isolated per test. |
| `flask_app` | Flask test client backed by `in_memory_db` and mocked dispatcher |
| `sample_technician` | Pre-inserted `Technician` record in `in_memory_db` |
| `sample_decision` | Pre-inserted `DispatchDecision` record |

### 16.2 Test Files

| File | What it tests |
|------|--------------|
| `test_agent_loop.py` | `run_dispatch()` and `run_dispatch_batch()`: mocked Anthropic responses, tool execution, timeout behavior |
| `test_perception_tools.py` | `get_new_tickets()`, `get_technician_workload()`, `get_technician_schedule()`, `PatternDetector` fingerprinting |
| `test_memory_tools.py` | DB CRUD: `get_tech_profile()`, `update_tech_profile()`, `log_dispatch_decision()`, `get_similar_past_tickets()` |
| `test_action_tools.py` | `assign_ticket()`, `update_ticket_notes()`, `flag_for_human_review()` with dry_run=True and False |
| `test_members_db.py` | `/api/members/*` routes against in-memory DB |
| `test_web.py` | `/api/dispatcher/status`, `/api/notes`, `/api/health` routes |
| `test_e2e_connectwise.py` | Live integration tests against real CW API (requires valid .env). Skipped in CI if env vars absent. |
| `test_playwright.py` | Browser UI tests using Playwright. Verifies portal renders, status panels update, manual dispatch form works. |

### 16.3 Running Tests

```bash
# All unit tests (no CW API calls)
pytest tests/ -v --ignore=tests/test_e2e_connectwise.py --ignore=tests/test_playwright.py

# Integration tests (requires real CW credentials in .env)
pytest tests/test_e2e_connectwise.py -v

# UI tests (requires running server + Playwright browsers installed)
playwright install
pytest tests/test_playwright.py -v
```

---

## 17. Stubbed & Planned Features

The following features are partially or fully implemented in code but not yet operational in production:

### 17.1 Teams Messaging

**Status:** Stubbed. The `message_technician()` and `message_team_channel()` tools exist in the tool registry and return valid-looking responses, but they return `{"_stub": True, "message": "Teams messaging not yet configured"}` rather than sending real messages.

**Blocker:** Per-technician Teams chat IDs are not yet stored. The Microsoft Graph API requires knowing the thread ID for a 1:1 chat before a message can be sent. The `teams_user_mapping.json` only stores Graph user object IDs (used for presence), not chat thread IDs.

**Path to production:** Store per-tech chat thread IDs in the `technicians.teams_chat_id` column (to be added). The `teams.py` client already has the Graph authentication and HTTP session set up.

### 17.2 RAG / Similarity Search

**Status:** Minimal implementation. `get_similar_past_tickets()` performs basic SQL keyword search on `dispatch_decisions.ticket_summary`. It returns up to 5 prior decisions matching the keyword string.

**Planned:** Full vector similarity search using sentence embeddings (e.g., via `sentence-transformers` or a hosted embedding API). Each `DispatchDecision` would store a vector representation of the ticket summary, enabling semantic similarity matching rather than keyword search.

### 17.3 Routing Training Data

**Status:** `data/routing_training.json` exists as a placeholder. Not read by any code.

**Planned:** Fine-tuned or few-shot routing model trained on historical dispatch data. Could supplement Claude's reasoning or serve as a fast pre-filter before calling Claude.

### 17.4 Email Notifications

**Status:** SMTP credentials exist in `.env`. No email-sending code is active.

**Planned:** Dispatch summary emails, SLA breach alerts, daily reports via SMTP2GO.

### 17.5 Barracuda MSP Integration

**Status:** Credentials present in `.env` but no Barracuda client exists.

**Planned:** Direct webhook/API integration with Barracuda MSP to receive RMM alerts and create CW tickets automatically.

### 17.6 Custom Field Updates

**Status:** `update_custom_fields` tool exists and passes through to `cw_agent_tools/connectwise_manage_actions.py`. The tool works but the custom field schema for Core12's CW instance is not fully mapped.

---

## 18. End-to-End Workflow Walkthroughs

### 18.1 Happy Path: Tier-2 Network Ticket Auto-Assigned

```
1. Scheduler fires (every 30s)
2. CW API query: boards=["Dispatch"], statuses=["New"], owner in [null, "supportdesk", "APIBot"]
   → Returns ticket #18422: "Customer cannot connect to VPN" — Company: Acme Corp — Priority: High
3. PatternDetector.analyze_ticket(#18422)
   → Fingerprint: "a3f8c2d1e4b90f56"
   → No existing ActiveIncident → creates new (status="new")
   → is_repeat=False, is_storm=False, is_suppressed=False
   → _context: {is_repeat: false, occurrence_count: 1, matching_operator_notes: []}
4. Situation briefing built:
   → 0 active operator notes
   → Last 4h: 3 decisions (akloss×2, jsmith×1)
   → 0 active incidents
5. Agent loop begins. System prompt includes:
   - akloss: "Tier 2 networking specialist... Skills: networking, vpn, firewall"
   - jsmith: "Tier 1 generalist... Skills: desktop_support, m365"
6. Claude ASSESS: High priority, networking issue, Tier-2 skill required
7. Claude → get_similar_past_tickets("VPN connect")
   → Returns 2 prior decisions: both routed to akloss
8. Claude → get_technician_workload("akloss")
   → {"open_tickets": 4, "total_open": 22, "overloaded": false}  (4/22 = 18%)
9. Claude → get_tech_availability("akloss")
   → {"availability": "Available"}
10. Claude decides: akloss, confidence=0.91, reason="VPN expertise + low workload + available"
11. Claude → assign_ticket("akloss", 18422)
    → CW PATCH /service/tickets/18422: {"op":"replace","path":"/owner","value":{"identifier":"akloss"}}
    → CW PATCH status → "Assigned"
    → Response: {"success": true, "ticket_id": 18422, "assigned_to": "akloss"}
12. Claude → update_ticket_notes(18422, "Routed to Aaron Kloss (akloss): VPN expertise matches issue type. Workload 18%, availability: Available. Confidence: 91%. Alternatives: jsmith (Tier-1, insufficient skills).")
    → CW POST /service/tickets/18422/notes
13. Claude → message_technician("akloss", "High priority ticket #18422 assigned: Customer VPN issue at Acme Corp", 18422)
    → Returns {_stub: true} (Teams not yet connected)
14. Claude → log_dispatch_decision(18422, "akloss", reason, 0.91, [{identifier:"jsmith",reason:"Tier-1, insufficient for VPN"}])
    → DB INSERT into dispatch_decisions
15. Claude: stop_reason = "end_turn"
16. AgentTrace saved to DB (7 iterations, 11.3s, status="ok")
17. DispatchRun updated: tickets_processed=1, tickets_assigned=1
```

### 18.2 Suppression Path: Alert Storm

```
1. 9:15 AM — Ticket #18500: "Barracuda backup failed — Server01" — Acme Corp
   → Fingerprint: "bb7ac3f1..."
   → is_repeat=False, occurrence_count=1 → create ActiveIncident
2. 9:17 AM — Ticket #18501: "Barracuda backup failed — Server01" — Acme Corp
   → Same fingerprint → occurrence_count=2, is_repeat=True
   → Claude routes to the same tech already handling #18500
3. 9:19 AM — Ticket #18502: Same alert
   → occurrence_count=3, is_storm=True (3 in 4 minutes)
   → Claude: group_with_incident(18502, incident_id=42)
   → Claude: suppress_alert(incident_key="bb7ac3f1...", hours=4)
   → DB: active_incidents.suppressed_until = 1:19 PM
4. 9:21 AM — Ticket #18503: Same alert
   → is_suppressed=True, suppressed_until=1:19 PM
   → Claude: acknowledges, skips routing, logs: reason="suppressed until 1:19 PM"
   → No CW PATCH, no assignment
5. 1:19 PM — Suppression expires
6. 1:21 PM — Ticket #18512: Same alert (suppression has expired)
   → is_suppressed=False, occurrence_count=5
   → is_storm check: first_seen=9:15 AM (>1h ago), is_storm=False
   → Normal routing resumes
```

### 18.3 Human Review Path: Low Confidence

```
1. Ticket #18600: "QuickBooks won't open — getting license error" — Retail Co — Priority: Medium
2. PatternDetector: No existing incident, no operator notes
3. Claude → get_similar_past_tickets("QuickBooks license")
   → 0 results (no prior QuickBooks tickets in history)
4. Claude evaluates roster:
   → akloss: networking specialist — not a match
   → jsmith: Tier-1 generalist — possible match, no QuickBooks specialty noted
5. Claude DECIDE: no strong skill match, confidence=0.45
6. Claude → flag_for_human_review(18600, reason="No technician with QuickBooks/accounting software experience in roster. Ticket requires specialty skill.")
   → CW POST note: "[AI Dispatcher] Flagged for human review: no strong skill match. Confidence 0.45."
7. Claude → log_dispatch_decision(18600, assigned_tech_id=null, reason="Flagged — no skill match", confidence=0.45)
8. DispatchRun: tickets_processed=1, tickets_flagged=1
9. Operator sees ticket in "Flagged" queue in portal, manually assigns
```

---

## Appendix A: Dependency Summary

**Production (`requirements.txt`):**

| Package | Purpose |
|---------|---------|
| `flask` | Web framework |
| `anthropic` | Claude API SDK |
| `requests` | CW API HTTP client |
| `sqlalchemy` | ORM / database layer |
| `apscheduler` | Background scheduler |
| `python-dotenv` | Load `.env` file |
| `msal` | Microsoft Graph auth (Teams) |

**Development (`requirements-dev.txt`):**

| Package | Purpose |
|---------|---------|
| `pytest` | Test runner |
| `pytest-mock` | Mock/patch helpers |
| `playwright` | Browser automation (UI tests) |
| `pytest-playwright` | Playwright pytest plugin |
| `httpx` | Async HTTP client for tests |

---

## Appendix B: Key File Cross-Reference

| Concern | Primary File | Secondary File |
|---------|-------------|----------------|
| App startup | `app/__init__.py` | `run.py` |
| Agent loop | `src/agent/loop.py` | — |
| System prompt | `src/agent/prompts.py` | `src/agent/briefing.py` |
| Tool schemas | `src/agent/tool_definitions.py` | — |
| Tool execution | `src/agent/tool_registry.py` | — |
| CW API calls | `src/clients/connectwise.py` | `cw_agent_tools/connectwise_manage_client.py` (legacy) |
| Database | `src/clients/database.py` | — |
| Ticket fetching | `src/tools/perception/tickets.py` | — |
| Workload/schedule | `src/tools/perception/technicians.py` | — |
| Repeat/storm detection | `src/tools/perception/pattern_detector.py` | — |
| Tech profiles (DB ops) | `src/tools/memory/tech_profiles.py` | — |
| Decision logging | `src/tools/memory/decision_log.py` | — |
| Background scheduler | `services/dispatcher.py` | — |
| Technician roster | `data/mappings.json` | SQLite `technicians` table |
| App settings | `data/portal_config.json` | `.env` |
| Credentials | `.env` | — |
| Portal UI | `templates/index.html` | — |
| Test fixtures | `tests/conftest.py` | — |

---

*Document generated April 15, 2026. Reflects current codebase state on branch `master` at commit `7304e85`.*
