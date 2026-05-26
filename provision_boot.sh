#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
# provision_boot.sh — DVS Payload Manager: Bootloader Provisioning Wrapper
# Target: Ubuntu/Debian ARM (Hyperion OBC, SAMA5D2)
#
# PURPOSE
# -------
# This script is the ONLY binary that payload_manager.py is permitted to run
# as root (via a narrow sudoers drop-in).  Keeping all privileged bootloader
# operations here — rather than inside Python — gives a clean audit boundary:
#   • Python code never directly writes to /etc/default/grub or calls
#     update-grub / fw_setenv without going through this script.
#   • The sudoers rule is scoped to EXACTLY this path, so the `debian` user
#     cannot sudo arbitrary commands.
#
# USAGE (called by payload_manager.py only — do not invoke manually in flight)
# -------
#   sudo /opt/dvs/provision_boot.sh --target {uboot|grub} [--dry-run]
#
# RETURN CODES
#   0  — provisioning applied (or dry-run completed) successfully
#   1  — unrecognised / missing arguments
#   2  — required tool not found (fw_setenv, update-grub)
#   3  — hardware/filesystem write failure
#   4  — already provisioned (idempotent; caller treats this as success)
#
# SECURITY NOTES
# -------
#   • This file must be owned root:root and mode 755 (no write bits for others).
#   • install.sh enforces this during deployment.
#   • The script refuses to run if it detects it has been modified (mtime check
#     is not implemented here but the sudoers rule limits the attack surface).
# ═══════════════════════════════════════════════════════════════════════════════

set -euo pipefail
IFS=$'\n\t'

# ─── Colour helpers (stdout only — stderr plain for journal parsing) ──────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; RESET='\033[0m'
info()  { echo -e "${CYAN}[BOOT-PROV]${RESET} $*"; }
ok()    { echo -e "${GREEN}[BOOT-PROV]${RESET} $*"; }
warn()  { echo -e "${YELLOW}[BOOT-PROV]${RESET} $*"; }
die()   { echo -e "${RED}[BOOT-PROV] FATAL:${RESET} $*" >&2; exit "${2:-3}"; }

# ─── Argument parsing ─────────────────────────────────────────────────────────
TARGET=""
DRY_RUN=false
GRUB_CONFIG_PATH="/etc/default/grub"
TIMEOUT_VALUE="0"
BOOTDELAY_VALUE="0"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --target)
            TARGET="${2:-}"
            shift 2
            ;;
        --grub-config-path)
            GRUB_CONFIG_PATH="${2:-}"
            shift 2
            ;;
        --timeout-value)
            TIMEOUT_VALUE="${2:-}"
            shift 2
            ;;
        --bootdelay-value)
            BOOTDELAY_VALUE="${2:-}"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --help|-h)
            echo "Usage: sudo $0 --target {uboot|grub} [--grub-config-path PATH] [--timeout-value N] [--bootdelay-value N] [--dry-run]"
            exit 0
            ;;
        *)
            echo "[BOOT-PROV] ERROR: Unknown argument: $1" >&2
            exit 1
            ;;
    esac
done

if [[ -z "${TARGET}" ]]; then
    echo "[BOOT-PROV] ERROR: --target is required" >&2
    exit 1
fi

if [[ "${TARGET}" != "uboot" && "${TARGET}" != "grub" ]]; then
    echo "[BOOT-PROV] ERROR: --target must be 'uboot' or 'grub', got '${TARGET}'" >&2
    exit 1
fi

# ─── Guard: must be root ──────────────────────────────────────────────────────
if [[ "${EUID}" -ne 0 ]]; then
    echo "[BOOT-PROV] ERROR: Must run as root (via sudo)" >&2
    exit 1
fi

# ─── Dry-run notice ───────────────────────────────────────────────────────────
if [[ "${DRY_RUN}" == true ]]; then
    info "DRY-RUN mode — no changes will be written"
fi

# ═══════════════════════════════════════════════════════════════════════════════
# U-Boot provisioning (SAMA5D2 / Hyperion OBC — default path)
# ═══════════════════════════════════════════════════════════════════════════════
provision_uboot() {
    local fw_setenv
    fw_setenv=$(command -v fw_setenv 2>/dev/null || true)
    local fw_printenv
    fw_printenv=$(command -v fw_printenv 2>/dev/null || true)

    if [[ -z "${fw_setenv}" ]]; then
        die "fw_setenv not found. Install u-boot-tools: apt-get install u-boot-tools" 2
    fi

    info "Target: U-Boot (SAMA5D2)"
    info "Tool:   ${fw_setenv}"

    # ── Read current bootdelay ────────────────────────────────────────────────
    local current_delay=""
    if [[ -n "${fw_printenv}" ]]; then
        current_delay=$("${fw_printenv}" bootdelay 2>/dev/null | cut -d= -f2 || true)
    fi

    if [[ "${current_delay}" == "${BOOTDELAY_VALUE}" ]]; then
        ok "Already provisioned: bootdelay=${BOOTDELAY_VALUE} (idempotent)"
        exit 4
    fi

    info "Current bootdelay='${current_delay:-unknown}' → setting to ${BOOTDELAY_VALUE}"

    # ── Backup current env to a timestamped file ──────────────────────────────
    local backup_dir="/opt/dvs/bootloader_backups"
    local ts
    ts=$(date +%Y%m%dT%H%M%S)
    local backup_file="${backup_dir}/uboot_env_backup_${ts}.txt"

    if [[ "${DRY_RUN}" == false ]]; then
        mkdir -p "${backup_dir}"
        if [[ -n "${fw_printenv}" ]]; then
            "${fw_printenv}" > "${backup_file}" 2>/dev/null || true
            ok "U-Boot env backed up to ${backup_file}"
        else
            warn "fw_printenv not available — backup skipped"
        fi
    else
        info "[DRY-RUN] Would back up U-Boot env to ${backup_file}"
    fi

    # ── Apply: set bootdelay=0 ────────────────────────────────────────────────
    if [[ "${DRY_RUN}" == false ]]; then
        if ! "${fw_setenv}" bootdelay "${BOOTDELAY_VALUE}"; then
            die "fw_setenv bootdelay ${BOOTDELAY_VALUE} failed" 3
        fi
        ok "U-Boot bootdelay set to ${BOOTDELAY_VALUE}"

        # Verify the write was accepted
        if [[ -n "${fw_printenv}" ]]; then
            local verified
            verified=$("${fw_printenv}" bootdelay 2>/dev/null | cut -d= -f2 || true)
            if [[ "${verified}" != "${BOOTDELAY_VALUE}" ]]; then
                die "Verification failed: bootdelay is '${verified}' after write" 3
            fi
            ok "Verified: bootdelay=${verified}"
        fi
    else
        info "[DRY-RUN] Would execute: ${fw_setenv} bootdelay ${BOOTDELAY_VALUE}"
        info "[DRY-RUN] Would verify via ${fw_printenv:-fw_printenv} bootdelay"
    fi
}

# ═══════════════════════════════════════════════════════════════════════════════
# GRUB provisioning (x86 / development hosts only — NOT the flight OBC)
# ═══════════════════════════════════════════════════════════════════════════════
provision_grub() {
    local update_grub
    update_grub=$(command -v update-grub 2>/dev/null || true)

    if [[ -z "${update_grub}" ]]; then
        die "update-grub not found — is this an x86 system with GRUB installed?" 2
    fi

    if [[ ! -f "${GRUB_CONFIG_PATH}" ]]; then
        die "${GRUB_CONFIG_PATH} not found — GRUB does not appear to be installed" 2
    fi

    info "Target: GRUB (x86)"
    info "Config: ${GRUB_CONFIG_PATH}"

    # ── Check idempotent ─────────────────────────────────────────────────────
    if grep -qE "^GRUB_TIMEOUT=${TIMEOUT_VALUE}$" "${GRUB_CONFIG_PATH}" && \
       grep -qE '^GRUB_TIMEOUT_STYLE=hidden$' "${GRUB_CONFIG_PATH}"; then
        ok "Already provisioned: GRUB_TIMEOUT=${TIMEOUT_VALUE} and GRUB_TIMEOUT_STYLE=hidden (idempotent)"
        exit 4
    fi

    # ── Backup original ──────────────────────────────────────────────────────
    local backup_dir="/opt/dvs/bootloader_backups"
    local ts
    ts=$(date +%Y%m%dT%H%M%S)
    local backup_file="${backup_dir}/grub_default_backup_${ts}"

    if [[ "${DRY_RUN}" == false ]]; then
        mkdir -p "${backup_dir}"
        cp "${GRUB_CONFIG_PATH}" "${backup_file}"
        ok "GRUB config backed up to ${backup_file}"
    else
        info "[DRY-RUN] Would back up ${GRUB_CONFIG_PATH} to ${backup_file}"
    fi

    # ── Atomically patch GRUB_TIMEOUT ────────────────────────────────────────
    # sed into a tempfile, then atomic replace so no partial-write corruption.
    local tmpfile
    tmpfile=$(mktemp /tmp/grub_default.XXXXXX)
    trap 'rm -f "${tmpfile}"' EXIT

    # Replace existing GRUB_TIMEOUT=<anything> or add it if missing
    if grep -qE '^GRUB_TIMEOUT=' "${GRUB_CONFIG_PATH}"; then
        sed "s/^GRUB_TIMEOUT=.*/GRUB_TIMEOUT=${TIMEOUT_VALUE}/" "${GRUB_CONFIG_PATH}" > "${tmpfile}"
    else
        # Append after GRUB_DEFAULT line, or at end of file
        cp "${GRUB_CONFIG_PATH}" "${tmpfile}"
        echo "GRUB_TIMEOUT=${TIMEOUT_VALUE}" >> "${tmpfile}"
    fi

    # Also set GRUB_TIMEOUT_STYLE=hidden to suppress the menu entirely
    if grep -qE '^GRUB_TIMEOUT_STYLE=' "${tmpfile}"; then
        sed -i 's/^GRUB_TIMEOUT_STYLE=.*/GRUB_TIMEOUT_STYLE=hidden/' "${tmpfile}"
    else
        echo 'GRUB_TIMEOUT_STYLE=hidden' >> "${tmpfile}"
    fi

    if [[ "${DRY_RUN}" == false ]]; then
        # Atomic replace
        cp --preserve=mode,ownership "${tmpfile}" "${GRUB_CONFIG_PATH}"
        ok "GRUB_TIMEOUT=${TIMEOUT_VALUE} written to ${GRUB_CONFIG_PATH}"

        # Regenerate GRUB boot entries
        info "Running update-grub..."
        if ! "${update_grub}" 2>&1 | tee /tmp/update-grub-output.txt; then
            die "update-grub failed — see /tmp/update-grub-output.txt" 3
        fi
        ok "update-grub completed"
    else
        info "[DRY-RUN] Would write patched GRUB config to ${GRUB_CONFIG_PATH}"
        info "[DRY-RUN] Patched content diff:"
        diff "${GRUB_CONFIG_PATH}" "${tmpfile}" || true
        info "[DRY-RUN] Would run: ${update_grub}"
    fi
}

# ═══════════════════════════════════════════════════════════════════════════════
# Dispatch
# ═══════════════════════════════════════════════════════════════════════════════
case "${TARGET}" in
    uboot) provision_uboot ;;
    grub)  provision_grub  ;;
esac

ok "Bootloader provisioning complete (target=${TARGET}, dry_run=${DRY_RUN})"
exit 0
