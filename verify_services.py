#!/usr/bin/env python3
"""
Read-Only CLI Diagnostic & Data Retrieval Tool for SolarHomeManager
Works on macOS / Linux with standard Python 3 (zero external pip dependencies).

Services tested (strictly read-only, never modifies device state):
  - inverter     : Fronius Smart Inverter (http://FRONIUS_IP/solar_api/v1/GetPowerFlowRealtimeData.fcgi)
  - charger      : Tesla Wall Connector Gen 3 (http://WALL_CONNECTOR_IP/api/1/vitals)
  - airtouch5    : Polyaire AirTouch 5 Console (AIRTOUCH_IP:9005 TCP binary socket)
  - bom          : Bureau of Meteorology (api.weather.bom.gov.au with Open-Meteo fallback)
  - presence_net : Network presence probe (Deco client list + ARP + ping for known laptops).
                   No software installed on devices. Read-only. Opt-in (not part of `all`).
  - all          : Run the 4 core service checks (default; excludes presence_net)

Usage examples:
  python3 verify_services.py
  python3 verify_services.py inverter
  python3 verify_services.py charger
  python3 verify_services.py airtouch5
  python3 verify_services.py bom
  python3 verify_services.py presence_net
  python3 verify_services.py presence_net --log presence_log.jsonl
  python3 verify_services.py presence_net --summarize presence_log.jsonl
  python3 verify_services.py --json
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from verify_socket import verify_airtouch

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
ENV_PATH = os.path.join(BASE_DIR, ".env")

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


def load_basic_settings() -> Dict[str, Any]:
    """Loads settings from config.json and .env using stdlib only.

    Same split as the orchestrator: behavior from config.json, identity
    (LAN IPs, presence targets) from .env first, falling back to config.json
    (migration) and then generic code defaults.
    """
    settings: Dict[str, Any] = {
        "LATITUDE": -33.8688,
        "LONGITUDE": 151.2093,
        "TIMEZONE": "Australia/Sydney",
        # Generic RFC1918 fallbacks below (not user data — real values live in .env).
        "DECO_MAIN_IP": "192.168.1.1",
        "AIRTOUCH_PORT": 9005,
        "PRESENCE_VACANT_AFTER_MINS": 45,
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                settings.update(json.load(f))
        except Exception:
            pass

    if os.path.exists(ENV_PATH):
        try:
            with open(ENV_PATH, "r") as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#") or "=" not in stripped:
                        continue
                    k, v = stripped.split("=", 1)
                    settings[k.strip()] = v.strip().strip('"').strip("'")
        except Exception:
            pass

    for _ip_key, _ip_default in (
        # Generic RFC1918 fallbacks (not user data — real values live in .env).
        ("FRONIUS_IP", "192.168.1.63"),
        ("TESLA_WALL_CONNECTOR_IP", "192.168.1.60"),
        ("AIRTOUCH_IP", "192.168.1.30"),
        ("DECO_MAIN_IP", "192.168.1.1"),
    ):
        if not settings.get(_ip_key):
            settings[_ip_key] = _ip_default

    raw_targets = settings.get("PRESENCE_TARGETS_JSON", "")
    if raw_targets:
        try:
            settings["PRESENCE_TARGETS"] = json.loads(raw_targets)
        except Exception:
            settings["PRESENCE_TARGETS"] = []
    elif "PRESENCE_TARGETS" not in settings:
        settings["PRESENCE_TARGETS"] = []

    return settings


def check_inverter(settings: Dict[str, Any], timeout: float = 10.0) -> Dict[str, Any]:
    """
    Read-only check of Fronius Solar Inverter real-time power flow.
    Includes a retry to handle Fronius Datamanager Wi-Fi power-save wake-up latency.
    """
    host = settings.get("FRONIUS_IP", "192.168.1.63")
    export_pos = str(settings.get("FRONIUS_EXPORT_IS_POSITIVE", "False")).lower() in (
        "true",
        "1",
        "yes",
    )
    url = f"http://{host}/solar_api/v1/GetPowerFlowRealtimeData.fcgi"
    last_err = None
    data = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except Exception as exc:
            last_err = exc
            if attempt == 0:
                time.sleep(1.0)
    if data is None:
        raise last_err  # type: ignore[misc]

    site = data["Body"]["Data"]["Site"]
    p_pv = float(site.get("P_PV") or 0.0)
    p_grid = float(site.get("P_Grid") or 0.0)
    p_load = float(site.get("P_Load") or 0.0)
    e_day = float(site.get("E_Day") or 0.0)
    autonomy = float(site.get("rel_Autonomy") or 0.0)
    excess_w = p_grid if export_pos else -p_grid

    return {
        "status": "OK",
        "endpoint": url,
        "pv_generation_w": round(p_pv, 1),
        "grid_power_w": round(p_grid, 1),
        "grid_direction": "EXPORTING" if excess_w > 0 else "IMPORTING",
        "excess_solar_w": round(excess_w, 1),
        "house_load_w": round(abs(p_load), 1),
        "energy_today_wh": round(e_day, 1),
        "autonomy_pct": round(autonomy, 1),
    }


def check_charger(settings: Dict[str, Any], timeout: float = 5.0) -> Dict[str, Any]:
    """Read-only check of Tesla Wall Connector Gen 3 local vitals API."""
    host = settings.get("TESLA_WALL_CONNECTOR_IP", "192.168.1.60")
    url = f"http://{host}/api/1/vitals"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    connected = bool(data.get("vehicle_connected", False))
    contactor = bool(data.get("contactor_closed", False))
    current_a = float(data.get("vehicle_current_a", 0.0))
    grid_v = float(data.get("grid_v", 0.0))
    grid_hz = float(data.get("grid_hz", 0.0))
    session_wh = float(data.get("session_energy_wh", 0.0))
    handle_temp = float(data.get("handle_temp_c", 0.0))
    mcu_temp = float(data.get("mcu_temp_c", 0.0))

    return {
        "status": "OK",
        "endpoint": url,
        "vehicle_connected": connected,
        "contactor_closed": contactor,
        "charging_active": connected and contactor,
        "vehicle_current_a": round(current_a, 2),
        "grid_voltage_v": round(grid_v, 1),
        "grid_frequency_hz": round(grid_hz, 2),
        "estimated_draw_w": round(current_a * grid_v, 1) if contactor else 0.0,
        "session_energy_wh": round(session_wh, 1),
        "handle_temp_c": round(handle_temp, 1),
        "mcu_temp_c": round(mcu_temp, 1),
    }


def check_airtouch5(settings: Dict[str, Any], timeout: float = 5.0) -> Dict[str, Any]:
    """Read-only check of AirTouch 5 Console over TCP port 9005."""
    host = settings.get("AIRTOUCH_IP", "192.168.1.30")
    port = int(settings.get("AIRTOUCH_PORT", 9005))
    report = verify_airtouch(host, port, timeout=timeout)
    report["status"] = "OK" if (report.get("ac_units") and report.get("zones")) else "INCOMPLETE"
    return report


def check_bom_weather(settings: Dict[str, Any], timeout: float = 6.0) -> Dict[str, Any]:
    """
    Read-only check of Australian Bureau of Meteorology (api.weather.bom.gov.au)
    observations & daily forecast, with automatic fallback to Open-Meteo BOM model.
    """
    lat = float(settings.get("LATITUDE", -33.8688))
    lon = float(settings.get("LONGITUDE", 151.2093))
    tz_name = str(settings.get("TIMEZONE", "Australia/Sydney"))
    geohash = str(settings.get("BOM_GEOHASH") or encode_geohash(lat, lon, precision=6))
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

    try:
        obs_url = f"https://api.weather.bom.gov.au/v1/locations/{geohash}/observations"
        fc_url = f"https://api.weather.bom.gov.au/v1/locations/{geohash}/forecasts/daily"

        req_obs = urllib.request.Request(obs_url, headers=headers)
        with urllib.request.urlopen(req_obs, timeout=timeout) as r_obs:
            obs_payload = json.loads(r_obs.read().decode("utf-8"))["data"]

        req_fc = urllib.request.Request(fc_url, headers=headers)
        with urllib.request.urlopen(req_fc, timeout=timeout) as r_fc:
            fc_payload = json.loads(r_fc.read().decode("utf-8"))["data"]

        outdoor_temp = float(obs_payload.get("temp") or 20.0)
        feels_like = obs_payload.get("temp_feels_like")
        humidity = obs_payload.get("humidity")
        station_name = obs_payload.get("station", {}).get("name", geohash)
        obs_min = (obs_payload.get("min_temp") or {}).get("value")
        obs_max = (obs_payload.get("max_temp") or {}).get("value")

        forecast_days = []
        for idx, day in enumerate(fc_payload[:3]):
            t_max = day.get("temp_max")
            t_min = day.get("temp_min")
            if idx == 0:
                if t_max is None:
                    t_max = obs_max if obs_max is not None else outdoor_temp
                if t_min is None:
                    t_min = obs_min if obs_min is not None else outdoor_temp
            forecast_days.append(
                {
                    "date": str(day.get("date", ""))[:10],
                    "min_c": float(t_min) if t_min is not None else None,
                    "max_c": float(t_max) if t_max is not None else None,
                    "short_text": day.get("short_text") or day.get("extended_text", ""),
                    "rain_chance_pct": (day.get("rain") or {}).get("chance"),
                }
            )

        return {
            "status": "OK",
            "provider": "Bureau of Meteorology (api.weather.bom.gov.au)",
            "geohash": geohash,
            "station": station_name,
            "timezone": tz_name,
            "coordinates": {"latitude": lat, "longitude": lon},
            "outdoor_temp_c": outdoor_temp,
            "feels_like_c": float(feels_like) if feels_like is not None else None,
            "humidity_pct": humidity,
            "today_max_c": forecast_days[0]["max_c"] if forecast_days else None,
            "today_min_c": forecast_days[0]["min_c"] if forecast_days else None,
            "forecast_days": forecast_days,
        }
    except Exception:
        # Fallback to Open-Meteo BOM model
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,cloud_cover"
            f"&daily=temperature_2m_max,temperature_2m_min"
            f"&timezone={urllib.parse.quote(tz_name)}&forecast_days=2"
        )
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        curr = data.get("current", {})
        daily = data.get("daily", {})
        dates = daily.get("time", [])
        maxs = daily.get("temperature_2m_max", [])
        mins = daily.get("temperature_2m_min", [])

        forecast_days = []
        for i in range(min(len(dates), len(maxs), len(mins))):
            forecast_days.append(
                {
                    "date": dates[i],
                    "min_c": float(mins[i]),
                    "max_c": float(maxs[i]),
                }
            )

        return {
            "status": "OK",
            "provider": "Open-Meteo BOM Fallback",
            "timezone": data.get("timezone", tz_name),
            "coordinates": {"latitude": lat, "longitude": lon},
            "outdoor_temp_c": float(curr.get("temperature_2m", 0.0)),
            "cloud_cover_pct": float(curr.get("cloud_cover", 0.0)),
            "today_max_c": forecast_days[0]["max_c"] if forecast_days else None,
            "today_min_c": forecast_days[0]["min_c"] if forecast_days else None,
            "forecast_days": forecast_days,
        }


def normalize_mac(raw: Any) -> Optional[str]:
    """Normalizes a MAC address to lowercase colon form, or None if unparseable.

    Accepts colon/dash/dot separators and unpadded groups (macOS `arp -a`
    prints e.g. `0:11:32:8e:7f:5f`), zero-padding each octet.
    """
    if not raw:
        return None
    text = str(raw).strip()
    if re.fullmatch(r"[0-9a-fA-F]{12}", text):
        cleaned = text.lower()
        return ":".join(cleaned[i : i + 2] for i in range(0, 12, 2))
    parts = re.split(r"[:-]", text)
    if len(parts) == 6 and all(re.fullmatch(r"[0-9a-fA-F]{1,2}", p) for p in parts):
        return ":".join(p.zfill(2).lower() for p in parts)
    if re.fullmatch(r"([0-9a-fA-F]{4}\.){2}[0-9a-fA-F]{4}", text):
        cleaned = re.sub(r"\.", "", text).lower()
        return ":".join(cleaned[i : i + 2] for i in range(0, 12, 2))
    return None


def parse_arp_a_output(text: str) -> List[Dict[str, str]]:
    """Parses `arp -a` output (macOS and Linux formats) into [{ip, mac}]."""
    entries = []
    # Groups may be unpadded on macOS (e.g. `0:11:32:8e:7f:5f`).
    pattern = re.compile(
        r"\(?(\d+\.\d+\.\d+\.\d+)\)?\s+at\s+((?:[0-9a-fA-F]{1,2}[:-]){5}[0-9a-fA-F]{1,2})",
        re.IGNORECASE,
    )
    for line in text.splitlines():
        if "incomplete" in line.lower():
            continue
        m = pattern.search(line)
        if m:
            mac = normalize_mac(m.group(2))
            if mac:
                entries.append({"ip": m.group(1), "mac": mac})
    return entries


def parse_proc_net_arp(text: str) -> List[Dict[str, str]]:
    """Parses Linux /proc/net/arp content into [{ip, mac}]."""
    entries = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4:
            ip, mac = parts[0], normalize_mac(parts[3])
            if mac and mac != "00:00:00:00:00:00":
                entries.append({"ip": ip, "mac": mac})
    return entries


def parse_ip_neigh(text: str) -> List[Dict[str, str]]:
    """Parses `ip neigh show` output (Linux/Synology) into [{ip, mac}]."""
    entries = []
    pattern = re.compile(
        r"(\d+\.\d+\.\d+\.\d+)\s+dev\s+\S+\s+lladdr\s+((?:[0-9a-fA-F]{1,2}[:-]){5}[0-9a-fA-F]{1,2})",
        re.IGNORECASE,
    )
    for line in text.splitlines():
        if "FAILED" in line.upper():
            continue
        m = pattern.search(line)
        if m:
            mac = normalize_mac(m.group(2))
            if mac:
                entries.append({"ip": m.group(1), "mac": mac})
    return entries


def read_local_arp_table() -> Dict[str, Any]:
    """Reads the local neighbour table via `arp -a`, `ip neigh`, or /proc/net/arp.

    An empty table with a working method is a valid result (quiet network or
    sandboxed child process); only report unavailable when no method works.
    """
    errors = []
    try:
        proc = subprocess.run(
            ["arp", "-a"], capture_output=True, text=True, timeout=10
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return {"method": "arp -a", "entries": parse_arp_a_output(proc.stdout)}
        errors.append(f"arp -a returned rc={proc.returncode} with empty output")
    except Exception as exc:
        errors.append(f"arp -a: {exc}")
    try:
        proc = subprocess.run(
            ["ip", "neigh", "show"], capture_output=True, text=True, timeout=10
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return {"method": "ip neigh show", "entries": parse_ip_neigh(proc.stdout)}
        errors.append(f"ip neigh show returned rc={proc.returncode} with empty output")
    except Exception as exc:
        errors.append(f"ip neigh: {exc}")
    try:
        with open("/proc/net/arp", "r") as f:
            content = f.read()
        if content.strip():
            return {"method": "/proc/net/arp", "entries": parse_proc_net_arp(content)}
        errors.append("/proc/net/arp is empty")
    except Exception as exc2:
        errors.append(f"/proc/net/arp: {exc2}")
    return {"method": "no neighbour source returned data", "entries": [], "error": "; ".join(errors)}


def ping_host_once(ip: str, timeout_s: float = 1.5) -> Dict[str, Any]:
    """Sends a single ICMP echo to one IP. Returns {ok, ms}. Never raises."""
    import platform

    system = platform.system().lower()
    if system == "darwin":
        cmd = ["ping", "-c", "1", "-t", str(max(1, int(timeout_s))), ip]
    else:
        # Linux / Synology busybox
        cmd = ["ping", "-c", "1", "-W", str(max(1, int(timeout_s))), ip]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s + 3)
        if proc.returncode == 0:
            m = re.search(r"time[=<]\s*([\d.]+)\s*ms", proc.stdout)
            return {"ok": True, "ms": float(m.group(1)) if m else None}
        return {"ok": False, "ms": None}
    except Exception as exc:
        return {"ok": False, "ms": None, "error": str(exc)}


def extract_form_fields(html: str, limit: int = 12) -> List[str]:
    """Extracts HTML form input names from a login/landing page (read-only recon)."""
    names = re.findall(r"""name\s*=\s*["']([^"']+)["']""", html or "")
    seen = []
    for n in names:
        if n not in seen:
            seen.append(n)
        if len(seen) >= limit:
            break
    return seen


def try_deco_client_list(settings: Dict[str, Any], timeout: float = 4.0) -> Dict[str, Any]:
    """Best-effort probe of the TP-Link Deco main node for its LAN client list.

    Status (Oct 2026, Deco BE65): the BE65 serves its client list through an
    AES-encrypted private web API with no documented local endpoint — older
    LuCI login shapes just return 403/404, so authenticated scraping was
    removed rather than kept as a dead password prompt. This probe fingerprints
    what is reachable (useful to confirm you are pointed at the Deco and not
    the ISP router); the Deco app remains the human-readable client list.
    Presence detection itself runs on ping + DHCP reservations and needs no
    Deco login. Read-only throughout.
    """
    ip = str(settings.get("DECO_MAIN_IP", "192.168.1.1"))
    notes: List[str] = []
    fingerprint: Dict[str, Any] = {}

    for scheme in ("http", "https"):
        try:
            req = urllib.request.Request(f"{scheme}://{ip}/", headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read(4096).decode("utf-8", errors="ignore")
                title_m = re.search(r"<title>(.*?)</title>", body, re.IGNORECASE | re.DOTALL)
                fingerprint[f"{scheme}_root"] = {
                    "http": resp.status,
                    "server": resp.headers.get("Server", ""),
                    "title": (title_m.group(1).strip()[:80] if title_m else ""),
                    "form_fields": extract_form_fields(body),
                    "looks_like_deco": ("deco" in body.lower() or "tp-link" in body.lower()),
                }
        except Exception as exc:
            fingerprint[f"{scheme}_root"] = {"error": str(exc)[:160]}

    notes.append("Deco authenticated client-list scraping is disabled (BE65 AES API, Oct 2026) — see docstring.")

    return {
        "deco_ip": ip,
        "method": "none",
        "fingerprint": fingerprint,
        "clients": [],
        "notes": notes,
    }


def verdict_for_device(
    found_now: bool, last_seen_iso: Optional[str], vacant_after_mins: float, now_iso: str
) -> str:
    """Conservative verdict: network may only suggest VACANT, never prove PRESENT.

    - found_now -> PRESENT_WEAK (device associated/pingable; human may still be absent)
    - missing longer than vacant_after_mins -> ABSENT_LONG (vacancy hint for deep setback only)
    - otherwise UNKNOWN (includes never-seen; fail safe = scheduled/ITC logic)
    """
    if found_now:
        return "PRESENT_WEAK"
    if not last_seen_iso:
        return "UNKNOWN"
    try:
        import datetime as _dt

        last = _dt.datetime.fromisoformat(last_seen_iso)
        now = _dt.datetime.fromisoformat(now_iso)
        if (now - last).total_seconds() / 60.0 >= vacant_after_mins:
            return "ABSENT_LONG"
    except Exception:
        return "UNKNOWN"
    return "UNKNOWN"


def sweep_subnet(
    base: str = "192.168.1",
    start: int = 2,
    end: int = 254,
    timeout_s: float = 1.0,
    workers: int = 32,
    resolve_names: bool = True,
) -> Dict[str, Any]:
    """One-shot fast ping sweep of base.start..end (diagnostic discovery only).

    Alive hosts are reverse-resolved best-effort (Deco DNS usually returns
    device hostnames such as MacBook names) and merged with the ARP table so
    the user can build the PRESENCE_TARGETS MAC->zone map. Not used by the
    orchestrator loop; run manually when devices change.
    """
    import concurrent.futures
    import socket as _socket

    ips = [f"{base}.{i}" for i in range(start, end + 1)]
    alive: List[str] = []

    def _probe(ip: str) -> Optional[str]:
        r = ping_host_once(ip, timeout_s=timeout_s)
        return ip if r.get("ok") else None

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for hit in pool.map(_probe, ips):
            if hit:
                alive.append(hit)

    hostnames: Dict[str, str] = {}
    if resolve_names:
        def _resolve(ip: str) -> tuple:
            try:
                return ip, _socket.gethostbyaddr(ip)[0]
            except Exception:
                return ip, ""

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for ip, name in pool.map(_resolve, alive):
                if name:
                    hostnames[ip] = name

    arp = read_local_arp_table()
    arp_by_ip = {e["ip"]: e["mac"] for e in arp.get("entries", [])}
    rows = [
        {"ip": ip, "mac": arp_by_ip.get(ip, ""), "hostname": hostnames.get(ip, "")}
        for ip in sorted(alive, key=lambda a: tuple(int(p) for p in a.split(".")))
    ]
    return {
        "status": "OK",
        "subnet": f"{base}.0/24",
        "alive_count": len(alive),
        "rows": rows,
        "arp_method": arp.get("method"),
    }


def check_presence_net(settings: Dict[str, Any], timeout: float = 4.0) -> Dict[str, Any]:
    """Read-only network presence probe for known laptops.

    Sources (all local, no device software, no packet sniffing):
      1. TP-Link Deco main-node client list (best-effort, firmware-dependent)
      2. Local neighbour table (ARP: associated recently, even if asleep/filtering ICMP)
      3. Single ICMP echo per configured IP (least reliable; sleep/firewall skew it)

    Targets are pinged FIRST so the neighbour table is freshly populated for
    known devices before it is read. Configure in .env -> PRESENCE_TARGETS_JSON,
    e.g. [{"label": "Study-Laptop", "zone": "4", "macs": [], "ips": ["192.168.1.50"]}]
    (identity lives in .env, never in config.json).
    MACs are confirmatory only: with Deco DHCP reservations the reserved IP is
    the stable identity, so targets work with ips alone.
    Secrets (DECO_MAIN_IP only) come from .env.
    """
    import datetime as _dt

    targets = settings.get("PRESENCE_TARGETS", []) or []
    vacant_after_mins = float(settings.get("PRESENCE_VACANT_AFTER_MINS", 45))
    now_iso = _dt.datetime.now().astimezone().isoformat()

    # Ping known targets first: populates ARP and tests liveness in one pass.
    primed_ping: Dict[str, Dict[str, Any]] = {}
    for t in targets:
        for ip in [str(i) for i in t.get("ips", []) if str(i).strip()]:
            if ip not in primed_ping:
                primed_ping[ip] = ping_host_once(ip, timeout_s=1.5)

    deco = try_deco_client_list(settings, timeout=timeout)
    deco_by_mac = {c["mac"]: c for c in deco.get("clients", [])}
    arp = read_local_arp_table()
    arp_by_mac = {e["mac"]: e["ip"] for e in arp.get("entries", [])}

    devices = []
    for t in targets:
        label = str(t.get("label", "unknown"))
        macs = [m for m in (normalize_mac(m) for m in t.get("macs", [])) if m]
        ips = [str(i) for i in t.get("ips", []) if str(i).strip()]
        found_mac = next((m for m in macs if m in deco_by_mac or m in arp_by_mac), None)
        deco_hit = deco_by_mac.get(found_mac) if found_mac else None
        ping_results = {ip: primed_ping.get(ip, {"ok": False, "ms": None}) for ip in ips}
        ping_ok = any(r.get("ok") for r in ping_results.values())
        found_now = bool(deco_hit) or (found_mac in arp_by_mac) or ping_ok
        devices.append(
            {
                "label": label,
                "zone": str(t.get("zone", "")),
                "macs": macs,
                "ips": ips,
                "in_deco_client_list": bool(deco_hit),
                "deco_node": (deco_hit or {}).get("node", ""),
                "deco_rssi": (deco_hit or {}).get("rssi", ""),
                "in_arp_table": found_mac in arp_by_mac,
                "arp_ip": arp_by_mac.get(found_mac, "") if found_mac else "",
                "ping": ping_results,
                "ping_ok": ping_ok,
                "found_now": found_now,
            }
        )

    unconfigured = not targets
    return {
        "status": "OK",
        "timestamp": now_iso,
        "vacant_after_mins": vacant_after_mins,
        "deco": {k: v for k, v in deco.items() if k != "clients"}
        | {"client_count": len(deco.get("clients", []))},
        "deco_clients": deco.get("clients", []),
        "arp": {"method": arp.get("method"), "entry_count": len(arp.get("entries", []))},
        "devices": devices,
        "unconfigured": unconfigured,
        "caveat": (
            "Network presence is a weak vacancy hint only: PRESENT_WEAK must not trigger "
            "heating/cooling; only ABSENT_LONG (missing > vacant_after_mins across repeated "
            "snapshots) may allow a deep setback, and manual tablet control always wins."
        ),
    }


def summarize_presence_log(path: str, vacant_after_mins: float = 45.0) -> Dict[str, Any]:
    """Analyzes a JSONL log of presence_net snapshots for false-negative measurement."""
    import datetime as _dt

    snaps = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    snaps.append(json.loads(line))
                except Exception:
                    continue
    per_label: Dict[str, Dict[str, Any]] = {}
    for snap in snaps:
        ts = snap.get("timestamp", "")
        for d in snap.get("devices", []):
            st = per_label.setdefault(
                d.get("label", "?"),
                {"snapshots": 0, "present_weak": 0, "missing": 0, "first_seen": ts, "last_seen": ts,
                 "last_present": None},
            )
            st["snapshots"] += 1
            st["last_seen"] = ts
            if d.get("found_now"):
                st["present_weak"] += 1
                st["last_present"] = ts
            else:
                st["missing"] += 1
    for st in per_label.values():
        if st["snapshots"]:
            st["present_fraction"] = round(st["present_weak"] / st["snapshots"], 3)
    return {
        "status": "OK",
        "log_path": path,
        "snapshots": len(snaps),
        "vacant_after_mins": vacant_after_mins,
        "devices": per_label,
        "guidance": (
            "If a laptop that was definitely home shows present_fraction < 0.5, ping/ARP alone "
            "is too flaky for that device: lengthen vacant_after_mins, rely on Deco association "
            "only, or accept schedule+ITC as ground truth for its zone."
        ),
    }


def print_human_report(results: Dict[str, Any]) -> None:
    print("=" * 74)
    print(" SOLAR HOME MANAGER — READ-ONLY SERVICE DIAGNOSTIC REPORT")
    print("=" * 74)

    if "inverter" in results:
        inv = results["inverter"]
        print("\n[1] FRONIUS SOLAR INVERTER")
        if inv.get("status") == "OK":
            print(f"    Status         : OK ({inv['endpoint']})")
            print(f"    PV Generation  : {inv['pv_generation_w']} W")
            print(f"    House Load     : {inv['house_load_w']} W")
            print(f"    Grid Power     : {inv['grid_power_w']} W ({inv['grid_direction']})")
            print(f"    Excess Solar   : {inv['excess_solar_w']} W")
            print(f"    Daily Energy   : {inv['energy_today_wh']} Wh (Autonomy: {inv['autonomy_pct']}%)")
        else:
            print(f"    Status         : ERROR ({inv.get('error')})")

    if "charger" in results:
        twc = results["charger"]
        print("\n[2] TESLA WALL CONNECTOR GEN 3")
        if twc.get("status") == "OK":
            print(f"    Status         : OK ({twc['endpoint']})")
            print(f"    Vehicle Plugged: {twc['vehicle_connected']}")
            print(f"    Contactor      : {'CLOSED (Charging)' if twc['contactor_closed'] else 'OPEN (Idle)'}")
            print(f"    Grid Voltage   : {twc['grid_voltage_v']} V ({twc['grid_frequency_hz']} Hz)")
            print(f"    Current / Draw : {twc['vehicle_current_a']} A ({twc['estimated_draw_w']} W)")
            print(f"    Session Energy : {twc['session_energy_wh']} Wh (Handle: {twc['handle_temp_c']}°C)")
        else:
            print(f"    Status         : ERROR ({twc.get('error')})")

    if "airtouch5" in results:
        at = results["airtouch5"]
        print("\n[3] POLYAIRE AIRTOUCH 5 CONSOLE")
        if at.get("status") == "OK":
            print(f"    Status         : OK ({at['console']})")
            for ac in at.get("ac_units", []):
                print(
                    f"    AC Unit {ac['ac_index']}      : Power={ac['power']}, Mode={ac['mode']}, "
                    f"Fan={ac['fan_speed']}, Setpoint={ac['setpoint_c']}°C, "
                    f"ReturnAir={ac['return_air_temp_c']}°C, Error={ac['error_code']}"
                )
            print("    Zones          :")
            for z in at.get("zones", []):
                batt = "LOW BATTERY" if z["battery_low"] else "OK"
                print(
                    f"      - [{z['zone_index']}] {z['name']:<14} | Power={z['power']:<3} | "
                    f"Temp={str(z['temperature_c']) + '°C':<7} -> Setpoint={str(z['setpoint_c']) + '°C':<7} | "
                    f"Damper={z['damper_open_pct']:>3}% | Battery={batt}"
                )
        else:
            print(f"    Status         : ERROR ({at.get('error')})")

    if "bom" in results:
        wx = results["bom"]
        print("\n[4] BUREAU OF METEOROLOGY (BOM) WEATHER & FORECAST")
        if wx.get("status") == "OK":
            print(f"    Status         : OK ({wx.get('provider')})")
            if wx.get("station"):
                print(f"    BOM Station    : {wx['station']} (geohash: {wx.get('geohash')})")
            extra = (
                f"Feels Like: {wx['feels_like_c']}°C, Humidity: {wx['humidity_pct']}%"
                if wx.get("feels_like_c") is not None
                else f"Cloud Cover: {wx.get('cloud_cover_pct')}%"
            )
            print(f"    Outdoor Temp   : {wx['outdoor_temp_c']}°C ({extra})")
            for day in wx.get("forecast_days", []):
                rain_info = (
                    f" | Rain: {day['rain_chance_pct']}%"
                    if day.get("rain_chance_pct") is not None
                    else ""
                )
                summary = f" | {day['short_text']}" if day.get("short_text") else ""
                print(
                    f"    Forecast {day['date']} : Min {day['min_c']}°C / Max {day['max_c']}°C{rain_info}{summary}"
                )
        else:
            print(f"    Status         : ERROR ({wx.get('error')})")

    if "presence_net" in results:
        pn = results["presence_net"]
        print("\n[5] NETWORK PRESENCE PROBE (DECO + ARP + PING, READ-ONLY)")
        if pn.get("status") == "OK":
            deco = pn.get("deco", {})
            print(f"    Deco node        : {deco.get('deco_ip')} (method: {deco.get('method')})")
            print(f"    Deco clients     : {deco.get('client_count')} | Neighbour entries ({pn.get('arp', {}).get('method')}): {pn.get('arp', {}).get('entry_count')}")
            fp = deco.get("fingerprint", {}) or {}
            for key in ("http_root", "https_root"):
                info = fp.get(key, {}) or {}
                if info.get("http"):
                    print(
                        f"      {key}: HTTP {info['http']} server='{info.get('server', '')}' "
                        f"title='{info.get('title', '')}' deco_like={info.get('looks_like_deco')}"
                    )
                    if info.get("form_fields"):
                        print(f"             form fields: {', '.join(info['form_fields'])}")
                elif info.get("error"):
                    print(f"      {key}: {info['error']}")
            for note in (deco.get("notes", []) or [])[:4]:
                print(f"      note: {note}")
            print("      (Fingerprint above only confirms something answers at DECO_MAIN_IP;")
            print("       the Deco app remains the human-readable client list.)")
            if pn.get("unconfigured"):
                print("    Targets          : none configured — set PRESENCE_TARGETS_JSON in .env")
                print('                       (identity lives in .env, never in config.json)')
            for d in pn.get("devices", []):
                state = "PRESENT_WEAK" if d["found_now"] else "missing-this-tick"
                print(
                    f"      - {d['label']:<16} (zone {d['zone']}) : {state} | "
                    f"deco={d['in_deco_client_list']} node={d['deco_node'] or '-'} | "
                    f"arp={d['in_arp_table']} | ping_ok={d['ping_ok']}"
                )
            print(f"    Caveat           : {pn.get('caveat')}")
        else:
            print(f"    Status         : ERROR ({pn.get('error')})")

    print("\n" + "=" * 74)


def run_diagnostics(
    target: str = "all",
    as_json: bool = False,
    log_file: Optional[str] = None,
    summarize_file: Optional[str] = None,
    sweep: bool = False,
    sweep_subnet_base: Optional[str] = None,
) -> int:
    settings = load_basic_settings()
    target_norm = target.lower().strip()
    aliases = {
        "fronius": "inverter",
        "twc": "charger",
        "airtouch": "airtouch5",
        "weather": "bom",
    }
    target_norm = aliases.get(target_norm, target_norm)
    if target_norm in ("presence_net", "presence", "deco", "network", "occupancy"):
        target_norm = "presence_net"

    if summarize_file and target_norm == "presence_net":
        summary = summarize_presence_log(
            summarize_file, float(settings.get("PRESENCE_VACANT_AFTER_MINS", 45))
        )
        if as_json:
            print(json.dumps(summary, indent=2))
        else:
            print("=" * 74)
            print(" PRESENCE LOG SUMMARY (read-only analysis, no polling)")
            print("=" * 74)
            print(f" Log file  : {summary['log_path']} ({summary['snapshots']} snapshots)")
            for label, st in summary["devices"].items():
                print(
                    f"  - {label:<16} : present {st['present_weak']}/{st['snapshots']} "
                    f"({st.get('present_fraction')}) | last present: {st['last_present']}"
                )
            print(f" Guidance  : {summary['guidance']}")
            print("=" * 74)
        return 0

    if target_norm == "presence_net":
        checks_to_run = ["presence_net"]
    else:
        checks_to_run = (
            ["inverter", "charger", "airtouch5", "bom"]
            if target_norm == "all"
            else [target_norm]
        )

    if sweep and target_norm == "presence_net":
        base = sweep_subnet_base or ".".join(
            str(settings.get("DECO_MAIN_IP", "192.168.1.1")).split(".")[:3]
        )
        sw = sweep_subnet(base=base)
        if as_json:
            print(json.dumps({"sweep": sw}, indent=2))
        else:
            print("=" * 74)
            print(f" SUBNET SWEEP (one-shot discovery, {sw['subnet']}) — neighbour table via {sw['arp_method']}")
            print("=" * 74)
            print(f" Alive hosts: {sw['alive_count']} — use IP+MAC rows to fill PRESENCE_TARGETS")
            for r in sw["rows"]:
                print(f"  - {r['ip']:<15} {r['mac'] or '(no ARP entry)':<19} {r['hostname']}")
            print("=" * 74)
        return 0

    results: Dict[str, Any] = {}
    any_failed = False

    for name in checks_to_run:
        try:
            if name == "inverter":
                results["inverter"] = check_inverter(settings)
            elif name == "charger":
                results["charger"] = check_charger(settings)
            elif name == "airtouch5":
                results["airtouch5"] = check_airtouch5(settings)
            elif name == "bom":
                results["bom"] = check_bom_weather(settings)
            elif name == "presence_net":
                results["presence_net"] = check_presence_net(settings)
            else:
                raise ValueError(f"Unknown service '{name}'")
            if results[name].get("status") != "OK":
                any_failed = True
        except Exception as exc:
            any_failed = True
            results[name] = {"status": "ERROR", "error": str(exc)}

    if log_file and "presence_net" in results and results["presence_net"].get("status") == "OK":
        try:
            with open(log_file, "a") as f:
                f.write(json.dumps(results["presence_net"]) + "\n")
            print(f"(appended snapshot to {log_file})")
        except Exception as exc:
            print(f"(WARNING: could not append to log file {log_file}: {exc})")

    if as_json:
        print(json.dumps(results, indent=2))
    else:
        print_human_report(results)

    return 1 if any_failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Read-only connectivity & data retrieval checks for SolarHomeManager services."
    )
    parser.add_argument(
        "service",
        nargs="?",
        default="all",
        choices=["all", "inverter", "fronius", "charger", "twc", "airtouch5", "airtouch", "bom", "weather",
                 "presence_net", "presence", "deco", "network", "occupancy"],
        help="Service to test (default: all; presence_net is opt-in, not included in all)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON instead of formatted table",
    )
    parser.add_argument(
        "--log",
        metavar="FILE",
        default=None,
        help="With presence_net: append this snapshot as one JSONL line to FILE for 24h analysis",
    )
    parser.add_argument(
        "--summarize",
        metavar="FILE",
        default=None,
        help="With presence_net: analyze a JSONL log file instead of polling the network",
    )
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="With presence_net: one-shot fast ping sweep of the LAN /24 to discover "
             "device IPs/MACs/hostnames for building PRESENCE_TARGETS (not part of polling loop)",
    )
    parser.add_argument(
        "--subnet",
        metavar="BASE",
        default=None,
        help="Subnet base for --sweep, e.g. 192.168.1 (defaults to DECO_MAIN_IP's /24)",
    )
    args = parser.parse_args()
    if args.summarize:
        sys.exit(run_diagnostics("presence_net", as_json=args.json, summarize_file=args.summarize))
    if args.sweep:
        sys.exit(run_diagnostics(args.service, as_json=args.json, sweep=True,
                                 sweep_subnet_base=args.subnet))
    sys.exit(run_diagnostics(args.service, as_json=args.json, log_file=args.log))
