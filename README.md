# DVS Payload Manager — `pm-resources`

Everything needed to deploy and operate the Da Vinci Satellite payload subsystem
on the Hyperion OBC (SAMA5D2, Ubuntu/Debian ARM).

---

## File inventory

| File | Target path on OBC | Purpose |
|------|--------------------|---------|
| `payload_manager.py` | `/opt/dvs/` | Unified entry point — all hardware drivers, UART dispatcher, worker threads |
| `payload_manager.yaml` | `/opt/dvs/` | All runtime configuration (ports, addresses, thresholds, flags) |
| `requirements.txt` | *(run once)* | Python package dependencies |
| `dvs-payload.service` | `/etc/systemd/system/` | systemd unit: restart policy, resource limits, security hardening |
| `install.sh` | *(run as root)* | Idempotent deploy + verify script |
| `provision_boot.sh` | `/opt/dvs/` *(root:root 755)* | Privileged bootloader provisioning wrapper for `OBC_PROVISION_BOOT` |
| `sudoers.d/dvs-provision` | `/etc/sudoers.d/` | Narrow sudo rule — `debian` may run only `provision_boot.sh` as root |
| `logging_guide.md` | *(reference)* | Log query commands, storage budget, logrotate/journald tuning |

Legacy source files (`DicePayload.py`, `PayloadManager.py`, `Constants.py`) are
retained for reference. They are **not** deployed — `payload_manager.py` supersedes them.

---

## Prerequisites

```bash
# System packages
sudo apt-get install python3-pip python3-smbus fswebcam i2c-tools u-boot-tools

# Python packages
pip3 install --break-system-packages -r requirements.txt
```

`u-boot-tools` (provides `fw_setenv`) is only needed if `bootloader.target_type: uboot`
and `allow_elevation: true`. Skip on development x86 hosts.

---

## Deploy — one command

```bash
sudo ./install.sh
```

The script is idempotent (safe to re-run). It:
- Creates `/opt/dvs/` and `/opt/dicepayload/output/`
- Installs Python packages
- Deploys `payload_manager.py` and `payload_manager.yaml` (skips YAML if already customised)
- Writes `/etc/logrotate.d/dvs-payload` and `/etc/systemd/journald.conf.d/dvs-limits.conf`
- Installs and enables `dvs-payload.service`
- Runs 13 verification checks and prints a pass/fail summary

For bootloader provisioning support, additionally:

```bash
# Deploy the privileged helper and sudoers rule
sudo install -o root -g root -m 755 provision_boot.sh /opt/dvs/provision_boot.sh
sudo cp sudoers.d/dvs-provision /etc/sudoers.d/dvs-provision
sudo chmod 440 /etc/sudoers.d/dvs-provision
sudo visudo -c   # ALWAYS syntax-check after touching sudoers
```

Then set `allow_elevation: true` in `/opt/dvs/payload_manager.yaml` to enable
`OBC_PROVISION_BOOT` over UART.

---

## Verify the deployment

```bash
# Service health
systemctl status dvs-payload.service

# Live logs
journalctl -u dvs-payload.service -f

# Quick UART smoke test (from a terminal on the OBC or via serial console):
echo "PING" | socat - /dev/ttyS0,raw,echo=0,b115200
# Expected: OK PONG

# I2C bus scan (TMP100 should appear at 0x4F)
i2cdetect -y 1

# Confirm bootloader helper permissions
ls -l /opt/dvs/provision_boot.sh      # should be -rwxr-xr-x root root
sudo -l -U debian | grep provision    # should list /opt/dvs/provision_boot.sh
```

---

## Key configuration knobs (`payload_manager.yaml`)

| Section | Key | Flight value | Note |
|---------|-----|-------------|------|
| `serial` | `port` | `/dev/ttyS0` | OBC UART0 |
| `dice` | `i2c_address` | **⚠ TBD** | Confirm from address-space spreadsheet |
| `tmp100` | `i2c_address` | `0x4F` | Hardware-fixed |
| `logging` | `level` | `WARNING` | Use `INFO` for dev, `WARNING` for flight |
| `bootloader` | `target_type` | `uboot` | SAMA5D2 OBC; use `grub` on x86 dev hosts |
| `bootloader` | `allow_elevation` | `false` | Set `true` only after sudoers drop-in is installed |

---

## UART command reference (abbreviated)

All commands: `COMMAND [ARGS]\n` → `OK <payload>\n` or `ERR <reason>\n`

| Command | Response | Description |
|---------|----------|-------------|
| `PING` | `OK PONG` | Liveness |
| `HEALTH` | `OK dice=OK tmp100=OK camera=OK …` | Subsystem status |
| `READ_TEMP` | `OK temp_c=23.56` | TMP100 reading |
| `DICE_STATUS` | `OK status=0x01` | Raw DICE status byte |
| `DICE_CLAMP` / `DICE_UNCLAMP` | `OK CLAMPED` / `OK UNCLAMPED` | Non-blocking motor commands |
| `DICE_CLAMP_SYNC` / `DICE_UNCLAMP_SYNC` | `OK CLAMPED_SYNC` / … | Blocking (≤10 s) |
| `DICE_SEQUENCE` | `OK sequence done img=…` | Unclamp → clamp → capture |
| `CAM_CAPTURE` | `OK saved=<filename>` | Single frame |
| `CAM_SWEEP` | `OK sweep=22 frames` | Full exposure sweep |
| `TELEMETRY_GET` | `OK temperature_c=…; …` | Latest cached readings |
| `OBC_PROVISION_BOOT` | `OK BOOT_PROVISIONED` | Set bootloader to headless mode |
| `SHUTDOWN` | `OK SHUTTING_DOWN` | Graceful stop (service auto-restarts) |

Full command list with all 30+ DICE ICD commands: see `DVS_PAYLOAD_INTEGRATION_REPORT.md § Appendix A`.

---

## Bootloader provisioning

`OBC_PROVISION_BOOT` instructs the payload manager to configure the board for
fully headless autonomous boot (zero countdown, no operator prompt).

**Pre-conditions (both required):**
1. `sudoers.d/dvs-provision` installed and syntax-checked (`visudo -c`)
2. `bootloader.allow_elevation: true` in `payload_manager.yaml`

**What happens internally:**
```
UART "OBC_PROVISION_BOOT"
  → PayloadManager._bootloader_provision_handler()
      checks allow_elevation gate
      calls sudo -n /opt/dvs/provision_boot.sh --target uboot
        → backs up current U-Boot env to /opt/dvs/bootloader_backups/
        → fw_setenv bootdelay 0
        → fw_printenv bootdelay  (verify)
  → OK BOOT_PROVISIONED
```

**Dry-run test (no changes written):**
```bash
sudo /opt/dvs/provision_boot.sh --target uboot --dry-run
sudo /opt/dvs/provision_boot.sh --target grub --dry-run
```

**Idempotent:** re-sending the command when already provisioned returns
`OK BOOT_ALREADY_AUTONOMOUS` — not an error.

---

## Service management quick reference

```bash
sudo systemctl start   dvs-payload.service
sudo systemctl stop    dvs-payload.service
sudo systemctl restart dvs-payload.service
sudo systemctl status  dvs-payload.service

# Errors only:
journalctl -u dvs-payload.service -p err

# Since last boot:
journalctl -u dvs-payload.service -b

# Storage check:
du -sh /var/log/payload_manager.log*
journalctl --disk-usage
df -h /opt/dicepayload/output
```

---

## Further reading

- `logging_guide.md` — two-tier log architecture, storage budget, verbosity matrix
- `DVS_PAYLOAD_INTEGRATION_REPORT.md` — full integration report with architecture diagrams,
  all 13 edge-case risks, open items, and complete UART command reference
