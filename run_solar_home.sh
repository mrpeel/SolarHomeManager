#!/bin/bash
# SolarHomeManager DSM Task Scheduler runner.
# Lowers CPU priority (nice) so Plex transcoding is never impacted.
# ionice is used only when present — stock DSM 7 userland doesn't ship it.
cd /volume1/homes/admin/solar_home_manager || exit 1
source env/bin/activate
if command -v ionice >/dev/null 2>&1; then
  exec nice -n 10 ionice -c 2 -n 7 python3 solar_home_manager.py >> execution.log 2>&1
else
  exec nice -n 10 python3 solar_home_manager.py >> execution.log 2>&1
fi
