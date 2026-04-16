# DispatchAgent

AI-powered ConnectWise Manage ticket dispatcher for Managed Service Providers. Continuously monitors configured boards for unassigned tickets and uses Claude (Anthropic) to route each ticket to the most appropriate technician — based on skills, real-time workload, and availability.

## How it works

1. A background scheduler polls ConnectWise Manage every 30–60 seconds for unassigned tickets.
2. Each ticket is fingerprinted and checked for repeat patterns, alert storms, or active suppressions.
3. A Claude-powered agent reasons through a 10-step dispatch workflow using 19 tools (read ticket history, check workload, verify availability, assign, note, log).
4. The decision is written back to ConnectWise Manage with an internal note explaining the reasoning.
5. A web portal provides real-time dispatch monitoring, manual controls, technician profile management, and natural-language operator notes.

For the full technical breakdown — database schema, API endpoints, agent tools, routing logic, and data sources — see [TECHNICAL_OVERVIEW.md](TECHNICAL_OVERVIEW.md).

## Stack

- **Python 3.11+** / Flask
- **Anthropic Claude** (`claude-sonnet-4-6`) via tool-use API
- **ConnectWise Manage REST API** (v2025_1)
- **SQLAlchemy** / SQLite
- **APScheduler** (background polling)
- **Microsoft Teams Graph API** (presence queries)

## Setup

### 1. Clone and install

```bash
git clone https://github.com/Core12-Automation/DispatchAgent.git
cd DispatchAgent
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

Required variables:

```
ANTHROPIC_API_KEY=sk-ant-api03-...
CWM_SITE=https://api-na.myconnectwise.net/v2025_1/apis/3.0/
CWM_COMPANY_ID=YourCompany
CWM_PUBLIC_KEY=...
CWM_PRIVATE_KEY=...
CLIENT_ID=...
```

### 3. Configure boards and technicians

Edit `data/portal_config.json` to set which boards to monitor:

```json
{
  "boards_to_scan": ["Dispatch"],
  "route_from_statuses": ["New", "New (Email connector)"],
  "unrouted_owner_identifiers": ["supportdesk", "APIBot"],
  "dry_run": true
}
```

Edit `data/mappings.json` to add your technicians and ConnectWise ID mappings (board IDs, member IDs, status IDs). Then sync to the database:

```bash
curl -X POST http://localhost:5000/api/members/sync
```

### 4. Run

```bash
python run.py
```

Portal available at `http://localhost:5000`.

Set `dry_run: false` in `portal_config.json` (or via the Settings tab in the portal) when ready to go live.

## Portal

The web UI at `/` provides:

- **Live dispatch feed** — real-time SSE stream of routing decisions as they happen
- **Dispatcher controls** — pause/resume/run-once the background scheduler
- **Technician profiles** — view and edit skills, specialties, notes, routability
- **Operator notes** — type plain English ("Mike is out sick today") and the system creates structured routing instructions
- **Settings** — edit `portal_config.json` and `.env` variables without restarting

## Key API endpoints

| Endpoint | Purpose |
|----------|---------|
| `POST /api/dispatch/run-single` | Dispatch a specific ticket by ID |
| `GET /api/dispatcher/status` | Scheduler state |
| `POST /api/dispatcher/toggle` | Pause / resume |
| `GET /api/dispatcher/metrics` | Assignment counts and averages |
| `GET /api/members/workload` | Open ticket counts per tech |
| `POST /api/notes/chat` | Natural language → operator note |
| `GET /api/notes/briefing` | Current situation briefing |
| `GET /api/health` | Health check |

## Testing

```bash
pip install -r requirements-dev.txt

# Unit tests (no live API required)
pytest tests/ -v --ignore=tests/test_e2e_connectwise.py --ignore=tests/test_playwright.py

# Integration tests (requires valid .env with real CW credentials)
pytest tests/test_e2e_connectwise.py -v

# UI tests (requires running server + Playwright)
playwright install
pytest tests/test_playwright.py -v
```

## Configuration files

| File | Purpose |
|------|---------|
| `.env` | API keys and credentials (never commit) |
| `data/portal_config.json` | Operational settings (boards, dry_run, model, etc.) |
| `data/mappings.json` | ConnectWise ID lookups + technician roster |
| `data/teams_user_mapping.json` | CW identifier → Microsoft Graph user ID (for Teams presence) |

## Architecture

See [TECHNICAL_OVERVIEW.md](TECHNICAL_OVERVIEW.md) for:

- Full system architecture diagram
- Complete database schema (7 tables)
- All 19 agent tools documented
- ConnectWise API endpoints called
- Routing decision logic and fingerprinting algorithm
- End-to-end workflow walkthroughs
