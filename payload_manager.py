#!/usr/bin/env python3
"""
payload_manager.py
==================
DVS Da Vinci Satellite — Integrated Payload Manager
Target: Ubuntu/Debian ARM (Hyperion OBC, SAMA5D2)

Usage:
    python3 payload_manager.py [--config /path/to/payload_manager.yaml] [--log-level DEBUG]

System packages (Ubuntu/Debian):
    sudo apt-get install python3-smbus fswebcam

Python packages:
    pip3 install pyserial smbus2 pyyaml opencv-python

Architecture
------------

                    ┌───────────────────────────────────────────┐
                    │             PayloadManager                │
                    │  (orchestrates drivers + worker threads)  │
                    └────────┬──────────┬──────────┬────────────┘
                             │          │          │
             ┌───────────────┘          │          └────────────────┐
             ▼                          ▼                           ▼
    ┌─────────────────┐    ┌──────────────────────┐    ┌───────────────────┐
    │   UARTWorker    │    │  TelemetryWorker      │    │   CameraWorker    │
    │  (Thread)       │    │  (Thread)             │    │   (Thread)        │
    └────────┬────────┘    └──────┬───────┬────────┘    └────────┬──────────┘
             │                   │       │                       │
             ▼                   ▼       ▼                       ▼
    ┌──────────────────┐  ┌──────────┐ ┌──────────────┐  ┌──────────────────┐
    │ CommandDispatcher│  │ TMP100   │ │ DiceI2CDriver│  │  CameraDriver    │
    └──────────────────┘  │ Driver   │ │  (I2C bus 1) │  │ (OpenCV/fswebcam)│
             │            └──────────┘ └──────────────┘  └──────────────────┘
             ▼                    ╲           /
    ┌──────────────────┐          ┌──────────┐
    │  UARTInterface   │          │  I2CBus  │   ← per-bus singleton + lock
    │  (pyserial)      │          │ (shared) │
    └──────────────────┘          └──────────┘

UART Protocol
-------------
  OBC → PM :  "COMMAND [ARG1 ARG2 ...]\n"
  PM  → OBC:  "OK <payload>\n"  or  "ERR <reason>\n"

Available commands (see register_*_commands for full list):
  PING                     — liveness check
  HEALTH                   — subsystem status summary
  DICE_STATUS              — raw DICE status byte
  DICE_LED_ON / _OFF
  DICE_CLAMP / _UNCLAMP    — non-blocking
  DICE_CLAMP_SYNC / _UNCLAMP_SYNC — blocks until motion complete
  DICE_STOP
  DICE_M1_CW / _CCW  DICE_M2_CW / _CCW
  DICE_M1_CW_M2_CW, DICE_M1_CCW_M2_CCW, ...
  DICE_READ_M1_SPEED / _M2_SPEED / _M1_LEN / _M2_LEN / _M1_POS
  DICE_READ_LED_BRIGHT / _LED_STATUS
  DICE_WRITE_M1_SPEED <v>  — set motor1 PWM (0-255)
  DICE_WRITE_M2_SPEED <v>
  DICE_WRITE_M1_LEN <v>
  DICE_WRITE_M2_LEN <v>
  DICE_WRITE_LED_BRIGHT <v>
  DICE_SWITCHES_ON / _OFF
  DICE_RESET
  DICE_SEQUENCE            — unclamp → clamp → capture
  READ_TEMP                — TMP100 temperature in °C
  CAM_CAPTURE              — single-frame capture
  CAM_SWEEP                — full exposure sweep
  TELEMETRY_GET            — latest cached sensor readings
  SHUTDOWN                 — graceful stop
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import queue
import select
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


# ─────────────────────────────────────────────────────────────────────────────
# Optional third-party imports — degrade gracefully when packages are missing
# ─────────────────────────────────────────────────────────────────────────────

try:
    import yaml as _yaml
    _YAML_OK = True
except ImportError:
    _YAML_OK = False
    _yaml = None  # type: ignore

try:
    import serial as _serial
    _SERIAL_OK = True
except ImportError:
    _SERIAL_OK = False
    _serial = None  # type: ignore

# Try smbus2 first (actively maintained), fall back to smbus (system package)
try:
    import smbus2 as _smbus
    _SMBUS_OK = True
except ImportError:
    try:
        import smbus as _smbus  # type: ignore
        _SMBUS_OK = True
    except ImportError:
        _SMBUS_OK = False
        _smbus = None  # type: ignore

try:
    import cv2 as _cv2
    _CV2_OK = True
except ImportError:
    _CV2_OK = False
    _cv2 = None  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# 1.  CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_CONFIG: Dict[str, Any] = {
    "serial": {
        "port": "/dev/ttyS0",
        "baud_rate": 115200,
        "read_timeout_s": 1.0,
        "reconnect_delay_s": 5.0,
        "encoding": "utf-8",
    },
    "dice": {
        "i2c_bus": 1,
        # TODO: Confirm address from "Payload i2c bus address space.xlsx"
        "i2c_address": 0x08,
        "response_delay_s": 0.010,   # 10 ms: Arduino preparation time
        "byte_read_delay_s": 0.001,  # 1 ms between individual byte reads
        "status_timeout_s": 10.0,
    },
    "tmp100": {
        "i2c_bus": 1,
        "i2c_address": 0x4F,
        "poll_interval_s": 30.0,
    },
    "camera": {
        "device_index": 0,
        "resolution": {"width": 3264, "height": 2448},
        "fps": 30,
        "connect_retries": 15,
        "connect_retry_delay_s": 2.0,
        "output_dir": "/opt/dicepayload/output",
        "exposure_sweep": list(range(-10, 11)) + [20],
        "inter_exposure_delay_s": 0.5,
        "fallback_device": "/dev/video0",
        "fallback_resolution": "640x480",
    },
    "logging": {
        "level": "INFO",
        "file": "/var/log/payload_manager.log",
        "max_bytes": 10_485_760,   # 10 MB
        "backup_count": 5,
        "console": True,
    },
    "health_monitor": {
        "check_interval_s": 60.0,
    },
}


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """
    Load config from a YAML (.yaml/.yml) or JSON (.json) file.
    Deep-merges with DEFAULT_CONFIG so any missing key falls back to its default.
    """
    cfg = _deep_merge({}, DEFAULT_CONFIG)

    if path is None:
        return cfg

    config_path = Path(path)
    if not config_path.exists():
        print(
            f"[WARN] Config file not found: {path} — using built-in defaults.",
            file=sys.stderr,
        )
        return cfg

    try:
        text = config_path.read_text(encoding="utf-8")
        if config_path.suffix in (".yaml", ".yml"):
            if not _YAML_OK:
                raise RuntimeError(
                    "pyyaml is not installed. "
                    "Install it with: pip3 install pyyaml"
                )
            data = _yaml.safe_load(text) or {}
        else:
            data = json.loads(text)
        cfg = _deep_merge(cfg, data)
    except Exception as exc:
        print(
            f"[ERROR] Failed to load config '{path}': {exc} — using defaults.",
            file=sys.stderr,
        )

    return cfg


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into a copy of *base*."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


# ─────────────────────────────────────────────────────────────────────────────
# 2.  LOGGING
# ─────────────────────────────────────────────────────────────────────────────

def setup_logging(cfg: Dict[str, Any]) -> logging.Logger:
    """
    Configure a rotating-file logger with optional console echo.
    All timestamps are UTC.
    """
    log_cfg = cfg.get("logging", {})
    level = getattr(logging, str(log_cfg.get("level", "INFO")).upper(), logging.INFO)

    logger = logging.getLogger("payload_manager")
    logger.setLevel(level)
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s [%(threadName)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    fmt.converter = time.gmtime  # UTC

    log_file = log_cfg.get("file", "/var/log/payload_manager.log")
    try:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=int(log_cfg.get("max_bytes", 10_485_760)),
            backupCount=int(log_cfg.get("backup_count", 5)),
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError as exc:
        print(f"[WARN] Cannot open log file '{log_file}': {exc}", file=sys.stderr)

    if log_cfg.get("console", True):
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        logger.addHandler(ch)

    return logger


# ─────────────────────────────────────────────────────────────────────────────
# 3.  SHARED STATE
# ─────────────────────────────────────────────────────────────────────────────

class TelemetryStore:
    """
    Thread-safe store for the most recent reading of each named sensor.
    Every entry carries the value and an ISO-8601 UTC timestamp.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: Dict[str, Dict[str, Any]] = {}

    def update(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = {
                "value": value,
                "ts": datetime.now(timezone.utc).isoformat(),
            }

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            entry = self._data.get(key)
            return dict(entry) if entry else None

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._data.items()}


# ─────────────────────────────────────────────────────────────────────────────
# 4.  HARDWARE DRIVERS
# ─────────────────────────────────────────────────────────────────────────────

# ── 4a.  Shared I2C bus manager ──────────────────────────────────────────────

class I2CBus:
    """
    Singleton per bus number.

    Multiple drivers on the same physical I2C bus share one SMBus handle and
    one threading.Lock.  Acquiring the lock before every transaction prevents
    bus collisions between the DiceI2CDriver and TMP100Driver running on
    separate threads.
    """

    _instances: Dict[int, "I2CBus"] = {}
    _class_lock = threading.Lock()

    def __new__(cls, bus_num: int) -> "I2CBus":
        with cls._class_lock:
            if bus_num not in cls._instances:
                instance = super().__new__(cls)
                instance._bus_num = bus_num          # type: ignore[attr-defined]
                instance._bus_lock = threading.Lock()  # type: ignore[attr-defined]
                instance._smbus_handle = None          # type: ignore[attr-defined]
                cls._instances[bus_num] = instance
        return cls._instances[bus_num]

    @property
    def lock(self) -> threading.Lock:
        return self._bus_lock  # type: ignore[attr-defined]

    @property
    def bus(self):  # returns smbus2.SMBus / smbus.SMBus
        return self._smbus_handle  # type: ignore[attr-defined]

    def open(self) -> None:
        if not _SMBUS_OK:
            raise RuntimeError(
                "smbus2 / smbus not installed. "
                "Install with: pip3 install smbus2  (or: apt-get install python3-smbus)"
            )
        if self._smbus_handle is None:  # type: ignore[attr-defined]
            self._smbus_handle = _smbus.SMBus(self._bus_num)  # type: ignore[attr-defined]

    def close(self) -> None:
        if self._smbus_handle is not None:  # type: ignore[attr-defined]
            try:
                self._smbus_handle.close()  # type: ignore[attr-defined]
            except Exception:
                pass
            self._smbus_handle = None  # type: ignore[attr-defined]

    def reset(self) -> None:
        self.close()
        time.sleep(0.5)
        self.open()


# ── 4b.  DICE Arduino I2C driver ─────────────────────────────────────────────

class DiceI2CDriver:
    """
    Full I2C driver for the DICE Arduino controller (all 30 ICD commands).
    Thread-safe through the shared I2CBus lock.

    Read/write methods return None / False (respectively) instead of raising
    on I/O errors so callers never need to handle OSError themselves.
    """

    # ── Status codes ──────────────────────────────────────────────────────
    STATUS_INIT = 0x00
    STATUS_OK   = 0x01
    STATUS_FAIL = 0xF1

    # ── Motor position codes ──────────────────────────────────────────────
    POS_UNKNOWN   = 0x00
    POS_CLAMPED   = 0x01
    POS_UNCLAMPED = 0x02

    # ── Command bytes (from ICD) ──────────────────────────────────────────
    CMD_STATUS           = 0x00
    CMD_R_M1_SPEED       = 0x01
    CMD_R_M2_SPEED       = 0x02
    CMD_R_M1_LENGTH      = 0x03
    CMD_R_M2_LENGTH      = 0x04
    CMD_R_M1_POSITION    = 0x05
    CMD_R_LED_BRIGHTNESS = 0x06
    CMD_R_LED_STATUS     = 0x07
    CMD_W_M1_SPEED       = 0x08
    CMD_W_M2_SPEED       = 0x09
    CMD_W_M1_LENGTH      = 0x0A
    CMD_W_M2_LENGTH      = 0x0B
    CMD_W_LED_BRIGHTNESS = 0x0C
    CMD_STOP_M1_M2       = 0x10
    CMD_RUN_M1_CW        = 0x12
    CMD_RUN_M1_CCW       = 0x13
    CMD_RUN_M2_CW        = 0x18
    CMD_RUN_M2_CCW       = 0x1C
    CMD_RUN_M1_CW_M2_CW  = 0x1A
    CMD_RUN_M1_CCW_M2_CW = 0x1B
    CMD_RUN_M1_CW_M2_CCW = 0x1E
    CMD_RUN_M1_CCW_M2_CCW= 0x1F
    CMD_LED_ON           = 0x20
    CMD_LED_OFF          = 0x21
    CMD_CLAMP            = 0x22
    CMD_UNCLAMP          = 0x23
    CMD_W_DISABLE_ALL    = 0xE0
    CMD_W_ENABLE_ALL     = 0xEF
    CMD_RESET            = 0xF2

    def __init__(self, cfg: Dict[str, Any], logger: logging.Logger) -> None:
        self._log          = logger.getChild("DiceI2C")
        self._bus_num      = int(cfg.get("i2c_bus", 1))
        self._address      = int(cfg.get("i2c_address", 0x08))
        self._resp_delay   = float(cfg.get("response_delay_s", 0.010))
        self._byte_delay   = float(cfg.get("byte_read_delay_s", 0.001))
        self._status_to    = float(cfg.get("status_timeout_s", 10.0))
        self._i2c          = I2CBus(self._bus_num)
        self._available    = False

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def open(self) -> None:
        self._i2c.open()
        self._available = True
        self._log.info(
            "DICE I2C driver opened  bus=%d  addr=0x%02X",
            self._bus_num, self._address,
        )

    def close(self) -> None:
        self._available = False

    def reset(self) -> None:
        self._log.warning("Resetting DICE I2C driver")
        self.close()
        try:
            self._i2c.reset()
            self._available = True
            self._log.info("DICE I2C driver reset OK")
        except Exception as exc:
            self._log.error("DICE I2C reset failed: %s", exc)

    # ── Low-level I2C primitives ──────────────────────────────────────────

    def _send(self, command: int, data: Optional[int] = None) -> None:
        """Write a command byte (+ optional data byte) to the controller."""
        with self._i2c.lock:
            if data is not None:
                self._i2c.bus.write_i2c_block_data(self._address, command, [data])
            else:
                self._i2c.bus.write_byte(self._address, command)

    def _read_response(self, command: int) -> int:
        """
        Send *command* and read back the 4-byte response packet.

        Packet format (Arduino → Pi):
            Byte 0: 0x24  '$'        start marker
            Byte 1: command_id       echo of the command sent
            Byte 2: data_byte        the actual payload
            Byte 3: 0x0A  '\\n'      stop marker

        Returns the data byte (index 2).
        Logs a warning if framing bytes are unexpected but still returns data.
        """
        with self._i2c.lock:
            self._i2c.bus.write_byte(self._address, command)
            time.sleep(self._resp_delay)

            # Prefer block read; fall back to sequential byte reads on error
            try:
                response = self._i2c.bus.read_i2c_block_data(self._address, 0, 4)
            except Exception as exc:
                self._log.debug("Block read failed (%s), using byte reads", exc)
                response = []
                for _ in range(4):
                    # FIX: was erroneously calling read_byte_block (non-existent)
                    response.append(self._i2c.bus.read_byte(self._address))
                    time.sleep(self._byte_delay)

        start, cmd_echo, data_byte, stop = response
        if start != 0x24 or stop != 0x0A:
            self._log.warning(
                "Bad packet framing from DICE: %s  (sent cmd=0x%02X)",
                [hex(b) for b in response], command,
            )
        return data_byte

    # ── Safe wrappers (never raise, return None / False on I/O fault) ─────

    def _safe_send(self, command: int, data: Optional[int] = None) -> bool:
        if not self._available:
            return False
        try:
            self._send(command, data)
            return True
        except OSError as exc:
            self._log.error("I2C send error cmd=0x%02X: %s", command, exc)
            self._available = False
            return False

    def _safe_read(self, command: int) -> Optional[int]:
        if not self._available:
            return None
        try:
            return self._read_response(command)
        except OSError as exc:
            self._log.error("I2C read error cmd=0x%02X: %s", command, exc)
            self._available = False
            return None

    # ── Status ────────────────────────────────────────────────────────────

    def get_status(self) -> Optional[int]:
        """Returns STATUS_INIT / STATUS_OK / STATUS_FAIL, or None on error."""
        return self._safe_read(self.CMD_STATUS)

    def wait_until_ready(self, timeout: Optional[float] = None) -> bool:
        """Block until status is not 'running', or until *timeout* seconds."""
        deadline = time.monotonic() + (timeout or self._status_to)
        while time.monotonic() < deadline:
            s = self.get_status()
            if s in (self.STATUS_OK, self.STATUS_INIT, self.STATUS_FAIL):
                return True
            time.sleep(0.1)
        self._log.warning(
            "wait_until_ready timed out after %.1f s", timeout or self._status_to
        )
        return False

    # ── Read commands ─────────────────────────────────────────────────────

    def read_motor1_speed(self)    -> Optional[int]: return self._safe_read(self.CMD_R_M1_SPEED)
    def read_motor2_speed(self)    -> Optional[int]: return self._safe_read(self.CMD_R_M2_SPEED)
    def read_motor1_length(self)   -> Optional[int]: return self._safe_read(self.CMD_R_M1_LENGTH)
    def read_motor2_length(self)   -> Optional[int]: return self._safe_read(self.CMD_R_M2_LENGTH)
    def read_motor1_position(self) -> Optional[int]: return self._safe_read(self.CMD_R_M1_POSITION)
    def read_led_brightness(self)  -> Optional[int]: return self._safe_read(self.CMD_R_LED_BRIGHTNESS)
    def read_led_status(self)      -> Optional[int]: return self._safe_read(self.CMD_R_LED_STATUS)

    # ── Write commands ────────────────────────────────────────────────────

    def write_motor1_speed(self, v: int)    -> bool: return self._safe_send(self.CMD_W_M1_SPEED, v)
    def write_motor2_speed(self, v: int)    -> bool: return self._safe_send(self.CMD_W_M2_SPEED, v)
    def write_motor1_length(self, v: int)   -> bool: return self._safe_send(self.CMD_W_M1_LENGTH, v)
    def write_motor2_length(self, v: int)   -> bool: return self._safe_send(self.CMD_W_M2_LENGTH, v)
    def write_led_brightness(self, v: int)  -> bool: return self._safe_send(self.CMD_W_LED_BRIGHTNESS, v)

    # ── LED ──────────────────────────────────────────────────────────────

    def led_on(self)  -> bool: return self._safe_send(self.CMD_LED_ON)
    def led_off(self) -> bool: return self._safe_send(self.CMD_LED_OFF)

    # ── Motor actions ─────────────────────────────────────────────────────

    def stop_motors(self)         -> bool: return self._safe_send(self.CMD_STOP_M1_M2)
    def run_m1_cw(self)           -> bool: return self._safe_send(self.CMD_RUN_M1_CW)
    def run_m1_ccw(self)          -> bool: return self._safe_send(self.CMD_RUN_M1_CCW)
    def run_m2_cw(self)           -> bool: return self._safe_send(self.CMD_RUN_M2_CW)
    def run_m2_ccw(self)          -> bool: return self._safe_send(self.CMD_RUN_M2_CCW)
    def run_m1_cw_m2_cw(self)     -> bool: return self._safe_send(self.CMD_RUN_M1_CW_M2_CW)
    def run_m1_ccw_m2_cw(self)    -> bool: return self._safe_send(self.CMD_RUN_M1_CCW_M2_CW)
    def run_m1_cw_m2_ccw(self)    -> bool: return self._safe_send(self.CMD_RUN_M1_CW_M2_CCW)
    def run_m1_ccw_m2_ccw(self)   -> bool: return self._safe_send(self.CMD_RUN_M1_CCW_M2_CCW)

    # ── Switch control ────────────────────────────────────────────────────

    def disable_all_switches(self) -> bool: return self._safe_send(self.CMD_W_DISABLE_ALL)
    def enable_all_switches(self)  -> bool: return self._safe_send(self.CMD_W_ENABLE_ALL)

    def configure_switches(
        self,
        clamped1: bool = True,
        clamped2: bool = True,
        unclamped1: bool = True,
        unclamped2: bool = True,
    ) -> bool:
        """Build and send the switch-configuration byte (0xEx)."""
        cmd = 0xE0
        if clamped1:   cmd |= 0x01
        if clamped2:   cmd |= 0x02
        if unclamped1: cmd |= 0x04
        if unclamped2: cmd |= 0x08
        return self._safe_send(cmd)

    def reset_device(self) -> bool:
        return self._safe_send(self.CMD_RESET)

    # ── Compound sequences ────────────────────────────────────────────────

    def clamp(self, sync: bool = False) -> bool:
        ok = self._safe_send(self.CMD_CLAMP)
        return (ok and self.wait_until_ready()) if sync else ok

    def unclamp(self, sync: bool = False) -> bool:
        ok = self._safe_send(self.CMD_UNCLAMP)
        return (ok and self.wait_until_ready()) if sync else ok

    def set_parameters(
        self,
        m1_speed: int,
        m2_speed: int,
        m1_length: int,
        m2_length: int,
        led_brightness: int,
        switches: Tuple[bool, bool, bool, bool] = (True, True, True, True),
    ) -> bool:
        """Write all motion/LED parameters atomically; returns True only if all succeed."""
        return all([
            self.write_motor1_speed(m1_speed),
            self.write_motor2_speed(m2_speed),
            self.write_motor1_length(m1_length),
            self.write_motor2_length(m2_length),
            self.write_led_brightness(led_brightness),
            self.configure_switches(*switches),
        ])


# ── 4c.  TMP100 Temperature Sensor Driver ────────────────────────────────────

class TMP100Driver:
    """
    I2C driver for the TMP100 digital thermometer (Texas Instruments).
    Configured for 12-bit resolution, continuous conversion mode.
    """

    REG_TEMP     = 0x00
    REG_CONFIG   = 0x01
    CONFIG_12BIT = 0x60   # SD=0 (continuous), R1=R0=1 (12-bit)

    def __init__(self, cfg: Dict[str, Any], logger: logging.Logger) -> None:
        self._log          = logger.getChild("TMP100")
        self._bus_num      = int(cfg.get("i2c_bus", 1))
        self._address      = int(cfg.get("i2c_address", 0x4F))
        self._poll_interval = float(cfg.get("poll_interval_s", 30.0))
        self._i2c          = I2CBus(self._bus_num)
        self._available    = False

    def open(self) -> None:
        self._i2c.open()
        with self._i2c.lock:
            self._i2c.bus.write_byte_data(
                self._address, self.REG_CONFIG, self.CONFIG_12BIT
            )
        time.sleep(0.5)    # Allow first conversion cycle to complete
        self._available = True
        self._log.info(
            "TMP100 opened  bus=%d  addr=0x%02X", self._bus_num, self._address
        )

    def close(self) -> None:
        self._available = False

    def reset(self) -> None:
        self._log.warning("Resetting TMP100 driver")
        self.close()
        try:
            self._i2c.reset()
            self._available = True
            self._log.info("TMP100 driver reset OK")
        except Exception as exc:
            self._log.error("TMP100 reset failed: %s", exc)

    def get_temperature(self) -> Optional[float]:
        """Return temperature in °C (12-bit precision, ±0.0625 °C), or None on error."""
        if not self._available:
            return None
        try:
            with self._i2c.lock:
                data = self._i2c.bus.read_i2c_block_data(
                    self._address, self.REG_TEMP, 2
                )
            raw = ((data[0] << 8) | (data[1] & 0xF0)) >> 4
            if raw > 2047:   # Two's complement for negative temperatures
                raw -= 4096
            return round(raw * 0.0625, 4)
        except OSError as exc:
            self._log.error("TMP100 read error: %s", exc)
            self._available = False
            return None


# ── 4d.  Camera Driver ────────────────────────────────────────────────────────

class CameraDriver:
    """
    IMX179 USB camera driver.

    Uses OpenCV (cv2) as the primary capture backend.
    Falls back to fswebcam CLI for single-shot captures when cv2 is unavailable.

    A per-instance threading.Lock ensures the camera handle is not accessed
    concurrently from both the CameraWorker thread and any synchronous calls.
    """

    def __init__(self, cfg: Dict[str, Any], logger: logging.Logger) -> None:
        self._log         = logger.getChild("Camera")
        self._index       = int(cfg.get("device_index", 0))
        res               = cfg.get("resolution", {})
        self._width       = int(res.get("width", 3264))
        self._height      = int(res.get("height", 2448))
        self._fps         = int(cfg.get("fps", 30))
        self._retries     = int(cfg.get("connect_retries", 15))
        self._retry_delay = float(cfg.get("connect_retry_delay_s", 2.0))
        self._output_dir  = Path(cfg.get("output_dir", "/opt/dicepayload/output"))
        self._exposures   = list(cfg.get("exposure_sweep", list(range(-10, 11)) + [20]))
        self._expo_delay  = float(cfg.get("inter_exposure_delay_s", 0.5))
        self._fb_device   = cfg.get("fallback_device", "/dev/video0")
        self._fb_res      = cfg.get("fallback_resolution", "640x480")

        self._cap         = None
        self._lock        = threading.Lock()
        self._available   = False

        self._output_dir.mkdir(parents=True, exist_ok=True)

    def open(self) -> None:
        if not _CV2_OK:
            self._log.warning(
                "opencv-python not installed — camera will use fswebcam fallback "
                "(single captures only; exposure sweep unavailable)"
            )
            self._available = True
            return

        last_exc: Optional[Exception] = None
        for attempt in range(1, self._retries + 1):
            try:
                cap = _cv2.VideoCapture(self._index)
                if not cap.isOpened():
                    raise RuntimeError(
                        f"cv2.VideoCapture({self._index}) could not be opened"
                    )
                cap.set(_cv2.CAP_PROP_FRAME_WIDTH,  self._width)
                cap.set(_cv2.CAP_PROP_FRAME_HEIGHT, self._height)
                cap.set(_cv2.CAP_PROP_FPS,          self._fps)
                self._cap       = cap
                self._available = True
                self._log.info(
                    "Camera opened  index=%d  res=%dx%d  attempt=%d/%d",
                    self._index, self._width, self._height,
                    attempt, self._retries,
                )
                return
            except Exception as exc:
                last_exc = exc
                self._log.warning(
                    "Camera open attempt %d/%d failed: %s",
                    attempt, self._retries, exc,
                )
                time.sleep(self._retry_delay)

        raise RuntimeError(
            f"Camera failed to open after {self._retries} attempts: {last_exc}"
        )

    def close(self) -> None:
        self._available = False
        if self._cap is not None:
            with self._lock:
                try:
                    self._cap.release()
                except Exception:
                    pass
                self._cap = None

    def reset(self) -> None:
        self._log.warning("Resetting camera driver")
        self.close()
        try:
            self.open()
        except Exception as exc:
            self._log.error("Camera reset failed: %s", exc)

    # ── Internal capture helpers ──────────────────────────────────────────

    def _stamp(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def _write_opencv(self, exposure: Optional[int] = None) -> Path:
        """Capture one frame via OpenCV at the specified exposure."""
        if exposure is not None:
            self._cap.set(_cv2.CAP_PROP_AUTO_EXPOSURE, 0)
            time.sleep(0.05)
            self._cap.set(_cv2.CAP_PROP_EXPOSURE, exposure)
            time.sleep(self._expo_delay)

        ret, frame = self._cap.read()
        if not ret:
            raise RuntimeError("cap.read() returned False")

        expo_tag = f"_expo{exposure}" if exposure is not None else ""
        path = self._output_dir / f"{self._stamp()}{expo_tag}.jpg"
        if not _cv2.imwrite(str(path), frame):
            raise RuntimeError(f"cv2.imwrite failed for {path}")
        return path

    def _write_fswebcam(self, path: Optional[Path] = None) -> Path:
        """Capture one frame via the fswebcam CLI (fallback path)."""
        if path is None:
            path = self._output_dir / f"{self._stamp()}_fswebcam.jpg"
        cmd = [
            "fswebcam",
            "-d", self._fb_device,
            "-r", self._fb_res,
            "--no-banner",
            str(path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            raise RuntimeError(f"fswebcam: {result.stderr.strip()}")
        return path

    # ── Public API ────────────────────────────────────────────────────────

    def capture_single(self) -> Optional[Path]:
        """Thread-safe single frame capture. Returns saved path, or None on error."""
        if not self._available:
            self._log.warning("Capture requested but camera is unavailable")
            return None
        with self._lock:
            try:
                if _CV2_OK and self._cap:
                    return self._write_opencv()
                return self._write_fswebcam()
            except Exception as exc:
                self._log.error("capture_single failed: %s", exc)
                self._available = False
                return None

    def exposure_sweep(self, exposures: Optional[List[int]] = None) -> List[Path]:
        """
        Capture one frame at each exposure in *exposures* (or the configured sweep).
        Returns the list of paths that were successfully written.
        Requires OpenCV; logs a warning and returns [] if only fswebcam is available.
        """
        if not self._available:
            self._log.warning("Sweep requested but camera is unavailable")
            return []
        if not (_CV2_OK and self._cap):
            self._log.warning(
                "Exposure sweep requires OpenCV — falling back to single fswebcam capture"
            )
            single = self.capture_single()
            return [single] if single else []

        sweep   = exposures if exposures is not None else self._exposures
        saved: List[Path] = []

        with self._lock:
            for expo in sweep:
                try:
                    path = self._write_opencv(expo)
                    saved.append(path)
                    self._log.debug("Saved %s", path.name)
                except Exception as exc:
                    self._log.error("Exposure %d capture failed: %s", expo, exc)

        self._log.info(
            "Exposure sweep complete: %d / %d frames saved",
            len(saved), len(sweep),
        )
        return saved


# ─────────────────────────────────────────────────────────────────────────────
# 5.  COMMAND DISPATCHER
# ─────────────────────────────────────────────────────────────────────────────

class CommandDispatcher:
    """
    Maps uppercase command strings to handler callables.

    Registration:  dispatcher.register("CMD_NAME", handler_fn)
    Dispatch:      response_bytes = dispatcher.dispatch("CMD_NAME ARG1 ARG2")

    Handlers receive positional string arguments and must return bytes.
    Unhandled exceptions in handlers are caught and converted to ERR responses.
    """

    def __init__(self, logger: logging.Logger) -> None:
        self._log      = logger.getChild("Dispatcher")
        self._handlers: Dict[str, Callable[..., bytes]] = {}

    def register(self, command: str, handler: Callable[..., bytes]) -> None:
        self._handlers[command.upper()] = handler

    def dispatch(self, raw: str) -> bytes:
        parts = raw.strip().split()
        if not parts:
            return b"ERR EMPTY_COMMAND\n"
        verb, args = parts[0].upper(), parts[1:]

        handler = self._handlers.get(verb)
        if handler is None:
            self._log.warning("Unknown command: %r", verb)
            return f"ERR UNKNOWN_CMD {verb}\n".encode()

        try:
            return handler(*args)
        except TypeError as exc:
            return f"ERR BAD_ARGS {exc}\n".encode()
        except Exception as exc:
            self._log.exception("Handler %r raised unexpectedly: %s", verb, exc)
            return f"ERR INTERNAL {exc}\n".encode()


# ── Response helpers ──────────────────────────────────────────────────────────

def _ok(payload: str = "") -> bytes:
    line = f"OK {payload}".strip()
    return (line + "\n").encode()

def _err(reason: str) -> bytes:
    return f"ERR {reason}\n".encode()

def _val(label: str, value: Any) -> bytes:
    return _ok(f"{label}={value}")


# ── Command registration ──────────────────────────────────────────────────────

def register_dice_commands(
    dispatcher:  CommandDispatcher,
    dice:        DiceI2CDriver,
    camera:      CameraDriver,
    telemetry:   TelemetryStore,
) -> None:
    """Register the complete DICE payload command set with the dispatcher."""

    # ── Liveness / status ─────────────────────────────────────────────────
    dispatcher.register("PING",        lambda: b"OK PONG\n")
    dispatcher.register("DICE_STATUS", lambda: _val("status", dice.get_status()))

    # ── LED ───────────────────────────────────────────────────────────────
    dispatcher.register("DICE_LED_ON",
        lambda: _ok("LED_ON")  if dice.led_on()  else _err("LED_ON failed"))
    dispatcher.register("DICE_LED_OFF",
        lambda: _ok("LED_OFF") if dice.led_off() else _err("LED_OFF failed"))

    # ── Motor actions ─────────────────────────────────────────────────────
    dispatcher.register("DICE_STOP",
        lambda: _ok("STOPPED") if dice.stop_motors() else _err("STOP failed"))
    dispatcher.register("DICE_M1_CW",
        lambda: _ok("M1_CW")   if dice.run_m1_cw()   else _err("M1_CW failed"))
    dispatcher.register("DICE_M1_CCW",
        lambda: _ok("M1_CCW")  if dice.run_m1_ccw()  else _err("M1_CCW failed"))
    dispatcher.register("DICE_M2_CW",
        lambda: _ok("M2_CW")   if dice.run_m2_cw()   else _err("M2_CW failed"))
    dispatcher.register("DICE_M2_CCW",
        lambda: _ok("M2_CCW")  if dice.run_m2_ccw()  else _err("M2_CCW failed"))
    dispatcher.register("DICE_M1_CW_M2_CW",
        lambda: _ok("M1CW_M2CW")    if dice.run_m1_cw_m2_cw()    else _err("failed"))
    dispatcher.register("DICE_M1_CCW_M2_CW",
        lambda: _ok("M1CCW_M2CW")   if dice.run_m1_ccw_m2_cw()   else _err("failed"))
    dispatcher.register("DICE_M1_CW_M2_CCW",
        lambda: _ok("M1CW_M2CCW")   if dice.run_m1_cw_m2_ccw()   else _err("failed"))
    dispatcher.register("DICE_M1_CCW_M2_CCW",
        lambda: _ok("M1CCW_M2CCW")  if dice.run_m1_ccw_m2_ccw()  else _err("failed"))

    # ── Clamp / unclamp ───────────────────────────────────────────────────
    dispatcher.register("DICE_CLAMP",
        lambda: _ok("CLAMPED")        if dice.clamp()           else _err("CLAMP failed"))
    dispatcher.register("DICE_UNCLAMP",
        lambda: _ok("UNCLAMPED")      if dice.unclamp()         else _err("UNCLAMP failed"))
    dispatcher.register("DICE_CLAMP_SYNC",
        lambda: _ok("CLAMPED_SYNC")   if dice.clamp(sync=True)  else _err("CLAMP_SYNC failed"))
    dispatcher.register("DICE_UNCLAMP_SYNC",
        lambda: _ok("UNCLAMPED_SYNC") if dice.unclamp(sync=True) else _err("UNCLAMP_SYNC failed"))

    # ── Read parameters ───────────────────────────────────────────────────
    dispatcher.register("DICE_READ_M1_SPEED",
        lambda: _val("m1_speed",      dice.read_motor1_speed()))
    dispatcher.register("DICE_READ_M2_SPEED",
        lambda: _val("m2_speed",      dice.read_motor2_speed()))
    dispatcher.register("DICE_READ_M1_LEN",
        lambda: _val("m1_length",     dice.read_motor1_length()))
    dispatcher.register("DICE_READ_M2_LEN",
        lambda: _val("m2_length",     dice.read_motor2_length()))
    dispatcher.register("DICE_READ_M1_POS",
        lambda: _val("m1_position",   dice.read_motor1_position()))
    dispatcher.register("DICE_READ_LED_BRIGHT",
        lambda: _val("led_brightness",dice.read_led_brightness()))
    dispatcher.register("DICE_READ_LED_STATUS",
        lambda: _val("led_status",    dice.read_led_status()))

    # ── Write parameters (accept a single integer argument) ───────────────
    def _write_param(setter: Callable, label: str, *args: str) -> bytes:
        if not args:
            return _err(f"{label} requires a value argument")
        try:
            val = int(args[0])
        except ValueError:
            return _err(f"{label}: expected integer, got {args[0]!r}")
        return _ok(f"{label}={val}") if setter(val) else _err(f"{label} write failed")

    dispatcher.register("DICE_WRITE_M1_SPEED",
        lambda *a: _write_param(dice.write_motor1_speed,   "M1_SPEED",   *a))
    dispatcher.register("DICE_WRITE_M2_SPEED",
        lambda *a: _write_param(dice.write_motor2_speed,   "M2_SPEED",   *a))
    dispatcher.register("DICE_WRITE_M1_LEN",
        lambda *a: _write_param(dice.write_motor1_length,  "M1_LENGTH",  *a))
    dispatcher.register("DICE_WRITE_M2_LEN",
        lambda *a: _write_param(dice.write_motor2_length,  "M2_LENGTH",  *a))
    dispatcher.register("DICE_WRITE_LED_BRIGHT",
        lambda *a: _write_param(dice.write_led_brightness, "LED_BRIGHT", *a))

    # ── Switch control ────────────────────────────────────────────────────
    dispatcher.register("DICE_SWITCHES_ON",
        lambda: _ok("SW_ENABLED")  if dice.enable_all_switches()  else _err("failed"))
    dispatcher.register("DICE_SWITCHES_OFF",
        lambda: _ok("SW_DISABLED") if dice.disable_all_switches() else _err("failed"))
    dispatcher.register("DICE_RESET",
        lambda: _ok("RESET")       if dice.reset_device()         else _err("RESET failed"))

    # ── Camera ────────────────────────────────────────────────────────────
    def _cam_capture() -> bytes:
        path = camera.capture_single()
        if path:
            telemetry.update("last_capture", str(path))
            return _ok(f"saved={path.name}")
        return _err("capture failed")

    def _cam_sweep() -> bytes:
        paths = camera.exposure_sweep()
        telemetry.update("last_sweep_count", len(paths))
        return _ok(f"sweep={len(paths)} frames")

    dispatcher.register("CAM_CAPTURE", _cam_capture)
    dispatcher.register("CAM_SWEEP",   _cam_sweep)

    # ── DICE experiment sequence (unclamp → clamp → capture) ─────────────
    def _dice_sequence() -> bytes:
        if not dice.unclamp(sync=True):
            return _err("SEQUENCE failed at UNCLAMP")
        if not dice.clamp(sync=True):
            return _err("SEQUENCE failed at CLAMP")
        path = camera.capture_single()
        if path:
            telemetry.update("last_capture", str(path))
        return _ok(f"sequence done img={path.name if path else 'NONE'}")

    dispatcher.register("DICE_SEQUENCE", _dice_sequence)

    # ── Telemetry dump ────────────────────────────────────────────────────
    def _get_telemetry() -> bytes:
        snap = telemetry.snapshot()
        parts = [f"{k}={v['value']}" for k, v in snap.items()]
        return _ok("; ".join(parts) if parts else "empty")

    dispatcher.register("TELEMETRY_GET", _get_telemetry)


def register_tmp100_commands(
    dispatcher: CommandDispatcher,
    tmp100:     TMP100Driver,
    telemetry:  TelemetryStore,
) -> None:
    def _read_temp() -> bytes:
        t = tmp100.get_temperature()
        if t is not None:
            telemetry.update("temperature_c", t)
            return _val("temp_c", t)
        return _err("TMP100 read failed")

    dispatcher.register("READ_TEMP", _read_temp)


# ─────────────────────────────────────────────────────────────────────────────
# 6.  WORKER THREADS
# ─────────────────────────────────────────────────────────────────────────────

class UARTWorker(threading.Thread):
    """
    Continuously polls the UART for newline-terminated command strings,
    dispatches each to CommandDispatcher, and writes the byte response back.

    On SerialException the port is closed, and the thread waits
    *reconnect_delay_s* before trying to reopen — ensuring the loop never
    terminates due to a transient cable or bus fault.
    """

    def __init__(
        self,
        cfg:        Dict[str, Any],
        dispatcher: CommandDispatcher,
        shutdown:   threading.Event,
        logger:     logging.Logger,
    ) -> None:
        super().__init__(name="UARTWorker", daemon=True)
        self._dispatcher      = dispatcher
        self._shutdown        = shutdown
        self._log             = logger.getChild("UART")
        self._port            = cfg.get("port", "/dev/ttyS0")
        self._baud            = int(cfg.get("baud_rate", 115200))
        self._timeout         = float(cfg.get("read_timeout_s", 1.0))
        self._reconnect_delay = float(cfg.get("reconnect_delay_s", 5.0))
        self._encoding        = cfg.get("encoding", "utf-8")
        self._ser             = None
        self._write_lock      = threading.Lock()

    # ── Port management ───────────────────────────────────────────────────

    def _open(self) -> bool:
        if not _SERIAL_OK:
            self._log.error(
                "pyserial not installed — UART worker disabled. "
                "Install with: pip3 install pyserial"
            )
            return False
        try:
            self._ser = _serial.Serial(
                self._port, self._baud, timeout=self._timeout
            )
            self._ser.reset_input_buffer()
            self._log.info("Serial port opened: %s @ %d baud", self._port, self._baud)
            return True
        except Exception as exc:
            self._log.error("Serial open failed (%s): %s", self._port, exc)
            self._ser = None
            return False

    def _close(self) -> None:
        if self._ser and self._ser.is_open:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None

    def _write(self, data: bytes) -> None:
        if not (self._ser and self._ser.is_open):
            return
        with self._write_lock:
            try:
                self._ser.write(data)
                self._ser.flush()
            except Exception as exc:
                self._log.error("Serial write error: %s", exc)

    # ── Main loop ─────────────────────────────────────────────────────────

    def run(self) -> None:
        self._log.info("UART worker starting")
        while not self._shutdown.is_set():

            # (Re)open port if not available
            if not (self._ser and self._ser.is_open):
                if not self._open():
                    self._shutdown.wait(timeout=self._reconnect_delay)
                    continue

            try:
                ready, _, _ = select.select([self._ser], [], [], 1.0)
                if not ready:
                    continue

                raw_bytes = self._ser.readline()
                if not raw_bytes:
                    continue

                command_str = raw_bytes.decode(self._encoding, errors="replace").strip()
                if not command_str:
                    continue

                self._log.debug("RX: %r", command_str)
                response = self._dispatcher.dispatch(command_str)
                self._log.debug("TX: %r", response)
                self._write(response)

            except _serial.SerialException as exc:
                self._log.error(
                    "SerialException: %s — closing port, retrying in %.1f s",
                    exc, self._reconnect_delay,
                )
                self._close()
                self._shutdown.wait(timeout=self._reconnect_delay)

            except Exception as exc:
                self._log.exception("Unexpected error in UART worker: %s", exc)

        self._close()
        self._log.info("UART worker stopped")


class TelemetryWorker(threading.Thread):
    """
    Periodically reads TMP100 temperature and DICE controller status,
    caching results in the shared TelemetryStore.

    All exceptions within a polling cycle are caught and logged; the worker
    always waits the full *poll_interval* before the next attempt.
    """

    def __init__(
        self,
        tmp100:       TMP100Driver,
        dice:         DiceI2CDriver,
        telemetry:    TelemetryStore,
        poll_interval: float,
        shutdown:     threading.Event,
        logger:       logging.Logger,
    ) -> None:
        super().__init__(name="TelemetryWorker", daemon=True)
        self._tmp100      = tmp100
        self._dice        = dice
        self._telemetry   = telemetry
        self._interval    = poll_interval
        self._shutdown    = shutdown
        self._log         = logger.getChild("Telemetry")

    def run(self) -> None:
        self._log.info("Telemetry worker starting  poll=%.1f s", self._interval)
        while not self._shutdown.is_set():
            try:
                # TMP100 temperature
                temp = self._tmp100.get_temperature()
                if temp is not None:
                    self._telemetry.update("temperature_c", temp)
                    self._log.info("TMP100 %.4f °C", temp)
                else:
                    self._log.warning("TMP100: no reading")

                # DICE status
                status = self._dice.get_status()
                if status is not None:
                    self._telemetry.update("dice_status", f"0x{status:02X}")
                    self._log.debug("DICE status 0x%02X", status)

                # Motor 1 position (useful for housekeeping telemetry)
                pos = self._dice.read_motor1_position()
                if pos is not None:
                    self._telemetry.update("m1_position", pos)

            except Exception as exc:
                self._log.exception("Telemetry poll error: %s", exc)

            self._shutdown.wait(timeout=self._interval)

        self._log.info("Telemetry worker stopped")


class CameraWorker(threading.Thread):
    """
    Executes camera capture jobs off the main execution thread, preventing
    long-exposure captures from blocking UART responses.

    Jobs are submitted via submit(job_type) which returns a Queue on which
    the result (path or list-of-paths) will be placed when complete.
    """

    def __init__(
        self,
        camera:   CameraDriver,
        shutdown: threading.Event,
        logger:   logging.Logger,
    ) -> None:
        super().__init__(name="CameraWorker", daemon=True)
        self._camera   = camera
        self._shutdown = shutdown
        self._log      = logger.getChild("CameraWorker")
        self.job_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()

    def submit(self, job_type: str) -> "queue.Queue[Any]":
        """Enqueue a job; return the result Queue for the caller to poll/block on."""
        result_q: "queue.Queue[Any]" = queue.Queue(maxsize=1)
        self.job_queue.put({"type": job_type, "result_q": result_q})
        return result_q

    def run(self) -> None:
        self._log.info("Camera worker starting")
        while not self._shutdown.is_set():
            try:
                job = self.job_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            result: Any = None
            try:
                jtype = job["type"]
                if jtype == "single":
                    result = self._camera.capture_single()
                elif jtype == "sweep":
                    result = self._camera.exposure_sweep()
                else:
                    self._log.warning("Unknown camera job type: %r", jtype)
            except Exception as exc:
                self._log.exception("Camera job %r failed: %s", job["type"], exc)

            try:
                job["result_q"].put_nowait(result)
            except queue.Full:
                pass   # Caller abandoned the result queue

        self._log.info("Camera worker stopped")


# ─────────────────────────────────────────────────────────────────────────────
# 7.  PAYLOAD MANAGER (ORCHESTRATOR)
# ─────────────────────────────────────────────────────────────────────────────

class PayloadManager:
    """
    Top-level orchestrator that owns all hardware drivers and worker threads.

    Lifecycle:
        pm = PayloadManager(cfg, logger)
        pm.start()   # blocks; returns only after stop() is called
        pm.stop()    # idempotent; safe to call from a signal handler
    """

    def __init__(self, cfg: Dict[str, Any], logger: logging.Logger) -> None:
        self._cfg      = cfg
        self._log      = logger
        self._shutdown = threading.Event()

        # ── Hardware drivers ──────────────────────────────────────────────
        self._dice   = DiceI2CDriver(cfg.get("dice",   {}), logger)
        self._tmp100 = TMP100Driver( cfg.get("tmp100", {}), logger)
        self._camera = CameraDriver( cfg.get("camera", {}), logger)

        # ── Shared state ──────────────────────────────────────────────────
        self._telemetry = TelemetryStore()

        # ── Command dispatcher ────────────────────────────────────────────
        self._dispatcher = CommandDispatcher(logger)
        register_dice_commands(
            self._dispatcher, self._dice, self._camera, self._telemetry
        )
        register_tmp100_commands(self._dispatcher, self._tmp100, self._telemetry)
        self._dispatcher.register("HEALTH",   self._health_handler)
        self._dispatcher.register("SHUTDOWN", self._shutdown_handler)

        # ── Worker threads ────────────────────────────────────────────────
        self._uart_worker = UARTWorker(
            cfg.get("serial", {}), self._dispatcher, self._shutdown, logger
        )
        self._telemetry_worker = TelemetryWorker(
            self._tmp100,
            self._dice,
            self._telemetry,
            float(cfg.get("tmp100", {}).get("poll_interval_s", 30.0)),
            self._shutdown,
            logger,
        )
        self._camera_worker = CameraWorker(self._camera, self._shutdown, logger)

        self._workers = [
            self._uart_worker,
            self._telemetry_worker,
            self._camera_worker,
        ]

    # ── Built-in command handlers ─────────────────────────────────────────

    def _health_handler(self) -> bytes:
        snap = self._telemetry.snapshot()
        return (
            f"OK dice={'OK' if self._dice._available   else 'FAIL'} "
            f"tmp100={'OK' if self._tmp100._available  else 'FAIL'} "
            f"camera={'OK' if self._camera._available  else 'FAIL'} "
            f"telemetry_keys={len(snap)}\n"
        ).encode()

    def _shutdown_handler(self) -> bytes:
        self._log.warning("SHUTDOWN command received via UART")
        threading.Thread(target=self.stop, daemon=True).start()
        return b"OK SHUTTING_DOWN\n"

    # ── Hardware bring-up ─────────────────────────────────────────────────

    def _open_hardware(self) -> None:
        """
        Attempt to open each hardware driver.
        A failure in one driver is logged but does not abort the others.
        """
        for name, driver in [
            ("DICE I2C", self._dice),
            ("TMP100",   self._tmp100),
            ("Camera",   self._camera),
        ]:
            try:
                driver.open()
            except Exception as exc:
                self._log.error(
                    "%s failed to initialise: %s — subsystem will be unavailable",
                    name, exc,
                )

    # ── Periodic health check & recovery ─────────────────────────────────

    def _health_check(self) -> None:
        self._log.info(
            "Health — DICE: %s  TMP100: %s  Camera: %s",
            "OK"   if self._dice._available   else "FAIL",
            "OK"   if self._tmp100._available else "FAIL",
            "OK"   if self._camera._available else "FAIL",
        )
        # Attempt automatic recovery for any failed subsystem
        for name, driver in [
            ("DICE I2C", self._dice),
            ("TMP100",   self._tmp100),
            ("Camera",   self._camera),
        ]:
            if not driver._available:
                self._log.warning("%s unavailable — attempting reset", name)
                try:
                    driver.reset()
                    if driver._available:
                        self._log.info("%s recovered", name)
                    else:
                        self._log.error("%s reset did not restore availability", name)
                except Exception as exc:
                    self._log.error("%s reset raised: %s", name, exc)

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Open hardware, start all threads, then block in the watchdog loop."""
        self._log.info("PayloadManager starting up")
        self._open_hardware()

        for worker in self._workers:
            worker.start()
        self._log.info("All workers started — entering watchdog loop")

        check_interval = float(
            self._cfg.get("health_monitor", {}).get("check_interval_s", 60.0)
        )
        try:
            while not self._shutdown.is_set():
                self._shutdown.wait(timeout=check_interval)
                if not self._shutdown.is_set():
                    self._health_check()
        except KeyboardInterrupt:
            self._log.info("KeyboardInterrupt — initiating shutdown")

        self.stop()

    def stop(self) -> None:
        """Signal all workers to stop and release hardware resources."""
        if self._shutdown.is_set():
            return   # Already stopping
        self._log.info("PayloadManager shutting down")
        self._shutdown.set()

        for worker in self._workers:
            worker.join(timeout=5.0)
            if worker.is_alive():
                self._log.warning("Worker %s did not stop within 5 s", worker.name)

        self._dice.close()
        self._tmp100.close()
        self._camera.close()
        self._log.info("PayloadManager stopped")


# ─────────────────────────────────────────────────────────────────────────────
# 8.  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="DVS Da Vinci Satellite — Payload Manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config", "-c",
        default=str(Path(__file__).with_name("payload_manager.yaml")),
        metavar="PATH",
        help="Path to YAML or JSON configuration file (default: payload_manager.yaml)",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=None,
        help="Override the log level set in the config file",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.log_level:
        cfg.setdefault("logging", {})["level"] = args.log_level

    logger = setup_logging(cfg)
    logger.info("=" * 60)
    logger.info("DVS Payload Manager — starting")
    logger.info("Config file: %s", args.config)

    pm = PayloadManager(cfg, logger)

    # Translate POSIX signals into a clean shutdown
    def _sig_handler(signum: int, _frame: Any) -> None:
        logger.info("Received signal %d — requesting shutdown", signum)
        pm.stop()

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT,  _sig_handler)

    pm.start()
    logger.info("DVS Payload Manager — exited cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
