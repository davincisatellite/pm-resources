#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
# install.sh — DVS Payload Manager deployment script
# Target: Ubuntu/Debian ARM (Hyperion OBC)
#
# Usage:
#   chmod +x install.sh
#   sudo ./install.sh          # full install
#   sudo ./install.sh --verify # verification only (no changes)
#
# This script is idempotent: running it again after it has already succeeded
# will leave the system in the same correct state.
# ═══════════════════════════════════════════════════════════════════════════════

set -euo pipefail   # Exit on error, unset variable, or pipe failure
IFS=$'\n\t'

# ─── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
ok()      { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[ERROR]${RESET} $*" >&2; }
section() { echo -e "\n${BOLD}━━━  $*  ━━━${RESET}"; }

# ─── Configuration ─────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Deployment paths on the target filesystem
INSTALL_DIR="/opt/dvs"
OUTPUT_DIR="/opt/dicepayload/output"
LOG_DIR="/var/log"
LOG_FILE="${LOG_DIR}/payload_manager.log"
LOGROTATE_CONF="/etc/logrotate.d/dvs-payload"
SERVICE_FILE="/etc/systemd/system/dvs-payload.service"
SERVICE_NAME="dvs-payload.service"
SERVICE_USER="debian"
SERVICE_GROUP="debian"

# Source files (relative to this script)
SRC_PY="${SCRIPT_DIR}/payload_manager.py"
SRC_YAML="${SCRIPT_DIR}/payload_manager.yaml"
SRC_SERVICE="${SCRIPT_DIR}/dvs-payload.service"
SRC_BOOT_PROV="${SCRIPT_DIR}/provision_boot.sh"
SRC_REQS="${SCRIPT_DIR}/requirements.txt"

# ─── Guard: must be root ───────────────────────────────────────────────────────
if [[ "${EUID}" -ne 0 ]]; then
    error "This script must be run as root (use: sudo ./install.sh)"
    exit 1
fi

# ─── Parse arguments ──────────────────────────────────────────────────────────
VERIFY_ONLY=false
for arg in "$@"; do
    case "${arg}" in
        --verify) VERIFY_ONLY=true ;;
        --help|-h)
            echo "Usage: sudo ./install.sh [--verify]"
            echo "  --verify   Run verification checks only; make no changes"
            exit 0 ;;
        *) warn "Unknown argument: ${arg}" ;;
    esac
done

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 1 — Pre-flight checks
# ═══════════════════════════════════════════════════════════════════════════════
section "Pre-flight checks"

for src in "${SRC_PY}" "${SRC_YAML}" "${SRC_SERVICE}" "${SRC_BOOT_PROV}" "${SRC_REQS}"; do
    if [[ ! -f "${src}" ]]; then
        error "Required source file not found: ${src}"
        exit 1
    fi
    ok "Found ${src}"
done

# Check Python 3 is available
PYTHON3=$(command -v python3 || true)
if [[ -z "${PYTHON3}" ]]; then
    error "python3 not found. Install with: apt-get install python3"
    exit 1
fi
PYVER=$("${PYTHON3}" --version 2>&1)
ok "Python: ${PYVER}"

# Check systemd is available
if ! command -v systemctl &>/dev/null; then
    error "systemctl not found — is this a systemd-managed system?"
    exit 1
fi
ok "systemd available"

if [[ "${VERIFY_ONLY}" == true ]]; then
    info "Pre-flight checks passed."
fi

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 2 — Create directory structure
# ═══════════════════════════════════════════════════════════════════════════════
section "Directory setup"
[[ "${VERIFY_ONLY}" == true ]] && { info "Skipping (--verify mode)"; } || {

    # Deployment directory
    mkdir -p "${INSTALL_DIR}"
    chown "${SERVICE_USER}:${SERVICE_GROUP}" "${INSTALL_DIR}"
    chmod 750 "${INSTALL_DIR}"
    ok "Created ${INSTALL_DIR}"

    # Camera output directory
    mkdir -p "${OUTPUT_DIR}"
    chown "${SERVICE_USER}:${SERVICE_GROUP}" "${OUTPUT_DIR}"
    chmod 755 "${OUTPUT_DIR}"
    ok "Created ${OUTPUT_DIR}"

    # Log directory (usually already exists, but ensure correct perms)
    touch "${LOG_FILE}"
    chown "${SERVICE_USER}:adm" "${LOG_FILE}"
    chmod 640 "${LOG_FILE}"
    ok "Initialised ${LOG_FILE}"
}

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 3 — Install system packages
# ═══════════════════════════════════════════════════════════════════════════════
section "System packages"
[[ "${VERIFY_ONLY}" == true ]] && { info "Skipping (--verify mode)"; } || {

    apt-get update -qq
    apt-get install -y --no-install-recommends \
        python3-pip \
        python3-smbus \
        fswebcam \
        i2c-tools
    ok "System packages installed"

    # Ensure the service user is in the hardware access groups
    usermod -aG i2c    "${SERVICE_USER}" 2>/dev/null || warn "Group 'i2c' not found"
    usermod -aG dialout "${SERVICE_USER}"
    usermod -aG video   "${SERVICE_USER}"
    ok "User ${SERVICE_USER} added to hardware groups (i2c, dialout, video)"
}

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 4 — Install Python packages
# ═══════════════════════════════════════════════════════════════════════════════
section "Python packages"
[[ "${VERIFY_ONLY}" == true ]] && { info "Skipping (--verify mode)"; } || {

    "${PYTHON3}" -m pip install --quiet --break-system-packages \
        -r "${SRC_REQS}"
    ok "Python packages installed from requirements.txt"

    # Smoke-test critical imports
    for pkg in serial smbus2 yaml; do
        if "${PYTHON3}" -c "import ${pkg}" 2>/dev/null; then
            ok "import ${pkg} → OK"
        else
            warn "import ${pkg} failed — check requirements.txt"
        fi
    done
    if "${PYTHON3}" -c "import cv2" 2>/dev/null; then
        ok "import cv2 → OK"
    else
        warn "import cv2 failed — camera will use fswebcam fallback"
    fi
}

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 5 — Deploy application files
# ═══════════════════════════════════════════════════════════════════════════════
section "Deploying application files"
[[ "${VERIFY_ONLY}" == true ]] && { info "Skipping (--verify mode)"; } || {

    install -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 640 \
        "${SRC_PY}"   "${INSTALL_DIR}/payload_manager.py"
    ok "Deployed payload_manager.py"

    # Only deploy the config if one doesn't already exist
    # (avoid overwriting operator customisations on re-deploy)
    if [[ ! -f "${INSTALL_DIR}/payload_manager.yaml" ]]; then
        install -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 640 \
            "${SRC_YAML}" "${INSTALL_DIR}/payload_manager.yaml"
        ok "Deployed payload_manager.yaml (new install)"
    else
        warn "payload_manager.yaml already exists at ${INSTALL_DIR} — skipping to preserve local edits"
        warn "To reset config: sudo cp ${SRC_YAML} ${INSTALL_DIR}/payload_manager.yaml"
    fi

    install -o root -g root -m 755 \
        "${SRC_BOOT_PROV}" "${INSTALL_DIR}/provision_boot.sh"
    ok "Deployed provision_boot.sh"

    SUDOERS_BOOT_PROV="/etc/sudoers.d/dvs-payload-boot"
    cat > "${SUDOERS_BOOT_PROV}" <<'SUDOERS_EOF'
debian ALL=(root) NOPASSWD: /opt/dvs/provision_boot.sh
SUDOERS_EOF
    chmod 440 "${SUDOERS_BOOT_PROV}"
    if command -v visudo &>/dev/null && visudo -cf "${SUDOERS_BOOT_PROV}" &>/dev/null; then
        ok "sudoers rule validated at ${SUDOERS_BOOT_PROV}"
    else
        warn "sudoers rule written to ${SUDOERS_BOOT_PROV} (validation tool unavailable or returned warnings)"
    fi
}

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 6 — Configure logrotate
# ═══════════════════════════════════════════════════════════════════════════════
section "Log rotation (logrotate)"
[[ "${VERIFY_ONLY}" == true ]] && { info "Skipping (--verify mode)"; } || {

    cat > "${LOGROTATE_CONF}" <<'LOGROTATE_EOF'
# /etc/logrotate.d/dvs-payload
# Rotates the Payload Manager application log file.
# Python's RotatingFileHandler handles size-based rotation of individual
# files; logrotate handles time-based archiving and compression of the
# rotated segments so storage doesn't accumulate unboundedly.

/var/log/payload_manager.log {
    # Rotate once per week; use 'daily' on high-verbosity builds.
    weekly

    # Keep 8 weeks of history (8 × ~10 MB ≈ 80 MB max on disk).
    rotate 8

    # Compress old logs with gzip to save space on the flash storage.
    compress
    # Delay compression by one cycle so the just-rotated file is readable
    # if the service hasn't been restarted yet.
    delaycompress

    # Don't error if the log file is missing (e.g. service not started yet).
    missingok

    # Don't rotate an empty log.
    notifempty

    # Create the new log file immediately after rotation.
    create 640 debian adm

    # After rotation, send SIGHUP to the running payload manager so it
    # re-opens the log file handle.  The PID file approach is more reliable
    # than postrotate/endscript when running under systemd.
    postrotate
        /bin/kill -HUP $(systemctl show -p MainPID --value dvs-payload.service 2>/dev/null) 2>/dev/null || true
    endscript
}
LOGROTATE_EOF

    chmod 644 "${LOGROTATE_CONF}"
    ok "logrotate config written to ${LOGROTATE_CONF}"

    # Dry-run logrotate to confirm syntax is valid
    if logrotate --debug "${LOGROTATE_CONF}" &>/dev/null; then
        ok "logrotate config syntax OK"
    else
        warn "logrotate dry-run produced warnings — check ${LOGROTATE_CONF}"
    fi
}

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 7 — Configure journald storage limits
# ═══════════════════════════════════════════════════════════════════════════════
section "journald storage limits"
[[ "${VERIFY_ONLY}" == true ]] && { info "Skipping (--verify mode)"; } || {

    JOURNALD_CONF="/etc/systemd/journald.conf.d/dvs-limits.conf"
    mkdir -p "$(dirname "${JOURNALD_CONF}")"
    cat > "${JOURNALD_CONF}" <<'JOURNALD_EOF'
# /etc/systemd/journald.conf.d/dvs-limits.conf
# Prevents the flight board's flash storage from being exhausted by journal growth.
[Journal]
# Hard cap on total journal disk usage.
SystemMaxUse=64M

# Keep at least this much free on the partition at all times.
SystemKeepFree=128M

# Maximum size of a single journal file before rotation.
SystemMaxFileSize=8M

# Retain journal files for at most 4 weeks.
MaxRetentionSec=4week

# Maximum number of journal files to keep.
SystemMaxFiles=8

# Limit burst logging rate to prevent a runaway process from filling the
# journal: max 1000 messages per 30 seconds per service.
RateLimitIntervalSec=30
RateLimitBurst=1000
JOURNALD_EOF

    chmod 644 "${JOURNALD_CONF}"
    ok "journald limits written to ${JOURNALD_CONF}"
    systemctl restart systemd-journald
    ok "journald restarted to apply new limits"
}

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 8 — Install and enable the systemd service
# ═══════════════════════════════════════════════════════════════════════════════
section "systemd service installation"
[[ "${VERIFY_ONLY}" == true ]] && { info "Skipping (--verify mode)"; } || {

    # Stop the service if already running (graceful restart on re-deploy)
    if systemctl is-active --quiet "${SERVICE_NAME}"; then
        info "Stopping running service for re-deploy..."
        systemctl stop "${SERVICE_NAME}"
        ok "Service stopped"
    fi

    install -o root -g root -m 644 \
        "${SRC_SERVICE}" "${SERVICE_FILE}"
    ok "Service unit deployed to ${SERVICE_FILE}"

    systemctl daemon-reload
    ok "systemd daemon reloaded"

    systemctl enable "${SERVICE_NAME}"
    ok "Service enabled (will start on next boot)"

    systemctl start "${SERVICE_NAME}"
    ok "Service started"

    # Give the process 3 seconds to initialise before checking status
    sleep 3
}

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 9 — Verification
# ═══════════════════════════════════════════════════════════════════════════════
section "Verification"

PASS=0; FAIL=0

_check() {
    local label="$1"; shift
    if "$@" &>/dev/null; then
        ok "${label}"
        ((PASS++)) || true
    else
        error "FAIL: ${label}"
        ((FAIL++)) || true
    fi
}

# File existence
_check "payload_manager.py deployed"    test -f "${INSTALL_DIR}/payload_manager.py"
_check "payload_manager.yaml present"   test -f "${INSTALL_DIR}/payload_manager.yaml"
_check "provision_boot.sh deployed"     test -x "${INSTALL_DIR}/provision_boot.sh"
_check "Service unit installed"         test -f "${SERVICE_FILE}"
_check "logrotate config present"       test -f "${LOGROTATE_CONF}"
_check "Log file exists"                test -f "${LOG_FILE}"
_check "Output dir exists"              test -d "${OUTPUT_DIR}"

# Permissions
_check "payload_manager.py owner=debian" \
    bash -c "[[ \"\$(stat -c '%U' ${INSTALL_DIR}/payload_manager.py)\" == 'debian' ]]"
_check "Log file readable by adm group" \
    bash -c "[[ \"\$(stat -c '%G' ${LOG_FILE})\" == 'adm' ]]"

# systemd
_check "Service enabled"    systemctl is-enabled --quiet "${SERVICE_NAME}"
_check "Service active"     systemctl is-active  --quiet "${SERVICE_NAME}"

# Python imports
_check "import serial"  "${PYTHON3}" -c "import serial"
_check "import smbus2"  "${PYTHON3}" -c "import smbus2"
_check "import yaml"    "${PYTHON3}" -c "import yaml"

# Hardware devices (warn but don't fail if absent in dev environment)
if [[ -e /dev/i2c-1 ]]; then
    ok "I2C device /dev/i2c-1 present"
else
    warn "/dev/i2c-1 not found — I2C will be unavailable at runtime"
fi
if [[ -e /dev/ttyS0 ]]; then
    ok "UART device /dev/ttyS0 present"
else
    warn "/dev/ttyS0 not found — UART will be unavailable at runtime"
fi

# ── Summary ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}━━━  Summary  ━━━${RESET}"
echo -e "  Passed : ${GREEN}${PASS}${RESET}"
echo -e "  Failed : $([ "${FAIL}" -gt 0 ] && echo "${RED}${FAIL}${RESET}" || echo "${GREEN}0${RESET}")"
echo ""

if [[ "${FAIL}" -gt 0 ]]; then
    error "${FAIL} check(s) failed — review output above"
    exit 1
fi

echo -e "${GREEN}${BOLD}Installation complete.${RESET}"
echo ""
echo -e "${BOLD}Useful commands:${RESET}"
echo "  Live service status  :  systemctl status ${SERVICE_NAME}"
echo "  Follow journal logs  :  journalctl -u ${SERVICE_NAME} -f"
echo "  Follow file log      :  tail -f ${LOG_FILE}"
echo "  Filter by level      :  journalctl -u ${SERVICE_NAME} -p err"
echo "  Restart service      :  sudo systemctl restart ${SERVICE_NAME}"
echo "  Stop service         :  sudo systemctl stop ${SERVICE_NAME}"
echo "  Disable on boot      :  sudo systemctl disable ${SERVICE_NAME}"
echo "  View recent logs     :  journalctl -u ${SERVICE_NAME} --since '1 hour ago'"
echo "  Check I2C bus        :  i2cdetect -y 1"
echo ""
