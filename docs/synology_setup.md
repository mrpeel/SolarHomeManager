# Synology NAS Installation & Progressive Rollout Guide

This guide explains how to deploy **SolarHomeManager** onto your Synology DS218+ NAS running DSM 7.x and safely transition from the standalone `FreeCharge` service using the 4-stage progressive rollout.

---

## 1. Prerequisites
1. **Python 3.11** if available on the NAS (`ls /usr/local/bin/python*` — a DS218+ with current DSM packages shows `python3.11`). Fall back to **Python 3.9** from the Synology Package Center only if 3.11 is absent.
   - Why 3.11 matters: live Tesla signed commands need `tesla-fleet-api>=1.5.0`, which requires Python ≥ 3.10. A 3.9 venv installs everything *except* that library, so Stages 1–3 (shadow) work on 3.9 but live Tesla charging needs 3.11. The code itself stays 3.9-compatible either way.
2. **SSH Enabled** on your Synology NAS (**Control Panel** $\rightarrow$ **Terminal & SNMP** $\rightarrow$ **Terminal** $\rightarrow$ **Enable SSH service**).

---

## 2. Copy Project Files to the Synology NAS

Create a directory on your NAS (e.g. `/volume1/homes/admin/solar_home_manager`) and copy the following files from your Mac:
- `solar_home_manager.py`
- `verify_services.py`
- `verify_socket.py`
- `config.json`
- `requirements.txt`
- `run_solar_home.sh`
- `.env` (or copy your existing `.env` and `tesla_private_key.pem` from `/volume1/homes/admin/tesla_tracker/`)

Example `scp` / `rsync` command from your Mac terminal:
```bash
rsync -av --exclude 'env' --exclude '__pycache__' --exclude '.git' \
  /Users/neilkloot/Code/SolarHomeManager/ \
  admin@<YOUR_NAS_IP>:/volume1/homes/admin/solar_home_manager/
```

If migrating from `FreeCharge`, copy your existing `.env` and private key on the NAS:
```bash
ssh admin@<YOUR_NAS_IP>
cp /volume1/homes/admin/tesla_tracker/.env /volume1/homes/admin/solar_home_manager/.env
cp /volume1/homes/admin/tesla_tracker/tesla_private_key.pem /volume1/homes/admin/solar_home_manager/tesla_private_key.pem
```

---

## 3. Set Up the Python Virtual Environment (3.11 Preferred)

SSH into your Synology NAS and create the virtual environment:

```bash
cd /volume1/homes/admin/solar_home_manager

# Prefer Python 3.11 when present (required for live Tesla signed commands).
# If you already built env/ with 3.9, remove it first: deactivate; rm -rf env
/usr/local/bin/python3.11 -m venv env

# (Fallback only: python3.9 -m venv env — Stages 1–3 shadow work, but
#  pip cannot install tesla-fleet-api>=1.5.0 on 3.9, so live Tesla stays off.)

# Activate virtual environment and install dependencies
source env/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Confirm the interpreter and the signed-command API
python --version                        # expect Python 3.11.x
python -c "from tesla_fleet_api.tesla.vehicle.signed import VehicleSigned; print('signed API ok')"
python -c "import requests, astral, dotenv; print('core deps ok')"

# Make scripts executable
chmod +x run_solar_home.sh verify_services.py verify_socket.py solar_home_manager.py
```

---

## 4. Configure `.env` (Secrets & Home Identity) and Verify All 4 Data Sources

Configuration is strictly split — each key lives in exactly one file:
- **`.env`** (git-ignored, per-machine): Tesla tokens, `TESLA_VIN`, home coordinates, LAN IPs, `ZONE_*_NAME` room names, `PRESENCE_TARGETS_JSON`. Never committed.
- **`config.json`**: pure behavior (thresholds, stages, dry-runs, mocks). Safe to commit.

Ensure `/volume1/homes/admin/solar_home_manager/.env` contains your secrets and household setup (copy the template from `.env.example` and fill in your values):

```ini
TESLA_VIN=5YJ3XXXXXXXXXXXXX
TESLA_CLIENT_ID=your_client_id
TESLA_CLIENT_SECRET=your_client_secret
TESLA_API_TOKEN=your_access_token
TESLA_REFRESH_TOKEN=your_refresh_token

FRONIUS_IP=192.168.1.63
AIRTOUCH_IP=192.168.1.30
AIRTOUCH_PORT=9005
TESLA_WALL_CONNECTOR_IP=192.168.1.60
DECO_MAIN_IP=192.168.1.1

ZONE_0_NAME=Bedroom 1
ZONE_1_NAME=Living Room
ZONE_2_NAME=Bedroom 2
ZONE_3_NAME=Bedroom 3
ZONE_4_NAME=Study

LATITUDE=-33.87
LONGITUDE=151.21
```

> The values above are examples — use whatever names/addresses your home uses. They stay on the NAS only.
> Migration note: `config.json` no longer carries IPs, room names, or presence targets. If your old `.env` (e.g. carried over from FreeCharge) still has `ROLLOUT_STAGE`, `DRY_RUN*`, or `MOCK_TESLA` lines, those keys now live in `config.json` instead — the old `.env` lines are ignored. Every run logs an `Effective config:` banner line so you can confirm what is actually live.

Run the read-only service diagnostic check on the NAS to confirm connectivity to all 4 services:
```bash
./env/bin/python3 verify_services.py
```
Verify that `[1] FRONIUS SOLAR INVERTER`, `[2] TESLA WALL CONNECTOR GEN 3`, `[3] POLYAIRE AIRTOUCH 5 CONSOLE`, and `[4] BUREAU OF METEOROLOGY (BOM)` all show `Status : OK`.

---

## 5. Configure Synology DSM Task Scheduler

`run_solar_home.sh` runs `solar_home_manager.py` under lowered CPU (`nice -n 10`) and I/O priority (`ionice -c 2 -n 7`) so Plex Media Server transcoding and playback are never impacted.

1. Open **DSM Control Panel** $\rightarrow$ **Task Scheduler**.
2. If replacing `FreeCharge`, uncheck **Enabled** on your old `Tesla Solar Tracker` task and click **Save**.
3. Click **Create** $\rightarrow$ **Scheduled Task** $\rightarrow$ **User-defined script**.
4. **General Tab:**
   - **Task:** `Solar Home Manager`
   - **User:** `admin`
   - **Enabled:** Checked
5. **Schedule Tab:**
   - **Run on the following days:** `Daily`
   - **Frequency:** `Every 2 minutes` (or `Every 5 minutes`)
   - **First run time:** `00:00`
   - **Last run time:** `23:58` *(Note: Running 24/7 allows Stage 4 to manage evening/overnight comfort and Night Purge, while solar EV charging and solar thermal banking automatically restrict themselves to `astral` sunrise–sunset windows).*
6. **Task Settings Tab:**
   - **Run command:**
     ```bash
     /bin/bash /volume1/homes/admin/solar_home_manager/run_solar_home.sh
     ```
7. Click **OK** to save.

---

## 6. Progressive 4-Stage Rollout & Verification Procedure

You can advance or roll back stages at any time simply by editing `"ROLLOUT_STAGE"` in `config.json` (no Task Scheduler changes required).

### Deployment 1: EV-Only Parity (`"ROLLOUT_STAGE": 1`)
- **Config (`config.json`):**
  ```json
  "ROLLOUT_STAGE": 1,
  "DRY_RUN": false,
  "DRY_RUN_TESLA": false,
  "DRY_RUN_CLIMATE": true,
  "MOCK_TESLA": false
  ```
- **Behavior:** AirTouch 5 and BOM Weather polling are completely disabled. Only Fronius, Wall Connector, and Tesla Fleet API charging run.
- **Verification:** Check `logs/execution_YYYYMMDD.log` and `state_cache.json` over 2–3 sunny days to confirm Tesla solar charging operates identically to `FreeCharge`.

### Deployment 2: Passive Telemetry Ingestion (`"ROLLOUT_STAGE": 2`)
- **Config (`config.json`):** Change `"ROLLOUT_STAGE": 2`.
- **Behavior:** Live Tesla charging continues. Each run now also polls the AirTouch 5 (`AIRTOUCH_IP:9005`) and BOM Weather API (cached 30m) and saves their state in `state_cache.json` (`airtouch_state` and `weather_state`).
- **Verification:** Inspect `state_cache.json` over 3–5 days to confirm accurate room temperatures, damper percentages, and BOM forecasts with zero disruptions to EV charging.

### Deployment 3: Shadow Decision Recording (`"ROLLOUT_STAGE": 3`)
- **Config (`config.json`):** Change `"ROLLOUT_STAGE": 3`.
- **Behavior:** Live Tesla charging continues. The 4-Tier Decision Engine now evaluates the full home state on every tick and logs `[SHADOW HVAC]` entries in `logs/execution_YYYYMMDD.log` and `shadow_hvac_plan` in `state_cache.json` showing the exact AC and zone commands it *would* have taken—without sending any commands to the AirTouch 5.
- **Verification:** Review `[SHADOW HVAC]` log lines across warm, cool, and sunny days. Tune `COMFORT_COOL_TRIGGER_C`, `PRECOOL_FORECAST_MAX_C`, or per-zone setpoints in `config.json` if desired.

### Deployment 4: Full Coordinated Autonomy (`"ROLLOUT_STAGE": 4`)
- **Config (`config.json`):**
  ```json
  "ROLLOUT_STAGE": 4,
  "DRY_RUN": false,
  "DRY_RUN_TESLA": false,
  "DRY_RUN_CLIMATE": false
  ```
- **Behavior:** Live AirTouch 5 control and live Tesla solar charging operate together under the 4-tier priority hierarchy (`Immediate Comfort` $\rightarrow$ `EV < 90%` $\rightarrow$ `Solar Pre-Cool/Heat` $\rightarrow$ `EV 90–100%`), with automatic 120-minute lockout whenever anyone adjusts the AirTouch wall tablet or phone app manually.
- **Instant Rollback:** Setting `"ROLLOUT_STAGE": 3` (or `1`) in `config.json` immediately disables live HVAC control on the next tick while keeping EV charging active.
