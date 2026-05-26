# DVS Payload Manager — Logging & Storage Reference

## Architecture: two-tier logging

The payload manager writes logs through two independent pipelines simultaneously.
Understanding both is essential for on-orbit debugging.

```
payload_manager.py
       │
       ├─► Python RotatingFileHandler
       │        └─► /var/log/payload_manager.log   (size-capped, gzip-compressed)
       │             └─► managed by logrotate
       │
       └─► stdout / stderr  (PYTHONUNBUFFERED=1)
                └─► systemd journal  (dvs-payload.service: StandardOutput=journal)
                         └─► managed by journald size limits
```

Using both tiers is intentional:

| Concern | Python file log | systemd journal |
|---------|----------------|-----------------|
| Persistence across reboots | ✅ plain files, always | ✅ persistent journal |
| Searchable by time range | via `grep` / `awk` | `journalctl --since` |
| Searchable by level | via `grep` | `journalctl -p err` |
| Boot-correlated context | ❌ | ✅ boot ID, unit metadata |
| Readable without systemd tools | ✅ `cat`, `tail` | ❌ |
| Space-controlled independently | ✅ logrotate | ✅ journald.conf |

---

## 1. Python file log — `/var/log/payload_manager.log`

Configured in `payload_manager.yaml`:

```yaml
logging:
  level: INFO
  file: /var/log/payload_manager.log
  max_bytes: 10485760    # 10 MB per file
  backup_count: 5        # keep 5 rotated segments
  console: true          # also write to stdout → journal
```

Python's `RotatingFileHandler` rotates when the active file hits `max_bytes`,
keeping `backup_count` numbered copies:

```
/var/log/payload_manager.log       ← current (active writes)
/var/log/payload_manager.log.1     ← most recent rotation
/var/log/payload_manager.log.2
...
/var/log/payload_manager.log.5     ← oldest kept
```

**Maximum disk usage from Python rotation alone:** 10 MB × 6 files = **60 MB**.

### Tuning for flight

On a flash-constrained flight board, reduce further in `payload_manager.yaml`:

```yaml
logging:
  level: WARNING         # suppress INFO in steady state; save to DEBUG on debug builds
  max_bytes: 5242880     # 5 MB
  backup_count: 3        # 5 MB × 4 = 20 MB max
```

---

## 2. logrotate — time-based archiving

File: `/etc/logrotate.d/dvs-payload` (written by `install.sh`)

```
/var/log/payload_manager.log {
    weekly          # rotate once per week
    rotate 8        # keep 8 compressed archives ≈ 80 MB absolute max
    compress
    delaycompress   # keep latest rotation uncompressed for 1 cycle
    missingok
    notifempty
    create 640 debian adm
    postrotate
        /bin/kill -HUP $(systemctl show -p MainPID --value dvs-payload.service) 2>/dev/null || true
    endscript
}
```

The `postrotate` `SIGHUP` causes the process to re-open its log file handle
after the rotation, so no log lines are lost to a stale file descriptor.

**Manual test (dry-run, no changes):**
```bash
sudo logrotate --debug /etc/logrotate.d/dvs-payload
```

**Force an immediate rotation (for testing):**
```bash
sudo logrotate --force /etc/logrotate.d/dvs-payload
```

---

## 3. systemd journal limits

File: `/etc/systemd/journald.conf.d/dvs-limits.conf` (written by `install.sh`)

```ini
[Journal]
SystemMaxUse=64M          # hard cap on all journal files combined
SystemKeepFree=128M       # always keep 128 MB free on the partition
SystemMaxFileSize=8M      # rotate a single journal file at 8 MB
MaxRetentionSec=4week     # discard entries older than 4 weeks
SystemMaxFiles=8          # keep at most 8 journal files
RateLimitIntervalSec=30
RateLimitBurst=1000       # max 1000 messages per service per 30 s
```

**Maximum journal disk usage: 64 MB** (enforced by `SystemMaxUse`).

Apply changes without a full reboot:
```bash
sudo systemctl restart systemd-journald
```

Check current journal disk usage:
```bash
journalctl --disk-usage
```

Manually vacuum old journal entries:
```bash
sudo journalctl --vacuum-size=32M    # keep only newest 32 MB
sudo journalctl --vacuum-time=2weeks # discard entries older than 2 weeks
```

---

## 4. Day-to-day log commands

### Live tailing

```bash
# Follow the structured journal (with colour, level prefix, timestamp):
journalctl -u dvs-payload.service -f

# Follow the plain file log (useful over a low-bandwidth serial console):
tail -f /var/log/payload_manager.log

# Follow both simultaneously (requires two terminals or tmux):
# Terminal 1: journalctl -u dvs-payload.service -f
# Terminal 2: tail -f /var/log/payload_manager.log
```

### Filtered queries

```bash
# Only errors and above:
journalctl -u dvs-payload.service -p err

# Only WARNING and above, last 50 lines:
journalctl -u dvs-payload.service -p warning -n 50

# Everything from the last boot:
journalctl -u dvs-payload.service -b

# Everything between two timestamps:
journalctl -u dvs-payload.service \
    --since "2025-06-01 12:00:00" \
    --until "2025-06-01 13:00:00"

# Search the file log for I2C errors:
grep -i "i2c\|oserror\|errno" /var/log/payload_manager.log

# Search the file log for temperature readings:
grep "TMP100" /var/log/payload_manager.log | tail -20

# Count error lines per day in the file log:
grep "ERROR" /var/log/payload_manager.log | awk '{print $1}' | sort | uniq -c
```

### Service health

```bash
# Full status + recent journal excerpt:
systemctl status dvs-payload.service

# Exit code of the last run:
systemctl show dvs-payload.service -p ExecMainStatus

# How many times the service has restarted since boot:
systemctl show dvs-payload.service -p NRestarts

# Memory and CPU usage right now:
systemctl status dvs-payload.service   # shows cgroup stats
# or more detail:
cat /sys/fs/cgroup/system.slice/dvs-payload.service/memory.current
```

### Storage budget summary

```bash
# Quick view of all log-related disk consumption:
du -sh /var/log/payload_manager.log*
journalctl --disk-usage
df -h /var/log
df -h /opt/dicepayload/output
```

---

## 5. Storage budget on a 4 GB flash card

| Source | Max size | Config |
|--------|----------|--------|
| Python RotatingFileHandler | 60 MB | `max_bytes=10M`, `backup_count=5` |
| logrotate archives | 80 MB | `rotate 8`, `compress` |
| systemd journal | 64 MB | `SystemMaxUse=64M` |
| Camera output (`/opt/dicepayload/output`) | unbounded ⚠️ | see below |
| **Total (logs only)** | **~204 MB** | |

> **Camera images are the real storage risk.** A single 8 MP JPEG is ~3–5 MB.
> A 22-exposure sweep produces ~66–110 MB per run. On a 4 GB card, you have
> roughly 30–60 full sweeps before storage is exhausted.
>
> Recommended mitigations:
> - Add a housekeeping cron job to archive images to the OBC's eMMC or delete
>   images older than N days.
> - Set the camera output directory to a tmpfs mount (`/tmp/dicepayload`) and
>   explicitly downlink images before clearing.
> - Monitor free space in the `_health_check()` loop and inhibit further sweeps
>   when free space falls below a threshold.

### Example housekeeping cron entry (as user `debian`):

```cron
# crontab -e  (as debian)
# Delete images older than 7 days at 03:00 every day:
0 3 * * * find /opt/dicepayload/output -name "*.jpg" -mtime +7 -delete
```
