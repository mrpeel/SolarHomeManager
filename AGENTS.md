# SolarHomeManager (Unified Solar, AirTouch 5 Climate & Tesla EV Orchestrator) — Agent Guide

Cross-tool project context for coding agents. Keep this file self-contained, pragmatic, and free of tool-specific ceremony.

## What this service is
A low-overhead, unified home energy and climate orchestrator running on a Synology DS218+ NAS (Python 3.11 venv preferred for live Tesla signed commands, 3.9-compatible code, via DSM Task Scheduler). It combines the proven `FreeCharge` Tesla solar charging service with bidirectional local TCP socket control of a Polyaire AirTouch 5 ducted HVAC system (`AIRTOUCH_IP:9005`), local Fronius Primo inverter telemetry (`FRONIUS_IP`), Tesla Wall Connector Gen 3 vitals (`WALL_CONNECTOR_IP`), and BOM/Open-Meteo weather forecasts.

## 4-Tier Priority Waterfall
All discretionary solar surplus and home climate actions are arbitrated in a single coordinated loop in strict priority order:
1. **Tier 1 — Immediate Home Climate Comfort:** Runs regardless of solar surplus when occupied zones breach comfort thresholds or qualify for Differential Night Purge. Treated as mandatory household load (steps down EV charging in the same tick when turning on the compressor).
2. **Tier 2 — EV Charging Below 90% SoC (`SoC < 90%`):** Takes first claim on the rolling median discretionary solar pool (`5 A` to `32 A`). Preempts Tier 3 thermal banking if the car is below 90%.
3. **Tier 3 — Home Solar Pre-Cooling / Pre-Heating (Thermal Banking):** Uses excess solar (`>= 2000 W`) during daylight hours to pre-cool (`21.0°C`) ahead of hot forecast days (`>= 30.0°C`) or pre-heat (`22.5°C`) ahead of cold days (`<= 16.0°C`). Preempts Tier 4 EV top-off (`90–100%`).
4. **Tier 4 — EV Top-Off Charging (`90% <= SoC < charge_limit_soc`):** Uses residual solar surplus remaining after Tier 3 is satisfied or not needed.

## 4-Stage Progressive Production Rollout
Controlled via `"ROLLOUT_STAGE"` (`1..4`) in `config.json`:
- **Stage 1 (`ROLLOUT_STAGE=1`):** EV-Only parity with `FreeCharge` (AirTouch & Weather disabled).
- **Stage 2 (`ROLLOUT_STAGE=2`):** Live EV charging + passive read-only polling of AirTouch 5 (`AIRTOUCH_IP:9005`) and Weather into `state_cache.json` and logs.
- **Stage 3 (`ROLLOUT_STAGE=3`):** Live EV charging + passive telemetry + Shadow 4-Tier Decision Engine (`[SHADOW HVAC]` logs & `shadow_hvac_plan` recorded without sending AC commands).
- **Stage 4 (`ROLLOUT_STAGE=4`):** Full coordinated autonomy (live AirTouch 5 commands + live Tesla charging).

## Configuration split (strict — one file per key)
- **`.env`** (git-ignored): secrets + home identity — Tesla tokens, `TESLA_VIN`, `LATITUDE`/`LONGITUDE`, LAN IPs, `ZONE_*_NAME` room names, `PRESENCE_TARGETS_JSON`. Never committed. (There is deliberately no Deco password: the BE65 exposes no usable local login, so presence runs on ping + DHCP reservations.)
- **`config.json`**: pure behavior only — thresholds, timings, bands, damper minima, priority weights, occupied-hour windows, `ROLLOUT_STAGE`, `DRY_RUN_*`, `MOCK_*` master switches. Safe to commit. Code fallback defaults are generic RFC1918 values, not user data.

## Critical Engineering Invariants
1. **Single-File Core Structure (`solar_home_manager.py`) & Python 3.9-Compatible Syntax:** Avoid Python 3.10+ syntax (`match/case`, `@dataclass(slots=True)`) so the code still parses on 3.9; the production venv should be 3.11+ because `tesla-fleet-api>=1.5.0` (live signed Tesla commands) requires Python ≥ 3.10.
2. **Fault Isolation:** Any failure in AirTouch 5 TCP socket communication or Weather API fetching must be caught and logged without interrupting Tesla solar charging.
3. **Manual Override Preemption:** If live AirTouch 5 state differs from `airtouch_last_dispatched`, lock out automated HVAC commands for `HVAC_MANUAL_OVERRIDE_MINUTES` (`90 mins`) while continuing to regulate EV solar charging around the manual AC load.
4. **Compressor Short-Cycle & Spill Protection:** Enforce `HVAC_MIN_ON_MINUTES` (`15m`), `HVAC_MIN_OFF_MINUTES` (`6m`), and `MIN_TOTAL_DAMPER_PCT` (`120%`).

## Commands & Verification
```bash
# Run read-only CLI checks on macOS for core services (or inverter | charger | airtouch5 | bom)
python3 verify_services.py
python3 verify_services.py inverter
python3 verify_services.py charger
python3 verify_services.py airtouch5
python3 verify_services.py bom

# Read-only network presence probe for known laptops (no software on devices; opt-in, not in `all`)
python3 verify_services.py presence_net
python3 verify_services.py presence_net --log presence_log.jsonl
python3 verify_services.py presence_net --summarize presence_log.jsonl

# Verify live AirTouch 5 TCP socket (AIRTOUCH_IP:9005)
python3 verify_socket.py --host AIRTOUCH_IP --port 9005

# Run unit test suite
python3 -m unittest discover -s tests -v

# Run orchestrator in dry-run mode
python3 solar_home_manager.py
```

## Documentation (`docs/`)
- `docs/architecture_and_design.md`: System architecture, solar pool math, 4-tier priority waterfall, and AirTouch 5 binary protocol.
- `docs/synology_setup.md`: Synology DS218+ installation, Python 3.9 venv setup, DSM Task Scheduler configuration, and 4-stage progressive rollout procedure.
- `docs/tesla_api_setup.md`: Tesla Developer Portal registration, free GitHub Pages/Vercel public key hosting, OAuth token exchange, and pure-Python command signing.
