#!/usr/bin/env python3
"""
SolarHomeManager: Unified Solar, AirTouch 5 Climate & Tesla EV Orchestrator
Designed for Synology DS218+ (DSM 7.x, Python 3.9+)

Priority Hierarchy:
  1. Immediate needs for home climate (comfort bounds & differential night purge)
  2. Charging the Tesla when battery is below 90% SoC
  3. Pre-cooling / pre-heating the home using excess solar for hot/cold weather
  4. Charging the Tesla from 90% to 100% SoC

Progressive Rollout Stages (config["ROLLOUT_STAGE"]):
  Stage 1: EV-only parity with FreeCharge (AirTouch & Weather disabled)
  Stage 2: EV charging + passive read-only telemetry from AirTouch 5 & Weather
  Stage 3: EV charging + passive telemetry + Shadow 4-Tier HVAC decision logging
  Stage 4: Full coordinated autonomy (live AirTouch 5 control + live EV charging)
"""
import datetime
from datetime import datetime as dt, timezone
import json
import math
import os
import random
import re
import socket
import struct
import sys
import time
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from astral import LocationInfo
from astral.sun import sun

# Set up paths relative to this file
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
CACHE_PATH = os.path.join(BASE_DIR, "state_cache.json")
ENV_PATH = os.path.join(BASE_DIR, ".env")
LOGS_DIR = os.path.join(BASE_DIR, "logs")

# AirTouch 5 binary protocol constants
MAGIC_HEADER = b"\x55\x55\x55\xaa"
STUFF_FIND = b"\x55\x55\x55"
STUFF_REPLACE = b"\x55\x55\x55\x00"

AC_POWER_MAP = {0: "OFF", 1: "ON", 2: "AWAY_OFF", 3: "AWAY_ON", 5: "SLEEP"}
AC_POWER_CMD_MAP = {"KEEP": 0, "TOGGLE": 1, "OFF": 2, "ON": 3, "AWAY": 4, "SLEEP": 5}

AC_MODE_MAP = {
    0: "AUTO",
    1: "HEAT",
    2: "DRY",
    3: "FAN",
    4: "COOL",
    8: "AUTO_HEAT",
    9: "AUTO_COOL",
}
AC_MODE_CMD_MAP = {
    "AUTO": 0,
    "HEAT": 1,
    "DRY": 2,
    "FAN": 3,
    "COOL": 4,
    "KEEP": 0x0F,
}

FAN_SPEED_MAP = {
    0: "AUTO",
    1: "QUIET",
    2: "LOW",
    3: "MEDIUM",
    4: "HIGH",
    5: "POWERFUL",
    7: "TURBO",
}
FAN_SPEED_CMD_MAP = {
    "AUTO": 0,
    "QUIET": 1,
    "LOW": 2,
    "MEDIUM": 3,
    "HIGH": 4,
    "POWERFUL": 5,
    "TURBO": 7,
    "INTELLIGENT_AUTO": 8,
    "KEEP": 0x0F,
}

ZONE_POWER_MAP = {0: "OFF", 1: "ON", 3: "TURBO"}
ZONE_POWER_CMD_MAP = {"KEEP": 0, "TOGGLE": 1, "OFF": 2, "ON": 3, "TURBO": 5}


# Redefine print to write to daily log files and standard output
def custom_print(*args, **kwargs):
    message = " ".join(str(arg) for arg in args)

    # Write to daily log
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        local_tz = ZoneInfo("Australia/Sydney")
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r") as f:
                cfg = json.load(f)
                if "TIMEZONE" in cfg:
                    local_tz = ZoneInfo(cfg["TIMEZONE"])
        now = dt.now(local_tz)
    except Exception:
        now = dt.now()

    # Standardize timezones in the message prefix if it has a naive timestamp
    match = re.match(r"^\[(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)\]", message)
    if match:
        offset = now.strftime("%z") if hasattr(now, "tzinfo") and now.tzinfo else ""
        if len(offset) == 5:
            offset_formatted = f"{offset[:3]}:{offset[3:]}"
        elif len(offset) == 6:
            offset_formatted = offset
        else:
            offset_formatted = ""
        ts_content = match.group(1)
        message = f"[{ts_content}{offset_formatted}]" + message[match.end() :]

    # Write to actual stdout terminal
    sys.__stdout__.write(message + "\n")

    log_file = os.path.join(LOGS_DIR, f"execution_{now.strftime('%Y%m%d')}.log")
    try:
        with open(log_file, "a") as f:
            if message.startswith("[202") or message.startswith("["):
                f.write(message + "\n")
            else:
                f.write(f"[{now}] {message}\n")
    except Exception as e:
        sys.__stdout__.write(f"[{now}] Logging write error: {e}\n")


# Override built-in print globally
print = custom_print

# Load environment variables
if os.path.exists(ENV_PATH):
    load_dotenv(ENV_PATH)


def _parse_bool(val, default=False) -> bool:
    if isinstance(val, bool):
        return val
    if val is None:
        return default
    return str(val).strip().lower() in ("true", "1", "yes", "on")


def load_config():
    """Loads configuration parameters from config.json and merges them with .env parameters."""
    defaults = {
        "FRONIUS_EXPORT_IS_POSITIVE": False,
        "TIMEZONE": "Australia/Sydney",
        "CITY_NAME": "Sydney",
        "VOLTAGE": 240,
        "MIN_AMPS": 5,
        "MAX_AMPS": 32,
        "BUFFER_WATTS": 150,
        "EV_PRIORITY_SOC_THRESHOLD": 90,
        "LOCATION_TOLERANCE": 0.001,
        "HISTORY_WINDOW_MINUTES": 15,
        "POLLING_INTERVAL_MINUTES": 5,
        "THROTTLE_INTERVAL_MINUTES": 10,
        "TELEMETRY_REFRESH_MINUTES": 15,
        "WAKE_COOLDOWN_MINUTES": 60,
        "TESLA_API_BASE_URL": "https://fleet-api.prd.na.vn.cloud.tesla.com",
        # NOTE: LAN IPs and zone names are identity, not behavior — they live
        # in .env (never committed), applied in load_config() below. The
        # generic RFC1918 fallbacks used when .env lacks them are in the
        # identity block, not here, so this dict stays public-safe.
        "HVAC_ESTIMATED_WATTS": 2000,
        "HVAC_MIN_ON_MINUTES": 15,
        "HVAC_MIN_OFF_MINUTES": 6,
        "HVAC_MANUAL_OVERRIDE_MINUTES": 120,
        "WEATHER_REFRESH_MINUTES": 30,
        "DAYTIME_HOURS": [7, 22],
        "DAY_COMFORT_MIN_C": 18.0,
        "DAY_COMFORT_MAX_C": 24.0,
        "DAY_COOL_TARGET_C": 22.0,
        "DAY_HEAT_TARGET_C": 20.0,
        "NIGHT_COMFORT_MIN_C": 16.0,
        "NIGHT_COMFORT_MAX_C": 23.0,
        "NIGHT_COOL_TARGET_C": 21.0,
        "NIGHT_HEAT_TARGET_C": 18.0,
        "UNOCCUPIED_COOL_SETBACK_C": 1.5,
        "UNOCCUPIED_HEAT_SETBACK_C": -1.5,
        "PRECOOL_FORECAST_MAX_C": 30.0,
        "PRECOOL_TARGET_C": 21.0,
        "PREHEAT_FORECAST_MAX_C": 16.0,
        "PREHEAT_FORECAST_MIN_C": 8.0,
        "PREHEAT_TARGET_C": 22.0,
        "HEAT_SUPPRESS_FORECAST_MAX_C": 26.0,
        "NIGHT_PURGE_DELTA_C": 2.0,
        "NIGHT_PURGE_MIN_OUTDOOR_C": 15.0,
        "FAN_LOW_TEMP_C": 21.0,
        "FAN_MED_TEMP_C": 22.0,
        "FAN_HIGH_TEMP_C": 23.0,
        "FAN_OFF_DIFFERENTIAL_C": 0.5,
        "MIN_TOTAL_DAMPER_PCT": 120,
        "ZONES": {
            "0": {
                "priority": 0.9,
                "occupied_hours": [20, 8],
                "weekend_occupied_hours": [7, 22],
                "min_damper": 20,
            },
            "1": {
                "priority": 1.0,
                "occupied_hours": [7, 22],
                "weekend_occupied_hours": [7, 22],
                "min_damper": 30,
            },
            "2": {
                "priority": 0.7,
                "occupied_hours": [15, 8],
                "weekend_occupied_hours": [7, 22],
                "min_damper": 0,
            },
            "3": {
                "priority": 0.7,
                "occupied_hours": [15, 8],
                "weekend_occupied_hours": [7, 22],
                "min_damper": 0,
            },
            "4": {
                "priority": 0.8,
                "occupied_hours": [8, 18],
                "weekend_occupied_hours": [9, 17],
                "min_damper": 15,
            },
        },
        "ROLLOUT_STAGE": 1,
        "ENABLE_CLIMATE_TELEMETRY": False,
        "ENABLE_CLIMATE_DECISIONS": False,
        "ENABLE_CLIMATE_CONTROL": False,
        "MAX_LOG_DAYS": 30,
        "DRY_RUN": True,
        "DRY_RUN_TESLA": True,
        "DRY_RUN_CLIMATE": True,
        "MOCK_TESLA": True,
        "MOCK_WALL_CONNECTOR": False,
        "MOCK_AIRTOUCH": False,
        "MOCK_WEATHER": False,
        "MOCK_API_FAILURE_RATE": 0.05,
    }
    config = defaults.copy()
    user_config: Dict = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                user_config = json.load(f)
                config.update(user_config)
        except Exception as e:
            print(
                f"[{dt.now()}] ERROR: Failed to parse {CONFIG_PATH} ({e}). Falling back to default settings."
            )

    # ------------------------------------------------------------------
    # Configuration split (strict — each key lives in exactly one file):
    #   config.json (.env NEVER overrides it):
    #     LAN IPs, API base URL, thresholds, zones, ROLLOUT_STAGE,
    #     DRY_RUN_*, MOCK_* master switches.
    #   .env (git-ignored, per-machine secrets & identity ONLY):
    #     Tesla OAuth tokens/secrets, TESLA_VIN,
    #     LATITUDE/LONGITUDE, plus MOCK_*_scenario test knobs which are
    #     only honored when the matching MOCK_* switch in config.json is true.
    # ------------------------------------------------------------------
    for bool_key in (
        "DRY_RUN",
        "DRY_RUN_TESLA",
        "DRY_RUN_CLIMATE",
        "MOCK_TESLA",
        "MOCK_WALL_CONNECTOR",
        "MOCK_AIRTOUCH",
        "MOCK_WEATHER",
        "FRONIUS_EXPORT_IS_POSITIVE",
        "ENABLE_CLIMATE_TELEMETRY",
        "ENABLE_CLIMATE_DECISIONS",
        "ENABLE_CLIMATE_CONTROL",
    ):
        config[bool_key] = _parse_bool(config.get(bool_key, False))
    config["ROLLOUT_STAGE"] = int(config.get("ROLLOUT_STAGE", 1))
    config["AIRTOUCH_PORT"] = int(config.get("AIRTOUCH_PORT", 9005))
    config["MOCK_API_FAILURE_RATE"] = float(config.get("MOCK_API_FAILURE_RATE", 0.05))

    # ------------------------------------------------------------------
    # Identity overlay (never committed — room names, LAN IPs, presence
    # targets). Precedence per key: .env first, then config.json (lets an
    # existing local config keep working during migration), then a generic
    # RFC1918 fallback. Shipped config.json carries none of these, so a
    # fresh clone is public-safe by default.
    # ------------------------------------------------------------------
    for _ip_key, _ip_default in (
        ("FRONIUS_IP", "192.168.1.63"),
        ("AIRTOUCH_IP", "192.168.1.30"),
        ("TESLA_WALL_CONNECTOR_IP", "192.168.1.60"),
    ):
        if os.getenv(_ip_key):
            config[_ip_key] = os.environ[_ip_key].strip()
        elif _ip_key not in user_config:
            config[_ip_key] = _ip_default
    if os.getenv("AIRTOUCH_PORT"):
        try:
            config["AIRTOUCH_PORT"] = int(os.getenv("AIRTOUCH_PORT", "9005"))
        except ValueError:
            pass
    elif "AIRTOUCH_PORT" not in user_config:
        config["AIRTOUCH_PORT"] = 9005

    for _z_key in list(config.get("ZONES", {}).keys()):
        _z_env = os.getenv(f"ZONE_{_z_key}_NAME", "").strip()
        if _z_env:
            config["ZONES"][_z_key]["name"] = _z_env
        elif not config["ZONES"][_z_key].get("name"):
            config["ZONES"][_z_key]["name"] = f"Zone_{_z_key}"

    config["TESLA_VIN"] = os.getenv("TESLA_VIN", "5YJ3XXXXXXXXXXXXX")
    config["TESLA_API_TOKEN"] = os.getenv("TESLA_API_TOKEN", "YOUR_TESLA_API_ACCESS_TOKEN")
    config["TESLA_REFRESH_TOKEN"] = os.getenv("TESLA_REFRESH_TOKEN", "")
    config["TESLA_CLIENT_ID"] = os.getenv("TESLA_CLIENT_ID", "")
    config["TESLA_CLIENT_SECRET"] = os.getenv("TESLA_CLIENT_SECRET", "")
    config["LATITUDE"] = float(os.getenv("LATITUDE", -33.8688))
    config["LONGITUDE"] = float(os.getenv("LONGITUDE", 151.2093))

    return config


def resolve_stage_flags(config: Dict) -> Dict:
    """Resolves progressive rollout stage (1..4) and granular feature flags."""
    stage = int(config.get("ROLLOUT_STAGE", 1))
    poll_climate = stage >= 2 or _parse_bool(config.get("ENABLE_CLIMATE_TELEMETRY", False))
    eval_climate = stage >= 3 or _parse_bool(config.get("ENABLE_CLIMATE_DECISIONS", False))
    enable_control_flag = stage >= 4 or _parse_bool(config.get("ENABLE_CLIMATE_CONTROL", False))
    dry_run_climate = _parse_bool(config.get("DRY_RUN_CLIMATE", stage < 4))
    if stage < 4 and not _parse_bool(config.get("ENABLE_CLIMATE_CONTROL", False)):
        dry_run_climate = True
    control_climate = enable_control_flag and not dry_run_climate

    return {
        "stage": stage,
        "poll_climate": poll_climate,
        "eval_climate": eval_climate,
        "control_climate": control_climate,
        "dry_run_climate": dry_run_climate,
        "dry_run_tesla": _parse_bool(config.get("DRY_RUN", config.get("DRY_RUN_TESLA", True))),
    }


def read_cache():
    """Reads current cache state or returns default if not present."""
    defaults = {
        "charging": False,
        "amps": 5,
        "last_command_time": None,
        "solar_history": [],
        "vehicle_state": {},
        "wall_connector_state": {},
        "last_telemetry_check_time": None,
        "last_wake_attempt_time": None,
        "last_sunrise_reset_date": None,
        # Climate & Weather state extensions (Stages 2..4)
        "airtouch_state": {},
        "airtouch_last_dispatched": {},
        "airtouch_manual_override_until": None,
        "airtouch_last_power_change_time": None,
        "airtouch_last_compressor_change_time": None,
        "hvac_compressor_running": None,
        "hvac_active_tier": "IDLE",
        "hvac_solar_bank_cancelled_date": None,
        "weather_state": {},
        "last_weather_check_time": None,
        "shadow_hvac_plan": {},
    }
    if not os.path.exists(CACHE_PATH):
        return defaults
    with open(CACHE_PATH, "r") as f:
        try:
            data = json.load(f)
            for key, val in defaults.items():
                if key not in data:
                    data[key] = val
            return data
        except json.JSONDecodeError:
            return defaults


def write_cache(state):
    """Writes updated state to local cache file."""
    with open(CACHE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def update_env_file(key, value):
    """Updates or adds a key-value pair in the local .env file."""
    if not os.path.exists(ENV_PATH):
        return
    with open(ENV_PATH, "r") as f:
        lines = f.readlines()

    updated = False
    new_lines = []
    for line in lines:
        if line.strip().startswith(f"{key}="):
            new_lines.append(f"{key}={value}\n")
            updated = True
        else:
            new_lines.append(line)

    if not updated:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] += "\n"
        new_lines.append(f"{key}={value}\n")

    with open(ENV_PATH, "w") as f:
        f.writelines(new_lines)


# ============================================================================
# AIRTOUCH 5 BINARY PROTOCOL (TCP PORT 9005)
# ============================================================================


def crc16_modbus(data: bytes) -> int:
    """Computes CRC-16/MODBUS over unstuffed frame body (Address..Data)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def pack_airtouch_frame(address: int, msg_id: int, msg_type: int, payload: bytes) -> bytes:
    """Assembles an AirTouch 5 frame with CRC-16 and 0x55555500 byte stuffing."""
    body = struct.pack(">HBBH", address, msg_id & 0xFF, msg_type, len(payload)) + payload
    crc = crc16_modbus(body)
    raw_after_magic = body + struct.pack(">H", crc)
    stuffed = raw_after_magic.replace(STUFF_FIND, STUFF_REPLACE)
    return MAGIC_HEADER + stuffed


def read_airtouch_frame(
    sock: socket.socket, rx_buf: bytearray
) -> Tuple[int, int, int, bytes]:
    """Reads from socket buffer until a valid, CRC-verified AirTouch 5 frame is decoded."""
    while True:
        idx = rx_buf.find(MAGIC_HEADER)
        if idx != -1:
            if idx > 0:
                del rx_buf[:idx]
            unstuffed = bytes(rx_buf[4:]).replace(STUFF_REPLACE, STUFF_FIND)
            if len(unstuffed) >= 6:
                address, msg_id, msg_type, data_len = struct.unpack(">HBBH", unstuffed[:6])
                total_unstuffed_len = 6 + data_len + 2
                if len(unstuffed) >= total_unstuffed_len:
                    frame_body = unstuffed[: 6 + data_len]
                    expected_crc = struct.unpack(
                        ">H", unstuffed[6 + data_len : total_unstuffed_len]
                    )[0]
                    wire_tail = (frame_body + struct.pack(">H", expected_crc)).replace(
                        STUFF_FIND, STUFF_REPLACE
                    )
                    del rx_buf[: 4 + len(wire_tail)]

                    calc_crc = crc16_modbus(frame_body)
                    if calc_crc != expected_crc:
                        raise ValueError(
                            f"CRC mismatch: calculated 0x{calc_crc:04X}, got 0x{expected_crc:04X}"
                        )
                    payload = frame_body[6:]
                    return address, msg_id, msg_type, payload

        chunk = sock.recv(1024)
        if not chunk:
            raise ConnectionError("Socket closed by AirTouch 5 console")
        rx_buf.extend(chunk)


def build_airtouch_status_req(subtype: int, msg_id: int) -> bytes:
    payload = struct.pack(">BBHHH", subtype, 0x00, 0, 0, 0)
    return pack_airtouch_frame(0x80B0, msg_id, 0xC0, payload)


def build_airtouch_zone_names_req(msg_id: int) -> bytes:
    return pack_airtouch_frame(0x90B0, msg_id, 0x1F, b"\xff\x13")


def build_ac_control_frame(
    ac_index: int = 0,
    power: Optional[str] = None,
    mode: Optional[str] = None,
    fan_speed: Optional[str] = None,
    setpoint_c: Optional[float] = None,
    msg_id: int = 10,
) -> bytes:
    """Builds an AirTouch 5 AC Control frame (Msg Type 0xC0, Subtype 0x22)."""
    pwr_bits = AC_POWER_CMD_MAP.get((power or "KEEP").upper(), 0)
    b0 = ((pwr_bits & 0x0F) << 4) | (ac_index & 0x0F)

    mode_bits = AC_MODE_CMD_MAP.get((mode or "KEEP").upper(), 0x0F)
    fan_bits = FAN_SPEED_CMD_MAP.get((fan_speed or "KEEP").upper(), 0x0F)
    b1 = ((mode_bits & 0x0F) << 4) | (fan_bits & 0x0F)

    if setpoint_c is not None:
        clamped = max(10.0, min(35.0, float(setpoint_c)))
        b2 = 0x40
        b3 = int(round(clamped * 10.0)) - 100
    else:
        b2 = 0x00
        b3 = 0xFF

    subhdr = struct.pack(">BBHHH", 0x22, 0x00, 0, 4, 1)
    rec = struct.pack(">BBBB", b0, b1, b2, b3)
    return pack_airtouch_frame(0x80B0, msg_id, 0xC0, subhdr + rec)


def build_zone_control_frame(zone_commands: List[Dict], msg_id: int = 11) -> bytes:
    """
    Builds a batched AirTouch 5 Zone Control frame (Msg Type 0xC0, Subtype 0x20).
    Each item in zone_commands supports:
      - zone_index: int
      - power: Optional[str] ("ON", "OFF", "TURBO", "KEEP")
      - control_mode: Optional[str] ("DAMPER_PCT", "TEMPERATURE", "KEEP")
      - damper_pct: Optional[int] (0..100, quantized to 5% steps)
      - setpoint_c: Optional[float] (10.0..35.0)
    """
    records = bytearray()
    for cmd in zone_commands:
        z_idx = int(cmd["zone_index"]) & 0x3F
        pwr_bits = ZONE_POWER_CMD_MAP.get(str(cmd.get("power") or "KEEP").upper(), 0) & 0x07

        ctrl_mode = str(cmd.get("control_mode") or "KEEP").upper()
        if ctrl_mode == "DAMPER_PCT":
            ctrl_bits = 0x10  # 0b10 << 3
        elif ctrl_mode == "TEMPERATURE":
            ctrl_bits = 0x18  # 0b11 << 3
        else:
            ctrl_bits = 0x00

        b1 = pwr_bits | ctrl_bits
        b2 = 0xFF

        if cmd.get("damper_pct") is not None:
            quantized = max(0, min(100, int(round(float(cmd["damper_pct"]) / 5.0) * 5)))
            b1 |= 0x80  # 0b100 << 5 (Set explicit damper %)
            b2 = quantized
        elif cmd.get("setpoint_c") is not None:
            clamped = max(10.0, min(35.0, float(cmd["setpoint_c"])))
            b1 |= 0xA0  # 0b101 << 5 (Set explicit setpoint)
            b2 = int(round(clamped * 10.0)) - 100

        records.extend(struct.pack(">BBBB", z_idx, b1, b2, 0x00))

    subhdr = struct.pack(">BBHHH", 0x20, 0x00, 0, 4, len(zone_commands))
    return pack_airtouch_frame(0x80B0, msg_id, 0xC0, subhdr + bytes(records))


def parse_ac_status_payload(payload: bytes) -> List[Dict]:
    if len(payload) < 8:
        return []
    subtype, _, norm_len, rpt_len, rpt_cnt = struct.unpack(">BBHHH", payload[:8])
    if subtype != 0x23:
        return []
    offset = 8 + norm_len
    acs = []
    for _ in range(rpt_cnt):
        rec = payload[offset : offset + rpt_len]
        offset += rpt_len
        ac_idx = rec[0] & 0x0F
        power_raw = (rec[0] >> 4) & 0x0F
        mode_raw = (rec[1] >> 4) & 0x0F
        fan_raw = rec[1] & 0x07
        intell_auto = bool(rec[1] & 0x08)
        setpoint_c = (rec[2] + 100) / 10.0 if rec[2] < 0xFF else None
        flags = rec[3]
        raw_temp = struct.unpack(">H", rec[4:6])[0] & 0x07FF
        ambient_c = (raw_temp - 500) / 10.0 if raw_temp <= 2000 else None
        err_code = struct.unpack(">H", rec[6:8])[0]
        acs.append(
            {
                "ac_index": ac_idx,
                "power": AC_POWER_MAP.get(power_raw, f"UNKNOWN({power_raw})"),
                "mode": AC_MODE_MAP.get(mode_raw, f"UNKNOWN({mode_raw})"),
                "fan_speed": FAN_SPEED_MAP.get(fan_raw, f"UNKNOWN({fan_raw})"),
                "intelligent_auto": intell_auto,
                "setpoint_c": setpoint_c,
                "return_air_temp_c": ambient_c,
                "spill_active": bool(flags & 0x02),
                "bypass_active": bool(flags & 0x04),
                "turbo_active": bool(flags & 0x08),
                "error_code": err_code,
            }
        )
    return acs


def parse_zone_status_payload(payload: bytes, zone_names: Dict[int, str]) -> List[Dict]:
    if len(payload) < 8:
        return []
    subtype, _, norm_len, rpt_len, rpt_cnt = struct.unpack(">BBHHH", payload[:8])
    if subtype != 0x21:
        return []
    offset = 8 + norm_len
    zones = []
    for _ in range(rpt_cnt):
        rec = payload[offset : offset + rpt_len]
        offset += rpt_len
        zone_idx = rec[0] & 0x3F
        power_raw = (rec[0] >> 6) & 0x03
        is_temp_control = bool(rec[1] & 0x80)
        damper_pct = rec[1] & 0x7F
        setpoint_c = (
            (rec[2] + 100) / 10.0 if (is_temp_control and rec[2] < 0xFF) else None
        )
        has_sensor = bool(rec[3] & 0x80)
        raw_sensor = struct.unpack(">H", rec[4:6])[0] & 0x07FF
        sensor_c = (
            (raw_sensor - 500) / 10.0
            if (has_sensor and raw_sensor <= 2000)
            else None
        )
        flags = rec[6]
        zones.append(
            {
                "zone_index": zone_idx,
                "name": zone_names.get(zone_idx, f"Zone_{zone_idx}"),
                "power": ZONE_POWER_MAP.get(power_raw, f"UNKNOWN({power_raw})"),
                "control_mode": "TEMPERATURE" if is_temp_control else "DAMPER_PCT",
                "damper_open_pct": damper_pct,
                "setpoint_c": setpoint_c,
                "has_itc_sensor": has_sensor,
                "temperature_c": sensor_c,
                "battery_low": bool(flags & 0x01),
                "spill_active": bool(flags & 0x02),
            }
        )
    return zones


def parse_zone_names_payload(payload: bytes) -> Dict[int, str]:
    if len(payload) < 2 or payload[:2] != b"\xff\x13":
        return {}
    names: Dict[int, str] = {}
    i = 2
    while i + 2 <= len(payload):
        z_idx = payload[i]
        name_len = payload[i + 1]
        raw_name = payload[i + 2 : i + 2 + name_len]
        names[z_idx] = raw_name.decode("utf-8", errors="ignore").strip("\x00")
        i += 2 + name_len
    return names


def get_airtouch_state(config: Dict, mock_airtouch: Optional[Dict] = None, timeout: float = 4.0) -> Optional[Dict]:
    """
    Fault-isolated AirTouch 5 status poller over TCP port 9005.
    Never raises unhandled exceptions so EV charging cannot be disrupted.
    """
    if mock_airtouch is not None:
        return mock_airtouch

    if config.get("MOCK_AIRTOUCH", False):
        zones_cfg = config.get("ZONES", {})
        mock_zones = {}
        for z_key, z_meta in zones_cfg.items():
            z_idx = int(z_key)
            mock_zones[str(z_idx)] = {
                "zone_index": z_idx,
                "name": z_meta.get("name", f"Zone_{z_idx}"),
                "power": "ON",
                "control_mode": "TEMPERATURE",
                "damper_open_pct": 50,
                "setpoint_c": 22.0,
                "has_itc_sensor": True,
                "temperature_c": 22.5,
                "battery_low": False,
                "spill_active": False,
            }
        return {
            "ac": {
                "ac_index": 0,
                "power": "OFF",
                "mode": "COOL",
                "fan_speed": "LOW",
                "intelligent_auto": False,
                "setpoint_c": 22.0,
                "return_air_temp_c": 22.5,
                "spill_active": False,
                "bypass_active": False,
                "turbo_active": False,
                "error_code": 0,
            },
            "zones": mock_zones,
            "low_battery_zones": [],
        }

    host = config.get("AIRTOUCH_IP", "192.168.1.30")
    port = int(config.get("AIRTOUCH_PORT", 9005))
    rx_buf = bytearray()
    zone_names: Dict[int, str] = {}
    ac_list: Optional[List[Dict]] = None
    zone_list: Optional[List[Dict]] = None

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(build_airtouch_zone_names_req(msg_id=1))
            sock.sendall(build_airtouch_status_req(subtype=0x23, msg_id=2))
            sock.sendall(build_airtouch_status_req(subtype=0x21, msg_id=3))

            deadline = time.monotonic() + timeout
            while (
                ac_list is None or zone_list is None or not zone_names
            ) and time.monotonic() < deadline:
                try:
                    _, _, msg_type, payload = read_airtouch_frame(sock, rx_buf)
                except socket.timeout:
                    break
                if msg_type == 0x1F and payload[:2] == b"\xff\x13":
                    zone_names = parse_zone_names_payload(payload)
                elif msg_type == 0xC0 and len(payload) >= 8:
                    subtype = payload[0]
                    if subtype == 0x23:
                        ac_list = parse_ac_status_payload(payload)
                    elif subtype == 0x21:
                        zone_list = parse_zone_status_payload(payload, zone_names)

        if not ac_list or zone_list is None:
            print(f"[{dt.now()}] WARNING: Incomplete AirTouch 5 response from {host}:{port}")
            return None

        zones_dict = {}
        low_battery = []
        for z in zone_list:
            z_idx = z["zone_index"]
            if z_idx in zone_names:
                z["name"] = zone_names[z_idx]
            zones_dict[str(z_idx)] = z
            if z.get("battery_low"):
                low_battery.append(z["name"])

        if low_battery:
            print(f"[{dt.now()}] WARNING: AirTouch ITC sensor low battery in zones: {', '.join(low_battery)}")

        return {
            "ac": ac_list[0],
            "zones": zones_dict,
            "low_battery_zones": low_battery,
        }
    except Exception as e:
        print(f"[{dt.now()}] WARNING: AirTouch 5 telemetry fetch error ({host}:{port}): {e}")
        return None


def dispatch_airtouch_commands(
    config: Dict,
    ac_cmd: Optional[Dict],
    zone_cmds: Optional[List[Dict]],
    dry_run_climate: bool = True,
    timeout: float = 4.0,
) -> bool:
    """Dispatches AC (0x22) and/or Zone (0x20) commands to AirTouch 5."""
    if not ac_cmd and not zone_cmds:
        return True

    if dry_run_climate or config.get("MOCK_AIRTOUCH", False):
        print(
            f"[{dt.now()}] [DRY RUN CLIMATE] Would dispatch to AirTouch 5 -> AC: {ac_cmd}, Zones: {zone_cmds}"
        )
        return True

    host = config.get("AIRTOUCH_IP", "192.168.1.30")
    port = int(config.get("AIRTOUCH_PORT", 9005))
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            if ac_cmd:
                frame_ac = build_ac_control_frame(
                    ac_index=ac_cmd.get("ac_index", 0),
                    power=ac_cmd.get("power"),
                    mode=ac_cmd.get("mode"),
                    fan_speed=ac_cmd.get("fan_speed"),
                    setpoint_c=ac_cmd.get("setpoint_c"),
                    msg_id=20,
                )
                sock.sendall(frame_ac)
                if zone_cmds:
                    time.sleep(0.4)
            if zone_cmds:
                frame_zones = build_zone_control_frame(zone_cmds, msg_id=21)
                sock.sendall(frame_zones)
        print(
            f"[{dt.now()}] Dispatched AirTouch 5 commands -> AC: {ac_cmd}, Zones: {len(zone_cmds or [])} zone(s)"
        )
        return True
    except Exception as e:
        print(f"[{dt.now()}] ERROR: Failed to dispatch AirTouch 5 commands: {e}")
        return False


# ============================================================================
# WEATHER & FORECAST TELEMETRY (BOM / OPEN-METEO)
# ============================================================================


_GEOHASH_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"


def encode_geohash(latitude: float, longitude: float, precision: int = 6) -> str:
    """Encodes (latitude, longitude) into a standard base32 geohash for BOM API lookup."""
    lat_interval = [-90.0, 90.0]
    lon_interval = [-180.0, 180.0]
    geohash = []
    bits = [16, 8, 4, 2, 1]
    bit = 0
    ch = 0
    even = True
    while len(geohash) < precision:
        if even:
            mid = (lon_interval[0] + lon_interval[1]) / 2.0
            if longitude > mid:
                ch |= bits[bit]
                lon_interval[0] = mid
            else:
                lon_interval[1] = mid
        else:
            mid = (lat_interval[0] + lat_interval[1]) / 2.0
            if latitude > mid:
                ch |= bits[bit]
                lat_interval[0] = mid
            else:
                lat_interval[1] = mid
        even = not even
        if bit < 4:
            bit += 1
        else:
            geohash.append(_GEOHASH_BASE32[ch])
            bit = 0
            ch = 0
    return "".join(geohash)


def get_weather_data(config: Dict, cache: Dict, now: dt, mock_weather: Optional[Dict] = None) -> Optional[Dict]:
    """
    Fetches outdoor ambient temperature and daily max/min forecast from the
    Australian Bureau of Meteorology (api.weather.bom.gov.au) with Open-Meteo fallback
    and 30-min caching. Fault-isolated so weather API issues never affect EV charging.
    """
    if mock_weather is not None:
        cache["weather_state"] = mock_weather
        cache["last_weather_check_time"] = now.isoformat()
        return mock_weather

    if config.get("MOCK_WEATHER", False):
        mock_wx = {
            "outdoor_temp_c": float(os.getenv("MOCK_OUTDOOR_TEMP_C", "22.0")),
            "cloud_cover_pct": float(os.getenv("MOCK_CLOUD_COVER_PCT", "15.0")),
            "forecast_max_c": float(os.getenv("MOCK_FORECAST_MAX_C", "24.0")),
            "forecast_min_c": float(os.getenv("MOCK_FORECAST_MIN_C", "14.0")),
        }
        cache["weather_state"] = mock_wx
        cache["last_weather_check_time"] = now.isoformat()
        return mock_wx

    refresh_mins = float(config.get("WEATHER_REFRESH_MINUTES", 30))
    last_wx_str = cache.get("last_weather_check_time")
    if last_wx_str and cache.get("weather_state"):
        try:
            last_wx_dt = dt.fromisoformat(last_wx_str).astimezone(now.tzinfo)
            if (now - last_wx_dt).total_seconds() / 60.0 < refresh_mins:
                return cache["weather_state"]
        except Exception:
            pass

    lat = float(config.get("LATITUDE", -33.8688))
    lon = float(config.get("LONGITUDE", 151.2093))
    tz_name = config.get("TIMEZONE", "Australia/Sydney")
    geohash = str(config.get("BOM_GEOHASH") or encode_geohash(lat, lon, precision=6))

    # Primary: Official Australian Bureau of Meteorology API
    try:
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        obs_url = f"https://api.weather.bom.gov.au/v1/locations/{geohash}/observations"
        fc_url = f"https://api.weather.bom.gov.au/v1/locations/{geohash}/forecasts/daily"

        r_obs = requests.get(obs_url, headers=headers, timeout=6)
        r_fc = requests.get(fc_url, headers=headers, timeout=6)
        if r_obs.status_code == 200 and r_fc.status_code == 200:
            obs_data = r_obs.json().get("data", {})
            fc_list = r_fc.json().get("data", [])
            outdoor_c = float(obs_data.get("temp") or 20.0)
            today_fc = fc_list[0] if fc_list else {}
            t_max = today_fc.get("temp_max")
            t_min = today_fc.get("temp_min")
            if t_max is None:
                t_max = (obs_data.get("max_temp") or {}).get("value", outdoor_c)
            if t_min is None:
                t_min = (obs_data.get("min_temp") or {}).get("value", outdoor_c)
            wx = {
                "provider": "BOM",
                "station": obs_data.get("station", {}).get("name", geohash),
                "outdoor_temp_c": float(outdoor_c),
                "humidity_pct": obs_data.get("humidity"),
                "forecast_max_c": float(t_max),
                "forecast_min_c": float(t_min),
                "short_text": today_fc.get("short_text") or today_fc.get("extended_text", ""),
            }
            cache["weather_state"] = wx
            cache["last_weather_check_time"] = now.isoformat()
            return wx
    except Exception as e:
        print(f"[{now}] WARNING: Primary BOM API fetch failed ({e}), trying Open-Meteo fallback...")

    # Fallback: Open-Meteo BOM model
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&current=temperature_2m,cloud_cover"
        f"&daily=temperature_2m_max,temperature_2m_min"
        f"&timezone={requests.utils.quote(tz_name)}&forecast_days=2"
    )
    try:
        resp = requests.get(url, timeout=6)
        if resp.status_code == 200:
            data = resp.json()
            curr = data.get("current", {})
            daily = data.get("daily", {})
            max_list = daily.get("temperature_2m_max", [])
            min_list = daily.get("temperature_2m_min", [])
            wx = {
                "provider": "Open-Meteo",
                "outdoor_temp_c": float(curr.get("temperature_2m", 20.0)),
                "cloud_cover_pct": float(curr.get("cloud_cover", 0.0)),
                "forecast_max_c": float(max_list[0]) if max_list else 22.0,
                "forecast_min_c": float(min_list[0]) if min_list else 14.0,
            }
            cache["weather_state"] = wx
            cache["last_weather_check_time"] = now.isoformat()
            return wx
        print(f"[{now}] WARNING: Weather API returned HTTP {resp.status_code}")
    except Exception as e:
        print(f"[{now}] WARNING: Weather API fetch error: {e}")

    return cache.get("weather_state") or None


# ============================================================================
# TESLA & FRONIUS TELEMETRY / CONTROL (FREECHARGE BASE)
# ============================================================================


def refresh_tesla_token(config):
    """Refreshes the Tesla OAuth token using the refresh token and writes the new tokens back to .env."""
    refresh_token = config.get("TESLA_REFRESH_TOKEN")
    client_id = config.get("TESLA_CLIENT_ID")
    client_secret = config.get("TESLA_CLIENT_SECRET")

    if not refresh_token or not client_id or not client_secret:
        print(
            f"[{dt.now()}] Cannot refresh Tesla token: TESLA_REFRESH_TOKEN, TESLA_CLIENT_ID, or TESLA_CLIENT_SECRET is missing."
        )
        return False

    print(f"[{dt.now()}] Attempting to refresh Tesla access token...")
    url = "https://auth.tesla.com/oauth2/v3/token"
    payload = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }

    try:
        response = requests.post(url, json=payload, timeout=15)
        if response.status_code == 200:
            data = response.json()
            new_access_token = data.get("access_token")
            new_refresh_token = data.get("refresh_token")

            if not new_access_token or not new_refresh_token:
                print(
                    f"[{dt.now()}] Token refresh response was missing access_token or refresh_token."
                )
                return False

            config["TESLA_API_TOKEN"] = new_access_token
            config["TESLA_REFRESH_TOKEN"] = new_refresh_token

            update_env_file("TESLA_API_TOKEN", new_access_token)
            update_env_file("TESLA_REFRESH_TOKEN", new_refresh_token)

            print(f"[{dt.now()}] Tesla token refreshed successfully.")
            return True
        else:
            print(
                f"[{dt.now()}] Failed to refresh Tesla token. HTTP {response.status_code}: {response.text}"
            )
            return False
    except Exception as e:
        print(f"[{dt.now()}] Error during Tesla token refresh: {e}")
        return False


def wake_up_vehicle(config, max_attempts=10, delay_seconds=5):
    """Sends wake_up command and polls until the vehicle state is 'online'."""
    headers = {
        "Authorization": f"Bearer {config['TESLA_API_TOKEN']}",
        "Content-Type": "application/json",
    }
    base_url = config.get(
        "TESLA_API_BASE_URL", "https://fleet-api.prd.na.vn.cloud.tesla.com"
    ).rstrip("/")
    url = f"{base_url}/api/1/vehicles/{config['TESLA_VIN']}/wake_up"

    print(f"[{dt.now()}] Vehicle is asleep or offline. Sending wake_up command...")

    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.post(url, headers=headers, timeout=15)
            if response.status_code == 401 or (
                response.status_code != 200 and "invalid authentication" in response.text
            ):
                print(
                    f"[{dt.now()}] Detected Tesla authentication failure on wake_up (HTTP {response.status_code}). Attempting token refresh..."
                )
                if refresh_tesla_token(config):
                    headers["Authorization"] = f"Bearer {config['TESLA_API_TOKEN']}"
                    response = requests.post(url, headers=headers, timeout=15)

            if response.status_code == 200:
                state_data = response.json().get("response", {})
                state = state_data.get("state")
                print(
                    f"[{dt.now()}] wake_up attempt {attempt}/{max_attempts}: Vehicle state is '{state}'"
                )
                if state == "online":
                    print(f"[{dt.now()}] Vehicle is online and awake.")
                    return True
            else:
                print(
                    f"[{dt.now()}] wake_up attempt {attempt}/{max_attempts} failed. HTTP {response.status_code}: {response.text}"
                )
        except Exception as e:
            print(f"[{dt.now()}] error during wake_up attempt {attempt}: {e}")

        if attempt < max_attempts:
            time.sleep(delay_seconds)

    print(f"[{dt.now()}] Timed out waiting for vehicle to wake up.")
    return False


def calculate_median(values):
    """Calculates the median value of a numerical list."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    mid = n // 2
    if n % 2 != 0:
        return sorted_vals[mid]
    return (sorted_vals[mid - 1] + sorted_vals[mid]) / 2.0


def cleanup_old_logs(logs_dir, max_days):
    """Prunes log files older than max_days in logs_dir."""
    if not os.path.exists(logs_dir):
        return
    now = dt.now()
    cutoff_time = now - datetime.timedelta(days=max_days)
    for filename in os.listdir(logs_dir):
        if filename.startswith("execution_") and filename.endswith(".log"):
            filepath = os.path.join(logs_dir, filename)
            try:
                file_mtime = dt.fromtimestamp(os.path.getmtime(filepath))
                if file_mtime < cutoff_time:
                    os.remove(filepath)
                    print(f"Cleaned up old log file: {filename}")
            except Exception as e:
                print(f"Failed to delete log file {filename}: {e}")


def get_daylight_window(config, override_time=None):
    """Calculates sunrise and sunset for the current day."""
    local_tz = ZoneInfo(config["TIMEZONE"])

    if override_time is not None:
        now = override_time.astimezone(local_tz)
    else:
        now = dt.now(local_tz)

    city = LocationInfo(
        config["CITY_NAME"],
        "State",
        config["TIMEZONE"],
        config["LATITUDE"],
        config["LONGITUDE"],
    )
    s = sun(city.observer, date=now.date(), tzinfo=local_tz)
    sunrise = s["sunrise"]
    sunset = s["sunset"]
    return sunrise, sunset, now


def get_excess_solar(config, mock_power=None):
    """Fetches real-time excess grid power from Fronius inverter.

    One immediate retry: the Datamanager's Wi-Fi power-save routinely drops
    the first request of a tick (read timeout at 10s), while the retry a
    second later succeeds. Logs only the final outcome to avoid noise.
    """
    if mock_power is not None:
        p_grid = mock_power
    else:
        p_grid = None
        last_err = None
        for attempt in (1, 2):
            try:
                url = f"http://{config['FRONIUS_IP']}/solar_api/v1/GetPowerFlowRealtimeData.fcgi"
                response = requests.get(url, timeout=10)
                data = response.json()
                p_grid = data["Body"]["Data"]["Site"]["P_Grid"]
                if attempt == 2:
                    print(f"[{dt.now()}] Fronius telemetry recovered on retry (attempt 2/2).")
                break
            except Exception as e:
                last_err = e
                if attempt == 1:
                    time.sleep(1.0)
        if p_grid is None:
            print(f"[{dt.now()}] Telemetry fetch error from Fronius (2 attempts): {last_err}")
            return None

    if config["FRONIUS_EXPORT_IS_POSITIVE"]:
        return float(p_grid)
    else:
        return -float(p_grid)


def get_wall_connector_vitals(config, mock_vitals=None):
    """Fetches real-time status from Tesla Wall Connector (Gen 3) local API."""
    if mock_vitals is not None:
        return mock_vitals

    if config.get("MOCK_WALL_CONNECTOR", False):
        mock_connected = _parse_bool(os.getenv("MOCK_WALL_CONNECTOR_CONNECTED", "True"))
        mock_contactor = _parse_bool(os.getenv("MOCK_WALL_CONNECTOR_CONTACTOR", "False"))
        mock_current = float(os.getenv("MOCK_WALL_CONNECTOR_CURRENT", "0.0"))
        return {
            "vehicle_connected": mock_connected,
            "contactor_closed": mock_contactor,
            "vehicle_current_a": mock_current,
            "grid_v": float(config.get("VOLTAGE", 240)),
        }

    twc_ip = config.get("TESLA_WALL_CONNECTOR_IP")
    if not twc_ip:
        return None

    url = f"http://{twc_ip}/api/1/vitals"
    try:
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            return {
                "vehicle_connected": bool(data.get("vehicle_connected", False)),
                "contactor_closed": bool(data.get("contactor_closed", False)),
                "vehicle_current_a": float(data.get("vehicle_current_a", 0.0)),
                "grid_v": float(data.get("grid_v", config.get("VOLTAGE", 240))),
            }
        else:
            print(f"[{dt.now()}] Tesla Wall Connector returned HTTP {response.status_code}")
            return None
    except Exception as e:
        print(f"[{dt.now()}] Telemetry fetch error from Tesla Wall Connector ({twc_ip}): {e}")
        return None


def is_vehicle_at_home(vehicle_lat, vehicle_lon, home_lat, home_lon, tolerance=0.001):
    """Checks if the vehicle is close to home coordinates."""
    lat_diff = abs(vehicle_lat - home_lat)
    lon_diff = abs(vehicle_lon - home_lon)
    at_home = lat_diff < tolerance and lon_diff < tolerance
    if not at_home:
        lat_dist = lat_diff * 111139.0
        lon_dist = lon_diff * 111139.0 * math.cos(math.radians(home_lat))
        distance_meters = math.sqrt(lat_dist**2 + lon_dist**2)
        print(
            f"[{dt.now()}] Vehicle Proximity Check Failed: Vehicle ({vehicle_lat:.6f}, {vehicle_lon:.6f}), Home ({home_lat:.6f}, {home_lon:.6f}), Diff ({lat_diff:.6f}, {lon_diff:.6f}), Est. Distance: {distance_meters:.1f} meters (Tolerance: {tolerance} deg, ~{tolerance * 111139.0:.0f}m)"
        )
    return at_home


def get_tesla_vehicle_data(config, allow_wake_up=True):
    """Fetches real vehicle data or returns mock data depending on configuration."""
    if config["MOCK_TESLA"]:
        mock_home = _parse_bool(os.getenv("MOCK_VEHICLE_HOME", "True"))
        mock_plugged = _parse_bool(os.getenv("MOCK_VEHICLE_PLUGGED", "True"))
        mock_soc = int(os.getenv("MOCK_VEHICLE_SOC", "75"))
        mock_limit = int(os.getenv("MOCK_VEHICLE_CHARGE_LIMIT", "90"))

        if mock_home:
            lat = config["LATITUDE"]
            lon = config["LONGITUDE"]
        else:
            lat = config["LATITUDE"] + 0.5
            lon = config["LONGITUDE"] + 0.5

        charging_state = "Stopped" if mock_plugged else "Disconnected"
        if mock_soc >= mock_limit:
            charging_state = "Complete"

        return {
            "latitude": lat,
            "longitude": lon,
            "charging_state": charging_state,
            "battery_level": mock_soc,
            "charge_limit_soc": mock_limit,
        }

    headers = {
        "Authorization": f"Bearer {config['TESLA_API_TOKEN']}",
        "Content-Type": "application/json",
    }
    base_url = config.get(
        "TESLA_API_BASE_URL", "https://fleet-api.prd.na.vn.cloud.tesla.com"
    ).rstrip("/")
    url = f"{base_url}/api/1/vehicles/{config['TESLA_VIN']}/vehicle_data?endpoints=location_data%3Bcharge_state%3Bdrive_state%3Bvehicle_state"

    max_retries = 3
    retry_delay = 5.0

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code == 401 or (
                response.status_code != 200 and "invalid authentication" in response.text
            ):
                print(
                    f"[{dt.now()}] Detected Tesla authentication failure (HTTP {response.status_code}). Attempting token refresh..."
                )
                if refresh_tesla_token(config):
                    headers["Authorization"] = f"Bearer {config['TESLA_API_TOKEN']}"
                    response = requests.get(url, headers=headers, timeout=15)

            is_asleep = False
            err_msg = ""
            if response.status_code != 200:
                try:
                    err_data = response.json()
                    err_msg = err_data.get("error", "")
                except Exception:
                    err_msg = response.text

                if "offline" in err_msg or "asleep" in err_msg or "timeout" in err_msg:
                    is_asleep = True

            if is_asleep:
                if not allow_wake_up:
                    print(
                        f"[{dt.now()}] Vehicle is asleep/offline or API timed out (allow_wake_up=False). Skipping wake-up."
                    )
                    return {"asleep": True}
                if wake_up_vehicle(config):
                    headers["Authorization"] = f"Bearer {config['TESLA_API_TOKEN']}"
                    response = requests.get(url, headers=headers, timeout=15)
                    if response.status_code == 401 or (
                        response.status_code != 200
                        and "invalid authentication" in response.text
                    ):
                        print(
                            f"[{dt.now()}] Detected Tesla authentication failure after wake_up (HTTP {response.status_code}). Attempting token refresh..."
                        )
                        if refresh_tesla_token(config):
                            headers["Authorization"] = f"Bearer {config['TESLA_API_TOKEN']}"
                            response = requests.get(url, headers=headers, timeout=15)

            if response.status_code == 200:
                data = response.json().get("response", {})
                drive_state = data.get("drive_state", {})
                charge_state = data.get("charge_state", {})
                return {
                    "latitude": drive_state.get("latitude", 0.0),
                    "longitude": drive_state.get("longitude", 0.0),
                    "charging_state": charge_state.get("charging_state", "Disconnected"),
                    "battery_level": charge_state.get("battery_level", 0),
                    "charge_limit_soc": charge_state.get("charge_limit_soc", 100),
                }
            print(
                f"[{dt.now()}] API error response from Tesla on /vehicle_data (Attempt {attempt}/{max_retries}): {response.text}"
            )
        except requests.exceptions.RequestException as e:
            print(
                f"[{dt.now()}] Connection failure to Tesla vehicle data API (Attempt {attempt}/{max_retries}): {e}"
            )

        if attempt < max_retries:
            time.sleep(retry_delay)

    return None


def call_tesla_api(config, endpoint, payload=None):
    """Calls Tesla Fleet API or simulates it depending on configuration."""
    now_str = dt.now().isoformat()

    if config.get("DRY_RUN", config.get("DRY_RUN_TESLA", True)):
        print(
            f"[{now_str}] [DRY RUN] Command /{endpoint} would have been executed with payload: {payload}. (Bypassing real API call)."
        )
        return True

    if config["MOCK_TESLA"]:
        failure_rate = config.get("MOCK_API_FAILURE_RATE", 0.05)
        is_success = random.random() >= failure_rate
        simulated_soc = random.randint(40, 85)

        if not is_success:
            print(
                f"[{now_str}] [MOCK TESLA] Simulated API failure (timeout/network error) on /{endpoint}"
            )
            return False

        print(
            f"[{now_str}] [MOCK TESLA] Command /{endpoint} succeeded. Payload: {payload}. (Est. Vehicle SoC: {simulated_soc}%)"
        )
        return True

    import asyncio
    import aiohttp
    from tesla_fleet_api import TeslaFleetApi
    from tesla_fleet_api.tesla.vehicle.signed import VehicleSigned
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    private_key_path = os.path.join(BASE_DIR, "tesla_private_key.pem")
    if not os.path.exists(private_key_path):
        print(
            f"[{now_str}] Private key file not found at {private_key_path}. Cannot sign command."
        )
        return False

    try:
        with open(private_key_path, "rb") as f:
            key_bytes = f.read()
        private_key = load_pem_private_key(key_bytes, password=None)
    except Exception as e:
        print(f"[{now_str}] Failed to load private key: {e}")
        return False

    async def _execute_command():
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            server = config.get("TESLA_API_BASE_URL")
            api = TeslaFleetApi(
                access_token=config["TESLA_API_TOKEN"],
                session=session,
                server=server,
                region=None,
            )
            api.private_key = private_key
            vehicle = VehicleSigned(api, config["TESLA_VIN"])

            if endpoint == "charge_start":
                return await vehicle.charge_start()
            elif endpoint == "charge_stop":
                return await vehicle.charge_stop()
            elif endpoint == "set_charging_amps":
                amps = payload.get("charging_amps") if payload else None
                if amps is None:
                    return None
                return await vehicle.set_charging_amps(amps)
            else:
                return None

    try:
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        try:
            result = loop.run_until_complete(_execute_command())
        except Exception as cmd_err:
            err_str = str(cmd_err).lower()
            if any(
                tok in err_str
                for tok in ("oauthexpired", "oauth", "token has expired", "invalid_token", "401", "unauthorized")
            ):
                if refresh_tesla_token(config):
                    result = loop.run_until_complete(_execute_command())
                else:
                    raise cmd_err
            else:
                raise cmd_err

        if result:
            print(f"[{now_str}] Command /{endpoint} executed successfully. Response: {result}")
            return True
        else:
            print(f"[{now_str}] Command /{endpoint} returned empty response or failed.")
            return False
    except Exception as e:
        print(f"[{now_str}] Tesla Fleet API command /{endpoint} failed: {e}")
        return False


def calculate_target_amps(excess_watts, config):
    """Calculates charging current target based on excess watts and safety buffer."""
    usable_watts = excess_watts - config["BUFFER_WATTS"]
    min_power_threshold = config["MIN_AMPS"] * config["VOLTAGE"]

    print(
        f"[{dt.now()}] Solar Control Inputs: Excess = {excess_watts:.1f} W, Buffer = {config['BUFFER_WATTS']} W, Usable surplus = {usable_watts:.1f} W, System Voltage = {config['VOLTAGE']} V"
    )

    if usable_watts < min_power_threshold:
        print(
            f"[{dt.now()}] Solar Control Decision: Usable surplus ({usable_watts:.1f} W) is below minimum charge threshold ({min_power_threshold} W)."
        )
        return False, config["MIN_AMPS"]

    calculated_amps = int(usable_watts // config["VOLTAGE"])
    target_amps = min(config["MAX_AMPS"], max(config["MIN_AMPS"], calculated_amps))
    print(
        f"[{dt.now()}] Solar Control Decision: Usable surplus ({usable_watts:.1f} W) yields calculated amps = {calculated_amps} A. Target current = {target_amps} A (Bounded: [{config['MIN_AMPS']} A, {config['MAX_AMPS']} A])."
    )
    return True, target_amps


# ============================================================================
# 4-TIER CLIMATE & ENERGY DECISION ENGINE
# ============================================================================


def is_zone_occupied(occupied_hours: List[int], current_hour: int) -> bool:
    """Evaluates whether current_hour falls within [start_hr, end_hr] (handles overnight wrap)."""
    if not occupied_hours or len(occupied_hours) < 2:
        return True
    start_hr, end_hr = int(occupied_hours[0]), int(occupied_hours[1])
    if start_hr == end_hr:
        return True
    if start_hr < end_hr:
        return start_hr <= current_hour < end_hr
    return current_hour >= start_hr or current_hour < end_hr


def get_active_comfort_band(now: dt, config: Dict) -> Dict[str, float]:
    """
    Returns the active comfort temperature band and targets based on Daytime vs Nighttime:
      - Daytime (default 07:00 - 22:00): 18.0°C - 24.0°C (Cool Target 22.0°C, Heat Target 20.0°C)
      - Nighttime (default 22:00 - 07:00): 16.0°C - 23.0°C (Cool Target 21.0°C, Heat Target 18.0°C)
    """
    day_hours = config.get("DAYTIME_HOURS", [7, 22])
    is_daytime = is_zone_occupied(day_hours, now.hour)

    if is_daytime:
        return {
            "period": "DAY",
            "min_c": float(config.get("DAY_COMFORT_MIN_C", config.get("COMFORT_HEAT_TRIGGER_C", 18.0))),
            "max_c": float(config.get("DAY_COMFORT_MAX_C", config.get("COMFORT_COOL_TRIGGER_C", 24.0))),
            "cool_target_c": float(config.get("DAY_COOL_TARGET_C", config.get("COMFORT_COOL_TARGET_C", 22.0))),
            "heat_target_c": float(config.get("DAY_HEAT_TARGET_C", config.get("COMFORT_HEAT_TARGET_C", 20.0))),
        }
    return {
        "period": "NIGHT",
        "min_c": float(config.get("NIGHT_COMFORT_MIN_C", 16.0)),
        "max_c": float(config.get("NIGHT_COMFORT_MAX_C", 23.0)),
        "cool_target_c": float(config.get("NIGHT_COOL_TARGET_C", 21.0)),
        "heat_target_c": float(config.get("NIGHT_HEAT_TARGET_C", 18.0)),
    }


def is_zone_effectively_occupied(z_meta: Dict, now: dt) -> bool:
    """
    Determines whether a zone is scheduled as occupied at `now`, accounting for
    weekday vs weekend schedules (`weekend_occupied_hours` on Saturday=5 / Sunday=6).
    """
    is_weekend = now.weekday() in (5, 6)
    hours = (
        z_meta.get("weekend_occupied_hours")
        if (is_weekend and z_meta.get("weekend_occupied_hours"))
        else z_meta.get("occupied_hours", [7, 22])
    )
    return is_zone_occupied(hours, now.hour)


def detect_manual_airtouch_override(
    live_airtouch: Dict, cache: Dict, now: dt, config: Dict
) -> Tuple[bool, Optional[str]]:
    """
    Checks if a manual override is currently active or newly triggered by a human
    changing Master AC Power/Mode/Setpoint OR turning a Zone ON/OFF or changing a
    Zone's setpoint on the wall tablet / app.
    """
    override_until_str = cache.get("airtouch_manual_override_until")
    if override_until_str:
        try:
            override_until_dt = dt.fromisoformat(override_until_str).astimezone(now.tzinfo)
            if now < override_until_dt:
                remaining_mins = (override_until_dt - now).total_seconds() / 60.0
                return True, f"Manual override active ({remaining_mins:.1f} mins remaining)"
            else:
                cache["airtouch_manual_override_until"] = None
        except Exception:
            cache["airtouch_manual_override_until"] = None

    last_dispatched = cache.get("airtouch_last_dispatched") or {}
    if not last_dispatched:
        return False, None

    live_ac = live_airtouch.get("ac", {})
    exp_ac = last_dispatched.get("ac", {})
    mismatch_reasons = []

    if exp_ac:
        if exp_ac.get("power") and live_ac.get("power") != exp_ac.get("power"):
            mismatch_reasons.append(f"AC power {exp_ac.get('power')}->{live_ac.get('power')}")
        if (
            live_ac.get("power") == "ON"
            and exp_ac.get("mode")
            and live_ac.get("mode") != exp_ac.get("mode")
        ):
            mismatch_reasons.append(f"AC mode {exp_ac.get('mode')}->{live_ac.get('mode')}")
        if (
            live_ac.get("power") == "ON"
            and exp_ac.get("setpoint_c") is not None
            and live_ac.get("setpoint_c") is not None
            and abs(float(live_ac["setpoint_c"]) - float(exp_ac["setpoint_c"])) >= 0.9
        ):
            mismatch_reasons.append(
                f"AC setpoint {exp_ac.get('setpoint_c')}->{live_ac.get('setpoint_c')}°C"
            )

    # Check per-zone manual changes (e.g. someone turned their room ON/OFF or changed room setpoint)
    exp_zones = last_dispatched.get("zones", {})
    live_zones = live_airtouch.get("zones", {})
    for z_key, exp_z in exp_zones.items():
        live_z = live_zones.get(str(z_key))
        if not live_z:
            continue
        z_name = live_z.get("name", f"Zone_{z_key}")
        if exp_z.get("power") and live_z.get("power") != exp_z.get("power"):
            mismatch_reasons.append(f"{z_name} power {exp_z.get('power')}->{live_z.get('power')}")
        if (
            live_z.get("power") == "ON"
            and exp_z.get("setpoint_c") is not None
            and live_z.get("setpoint_c") is not None
            and abs(float(live_z["setpoint_c"]) - float(exp_z["setpoint_c"])) >= 0.9
        ):
            mismatch_reasons.append(
                f"{z_name} setpoint {exp_z.get('setpoint_c')}->{live_z.get('setpoint_c')}°C"
            )

    if mismatch_reasons:
        hold_mins = float(config.get("HVAC_MANUAL_OVERRIDE_MINUTES", 120))
        until_dt = now + datetime.timedelta(minutes=hold_mins)
        cache["airtouch_manual_override_until"] = until_dt.isoformat()
        if live_ac.get("power") == "OFF" and cache.get("hvac_active_tier") in (
            "TIER3_PRECOOL",
            "TIER3_PREHEAT",
        ):
            cache["hvac_solar_bank_cancelled_date"] = now.date().isoformat()

        cache["airtouch_last_dispatched"] = _snapshot_airtouch_expectations(live_airtouch)
        reason = (
            f"Detected manual tablet/app change ({', '.join(mismatch_reasons)}). "
            f"Locking out automation for {hold_mins:.0f} mins."
        )
        print(f"[{now}] [MANUAL OVERRIDE] {reason}")
        return True, reason

    return False, None


def _snapshot_airtouch_expectations(
    live_airtouch: Dict, hvac_plan: Optional[Dict] = None
) -> Dict:
    """Builds the expected AC + Zone state dictionary for manual override tracking."""
    live_ac = live_airtouch.get("ac", {})
    live_zones = live_airtouch.get("zones", {})

    if hvac_plan is None:
        zones_snap = {}
        for z_k, z_v in live_zones.items():
            zones_snap[str(z_k)] = {
                "power": z_v.get("power"),
                "setpoint_c": z_v.get("setpoint_c"),
            }
        return {
            "ac": {
                "power": live_ac.get("power"),
                "mode": live_ac.get("mode"),
                "setpoint_c": live_ac.get("setpoint_c"),
            },
            "zones": zones_snap,
        }

    plan_zones_by_idx = {
        str(int(c["zone_index"])): c for c in hvac_plan.get("zone_commands", [])
    }
    zones_snap = {}
    for z_k, z_v in live_zones.items():
        planned = plan_zones_by_idx.get(str(z_k), {})
        zones_snap[str(z_k)] = {
            "power": planned.get("power", z_v.get("power")),
            "setpoint_c": (
                planned.get("setpoint_c")
                if planned.get("setpoint_c") is not None
                else z_v.get("setpoint_c")
            ),
        }
    return {
        "ac": {
            "power": hvac_plan.get("power", live_ac.get("power")),
            "mode": hvac_plan.get("mode", live_ac.get("mode")),
            "setpoint_c": (
                hvac_plan.get("setpoint_c")
                if hvac_plan.get("setpoint_c") is not None
                else live_ac.get("setpoint_c")
            ),
        },
        "zones": zones_snap,
    }


def can_change_ac_power(
    target_on: bool, current_on: bool, cache: Dict, now: dt, config: Dict
) -> Tuple[bool, Optional[str]]:
    """Enforces minimum compressor run-time (15m) and minimum off-time (6m)."""
    if target_on == current_on:
        return True, None

    last_change_str = cache.get("airtouch_last_power_change_time")
    if not last_change_str:
        return True, None

    try:
        last_change_dt = dt.fromisoformat(last_change_str).astimezone(now.tzinfo)
        elapsed_mins = (now - last_change_dt).total_seconds() / 60.0
    except Exception:
        return True, None

    if current_on and not target_on:
        min_on = float(config.get("HVAC_MIN_ON_MINUTES", 15))
        if elapsed_mins < min_on:
            return (
                False,
                f"Compressor min run-time guard ({elapsed_mins:.1f}/{min_on:.0f} mins elapsed)",
            )
    elif not current_on and target_on:
        min_off = float(config.get("HVAC_MIN_OFF_MINUTES", 6))
        if elapsed_mins < min_off:
            return (
                False,
                f"Compressor min off-time guard ({elapsed_mins:.1f}/{min_off:.0f} mins elapsed)",
            )

    return True, None


def compressor_is_running(live_ac: Dict) -> bool:
    """True if the compressor is currently running (COOL/HEAT/DRY), False for FAN/OFF."""
    return live_ac.get("power") == "ON" and live_ac.get("mode") in ("COOL", "HEAT", "DRY")


def can_change_compressor(
    target_running: bool, current_running: bool, cache: Dict, now: dt, config: Dict
) -> Tuple[bool, Optional[str]]:
    """
    Enforces minimum compressor run-time and off-time across *mode* changes
    (e.g. FAN<->COOL), which the power-toggle guard cannot see because AC
    power stays ON. Uses airtouch_last_compressor_change_time; no timestamp
    on record means no constraint (fresh cache or manual change long past).
    """
    if target_running == current_running:
        return True, None

    last_change_str = cache.get("airtouch_last_compressor_change_time")
    if not last_change_str:
        return True, None

    try:
        last_change_dt = dt.fromisoformat(last_change_str).astimezone(now.tzinfo)
        elapsed_mins = (now - last_change_dt).total_seconds() / 60.0
    except Exception:
        return True, None

    if current_running and not target_running:
        min_on = float(config.get("HVAC_MIN_ON_MINUTES", 15))
        if elapsed_mins < min_on:
            return (
                False,
                f"Compressor min run-time guard ({elapsed_mins:.1f}/{min_on:.0f} mins elapsed)",
            )
    elif not current_running and target_running:
        min_off = float(config.get("HVAC_MIN_OFF_MINUTES", 6))
        if elapsed_mins < min_off:
            return (
                False,
                f"Compressor min off-time guard ({elapsed_mins:.1f}/{min_off:.0f} mins elapsed)",
            )

    return True, None


def tier1_blocks_banking(tier1_active: bool, hvac_plan: Dict) -> bool:
    """
    Fan-only circulation never blocks solar thermal banking (Tier 3 may
    supersede it); compressor-driven Tier 1 tiers (COOL/HEAT/NIGHT_PURGE) do.
    """
    return bool(tier1_active) and hvac_plan.get("tier") != "TIER1_FAN_CIRCULATE"


def build_zone_plan_for_mode(
    mode: str,
    is_solar_banking: bool,
    now: dt,
    live_zones: Dict[str, Dict],
    config: Dict,
) -> List[Dict]:
    """
    Computes per-zone commands for COOL, HEAT, or FAN (Night Purge).
    Crucial Practical Design:
      - Never forces an active zone OFF if it has an ITC temperature sensor!
      - Instead, scheduled occupied zones get the active comfort target (e.g. 22.0°C day / 21.0°C night),
        while scheduled unoccupied zones get a gentle setback (+1.5°C in COOL, -1.5°C in HEAT) so
        AirTouch 5's native ITC controller automatically closes their damper to 0% when empty,
        AND if someone is home sick or working on a holiday in that room, their room stays ON!
    """
    zones_cfg = config.get("ZONES", {})
    band = get_active_comfort_band(now, config)
    cool_setback = float(config.get("UNOCCUPIED_COOL_SETBACK_C", 1.5))
    heat_setback = float(config.get("UNOCCUPIED_HEAT_SETBACK_C", -1.5))

    zone_cmds = []

    for z_key, z_meta in sorted(zones_cfg.items(), key=lambda x: int(x[0])):
        z_idx = int(z_key)
        z_live = live_zones.get(str(z_idx), {})
        occupied = is_zone_effectively_occupied(z_meta, now)
        priority = float(z_meta.get("priority", 0.5))
        currently_on = z_live.get("power", "ON") == "ON"

        if is_solar_banking:
            should_condition_full = priority >= 0.7 or occupied
            if mode == "COOL":
                base_target = float(
                    z_meta.get("precool_c", config.get("PRECOOL_TARGET_C", 21.0))
                )
                target_temp = base_target if should_condition_full else (base_target + 1.0)
            else:
                base_target = float(
                    z_meta.get("preheat_c", config.get("PREHEAT_TARGET_C", 22.0))
                )
                target_temp = base_target if should_condition_full else (base_target - 1.0)
        else:
            should_condition_full = occupied or priority >= 0.9
            if mode == "COOL":
                base_target = float(z_meta.get("comfort_cool_c", band["cool_target_c"]))
                target_temp = (
                    base_target if should_condition_full else (base_target + cool_setback)
                )
            else:
                base_target = float(z_meta.get("comfort_heat_c", band["heat_target_c"]))
                target_temp = (
                    base_target if should_condition_full else (base_target + heat_setback)
                )

        if mode == "FAN":
            # Night Purge: open occupied/priority zones (or any zone currently ON)
            if should_condition_full or currently_on:
                pct = 100 if (priority >= 0.8 or occupied) else 60
                zone_cmds.append(
                    {
                        "zone_index": z_idx,
                        "name": z_meta.get("name", f"Zone_{z_idx}"),
                        "power": "ON",
                        "control_mode": "DAMPER_PCT",
                        "damper_pct": pct,
                    }
                )
        else:
            has_sensor = z_live.get("has_itc_sensor", True)
            if has_sensor:
                # Keep ITC zones in TEMPERATURE mode; only turn ON if occupied/banking or already ON
                target_power = "ON" if (should_condition_full or currently_on) else "OFF"
                zone_cmds.append(
                    {
                        "zone_index": z_idx,
                        "name": z_meta.get("name", f"Zone_{z_idx}"),
                        "power": target_power,
                        "control_mode": "TEMPERATURE",
                        "setpoint_c": round(target_temp, 1),
                    }
                )
            else:
                if should_condition_full or currently_on:
                    zone_cmds.append(
                        {
                            "zone_index": z_idx,
                            "name": z_meta.get("name", f"Zone_{z_idx}"),
                            "power": "ON",
                            "control_mode": "DAMPER_PCT",
                            "damper_pct": 80 if should_condition_full else 25,
                        }
                    )

    if zone_cmds and all(c.get("power") == "OFF" for c in zone_cmds):
        zone_cmds[0]["power"] = "ON"
        zone_cmds[0]["control_mode"] = "TEMPERATURE"
        zone_cmds[0]["setpoint_c"] = band["cool_target_c"]

    return zone_cmds


def evaluate_tier1_immediate_comfort(
    now: dt,
    airtouch_state: Dict,
    weather_state: Optional[Dict],
    cache: Dict,
    config: Dict,
) -> Tuple[bool, Dict]:
    """
    Evaluates Priority 1: Immediate Home Climate Comfort & Differential Night Purge.
    Uses Daytime (18-24°C) vs Nighttime (16-23°C) comfort bands, and treats any zone
    that is either scheduled-occupied OR currently switched ON as an active comfort zone.

    Below the compressor triggers, graduated fan-only circulation (21°C low,
    22°C medium, 23°C high with 0.5°C switch-off hysteresis) keeps air moving
    for comfort at negligible (~80W) draw. Compressor modes always take
    precedence at their triggers; Tier 3 solar banking may supersede fan-only
    operation but never the reverse.
    """
    zones_cfg = config.get("ZONES", {})
    live_zones = airtouch_state.get("zones", {})
    active_tier = cache.get("hvac_active_tier", "IDLE")
    band = get_active_comfort_band(now, config)

    monitored_temps = []
    for z_key, z_meta in zones_cfg.items():
        z_live = live_zones.get(str(int(z_key)), {})
        temp_c = z_live.get("temperature_c")
        if temp_c is None:
            continue
        # Monitor the zone if it is within its scheduled occupied hours OR if it is switched ON
        if is_zone_effectively_occupied(z_meta, now) or z_live.get("power") == "ON":
            monitored_temps.append((int(z_key), z_meta.get("name", z_key), float(temp_c)))

    if not monitored_temps:
        return False, {"tier": "IDLE", "power": "OFF", "reason": "No active zone readings"}

    max_zone_idx, max_zone_name, max_temp_c = max(monitored_temps, key=lambda x: x[2])
    min_zone_idx, min_zone_name, min_temp_c = min(monitored_temps, key=lambda x: x[2])

    cool_trigger = band["max_c"]
    cool_target = band["cool_target_c"]
    heat_trigger = band["min_c"]
    heat_target = band["heat_target_c"]

    # Check Cooling Trigger (with hysteresis if already in TIER1_COMFORT_COOL or TIER1_NIGHT_PURGE)
    needs_cooling = max_temp_c >= cool_trigger or (
        active_tier in ("TIER1_COMFORT_COOL", "TIER1_NIGHT_PURGE")
        and max_temp_c > (cool_target + 0.3)
    )
    if needs_cooling:
        outdoor_c = weather_state.get("outdoor_temp_c") if weather_state else None
        purge_delta = float(config.get("NIGHT_PURGE_DELTA_C", 2.0))
        purge_min_outdoor = float(config.get("NIGHT_PURGE_MIN_OUTDOOR_C", 15.0))

        # Differential Cooling / Night Purge check
        if (
            outdoor_c is not None
            and outdoor_c <= (max_temp_c - purge_delta)
            and outdoor_c >= purge_min_outdoor
            and max_temp_c < (cool_trigger + 1.5)
        ):
            zone_cmds = build_zone_plan_for_mode("FAN", False, now, live_zones, config)
            return True, {
                "tier": "TIER1_NIGHT_PURGE",
                "power": "ON",
                "mode": "FAN",
                "fan_speed": "HIGH",
                "setpoint_c": None,
                "zone_commands": zone_cmds,
                "draws_compressor_power": False,
                "reason": (
                    f"Differential Night Purge ({band['period']}): {max_zone_name}={max_temp_c:.1f}°C, "
                    f"Outdoor={outdoor_c:.1f}°C (delta >= {purge_delta:.1f}°C)"
                ),
            }

        zone_cmds = build_zone_plan_for_mode("COOL", False, now, live_zones, config)
        return True, {
            "tier": "TIER1_COMFORT_COOL",
            "power": "ON",
            "mode": "COOL",
            "fan_speed": "LOW",
            "setpoint_c": cool_target,
            "zone_commands": zone_cmds,
            "draws_compressor_power": True,
            "reason": (
                f"Immediate cooling needed ({band['period']} band {heat_trigger:.0f}-{cool_trigger:.0f}°C): "
                f"{max_zone_name}={max_temp_c:.1f}°C >= {cool_trigger:.1f}°C -> Target {cool_target:.1f}°C"
            ),
        }

    # Graduated fan-only circulation for the warm-but-not-hot band.
    # Compressor triggers above take precedence; this branch only runs when no
    # compressor action is needed. Thresholds shift down by the switch-off
    # differential while already circulating, giving 0.5°C hysteresis so the
    # fan does not flutter at a boundary.
    fan_low = float(config.get("FAN_LOW_TEMP_C", 21.0))
    fan_med = float(config.get("FAN_MED_TEMP_C", 22.0))
    fan_high = float(config.get("FAN_HIGH_TEMP_C", 23.0))
    fan_hyst = float(config.get("FAN_OFF_DIFFERENTIAL_C", 0.5))
    if active_tier == "TIER1_FAN_CIRCULATE":
        fan_low -= fan_hyst
        fan_med -= fan_hyst
        fan_high -= fan_hyst
    fan_speed = None
    if max_temp_c >= fan_high:
        fan_speed = "HIGH"
    elif max_temp_c >= fan_med:
        fan_speed = "MEDIUM"
    elif max_temp_c >= fan_low:
        fan_speed = "LOW"
    if fan_speed is not None:
        zone_cmds: List[Dict] = []
        if not any(z.get("power") == "ON" for z in live_zones.values()):
            # Nothing open to push air through: open scheduled-occupied zones,
            # keeping their existing control mode/setpoints (power only).
            for z_key, z_meta in sorted(zones_cfg.items(), key=lambda x: int(x[0])):
                if is_zone_effectively_occupied(z_meta, now):
                    zone_cmds.append(
                        {
                            "zone_index": int(z_key),
                            "name": z_meta.get("name", f"Zone_{z_key}"),
                            "power": "ON",
                        }
                    )
        return True, {
            "tier": "TIER1_FAN_CIRCULATE",
            "power": "ON",
            "mode": "FAN",
            "fan_speed": fan_speed,
            "setpoint_c": None,
            "zone_commands": zone_cmds,
            "draws_compressor_power": False,
            "reason": (
                f"Fan circulation ({band['period']}): {max_zone_name}={max_temp_c:.1f}°C -> "
                f"FAN {fan_speed} (compressor off)"
            ),
        }

    # Check Heating Trigger (with hysteresis if already in TIER1_COMFORT_HEAT)
    needs_heating = min_temp_c <= heat_trigger or (
        active_tier == "TIER1_COMFORT_HEAT" and min_temp_c < (heat_target - 0.3)
    )
    if needs_heating:
        # Forecast gate: never burn compressor energy heating on mornings
        # ahead of hot days; the sun warms the house for free within hours.
        try:
            forecast_max = (
                float(weather_state.get("forecast_max_c", 0.0))
                if weather_state
                else 0.0
            )
        except (TypeError, ValueError):
            forecast_max = 0.0
        suppress_at = float(config.get("HEAT_SUPPRESS_FORECAST_MAX_C", 26.0))
        if weather_state is not None and forecast_max >= suppress_at:
            return False, {
                "tier": "IDLE",
                "power": "OFF",
                "reason": (
                    f"Morning heating suppressed (forecast max {forecast_max:.1f}°C "
                    f">= {suppress_at:.1f}°C, house warms naturally): "
                    f"{min_zone_name}={min_temp_c:.1f}°C <= {heat_trigger:.1f}°C"
                ),
            }
        zone_cmds = build_zone_plan_for_mode("HEAT", False, now, live_zones, config)
        return True, {
            "tier": "TIER1_COMFORT_HEAT",
            "power": "ON",
            "mode": "HEAT",
            "fan_speed": "LOW",
            "setpoint_c": heat_target,
            "zone_commands": zone_cmds,
            "draws_compressor_power": True,
            "reason": (
                f"Immediate heating needed ({band['period']} band {heat_trigger:.0f}-{cool_trigger:.0f}°C): "
                f"{min_zone_name}={min_temp_c:.1f}°C <= {heat_trigger:.1f}°C -> Target {heat_target:.1f}°C"
            ),
        }

    return False, {
        "tier": "IDLE",
        "power": "OFF",
        "reason": f"{band['period']} comfort bounds ({heat_trigger:.0f}-{cool_trigger:.0f}°C) satisfied",
    }


def evaluate_tier3_solar_banking(
    now: dt,
    sunset: dt,
    available_solar_watts: float,
    airtouch_state: Dict,
    weather_state: Optional[Dict],
    cache: Dict,
    config: Dict,
) -> Tuple[bool, Dict]:
    """
    Evaluates Priority 3: Home Solar Pre-Cooling / Pre-Heating (Thermal Banking)
    in preparation for forecast hot/cold weather using excess solar.
    """
    if not weather_state:
        return False, {"tier": "IDLE", "power": "OFF", "reason": "No weather forecast available"}

    if cache.get("hvac_solar_bank_cancelled_date") == now.date().isoformat():
        return False, {
            "tier": "IDLE",
            "power": "OFF",
            "reason": "Solar thermal banking cancelled by manual override today",
        }

    # Only bank thermal mass during strong solar window (10:00 AM until 1 hour before sunset)
    latest_bank_hour = max(15, sunset.hour - 1)
    if not (10 <= now.hour < latest_bank_hour):
        return False, {"tier": "IDLE", "power": "OFF", "reason": "Outside solar banking time window"}

    hvac_est_w = float(config.get("HVAC_ESTIMATED_WATTS", 2000))
    active_tier = cache.get("hvac_active_tier", "IDLE")

    # Hysteresis: require full HVAC_ESTIMATED_WATTS (e.g. 2000W) to start,
    # and maintain as long as available solar >= 75% of HVAC_ESTIMATED_WATTS (1500W).
    min_required_w = (hvac_est_w * 0.75) if active_tier in ("TIER3_PRECOOL", "TIER3_PREHEAT") else hvac_est_w
    if available_solar_watts < min_required_w:
        return False, {
            "tier": "IDLE",
            "power": "OFF",
            "reason": f"Available solar ({available_solar_watts:.0f}W) below Tier 3 threshold ({min_required_w:.0f}W)",
        }

    forecast_max_c = float(weather_state.get("forecast_max_c", 22.0))
    forecast_min_c = float(weather_state.get("forecast_min_c", 14.0))
    precool_forecast_c = float(config.get("PRECOOL_FORECAST_MAX_C", 30.0))
    precool_target_c = float(config.get("PRECOOL_TARGET_C", 21.0))
    preheat_forecast_max_c = float(config.get("PREHEAT_FORECAST_MAX_C", 16.0))
    preheat_forecast_min_c = float(config.get("PREHEAT_FORECAST_MIN_C", 8.0))
    preheat_target_c = float(config.get("PREHEAT_TARGET_C", 22.5))

    zones_cfg = config.get("ZONES", {})
    live_zones = airtouch_state.get("zones", {})
    priority_temps = []
    for z_key, z_meta in zones_cfg.items():
        if float(z_meta.get("priority", 0.5)) >= 0.7:
            z_live = live_zones.get(str(int(z_key)), {})
            if z_live.get("temperature_c") is not None:
                priority_temps.append(float(z_live["temperature_c"]))

    if not priority_temps:
        return False, {"tier": "IDLE", "power": "OFF", "reason": "No priority zone temps available"}

    max_prio_temp = max(priority_temps)
    min_prio_temp = min(priority_temps)

    # Check Solar Pre-Cool
    if forecast_max_c >= precool_forecast_c:
        threshold_temp = (precool_target_c + 0.2) if active_tier == "TIER3_PRECOOL" else (precool_target_c + 0.6)
        if max_prio_temp > threshold_temp:
            zone_cmds = build_zone_plan_for_mode("COOL", True, now, live_zones, config)
            return True, {
                "tier": "TIER3_PRECOOL",
                "power": "ON",
                "mode": "COOL",
                "fan_speed": "MEDIUM",
                "setpoint_c": precool_target_c,
                "zone_commands": zone_cmds,
                "draws_compressor_power": True,
                "reason": (
                    f"Solar Pre-Cool: Forecast Max={forecast_max_c:.1f}°C (>= {precool_forecast_c:.1f}°C), "
                    f"Max Priority Zone={max_prio_temp:.1f}°C -> Target {precool_target_c:.1f}°C "
                    f"(Solar={available_solar_watts:.0f}W)"
                ),
            }

    # Check Solar Pre-Heat
    if forecast_max_c <= preheat_forecast_max_c or forecast_min_c <= preheat_forecast_min_c:
        threshold_temp = (preheat_target_c - 0.2) if active_tier == "TIER3_PREHEAT" else (preheat_target_c - 0.6)
        if min_prio_temp < threshold_temp:
            zone_cmds = build_zone_plan_for_mode("HEAT", True, now, live_zones, config)
            return True, {
                "tier": "TIER3_PREHEAT",
                "power": "ON",
                "mode": "HEAT",
                "fan_speed": "MEDIUM",
                "setpoint_c": preheat_target_c,
                "zone_commands": zone_cmds,
                "draws_compressor_power": True,
                "reason": (
                    f"Solar Pre-Heat: Forecast Max={forecast_max_c:.1f}°C / Min={forecast_min_c:.1f}°C, "
                    f"Min Priority Zone={min_prio_temp:.1f}°C -> Target {preheat_target_c:.1f}°C "
                    f"(Solar={available_solar_watts:.0f}W)"
                ),
            }

    return False, {"tier": "IDLE", "power": "OFF", "reason": "No solar thermal banking required"}


def compute_airtouch_delta_commands(
    live_airtouch: Dict, hvac_plan: Dict
) -> Tuple[Optional[Dict], List[Dict]]:
    """
    Compares desired `hvac_plan` against `live_airtouch` and produces only the
    changed AC command (`ac_cmd`) and Zone commands (`zone_cmds`).
    """
    live_ac = live_airtouch.get("ac", {})
    live_zones = live_airtouch.get("zones", {})
    target_power = hvac_plan.get("power", "OFF")

    ac_cmd: Optional[Dict] = None
    zone_cmds: List[Dict] = []

    if target_power == "OFF":
        if live_ac.get("power") != "OFF":
            ac_cmd = {"ac_index": live_ac.get("ac_index", 0), "power": "OFF"}
        return ac_cmd, zone_cmds

    # Target power is ON
    turning_on = live_ac.get("power") != "ON"
    need_ac_cmd = False
    ac_candidate: Dict = {"ac_index": live_ac.get("ac_index", 0)}
    if turning_on:
        ac_candidate["power"] = "ON"
        need_ac_cmd = True
    if hvac_plan.get("mode") and (turning_on or live_ac.get("mode") != hvac_plan["mode"]):
        ac_candidate["mode"] = hvac_plan["mode"]
        need_ac_cmd = True
    if hvac_plan.get("fan_speed") and (
        turning_on or live_ac.get("fan_speed") != hvac_plan["fan_speed"]
    ):
        ac_candidate["fan_speed"] = hvac_plan["fan_speed"]
        need_ac_cmd = True
    if hvac_plan.get("setpoint_c") is not None:
        cur_sp = live_ac.get("setpoint_c")
        if cur_sp is None or abs(float(cur_sp) - float(hvac_plan["setpoint_c"])) >= 0.5:
            ac_candidate["setpoint_c"] = float(hvac_plan["setpoint_c"])
            need_ac_cmd = True

    if need_ac_cmd:
        ac_cmd = ac_candidate

    for z_des in hvac_plan.get("zone_commands", []):
        z_idx = int(z_des["zone_index"])
        z_live = live_zones.get(str(z_idx), {})
        des_pwr = z_des.get("power", "ON")
        cur_pwr = z_live.get("power", "OFF")

        if des_pwr == "OFF":
            if cur_pwr != "OFF":
                zone_cmds.append({"zone_index": z_idx, "power": "OFF"})
            continue

        # Zone should be ON
        z_diff = False
        z_cmd: Dict = {"zone_index": z_idx}
        if cur_pwr != "ON":
            z_cmd["power"] = "ON"
            z_diff = True

        des_mode = z_des.get("control_mode")
        if des_mode and z_live.get("control_mode") != des_mode:
            z_cmd["control_mode"] = des_mode
            z_diff = True

        if z_des.get("setpoint_c") is not None:
            cur_z_sp = z_live.get("setpoint_c")
            if cur_z_sp is None or abs(float(cur_z_sp) - float(z_des["setpoint_c"])) >= 0.5:
                z_cmd["control_mode"] = "TEMPERATURE"
                z_cmd["setpoint_c"] = float(z_des["setpoint_c"])
                z_diff = True
        elif z_des.get("damper_pct") is not None:
            cur_pct = int(z_live.get("damper_open_pct", 0))
            des_pct = int(round(float(z_des["damper_pct"]) / 5.0) * 5)
            if abs(des_pct - cur_pct) >= 10 or z_live.get("control_mode") != "DAMPER_PCT":
                z_cmd["control_mode"] = "DAMPER_PCT"
                z_cmd["damper_pct"] = des_pct
                z_diff = True

        if z_diff:
            zone_cmds.append(z_cmd)

    return ac_cmd, zone_cmds


# ============================================================================
# MAIN UNIFIED ORCHESTRATOR LOOP
# ============================================================================


def run_solar_loop(
    override_time=None,
    mock_power=None,
    mock_twc=None,
    mock_airtouch=None,
    mock_weather=None,
):
    """
    Main unified execution loop for SolarHomeManager.
    Supports Stages 1..4 and the 4-tier priority hierarchy:
      Tier 1: Immediate Home Climate Comfort
      Tier 2: Tesla EV Charging (< 90% SoC)
      Tier 3: Home Solar Pre-Cooling / Pre-Heating
      Tier 4: Tesla EV Top-Off Charging (90% - 100% SoC)
    """
    config = load_config()
    flags = resolve_stage_flags(config)
    print(
        f"[{dt.now()}] Effective config: stage={flags['stage']} "
        f"dry_run_tesla={flags['dry_run_tesla']} dry_run_climate={flags['dry_run_climate']} "
        f"mock_tesla={config.get('MOCK_TESLA')} mock_tw={config.get('MOCK_WALL_CONNECTOR')} "
        f"mock_airtouch={config.get('MOCK_AIRTOUCH')} mock_weather={config.get('MOCK_WEATHER')}"
    )
    cleanup_old_logs(LOGS_DIR, config.get("MAX_LOG_DAYS", 30))
    cache = read_cache()
    sunrise, sunset, now = get_daylight_window(config, override_time=override_time)

    # ------------------------------------------------------------------
    # STAGE 2+: PASSIVE TELEMETRY INGESTION (AIRTOUCH 5 & WEATHER)
    # ------------------------------------------------------------------
    airtouch_state = None
    weather_state = None
    if flags["poll_climate"]:
        airtouch_state = get_airtouch_state(config, mock_airtouch=mock_airtouch)
        if airtouch_state is not None:
            cache["airtouch_state"] = airtouch_state
        weather_state = get_weather_data(config, cache, now, mock_weather=mock_weather)
        write_cache(cache)

    # ------------------------------------------------------------------
    # STAGE 3+: TIER 1 IMMEDIATE CLIMATE COMFORT EVALUATION (DAY OR NIGHT)
    # ------------------------------------------------------------------
    manual_override_active = False
    manual_override_reason = None
    tier1_active = False
    hvac_plan = {"tier": "IDLE", "power": "OFF", "reason": "Stage < 3"}

    if flags["eval_climate"] and airtouch_state is not None:
        manual_override_active, manual_override_reason = detect_manual_airtouch_override(
            airtouch_state, cache, now, config
        )
        if manual_override_active:
            hvac_plan = {
                "tier": "MANUAL_OVERRIDE",
                "power": airtouch_state.get("ac", {}).get("power", "OFF"),
                "reason": manual_override_reason,
            }
        else:
            tier1_active, hvac_plan = evaluate_tier1_immediate_comfort(
                now, airtouch_state, weather_state, cache, config
            )

    # ------------------------------------------------------------------
    # 1. OUT OF DAYLIGHT HOURS ROUTINE
    # ------------------------------------------------------------------
    if now < sunrise or now > sunset:
        # Even at night, Stage 3/4 evaluates Tier 1 comfort / Night Purge
        if flags["eval_climate"] and airtouch_state is not None:
            _finalize_hvac_execution(
                now, airtouch_state, hvac_plan, manual_override_active, cache, config, flags
            )
            write_cache(cache)

        if now > sunset and cache.get("charging", False):
            print(f"[{now}] Past sunset. Shutting down charging.")
            if call_tesla_api(config, "charge_stop"):
                cache["charging"] = False
                if cache.get("vehicle_state"):
                    cache["vehicle_state"]["charging_state"] = "Stopped"
                cache["last_command_time"] = now.isoformat()
                write_cache(cache)
                print(f"[{now}] Success: Charger safely turned off for the night.")
        else:
            print(f"[{now}] Out of daylight hours. Loop idle.")
        return

    # Sunrise State Initialization (Start of Day)
    current_date_str = now.date().isoformat()
    if cache.get("last_sunrise_reset_date") != current_date_str:
        print(f"[{now}] New day detected. Performing sunrise state reset.")
        cache["last_sunrise_reset_date"] = current_date_str
        cache["hvac_solar_bank_cancelled_date"] = None
        write_cache(cache)

    if not cache.get("vehicle_state"):
        cache["vehicle_state"] = {
            "latitude": config["LATITUDE"],
            "longitude": config["LONGITUDE"],
            "charging_state": "Stopped",
            "battery_level": 50,
            "charge_limit_soc": 90,
        }
        write_cache(cache)

    telemetry_fetched_this_loop = False

    # ------------------------------------------------------------------
    # 2. ACTIVE SUNLIGHT TRACKING & DISCRETIONARY SOLAR POOL
    # ------------------------------------------------------------------
    excess_watts = get_excess_solar(config, mock_power=mock_power)
    if excess_watts is None:
        # Even if Fronius is unreachable, Tier 1 comfort still executes in Stage 3/4
        if flags["eval_climate"] and airtouch_state is not None and tier1_active:
            _finalize_hvac_execution(
                now, airtouch_state, hvac_plan, manual_override_active, cache, config, flags
            )
            write_cache(cache)
        return

    # Reclaim active EV charging draw
    actual_charging_watts = 0.0
    if cache.get("charging", False):
        active_amps = cache.get("amps", config["MIN_AMPS"])
        actual_charging_watts = float(active_amps * config["VOLTAGE"])

    # Reclaim active Tier 3 discretionary HVAC draw (only in Stage 4 when AC is actually controlled in Tier 3)
    active_tier3_hvac_watts = 0.0
    if (
        flags["control_climate"]
        and airtouch_state is not None
        and airtouch_state.get("ac", {}).get("power") == "ON"
        and cache.get("hvac_active_tier") in ("TIER3_PRECOOL", "TIER3_PREHEAT")
    ):
        active_tier3_hvac_watts = float(config.get("HVAC_ESTIMATED_WATTS", 2000))

    adjusted_excess_watts = excess_watts + actual_charging_watts + active_tier3_hvac_watts
    print(
        f"[{now}] Raw Excess: {excess_watts:.1f} W, Car Charging Draw: {actual_charging_watts:.1f} W, Adjusted Excess: {adjusted_excess_watts:.1f} W"
    )

    # Append to rolling history
    cache["solar_history"].append({"timestamp": now.isoformat(), "watts": adjusted_excess_watts})

    cutoff = now - datetime.timedelta(minutes=config.get("HISTORY_WINDOW_MINUTES", 15) + 2)
    valid_history = []
    for item in cache["solar_history"]:
        try:
            item_time = dt.fromisoformat(item["timestamp"]).astimezone(now.tzinfo)
            if item_time >= cutoff:
                valid_history.append(item)
        except (ValueError, KeyError):
            pass

    max_size = max(
        1, config.get("HISTORY_WINDOW_MINUTES", 15) // config.get("POLLING_INTERVAL_MINUTES", 5)
    )
    valid_history = valid_history[-max_size:]
    cache["solar_history"] = valid_history
    write_cache(cache)

    watts_list = [item["watts"] for item in valid_history]
    median_excess = calculate_median(watts_list)
    print(
        f"[{now}] Rolling History: {len(valid_history)} readings over last {config.get('HISTORY_WINDOW_MINUTES', 15)} mins. Median = {median_excess:.2f} W. (Readings: {[round(w, 1) for w in watts_list]})"
    )

    # Same-tick deduction if Tier 1 Comfort is newly turning the AC compressor ON right now
    hvac_est_watts = float(config.get("HVAC_ESTIMATED_WATTS", 2000))
    tier1_new_startup_deduction = 0.0
    if (
        flags["eval_climate"]
        and tier1_active
        and hvac_plan.get("draws_compressor_power")
        and airtouch_state is not None
        and airtouch_state.get("ac", {}).get("power") != "ON"
    ):
        tier1_new_startup_deduction = hvac_est_watts

    pool_after_tier1 = (
        max(0.0, median_excess - tier1_new_startup_deduction)
        if flags["control_climate"]
        else median_excess
    )

    target_charging, target_amps = calculate_target_amps(pool_after_tier1, config)
    current_charging = cache.get("charging", False)
    current_amps = cache.get("amps", config["MIN_AMPS"])

    # RULE 1: ZERO-SURPLUS INACTION FOR TESLA CLOUD
    if not current_charging and not target_charging:
        # Even when solar is insufficient for EV charging, execute Stage 3/4 HVAC evaluation
        if flags["eval_climate"] and airtouch_state is not None:
            if not manual_override_active and not tier1_blocks_banking(tier1_active, hvac_plan):
                tier3_active_here, tier3_plan_here = evaluate_tier3_solar_banking(
                    now, sunset, pool_after_tier1, airtouch_state, weather_state, cache, config
                )
                if tier3_active_here:
                    hvac_plan = tier3_plan_here
            _finalize_hvac_execution(
                now, airtouch_state, hvac_plan, manual_override_active, cache, config, flags
            )
            write_cache(cache)
        print(
            f"[{now}] Surplus ({median_excess:.1f} W) insufficient to charge. System idle ($0.00 cloud cost)."
        )
        return

    # RULE 2: LOCAL HARDWARE GATEKEEPING (Tesla Wall Connector Gen 3)
    twc_vitals = get_wall_connector_vitals(config, mock_vitals=mock_twc)
    if twc_vitals is not None:
        cache["wall_connector_state"] = twc_vitals
        is_connected = twc_vitals.get("vehicle_connected", False)
        if not is_connected:
            if current_charging:
                print(
                    f"[{now}] Vehicle disconnected from Wall Connector during active charge. Stopping charge..."
                )
                call_tesla_api(config, "charge_stop")
                cache["charging"] = False
                if cache.get("vehicle_state"):
                    cache["vehicle_state"]["charging_state"] = "Disconnected"
                cache["last_command_time"] = now.isoformat()

            # Since car is unplugged, full solar pool is available for Tier 3 HVAC banking!
            if flags["eval_climate"] and airtouch_state is not None:
                if not manual_override_active and not tier1_blocks_banking(tier1_active, hvac_plan):
                    tier3_active_here, tier3_plan_here = evaluate_tier3_solar_banking(
                        now, sunset, pool_after_tier1, airtouch_state, weather_state, cache, config
                    )
                    if tier3_active_here:
                        hvac_plan = tier3_plan_here
                _finalize_hvac_execution(
                    now, airtouch_state, hvac_plan, manual_override_active, cache, config, flags
                )

            print(
                f"[{now}] Wall Connector: No vehicle connected. Gating failed: vehicle not plugged in. System idle ($0.00 cloud cost)."
            )
            write_cache(cache)
            return
        else:
            print(f"[{now}] Wall Connector: Vehicle is physically connected at home.")
            if cache.get("vehicle_state"):
                cache["vehicle_state"]["charging_state"] = (
                    "Charging" if current_charging else "Stopped"
                )
                cache["vehicle_state"]["latitude"] = config["LATITUDE"]
                cache["vehicle_state"]["longitude"] = config["LONGITUDE"]

    # 3. SAVED STATE EVALUATION & TELEMETRY REFRESH
    saved_state = cache.get("vehicle_state", {})
    is_home = is_vehicle_at_home(
        saved_state.get("latitude", config["LATITUDE"]),
        saved_state.get("longitude", config["LONGITUDE"]),
        config["LATITUDE"],
        config["LONGITUDE"],
        config.get("LOCATION_TOLERANCE", 0.001),
    )
    is_plugged = saved_state.get("charging_state", "Stopped") != "Disconnected"
    soc = saved_state.get("battery_level", 50)
    charge_limit = saved_state.get("charge_limit_soc", 100)
    is_full = soc >= charge_limit

    # RULE 3: PROACTIVE TELEMETRY REFRESH & WAKE COOLDOWN
    last_check_str = cache.get("last_telemetry_check_time")
    should_refresh = False
    refresh_interval = 60.0 if is_full else float(config.get("TELEMETRY_REFRESH_MINUTES", 15))

    if last_check_str:
        try:
            last_check_dt = dt.fromisoformat(last_check_str).astimezone(now.tzinfo)
            time_since_last_check = (now - last_check_dt).total_seconds() / 60.0
            if time_since_last_check >= refresh_interval:
                should_refresh = True
        except Exception:
            should_refresh = True
    else:
        should_refresh = True

    if should_refresh:
        wake_cooldown = float(config.get("WAKE_COOLDOWN_MINUTES", 60))
        last_wake_str = cache.get("last_wake_attempt_time")
        wake_cooldown_elapsed = True
        if last_wake_str:
            try:
                last_wake_dt = dt.fromisoformat(last_wake_str).astimezone(now.tzinfo)
                if (now - last_wake_dt).total_seconds() / 60.0 < wake_cooldown:
                    wake_cooldown_elapsed = False
            except Exception:
                pass

        if twc_vitals is not None and twc_vitals.get("vehicle_connected", False):
            allow_wake_up = (target_charging and not current_charging) and wake_cooldown_elapsed
        else:
            allow_wake_up = (
                is_home
                and is_plugged
                and not is_full
                and (target_charging and not current_charging)
                and wake_cooldown_elapsed
            )

        print(
            f"[{now}] Cached telemetry is stale (older than {refresh_interval} mins). Querying live Tesla telemetry (allow_wake_up={allow_wake_up})..."
        )
        live_data = get_tesla_vehicle_data(config, allow_wake_up=allow_wake_up)

        if live_data is not None:
            telemetry_fetched_this_loop = True
            cache["last_telemetry_check_time"] = now.isoformat()

            if "asleep" in live_data:
                print(f"[{now}] Vehicle is asleep. Keeping existing cached vehicle state.")
                if allow_wake_up:
                    cache["last_wake_attempt_time"] = now.isoformat()
                write_cache(cache)
            else:
                cache["vehicle_state"] = live_data
                if allow_wake_up:
                    cache["last_wake_attempt_time"] = now.isoformat()
                if live_data.get("charging_state") in ("Charging", "Starting"):
                    if not cache.get("charging", False):
                        print(
                            f"[{now}] Live telemetry indicates vehicle is actively charging. Syncing cache charging state to True."
                        )
                    cache["charging"] = True
                    current_charging = True
                elif live_data.get("charging_state") in ("Stopped", "Disconnected", "Complete"):
                    if cache.get("charging", False):
                        print(
                            f"[{now}] Live telemetry indicates vehicle is not charging. Syncing cache charging state to False."
                        )
                    cache["charging"] = False
                    current_charging = False

                write_cache(cache)
                saved_state = live_data
                is_home = is_vehicle_at_home(
                    saved_state["latitude"],
                    saved_state["longitude"],
                    config["LATITUDE"],
                    config["LONGITUDE"],
                    config.get("LOCATION_TOLERANCE", 0.001),
                )
                is_plugged = saved_state["charging_state"] != "Disconnected"
                soc = saved_state["battery_level"]
                charge_limit = saved_state.get("charge_limit_soc", 100)
                is_full = soc >= charge_limit
        else:
            print(f"[{now}] Failed to fetch live Tesla telemetry. Proceeding with saved cached state.")

    # Check EV Gating criteria
    if not is_home or not is_plugged or is_full:
        reason = []
        if not is_home:
            reason.append("vehicle not at home")
        if not is_plugged:
            reason.append("vehicle not plugged in")
        if is_full:
            reason.append(f"vehicle charged to limit ({soc}% >= {charge_limit}%)")

        throttle_info = ""
        last_check_str = cache.get("last_telemetry_check_time")
        if last_check_str:
            try:
                last_check_dt = dt.fromisoformat(last_check_str).astimezone(now.tzinfo)
                elapsed = (now - last_check_dt).total_seconds() / 60.0
                refresh_interval = (
                    60.0 if is_full else float(config.get("TELEMETRY_REFRESH_MINUTES", 15))
                )
                if elapsed < refresh_interval:
                    remaining = refresh_interval - elapsed
                    throttle_info = (
                        f" (Telemetry query throttled. Remaining: {remaining:.1f} mins)"
                    )
            except Exception:
                pass

        print(f"[{now}] Gating criteria failed: {', '.join(reason)}.{throttle_info} System idle.")

        if current_charging:
            print(
                f"[{now}] Disabling charging due to gate failure. Sending command /charge_stop to Tesla..."
            )
            if call_tesla_api(config, "charge_stop"):
                cache["charging"] = False
                if cache.get("vehicle_state"):
                    cache["vehicle_state"]["charging_state"] = "Stopped"
                cache["last_command_time"] = now.isoformat()
                write_cache(cache)

        # Car cannot charge -> all remaining solar is available for Tier 3 HVAC banking!
        if flags["eval_climate"] and airtouch_state is not None:
            if not manual_override_active and not tier1_blocks_banking(tier1_active, hvac_plan):
                tier3_active_here, tier3_plan_here = evaluate_tier3_solar_banking(
                    now, sunset, pool_after_tier1, airtouch_state, weather_state, cache, config
                )
                if tier3_active_here:
                    hvac_plan = tier3_plan_here
            _finalize_hvac_execution(
                now, airtouch_state, hvac_plan, manual_override_active, cache, config, flags
            )
            write_cache(cache)
        return

    # ------------------------------------------------------------------
    # 4-TIER PRIORITY ARBITRATION BETWEEN EV (TIER 2 vs TIER 4) AND HVAC (TIER 3)
    # ------------------------------------------------------------------
    ev_priority_soc = int(config.get("EV_PRIORITY_SOC_THRESHOLD", 90))
    if flags["eval_climate"] and airtouch_state is not None:
        if soc < ev_priority_soc:
            # PRIORITY 2: EV is below 90% SoC -> EV gets first claim on solar pool!
            ev_allocated_w = (target_amps * config["VOLTAGE"]) if target_charging else 0.0
            solar_for_tier3 = max(0.0, pool_after_tier1 - ev_allocated_w)
            if not manual_override_active and not tier1_blocks_banking(tier1_active, hvac_plan):
                tier3_active_here, tier3_plan_here = evaluate_tier3_solar_banking(
                    now, sunset, solar_for_tier3, airtouch_state, weather_state, cache, config
                )
                if tier3_active_here:
                    hvac_plan = tier3_plan_here
        else:
            # PRIORITY 3 beats PRIORITY 4 (90% <= SoC < charge_limit):
            # Evaluate Tier 3 Solar Pre-Cool/Pre-Heat BEFORE allocating to EV 90-100%!
            tier3_active = False
            if not manual_override_active and not tier1_blocks_banking(tier1_active, hvac_plan):
                tier3_active, tier3_plan = evaluate_tier3_solar_banking(
                    now, sunset, pool_after_tier1, airtouch_state, weather_state, cache, config
                )
                if tier3_active:
                    hvac_plan = tier3_plan
            if tier3_active:
                solar_for_tier4 = max(0.0, pool_after_tier1 - hvac_est_watts)
                if flags["control_climate"]:
                    target_charging, target_amps = calculate_target_amps(solar_for_tier4, config)
                else:
                    shadow_chg, shadow_amps = calculate_target_amps(solar_for_tier4, config)
                    hvac_plan["shadow_tier4_ev"] = {
                        "would_charge": shadow_chg,
                        "would_amps": shadow_amps,
                    }

    # If Stage 4 is reducing EV amps / stopping EV to make room for HVAC, execute EV reduction FIRST
    ev_reducing_load = (current_charging and not target_charging) or (
        current_charging and target_charging and target_amps < current_amps
    )

    if not ev_reducing_load and flags["eval_climate"] and airtouch_state is not None:
        _finalize_hvac_execution(
            now, airtouch_state, hvac_plan, manual_override_active, cache, config, flags
        )
        write_cache(cache)

    # ------------------------------------------------------------------
    # 5. TESLA STATE COMPARATIVE TRANSITIONS
    # ------------------------------------------------------------------
    state_changed = (target_charging != current_charging) or (
        target_charging and target_amps != current_amps
    )

    if state_changed:
        if telemetry_fetched_this_loop:
            live_data = saved_state
        else:
            print(
                f"[{now}] Calculated action indicates a change (Charging: {current_charging}->{target_charging}, Amps: {current_amps}->{target_amps}). Confirming saved state with live Tesla telemetry..."
            )
            allow_wake_up = target_charging and not current_charging
            live_data = get_tesla_vehicle_data(config, allow_wake_up=allow_wake_up)
            if live_data is None:
                print(f"[{now}] Failed to fetch live Tesla telemetry. Aborting state change.")
                if ev_reducing_load and flags["eval_climate"] and airtouch_state is not None:
                    _finalize_hvac_execution(
                        now, airtouch_state, hvac_plan, manual_override_active, cache, config, flags
                    )
                    write_cache(cache)
                return

            cache["last_telemetry_check_time"] = now.isoformat()
            if "asleep" not in live_data:
                cache["vehicle_state"] = live_data
                saved_state = live_data
            write_cache(cache)

        if "asleep" not in live_data:
            is_home = is_vehicle_at_home(
                saved_state["latitude"],
                saved_state["longitude"],
                config["LATITUDE"],
                config["LONGITUDE"],
                config.get("LOCATION_TOLERANCE", 0.001),
            )
            is_plugged = saved_state["charging_state"] != "Disconnected"
            soc = saved_state["battery_level"]
            charge_limit = saved_state.get("charge_limit_soc", 100)
            is_full = soc >= charge_limit

            if not is_home or not is_plugged or is_full:
                reason = []
                if not is_home:
                    reason.append("vehicle not at home")
                if not is_plugged:
                    reason.append("vehicle not plugged in")
                if is_full:
                    reason.append(f"vehicle charged to limit ({soc}% >= {charge_limit}%)")
                print(
                    f"[{now}] Live gating check failed after validation: {', '.join(reason)}. Aborting command."
                )

                if current_charging:
                    print(
                        f"[{now}] Disabling charging due to gate failure. Sending command /charge_stop to Tesla..."
                    )
                    if call_tesla_api(config, "charge_stop"):
                        cache["charging"] = False
                        if cache.get("vehicle_state"):
                            cache["vehicle_state"]["charging_state"] = "Stopped"
                        cache["last_command_time"] = now.isoformat()
                        write_cache(cache)
                return

        # Check throttling rule
        last_cmd = cache.get("last_command_time")
        throttled = False
        if last_cmd is not None:
            try:
                last_cmd_dt = dt.fromisoformat(last_cmd).astimezone(now.tzinfo)
                time_since_last_cmd = (now - last_cmd_dt).total_seconds() / 60.0
                throttle_limit = config.get("THROTTLE_INTERVAL_MINUTES", 10)
                if time_since_last_cmd < throttle_limit:
                    remaining = throttle_limit - time_since_last_cmd
                    print(
                        f"[{now}] Throttle Active: Change (Charging: {current_charging}->{target_charging}, Amps: {current_amps}->{target_amps}) throttled. Remaining: {remaining:.1f} mins. Maintaining current state."
                    )
                    throttled = True
            except Exception:
                pass

        if not throttled:
            if target_charging:
                if not current_charging:
                    print(
                        f"[{now}] Solar surplus ({median_excess:.1f} W) detected. Starting charge at {target_amps} A. Sending command /charge_start to Tesla..."
                    )
                    if call_tesla_api(config, "charge_start"):
                        print(
                            f"[{now}] Sending command /set_charging_amps with payload {{'charging_amps': {target_amps}}} to Tesla..."
                        )
                        if call_tesla_api(
                            config, "set_charging_amps", {"charging_amps": target_amps}
                        ):
                            cache["charging"] = True
                            cache["amps"] = target_amps
                            if cache.get("vehicle_state"):
                                cache["vehicle_state"]["charging_state"] = "Charging"
                            cache["last_command_time"] = now.isoformat()
                            write_cache(cache)
                elif target_amps != current_amps:
                    print(
                        f"[{now}] Surplus changed. Adjusting charge: {current_amps} A -> {target_amps} A. Sending command /set_charging_amps to Tesla..."
                    )
                    if call_tesla_api(
                        config, "set_charging_amps", {"charging_amps": target_amps}
                    ):
                        cache["amps"] = target_amps
                        cache["last_command_time"] = now.isoformat()
                        write_cache(cache)
            else:
                if current_charging:
                    print(
                        f"[{now}] Solar surplus dropped to ({median_excess:.1f} W). Stopping charge. Sending command /charge_stop to Tesla..."
                    )
                    if call_tesla_api(config, "charge_stop"):
                        cache["charging"] = False
                        if cache.get("vehicle_state"):
                            cache["vehicle_state"]["charging_state"] = "Stopped"
                        cache["last_command_time"] = now.isoformat()
                        write_cache(cache)
    else:
        if target_charging:
            print(f"[{now}] In balance. Maintaining {current_amps} A.")
        else:
            print(f"[{now}] Surplus ({median_excess:.1f} W) insufficient to charge. System idle.")

    # If EV reduced load first, now execute HVAC ramp-up second
    if ev_reducing_load and flags["eval_climate"] and airtouch_state is not None:
        _finalize_hvac_execution(
            now, airtouch_state, hvac_plan, manual_override_active, cache, config, flags
        )
        write_cache(cache)


def _finalize_hvac_execution(
    now: dt,
    airtouch_state: Dict,
    hvac_plan: Dict,
    manual_override_active: bool,
    cache: Dict,
    config: Dict,
    flags: Dict,
) -> None:
    """
    Applies compressor short-cycle guards, logs Shadow Decisions in Stage 3,
    or dispatches live AirTouch 5 binary commands in Stage 4.
    """
    if manual_override_active:
        cache["hvac_active_tier"] = "MANUAL_OVERRIDE"
        cache["shadow_hvac_plan"] = {
            "timestamp": now.isoformat(),
            "tier": "MANUAL_OVERRIDE",
            "reason": hvac_plan.get("reason"),
            "ac_cmd": None,
            "zone_cmds": [],
        }
        return

    live_ac = airtouch_state.get("ac", {})
    current_on = live_ac.get("power") == "ON"
    target_on = hvac_plan.get("power") == "ON"

    allowed, block_reason = can_change_ac_power(target_on, current_on, cache, now, config)
    if allowed:
        # Compressor short-cycle guard across *mode* changes (e.g. FAN<->COOL),
        # which the power-toggle guard above cannot see. Ground the current
        # state in live telemetry on first contact; track transitions on dispatch.
        if cache.get("hvac_compressor_running") is None:
            cache["hvac_compressor_running"] = compressor_is_running(live_ac)
        target_compressor = target_on and hvac_plan.get("mode") in ("COOL", "HEAT")
        allowed, block_reason = can_change_compressor(
            target_compressor, bool(cache["hvac_compressor_running"]), cache, now, config
        )
    if not allowed:
        print(f"[{now}] [HVAC GUARD] {block_reason}. Holding current AC power state.")
        hvac_plan = {
            "tier": cache.get("hvac_active_tier", "IDLE"),
            "power": "ON" if current_on else "OFF",
            "reason": block_reason,
        }
        ac_cmd, zone_cmds = None, []
    else:
        ac_cmd, zone_cmds = compute_airtouch_delta_commands(airtouch_state, hvac_plan)

    cache["shadow_hvac_plan"] = {
        "timestamp": now.isoformat(),
        "tier": hvac_plan.get("tier", "IDLE"),
        "reason": hvac_plan.get("reason", ""),
        "ac_cmd": ac_cmd,
        "zone_cmds": zone_cmds,
    }

    if not flags["control_climate"]:
        # Stage 3: Shadow Decision Recording only
        print(
            f"[{now}] [SHADOW HVAC] Tier={hvac_plan.get('tier')} | Reason={hvac_plan.get('reason')} | "
            f"Would Dispatch AC={ac_cmd}, Zones={zone_cmds}"
        )
        return

    # Stage 4: Live Command Dispatch
    if ac_cmd or zone_cmds:
        if dispatch_airtouch_commands(
            config, ac_cmd, zone_cmds, dry_run_climate=flags["dry_run_climate"]
        ):
            if target_on != current_on:
                cache["airtouch_last_power_change_time"] = now.isoformat()
            target_compressor = target_on and hvac_plan.get("mode") in ("COOL", "HEAT")
            if bool(cache.get("hvac_compressor_running")) != target_compressor:
                cache["hvac_compressor_running"] = target_compressor
                cache["airtouch_last_compressor_change_time"] = now.isoformat()
            cache["hvac_active_tier"] = hvac_plan.get("tier", "IDLE")
            cache["airtouch_last_dispatched"] = _snapshot_airtouch_expectations(
                airtouch_state, hvac_plan
            )
    else:
        cache["hvac_active_tier"] = hvac_plan.get("tier", "IDLE")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="SolarHomeManager: Unified Solar, AirTouch 5 Climate & Tesla EV Orchestrator"
    )
    parser.add_argument(
        "--check",
        choices=["all", "inverter", "fronius", "charger", "twc", "airtouch5", "airtouch", "bom", "weather",
                 "presence_net", "presence", "deco", "network", "occupancy"],
        help="Run read-only connectivity and data retrieval check for the specified service(s) and exit.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="When used with --check, output raw JSON.",
    )
    args = parser.parse_args()
    if args.check:
        from verify_services import run_diagnostics

        sys.exit(run_diagnostics(args.check, as_json=args.json))
    run_solar_loop()
