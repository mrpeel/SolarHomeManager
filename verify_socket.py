#!/usr/bin/env python3
"""
Phase 1: AirTouch 5 Raw Socket & Binary Protocol Verification Script
Target: 192.168.1.30:9005 (Python 3.9+ standard library only)
"""
import argparse
import json
import socket
import struct
import time
from typing import Dict, List, Optional, Tuple

MAGIC_HEADER = b"\x55\x55\x55\xaa"
STUFF_FIND = b"\x55\x55\x55"
STUFF_REPLACE = b"\x55\x55\x55\x00"

AC_POWER_MAP = {0: "OFF", 1: "ON", 2: "AWAY_OFF", 3: "AWAY_ON", 5: "SLEEP"}
AC_MODE_MAP = {
    0: "AUTO",
    1: "HEAT",
    2: "DRY",
    3: "FAN",
    4: "COOL",
    8: "AUTO_HEAT",
    9: "AUTO_COOL",
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
ZONE_POWER_MAP = {0: "OFF", 1: "ON", 3: "TURBO"}


def crc16_modbus(data: bytes) -> int:
    """Compute CRC-16/MODBUS over unstuffed frame body (Address..Data)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def pack_frame(address: int, msg_id: int, msg_type: int, payload: bytes) -> bytes:
    """Assemble an AirTouch 5 frame with CRC-16 and 0x55555500 byte stuffing."""
    body = struct.pack(">HBBH", address, msg_id & 0xFF, msg_type, len(payload)) + payload
    crc = crc16_modbus(body)
    raw_after_magic = body + struct.pack(">H", crc)
    stuffed = raw_after_magic.replace(STUFF_FIND, STUFF_REPLACE)
    return MAGIC_HEADER + stuffed


def read_next_frame(sock: socket.socket, rx_buf: bytearray) -> Tuple[int, int, int, bytes]:
    """Read from socket buffer until a valid, CRC-verified frame is decoded."""
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


def build_status_req(subtype: int, msg_id: int) -> bytes:
    payload = struct.pack(">BBHHH", subtype, 0x00, 0, 0, 0)
    return pack_frame(0x80B0, msg_id, 0xC0, payload)


def build_zone_names_req(msg_id: int) -> bytes:
    return pack_frame(0x90B0, msg_id, 0x1F, b"\xff\x13")


def parse_ac_status(payload: bytes) -> List[Dict]:
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


def parse_zone_status(payload: bytes, zone_names: Dict[int, str]) -> List[Dict]:
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


def parse_zone_names(payload: bytes) -> Dict[int, str]:
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


def verify_airtouch(host: str, port: int, timeout: float = 5.0) -> Dict:
    rx_buf = bytearray()
    zone_names: Dict[int, str] = {}
    ac_list: Optional[List[Dict]] = None
    zone_list: Optional[List[Dict]] = None

    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(build_zone_names_req(msg_id=1))
        sock.sendall(build_status_req(subtype=0x23, msg_id=2))
        sock.sendall(build_status_req(subtype=0x21, msg_id=3))

        deadline = time.monotonic() + timeout
        while (
            ac_list is None or zone_list is None or not zone_names
        ) and time.monotonic() < deadline:
            try:
                addr, msg_id, msg_type, payload = read_next_frame(sock, rx_buf)
            except socket.timeout:
                break
            if msg_type == 0x1F and payload[:2] == b"\xff\x13":
                zone_names = parse_zone_names(payload)
            elif msg_type == 0xC0 and len(payload) >= 8:
                subtype = payload[0]
                if subtype == 0x23:
                    ac_list = parse_ac_status(payload)
                elif subtype == 0x21:
                    zone_list = parse_zone_status(payload, zone_names)

    if zone_list and zone_names:
        for z in zone_list:
            z["name"] = zone_names.get(z["zone_index"], z["name"])

    return {
        "console": f"{host}:{port}",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "ac_units": ac_list or [],
        "zones": zone_list or [],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verify AirTouch 5 TCP Socket")
    parser.add_argument("--host", default="192.168.1.30", help="AirTouch 5 Console IP")
    parser.add_argument("--port", type=int, default=9005, help="AirTouch 5 TCP Port")
    args = parser.parse_args()
    report = verify_airtouch(args.host, args.port)
    print(json.dumps(report, indent=2))
