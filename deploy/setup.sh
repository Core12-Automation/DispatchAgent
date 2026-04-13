#!/usr/bin/env bash
# deploy/setup.sh
#
# Production setup script for the AI Dispatcher on a Linux VM.
# Run as root (or via sudo) from any directory.
#
# Usage:
#   sudo bash deploy/setup.sh [--source <project-dir>]
#
# The --source flag lets you point to a checked-out copy of the repo
# somewhere else (e.g., /tmp/DispatchAgent).  Defaults to the directory
# that contains this script's parent.
#
# Idempotent: safe to re-run for updates.

set -euo pipefail

# ── Configurable constants ────────────────────────────────────────────────────
INSTALL_DIR="/opt/ai-dispatcher"
SERVICE_USER="dispatcher"
SERVICE_NAME="ai-dispatcher"
LOG_DIR="/var/log/ai-dispatcher"
PYTHON_MIN="3.10"
VENV_DIR="${INSTALL_DIR}/venv"

# Resolve source directory (default: parent of this script)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$(dirname "${SCRIPT_DIR}")"

# Parse --source flag
while [[ $# -gt 0 ]]; do
    case "$1" in
        --source) SOURCE_DIR="$2"; shift 2 ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

# ── Helpers ───────────────────────────────────────────────────────────────────
info()  { echo -e "\033[1;34m[INFO]\033[0m  $*"; }
ok()    { echo -e "\033[1;32m[OK]\033[0m    $*"; }
warn()  { echo -e "\033[1;33m[WARN]\033[0m  $*"; }
die()   { echo -e "\033[1;31m[ERROR]\033[0m $*" >&2; exit 1; }

require_root() {
    [[ "$(id -u)" -eq 0 ]] || die "This script must be run as root (or via sudo)."
}

check_python() {
    local py
    py=$(command -v python3 2>/dev/null || true)
    [[ -n "$py" ]] || die "python3 not found. Install Python ${PYTHON_MIN}+ first."
    local ver
    ver=$(python3 -c "import sys; print('%d.%d' % sys.version_info[:2])")
    python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" \
        || die "Python ${ver} found but ${PYTHON_MIN}+ is required."
    ok "Python ${ver}"
}

# ── Begin ─────────────────────────────────────────────────────────────────────
require_root
echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║   AI Dispatcher — Production Setup           ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

info "Source  : ${SOURCE_DIR}"
info "Install : ${INSTALL_DIR}"
info "User    : ${SERVICE_USER}"
echo ""

# ── 1. System prerequisites ───────────────────────────────────────────────────
info "[1/8] Checking prerequisites ..."
check_python

for cmd in git rsync systemctl; do
    command -v "$cmd" &>/dev/null || die "'$cmd' is required but not installed."
done
ok "Prerequisites satisfied"

# ── 2. Create dedicated service user ─────────────────────────────────────────
info "[2/8] Creating service user '${SERVICE_USER}' ..."
if id "${SERVICE_USER}" &>/dev/null; then
    ok "User '${SERVICE_USER}' already exists"
else
    useradd \
        --system \
        --shell /sbin/nologin \
        --home-dir "${INSTALL_DIR}" \
        --create-home \
        --comment "AI Dispatcher service account" \
        "${SERVICE_USER}"
    ok "User '${SERVICE_USER}' created"
fi

# ── 3. Create install directory and copy project files ────────────────────────
info "[3/8] Installing project files to ${INSTALL_DIR} ..."
mkdir -p "${INSTALL_DIR}"

rsync -a --delete \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    --exclude='.env' \
    --exclude='venv/' \
    --exclude='data/dispatcher.db' \
    "${SOURCE_DIR}/" "${INSTALL_DIR}/"

# Ensure data directory exists
mkdir -p "${INSTALL_DIR}/data"
ok "Project files synced"

# ── 4. Python virtual environment & dependencies ──────────────────────────────
info "[4/8] Creating Python virtual environment ..."
if [[ ! -d "${VENV_DIR}" ]]; then
    python3 -m venv "${VENV_DIR}"
    ok "Virtualenv created at ${VENV_DIR}"
else
    ok "Virtualenv already exists — skipping creation"
fi

info "      Installing dependencies from requirements.txt ..."
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt"
ok "Dependencies installed"

# ── 5. .env file ──────────────────────────────────────────────────────────────
info "[5/8] Configuring .env file ..."
ENV_FILE="${INSTALL_DIR}/.env"
if [[ -f "${ENV_FILE}" ]]; then
    ok ".env already exists — not overwriting (edit manually to update secrets)"
else
    warn ".env does not exist — creating a template."
    warn "Edit ${ENV_FILE} and fill in your credentials before starting the service."
    cat > "${ENV_FILE}" <<'ENVTEMPLATE'
# AI Dispatcher — environment variables
# Fill in all values before starting the service.

# ConnectWise Manage API
CWM_SITE=https://na.myconnectwise.net/v4_6_release/apis/3.0
CWM_COMPANY_ID=yourcompanyid
CWM_PUBLIC_KEY=REPLACE_ME
CWM_PRIVATE_KEY=REPLACE_ME
CLIENT_ID=REPLACE_ME

# Anthropic Claude API
ANTHROPIC_API_KEY=REPLACE_ME

# Microsoft Teams (optional — for alerts)
TENANT_ID=
TEAMS_CLIENT_ID=
TEAMS_CLIENT_VALUE=
CHAT_ID=

# CW Manage portal URL (for ticket links in dashboard)
CWM_MANAGE_BASE=https://yourcompany.myconnectwise.net

# Rate limits (calls per hour; defaults shown)
CLAUDE_CALLS_PER_HOUR=200
CW_CALLS_PER_HOUR=2000

# Dispatcher schedule
DISPATCH_INTERVAL_SECONDS=60

# DRY_RUN flag — leave true until you have verified dispatch works correctly
# Change via the portal or portal_config.json
ENVTEMPLATE
fi

# Lock down .env — readable only by owner
chmod 600 "${ENV_FILE}"
chown "${SERVICE_USER}:${SERVICE_USER}" "${ENV_FILE}"
ok ".env permissions secured (600)"

# ── 6. Log directory ──────────────────────────────────────────────────────────
info "[6/8] Creating log directory ${LOG_DIR} ..."
mkdir -p "${LOG_DIR}"
chown "${SERVICE_USER}:${SERVICE_USER}" "${LOG_DIR}"
chmod 750 "${LOG_DIR}"
ok "Log directory ready"

# Install logrotate config
if [[ -f "${SOURCE_DIR}/deploy/logrotate.conf" ]]; then
    cp "${SOURCE_DIR}/deploy/logrotate.conf" /etc/logrotate.d/${SERVICE_NAME}
    ok "logrotate config installed at /etc/logrotate.d/${SERVICE_NAME}"
fi

# ── 7. Database migration ─────────────────────────────────────────────────────
info "[7/8] Running database migration ..."
# Run as the dispatcher user so the DB file is owned correctly
sudo -u "${SERVICE_USER}" \
    env HOME="${INSTALL_DIR}" \
    "${VENV_DIR}/bin/python" "${INSTALL_DIR}/scripts/migrate_to_db.py" \
    || warn "Migration returned non-zero — check output above"

chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}/data"
ok "Database migration complete"

# ── 8. Install and enable systemd service ─────────────────────────────────────
info "[8/8] Installing systemd service ..."
cp "${SOURCE_DIR}/deploy/ai-dispatcher.service" \
    "/etc/systemd/system/${SERVICE_NAME}.service"

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service"
ok "Service enabled (${SERVICE_NAME}.service)"

# Set final ownership
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"
# .env was already 600; restore after chown in case it changed
chmod 600 "${ENV_FILE}"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║   Setup complete!                            ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

if grep -q "REPLACE_ME" "${ENV_FILE}" 2>/dev/null; then
    warn "IMPORTANT: ${ENV_FILE} contains placeholder values."
    warn "Edit it with real credentials before starting the service:"
    warn "  sudo nano ${ENV_FILE}"
    warn "  sudo systemctl start ${SERVICE_NAME}"
else
    info "Starting service ..."
    systemctl start "${SERVICE_NAME}.service"
    sleep 2
    systemctl status "${SERVICE_NAME}.service" --no-pager || true
    echo ""
    ok "Service started. Health check:"
    curl -sf "http://127.0.0.1:5000/health" | python3 -m json.tool 2>/dev/null || \
        warn "Health endpoint not yet ready — give it a few seconds, then:"
    echo ""
    echo "  curl http://127.0.0.1:5000/health"
fi

echo ""
echo "Useful commands:"
echo "  sudo systemctl status  ${SERVICE_NAME}"
echo "  sudo systemctl restart ${SERVICE_NAME}"
echo "  sudo journalctl -u ${SERVICE_NAME} -f"
echo "  tail -f ${LOG_DIR}/app.log | python3 -m json.tool"
echo ""
