import datetime
from datetime import datetime as dt
import json
import os
import struct
import time
import unittest
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import solar_home_manager

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TEST_DIR)

# Redirect logs directory globally for test isolation
solar_home_manager.LOGS_DIR = os.path.join(TEST_DIR, "temp_test_logs")
os.makedirs(solar_home_manager.LOGS_DIR, exist_ok=True)


def make_default_zones_config():
    return {
        "0": {
            "name": "Bedroom 1",
            "priority": 0.9,
            "occupied_hours": [20, 8],
            "min_damper": 20,
            "comfort_cool_c": 22.5,
            "comfort_heat_c": 20.0,
            "precool_c": 21.0,
            "preheat_c": 22.0,
        },
        "1": {
            "name": "Living",
            "priority": 1.0,
            "occupied_hours": [7, 22],
            "min_damper": 30,
            "comfort_cool_c": 23.0,
            "comfort_heat_c": 20.5,
            "precool_c": 21.0,
            "preheat_c": 22.5,
        },
        "4": {
            "name": "Study",
            "priority": 0.8,
            "occupied_hours": [8, 18],
            "min_damper": 15,
            "comfort_cool_c": 23.0,
            "comfort_heat_c": 20.5,
            "precool_c": 21.5,
            "preheat_c": 22.0,
        },
    }


def make_mock_airtouch_state(ac_power="OFF", ac_mode="COOL", ac_setpoint=22.0, living_temp=22.5):
    return {
        "ac": {
            "ac_index": 0,
            "power": ac_power,
            "mode": ac_mode,
            "fan_speed": "LOW",
            "intelligent_auto": False,
            "setpoint_c": ac_setpoint,
            "return_air_temp_c": living_temp,
            "spill_active": False,
            "bypass_active": False,
            "turbo_active": False,
            "error_code": 0,
        },
        "zones": {
            "0": {
                "zone_index": 0,
                "name": "Bedroom 1",
                "power": "ON",
                "control_mode": "TEMPERATURE",
                "damper_open_pct": 50,
                "setpoint_c": 22.0,
                "has_itc_sensor": True,
                "temperature_c": living_temp,
                "battery_low": False,
                "spill_active": False,
            },
            "1": {
                "zone_index": 1,
                "name": "Living",
                "power": "ON",
                "control_mode": "TEMPERATURE",
                "damper_open_pct": 80,
                "setpoint_c": 22.5,
                "has_itc_sensor": True,
                "temperature_c": living_temp,
                "battery_low": False,
                "spill_active": False,
            },
            "4": {
                "zone_index": 4,
                "name": "Study",
                "power": "ON",
                "control_mode": "TEMPERATURE",
                "damper_open_pct": 50,
                "setpoint_c": 22.5,
                "has_itc_sensor": True,
                "temperature_c": living_temp,
                "battery_low": False,
                "spill_active": False,
            },
        },
        "low_battery_zones": [],
    }


class TestAirTouchProtocolFraming(unittest.TestCase):
    def test_crc16_reference_vectors(self):
        # Reference vector from AirTouch 5 spec §4.a.ii (Request Zone Status)
        zone_req = solar_home_manager.build_airtouch_status_req(subtype=0x21, msg_id=1)
        expected = bytes(
            [
                0x55, 0x55, 0x55, 0xAA,
                0x80, 0xB0, 0x01, 0xC0, 0x00, 0x08,
                0x21, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
                0xA4, 0x31,
            ]
        )
        self.assertEqual(zone_req, expected)

        # Reference vector from AirTouch 5 spec §4.a.iv (Request AC Status)
        ac_req = solar_home_manager.build_airtouch_status_req(subtype=0x23, msg_id=1)
        expected_ac = bytes(
            [
                0x55, 0x55, 0x55, 0xAA,
                0x80, 0xB0, 0x01, 0xC0, 0x00, 0x08,
                0x23, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
                0x7D, 0xB0,
            ]
        )
        self.assertEqual(ac_req, expected_ac)

    def test_byte_stuffing_and_unstuffing(self):
        # Payload containing three consecutive 0x55 bytes must be stuffed with 0x00
        payload = b"\x01\x55\x55\x55\x02"
        frame = solar_home_manager.pack_airtouch_frame(0x80B0, 5, 0x1F, payload)
        self.assertIn(b"\x55\x55\x55\x00\x02", frame[4:])

        # Simulated socket read should unstuff and verify CRC cleanly
        mock_sock = MagicMock()
        rx_buf = bytearray(frame)
        addr, msg_id, msg_type, decoded_payload = solar_home_manager.read_airtouch_frame(
            mock_sock, rx_buf
        )
        self.assertEqual(addr, 0x80B0)
        self.assertEqual(msg_id, 5)
        self.assertEqual(msg_type, 0x1F)
        self.assertEqual(decoded_payload, payload)

    def test_ac_and_zone_control_frame_encoding(self):
        # Spec §4.a.iii: Turn off second AC (ac_index=1)
        frame_off = solar_home_manager.build_ac_control_frame(
            ac_index=1, power="OFF", msg_id=1
        )
        expected_off = bytes(
            [
                0x55, 0x55, 0x55, 0xAA,
                0x80, 0xB0, 0x01, 0xC0, 0x00, 0x0C,
                0x22, 0x00, 0x00, 0x00, 0x00, 0x04, 0x00, 0x01,
                0x21, 0xFF, 0x00, 0xFF,
                0xD3, 0x47,
            ]
        )
        self.assertEqual(frame_off, expected_off)

        # Spec §4.a.i: Turn off second zone (zone_index=1)
        frame_z_off = solar_home_manager.build_zone_control_frame(
            [{"zone_index": 1, "power": "OFF"}], msg_id=0x0F
        )
        expected_z_off = bytes(
            [
                0x55, 0x55, 0x55, 0xAA,
                0x80, 0xB0, 0x0F, 0xC0, 0x00, 0x0C,
                0x20, 0x00, 0x00, 0x00, 0x00, 0x04, 0x00, 0x01,
                0x01, 0x02, 0xFF, 0x00,
                0xF0, 0xA1,
            ]
        )
        self.assertEqual(frame_z_off, expected_z_off)


class TestSolarControlMath(unittest.TestCase):
    def setUp(self):
        self.config = {
            "MIN_AMPS": 5,
            "MAX_AMPS": 32,
            "VOLTAGE": 240,
            "BUFFER_WATTS": 150,
        }

    def test_insufficient_solar(self):
        charging, amps = solar_home_manager.calculate_target_amps(1000, self.config)
        self.assertFalse(charging)
        self.assertEqual(amps, 5)

    def test_sufficient_solar_mid_range(self):
        charging, amps = solar_home_manager.calculate_target_amps(3000, self.config)
        self.assertTrue(charging)
        self.assertEqual(amps, 11)

    def test_solar_max_amps_cap(self):
        charging, amps = solar_home_manager.calculate_target_amps(10000, self.config)
        self.assertTrue(charging)
        self.assertEqual(amps, 32)


class TestFroniusRetry(unittest.TestCase):
    def setUp(self):
        self.config = {
            "FRONIUS_IP": "127.0.0.1",
            "FRONIUS_EXPORT_IS_POSITIVE": False,
        }

    def _ok_response(self, p_grid):
        resp = MagicMock()
        resp.json.return_value = {"Body": {"Data": {"Site": {"P_Grid": p_grid}}}}
        return resp

    @patch("solar_home_manager.time.sleep", return_value=None)
    @patch("solar_home_manager.requests.get")
    def test_retry_succeeds_on_second_attempt(self, mock_get, _mock_sleep):
        """A single dropped read (Datamanager Wi-Fi power-save) is recovered by the retry."""
        import requests as real_requests

        mock_get.side_effect = [
            real_requests.exceptions.ReadTimeout("timed out"),
            self._ok_response(-3000.0),
        ]
        self.assertEqual(solar_home_manager.get_excess_solar(self.config), 3000.0)
        self.assertEqual(mock_get.call_count, 2)

    @patch("solar_home_manager.time.sleep", return_value=None)
    @patch("solar_home_manager.requests.get")
    def test_returns_none_after_two_failures(self, mock_get, _mock_sleep):
        """Two consecutive failures still skip the tick safely instead of crashing."""
        import requests as real_requests

        mock_get.side_effect = real_requests.exceptions.ConnectTimeout("down")
        self.assertIsNone(solar_home_manager.get_excess_solar(self.config))
        self.assertEqual(mock_get.call_count, 2)

    @patch("solar_home_manager.requests.get")
    def test_no_retry_when_first_attempt_succeeds(self, mock_get):
        """The happy path makes exactly one request (no pointless retry delay)."""
        mock_get.return_value = self._ok_response(-1500.0)
        self.assertEqual(solar_home_manager.get_excess_solar(self.config), 1500.0)
        self.assertEqual(mock_get.call_count, 1)


class TestProgressiveRolloutAnd4TierPriority(unittest.TestCase):
    def setUp(self):
        self.original_cache_path = solar_home_manager.CACHE_PATH
        solar_home_manager.CACHE_PATH = os.path.join(TEST_DIR, "temp_rollout_cache.json")
        if os.path.exists(solar_home_manager.CACHE_PATH):
            os.remove(solar_home_manager.CACHE_PATH)

        self.config_patcher = patch("solar_home_manager.load_config")
        self.mock_load_config = self.config_patcher.start()

        self.base_config = {
            "FRONIUS_IP": "127.0.0.1",
            "FRONIUS_EXPORT_IS_POSITIVE": False,
            "TESLA_WALL_CONNECTOR_IP": "192.168.1.60",
            "MOCK_WALL_CONNECTOR": False,
            "TESLA_VIN": "test_vin",
            "TESLA_API_BASE_URL": "https://fleet-api.prd.na.vn.cloud.tesla.com",
            "TESLA_API_TOKEN": "token",
            "MOCK_TESLA": True,
            "MOCK_API_FAILURE_RATE": 0.0,
            "DRY_RUN": True,
            "DRY_RUN_TESLA": True,
            "DRY_RUN_CLIMATE": True,
            "LATITUDE": -33.8688,
            "LONGITUDE": 151.2093,
            "TIMEZONE": "Australia/Sydney",
            "CITY_NAME": "Sydney",
            "VOLTAGE": 240,
            "MIN_AMPS": 5,
            "MAX_AMPS": 32,
            "BUFFER_WATTS": 150,
            "EV_PRIORITY_SOC_THRESHOLD": 90,
            "LOCATION_TOLERANCE": 0.001,
            "HISTORY_WINDOW_MINUTES": 10,
            "POLLING_INTERVAL_MINUTES": 5,
            "THROTTLE_INTERVAL_MINUTES": 10,
            "TELEMETRY_REFRESH_MINUTES": 15,
            "WAKE_COOLDOWN_MINUTES": 60,
            "AIRTOUCH_IP": "192.168.1.30",
            "AIRTOUCH_PORT": 9005,
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
            "ZONES": make_default_zones_config(),
            "ROLLOUT_STAGE": 1,
            "ENABLE_CLIMATE_TELEMETRY": False,
            "ENABLE_CLIMATE_DECISIONS": False,
            "ENABLE_CLIMATE_CONTROL": False,
            "MAX_LOG_DAYS": 30,
        }
        self.mock_load_config.return_value = self.base_config

    def tearDown(self):
        self.config_patcher.stop()
        if os.path.exists(solar_home_manager.CACHE_PATH):
            os.remove(solar_home_manager.CACHE_PATH)
        solar_home_manager.CACHE_PATH = self.original_cache_path

    @patch("solar_home_manager.get_weather_data")
    @patch("solar_home_manager.get_airtouch_state")
    @patch("solar_home_manager.get_tesla_vehicle_data")
    def test_stage_1_ev_only_parity_skips_climate_and_weather(
        self, mock_vehicle, mock_airtouch, mock_weather
    ):
        """Deployment 1 (ROLLOUT_STAGE=1): Only EV charging runs; AirTouch and Weather are never polled."""
        self.base_config["ROLLOUT_STAGE"] = 1
        tz = ZoneInfo("Australia/Sydney")
        midday = dt(2026, 10, 19, 12, 0, 0, tzinfo=tz)

        mock_twc = {
            "vehicle_connected": True,
            "contactor_closed": False,
            "vehicle_current_a": 0.0,
            "grid_v": 240.0,
        }
        mock_vehicle.return_value = {
            "latitude": -33.8688,
            "longitude": 151.2093,
            "charging_state": "Stopped",
            "battery_level": 70,
            "charge_limit_soc": 90,
        }

        solar_home_manager.run_solar_loop(
            override_time=midday, mock_power=-3000.0, mock_twc=mock_twc
        )

        mock_airtouch.assert_not_called()
        mock_weather.assert_not_called()
        cache = solar_home_manager.read_cache()
        self.assertTrue(cache["charging"])
        self.assertEqual(cache["amps"], 11)

    @patch("solar_home_manager.dispatch_airtouch_commands")
    @patch("solar_home_manager.get_tesla_vehicle_data")
    def test_stage_2_passive_telemetry_updates_state_without_decisions(
        self, mock_vehicle, mock_dispatch_at
    ):
        """Deployment 2 (ROLLOUT_STAGE=2): Reads AirTouch & Weather into cache, but makes no HVAC decisions."""
        self.base_config["ROLLOUT_STAGE"] = 2
        tz = ZoneInfo("Australia/Sydney")
        midday = dt(2026, 10, 19, 12, 0, 0, tzinfo=tz)

        mock_twc = {
            "vehicle_connected": True,
            "contactor_closed": False,
            "vehicle_current_a": 0.0,
            "grid_v": 240.0,
        }
        mock_vehicle.return_value = {
            "latitude": -33.8688,
            "longitude": 151.2093,
            "charging_state": "Stopped",
            "battery_level": 70,
            "charge_limit_soc": 90,
        }
        at_state = make_mock_airtouch_state(living_temp=26.0)  # Hot indoor temp
        wx_state = {
            "outdoor_temp_c": 31.0,
            "cloud_cover_pct": 10.0,
            "forecast_max_c": 34.0,
            "forecast_min_c": 18.0,
        }

        solar_home_manager.run_solar_loop(
            override_time=midday,
            mock_power=-3000.0,
            mock_twc=mock_twc,
            mock_airtouch=at_state,
            mock_weather=wx_state,
        )

        mock_dispatch_at.assert_not_called()
        cache = solar_home_manager.read_cache()
        self.assertEqual(cache["airtouch_state"]["zones"]["1"]["temperature_c"], 26.0)
        self.assertEqual(cache["weather_state"]["forecast_max_c"], 34.0)
        self.assertEqual(cache["shadow_hvac_plan"], {})
        # EV still gets full 3000W -> 11A because Stage 2 does not run HVAC decisions
        self.assertTrue(cache["charging"])
        self.assertEqual(cache["amps"], 11)

    @patch("solar_home_manager.dispatch_airtouch_commands")
    @patch("solar_home_manager.get_tesla_vehicle_data")
    def test_stage_3_shadow_decisions_records_plan_without_dispatching(
        self, mock_vehicle, mock_dispatch_at
    ):
        """Deployment 3 (ROLLOUT_STAGE=3): Records [SHADOW HVAC] action in cache without touching AC or reducing EV amps."""
        self.base_config["ROLLOUT_STAGE"] = 3
        tz = ZoneInfo("Australia/Sydney")
        midday = dt(2026, 10, 19, 12, 0, 0, tzinfo=tz)

        mock_twc = {
            "vehicle_connected": True,
            "contactor_closed": False,
            "vehicle_current_a": 0.0,
            "grid_v": 240.0,
        }
        mock_vehicle.return_value = {
            "latitude": -33.8688,
            "longitude": 151.2093,
            "charging_state": "Stopped",
            "battery_level": 70,
            "charge_limit_soc": 90,
        }
        # Indoor Living temp is 25.5°C (>= 24.5°C Tier 1 comfort trigger)
        at_state = make_mock_airtouch_state(ac_power="OFF", living_temp=25.5)
        wx_state = {
            "outdoor_temp_c": 29.0,
            "cloud_cover_pct": 5.0,
            "forecast_max_c": 31.0,
            "forecast_min_c": 17.0,
        }

        solar_home_manager.run_solar_loop(
            override_time=midday,
            mock_power=-3500.0,
            mock_twc=mock_twc,
            mock_airtouch=at_state,
            mock_weather=wx_state,
        )

        mock_dispatch_at.assert_not_called()
        cache = solar_home_manager.read_cache()
        self.assertEqual(cache["shadow_hvac_plan"]["tier"], "TIER1_COMFORT_COOL")
        self.assertEqual(cache["shadow_hvac_plan"]["ac_cmd"]["power"], "ON")
        self.assertEqual(cache["shadow_hvac_plan"]["ac_cmd"]["mode"], "COOL")
        # In Stage 3, EV charging is still unadjusted (3500 - 150 = 3350 // 240 = 13A)
        self.assertTrue(cache["charging"])
        self.assertEqual(cache["amps"], 13)

    @patch("solar_home_manager.dispatch_airtouch_commands")
    @patch("solar_home_manager.get_tesla_vehicle_data")
    def test_stage_4_priority_1_comfort_deducts_solar_and_steps_down_ev(
        self, mock_vehicle, mock_dispatch_at
    ):
        """
        Priority 1 (Immediate Home Comfort) beats Priority 2 (EV < 90%):
        When Living is 25.5°C and AC turns ON (2000W draw), 3500W export pool is reduced
        to 1500W for the EV -> EV charges at 5A ((1500 - 150) // 240 = 5A) instead of 13A.
        """
        self.base_config["ROLLOUT_STAGE"] = 4
        self.base_config["DRY_RUN_CLIMATE"] = False
        mock_dispatch_at.return_value = True

        tz = ZoneInfo("Australia/Sydney")
        midday = dt(2026, 10, 19, 12, 0, 0, tzinfo=tz)

        mock_twc = {
            "vehicle_connected": True,
            "contactor_closed": False,
            "vehicle_current_a": 0.0,
            "grid_v": 240.0,
        }
        mock_vehicle.return_value = {
            "latitude": -33.8688,
            "longitude": 151.2093,
            "charging_state": "Stopped",
            "battery_level": 65,  # Below 90%
            "charge_limit_soc": 90,
        }
        at_state = make_mock_airtouch_state(ac_power="OFF", living_temp=25.5)
        wx_state = {
            "outdoor_temp_c": 29.0,
            "cloud_cover_pct": 5.0,
            "forecast_max_c": 31.0,
            "forecast_min_c": 17.0,
        }

        solar_home_manager.run_solar_loop(
            override_time=midday,
            mock_power=-3500.0,
            mock_twc=mock_twc,
            mock_airtouch=at_state,
            mock_weather=wx_state,
        )

        mock_dispatch_at.assert_called_once()
        cache = solar_home_manager.read_cache()
        self.assertEqual(cache["hvac_active_tier"], "TIER1_COMFORT_COOL")
        self.assertTrue(cache["charging"])
        self.assertEqual(cache["amps"], 5)

    @patch("solar_home_manager.dispatch_airtouch_commands")
    @patch("solar_home_manager.get_tesla_vehicle_data")
    def test_stage_4_priority_2_ev_below_90_beats_tier_3_precool(
        self, mock_vehicle, mock_dispatch_at
    ):
        """
        Priority 2 (EV < 90% SoC) beats Priority 3 (Solar Pre-Cooling):
        Home is at 22.5°C, Forecast is hot (33°C), Solar is 3000W, EV is at 70% SoC.
        EV claims 11A (2640W), leaving only 360W (< 2000W needed for Tier 3 Pre-Cool).
        Result: EV charges at 11A, Tier 3 Pre-Cool does NOT start.
        On a 27°C-outdoor day fan circulation is suppressed by the duct-heat
        guard (ceiling ducts would re-heat the rooms), so HVAC stays IDLE/OFF
        instead of blowing warm air while the EV charges.
        """
        self.base_config["ROLLOUT_STAGE"] = 4
        self.base_config["DRY_RUN_CLIMATE"] = False
        mock_dispatch_at.return_value = True

        tz = ZoneInfo("Australia/Sydney")
        midday = dt(2026, 10, 19, 12, 0, 0, tzinfo=tz)

        mock_twc = {
            "vehicle_connected": True,
            "contactor_closed": False,
            "vehicle_current_a": 0.0,
            "grid_v": 240.0,
        }
        mock_vehicle.return_value = {
            "latitude": -33.8688,
            "longitude": 151.2093,
            "charging_state": "Stopped",
            "battery_level": 70,  # < 90%
            "charge_limit_soc": 100,
        }
        at_state = make_mock_airtouch_state(ac_power="OFF", living_temp=22.5)
        wx_state = {
            "outdoor_temp_c": 27.0,
            "cloud_cover_pct": 0.0,
            "forecast_max_c": 33.0,  # Hot forecast
            "forecast_min_c": 18.0,
        }

        solar_home_manager.run_solar_loop(
            override_time=midday,
            mock_power=-3000.0,
            mock_twc=mock_twc,
            mock_airtouch=at_state,
            mock_weather=wx_state,
        )

        # Fan suppressed (outdoor 27°C >= 26°C guard): no dispatch, AC stays OFF.
        mock_dispatch_at.assert_not_called()
        cache = solar_home_manager.read_cache()
        self.assertEqual(cache["hvac_active_tier"], "IDLE")
        self.assertTrue(cache["charging"])
        self.assertEqual(cache["amps"], 11)

    @patch("solar_home_manager.dispatch_airtouch_commands")
    @patch("solar_home_manager.get_tesla_vehicle_data")
    def test_stage_4_priority_3_precool_beats_priority_4_ev_90_to_100(
        self, mock_vehicle, mock_dispatch_at
    ):
        """
        Priority 3 (Solar Pre-Cooling) beats Priority 4 (EV 90-100% SoC):
        EV is at 92% SoC (limit 100%), Forecast is hot (33°C), Solar is 2800W.
        Tier 3 Pre-Cool claims 2000W first, leaving 800W (< 1200W min EV threshold).
        Result: AirTouch 5 starts Tier 3 Pre-Cooling (21.0°C), and EV does NOT charge.
        """
        self.base_config["ROLLOUT_STAGE"] = 4
        self.base_config["DRY_RUN_CLIMATE"] = False
        mock_dispatch_at.return_value = True

        tz = ZoneInfo("Australia/Sydney")
        midday = dt(2026, 10, 19, 12, 0, 0, tzinfo=tz)

        mock_twc = {
            "vehicle_connected": True,
            "contactor_closed": False,
            "vehicle_current_a": 0.0,
            "grid_v": 240.0,
        }
        mock_vehicle.return_value = {
            "latitude": -33.8688,
            "longitude": 151.2093,
            "charging_state": "Stopped",
            "battery_level": 92,  # >= 90%
            "charge_limit_soc": 100,
        }
        at_state = make_mock_airtouch_state(ac_power="OFF", living_temp=22.5)
        wx_state = {
            "outdoor_temp_c": 27.0,
            "cloud_cover_pct": 0.0,
            "forecast_max_c": 33.0,
            "forecast_min_c": 18.0,
        }

        solar_home_manager.run_solar_loop(
            override_time=midday,
            mock_power=-2800.0,
            mock_twc=mock_twc,
            mock_airtouch=at_state,
            mock_weather=wx_state,
        )

        mock_dispatch_at.assert_called_once()
        cache = solar_home_manager.read_cache()
        self.assertEqual(cache["hvac_active_tier"], "TIER3_PRECOOL")
        self.assertFalse(cache["charging"])

    @patch("solar_home_manager.dispatch_airtouch_commands")
    @patch("solar_home_manager.get_tesla_vehicle_data")
    def test_stage_4_priority_4_ev_90_to_100_uses_residual_solar_with_tier_3(
        self, mock_vehicle, mock_dispatch_at
    ):
        """
        When Solar is 3600W and EV is at 92% SoC on a 33°C forecast day:
        Tier 3 Pre-Cool claims 2000W, leaving 1600W residual solar.
        Tier 4 EV uses the residual 1600W ((1600 - 150) // 240 = 6A).
        Both Tier 3 Pre-Cool AND Tier 4 EV charging run simultaneously!
        """
        self.base_config["ROLLOUT_STAGE"] = 4
        self.base_config["DRY_RUN_CLIMATE"] = False
        mock_dispatch_at.return_value = True

        tz = ZoneInfo("Australia/Sydney")
        midday = dt(2026, 10, 19, 12, 0, 0, tzinfo=tz)

        mock_twc = {
            "vehicle_connected": True,
            "contactor_closed": False,
            "vehicle_current_a": 0.0,
            "grid_v": 240.0,
        }
        mock_vehicle.return_value = {
            "latitude": -33.8688,
            "longitude": 151.2093,
            "charging_state": "Stopped",
            "battery_level": 92,
            "charge_limit_soc": 100,
        }
        at_state = make_mock_airtouch_state(ac_power="OFF", living_temp=22.5)
        wx_state = {
            "outdoor_temp_c": 27.0,
            "cloud_cover_pct": 0.0,
            "forecast_max_c": 33.0,
            "forecast_min_c": 18.0,
        }

        solar_home_manager.run_solar_loop(
            override_time=midday,
            mock_power=-3600.0,
            mock_twc=mock_twc,
            mock_airtouch=at_state,
            mock_weather=wx_state,
        )

        mock_dispatch_at.assert_called_once()
        cache = solar_home_manager.read_cache()
        self.assertEqual(cache["hvac_active_tier"], "TIER3_PRECOOL")
        self.assertTrue(cache["charging"])
        self.assertEqual(cache["amps"], 6)

    @patch("solar_home_manager.dispatch_airtouch_commands")
    @patch("solar_home_manager.get_tesla_vehicle_data")
    def test_manual_tablet_override_locks_out_hvac_but_continues_ev_charging(
        self, mock_vehicle, mock_dispatch_at
    ):
        """
        If the user manually changes the AirTouch state on the wall tablet,
        HVAC automation locks out for 120 minutes while EV charging continues normally.
        """
        self.base_config["ROLLOUT_STAGE"] = 4
        self.base_config["DRY_RUN_CLIMATE"] = False

        tz = ZoneInfo("Australia/Sydney")
        midday = dt(2026, 10, 19, 12, 0, 0, tzinfo=tz)

        # Seed cache expecting AC to be OFF, but user manually turned AC ON to 20.0°C on tablet
        solar_home_manager.write_cache(
            {
                "charging": False,
                "amps": 5,
                "airtouch_last_dispatched": {
                    "ac": {"power": "OFF", "mode": "COOL", "setpoint_c": 22.5}
                },
                "last_sunrise_reset_date": "2026-10-19",
                "solar_history": [],
            }
        )

        mock_twc = {
            "vehicle_connected": True,
            "contactor_closed": False,
            "vehicle_current_a": 0.0,
            "grid_v": 240.0,
        }
        mock_vehicle.return_value = {
            "latitude": -33.8688,
            "longitude": 151.2093,
            "charging_state": "Stopped",
            "battery_level": 75,
            "charge_limit_soc": 90,
        }
        at_state = make_mock_airtouch_state(ac_power="ON", ac_mode="COOL", ac_setpoint=20.0, living_temp=22.0)
        wx_state = {
            "outdoor_temp_c": 24.0,
            "cloud_cover_pct": 10.0,
            "forecast_max_c": 25.0,
            "forecast_min_c": 15.0,
        }

        solar_home_manager.run_solar_loop(
            override_time=midday,
            mock_power=-2600.0,
            mock_twc=mock_twc,
            mock_airtouch=at_state,
            mock_weather=wx_state,
        )

        # Must NOT send any command to overwrite the human's manual AirTouch setting!
        mock_dispatch_at.assert_not_called()
        cache = solar_home_manager.read_cache()
        self.assertEqual(cache["hvac_active_tier"], "MANUAL_OVERRIDE")
        self.assertIsNotNone(cache["airtouch_manual_override_until"])
        # EV charging still operates normally on remaining net export (2600 - 150 = 2450 // 240 = 10A)
        self.assertTrue(cache["charging"])
        self.assertEqual(cache["amps"], 10)

    def test_differential_night_purge_uses_fan_mode(self):
        """When indoor is warm (24.8°C) and outdoor is cool (19.5°C), Tier 1 uses low-power FAN mode."""
        tz = ZoneInfo("Australia/Sydney")
        evening = dt(2026, 10, 19, 20, 30, 0, tzinfo=tz)
        at_state = make_mock_airtouch_state(ac_power="OFF", living_temp=24.8)
        wx_state = {
            "outdoor_temp_c": 19.5,
            "cloud_cover_pct": 0.0,
            "forecast_max_c": 28.0,
            "forecast_min_c": 16.0,
        }
        cache = solar_home_manager.read_cache()

        needed, plan = solar_home_manager.evaluate_tier1_immediate_comfort(
            evening, at_state, wx_state, cache, self.base_config
        )
        self.assertTrue(needed)
        self.assertEqual(plan["tier"], "TIER1_NIGHT_PURGE")
        self.assertEqual(plan["power"], "ON")
        self.assertEqual(plan["mode"], "FAN")
        self.assertEqual(plan["fan_speed"], "HIGH")
        self.assertFalse(plan["draws_compressor_power"])

    def test_compressor_short_cycle_guard_blocks_premature_shutoff(self):
        """AC cannot be turned OFF if it was turned ON less than HVAC_MIN_ON_MINUTES (15m) ago."""
        tz = ZoneInfo("Australia/Sydney")
        now = dt(2026, 10, 19, 12, 10, 0, tzinfo=tz)
        turned_on_at = dt(2026, 10, 19, 12, 0, 0, tzinfo=tz)  # Only 10 mins ago

        cache = solar_home_manager.read_cache()
        cache["airtouch_last_power_change_time"] = turned_on_at.isoformat()

        allowed, reason = solar_home_manager.can_change_ac_power(
            target_on=False, current_on=True, cache=cache, now=now, config=self.base_config
        )
        self.assertFalse(allowed)
        self.assertIn("Compressor min run-time guard", reason)

    def test_tier_3_solar_preheat_on_cold_forecast(self):
        """When forecast max <= 16.0°C and solar >= 2000W, Tier 3 activates Solar Pre-Heat."""
        tz = ZoneInfo("Australia/Sydney")
        midday = dt(2026, 7, 15, 12, 0, 0, tzinfo=tz)
        sunset = dt(2026, 7, 15, 17, 10, 0, tzinfo=tz)
        at_state = make_mock_airtouch_state(ac_power="OFF", living_temp=19.8)
        wx_state = {
            "outdoor_temp_c": 13.0,
            "cloud_cover_pct": 20.0,
            "forecast_max_c": 15.0,
            "forecast_min_c": 7.0,
        }
        cache = solar_home_manager.read_cache()

        active, plan = solar_home_manager.evaluate_tier3_solar_banking(
            midday, sunset, 2500.0, at_state, wx_state, cache, self.base_config
        )
        self.assertTrue(active)
        self.assertEqual(plan["tier"], "TIER3_PREHEAT")
        self.assertEqual(plan["power"], "ON")
        self.assertEqual(plan["mode"], "HEAT")
        self.assertEqual(plan["setpoint_c"], 22.0)

    def test_day_vs_night_comfort_bands(self):
        """Daytime uses 18-24°C band; Nighttime uses 16-23°C band."""
        tz = ZoneInfo("Australia/Sydney")
        day_time = dt(2026, 10, 21, 14, 0, 0, tzinfo=tz)
        night_time = dt(2026, 10, 21, 23, 0, 0, tzinfo=tz)
        cache = solar_home_manager.read_cache()

        # At 23.4°C: Daytime (18-24°C) runs graduated FAN circulation (HIGH),
        # Nighttime (16-23°C) still triggers compressor COOL to 21.0°C.
        at_23_4 = make_mock_airtouch_state(ac_power="OFF", living_temp=23.4)
        day_need, day_plan = solar_home_manager.evaluate_tier1_immediate_comfort(
            day_time, at_23_4, None, cache, self.base_config
        )
        self.assertTrue(day_need)
        self.assertEqual(day_plan["tier"], "TIER1_FAN_CIRCULATE")
        self.assertEqual(day_plan["mode"], "FAN")
        self.assertEqual(day_plan["fan_speed"], "HIGH")
        self.assertFalse(day_plan["draws_compressor_power"])

        night_need, night_plan = solar_home_manager.evaluate_tier1_immediate_comfort(
            night_time, at_23_4, None, cache, self.base_config
        )
        self.assertTrue(night_need)
        self.assertEqual(night_plan["tier"], "TIER1_COMFORT_COOL")
        self.assertEqual(night_plan["setpoint_c"], 21.0)

        # At 17.0°C: Nighttime (16-23°C) should be IDLE, Daytime (18-24°C) should trigger HEAT to 20.0°C!
        at_17_0 = make_mock_airtouch_state(ac_power="OFF", living_temp=17.0)
        night_heat_need, _ = solar_home_manager.evaluate_tier1_immediate_comfort(
            night_time, at_17_0, None, cache, self.base_config
        )
        self.assertFalse(night_heat_need)

        day_heat_need, day_heat_plan = solar_home_manager.evaluate_tier1_immediate_comfort(
            day_time, at_17_0, None, cache, self.base_config
        )
        self.assertTrue(day_heat_need)
        self.assertEqual(day_heat_plan["tier"], "TIER1_COMFORT_HEAT")
        self.assertEqual(day_heat_plan["setpoint_c"], 20.0)

    def test_heat_suppressed_ahead_of_hot_day(self):
        """HK at 17.7C on a 28C-forecast morning stays IDLE; the sun warms the house for free."""
        tz = ZoneInfo("Australia/Sydney")
        morning = dt(2026, 11, 10, 8, 15, 0, tzinfo=tz)
        cache = solar_home_manager.read_cache()
        at_cold = make_mock_airtouch_state(ac_power="OFF", living_temp=17.7)
        wx_hot = {
            "outdoor_temp_c": 18.0,
            "cloud_cover_pct": 10.0,
            "forecast_max_c": 28.0,
            "forecast_min_c": 16.0,
        }
        needed, plan = solar_home_manager.evaluate_tier1_immediate_comfort(
            morning, at_cold, wx_hot, cache, self.base_config
        )
        self.assertFalse(needed)
        self.assertEqual(plan["tier"], "IDLE")
        self.assertIn("suppressed", plan["reason"])

    def test_heat_still_fires_on_mild_day(self):
        """Same 17.7C room with a 20C forecast still heats; suppression is forecast-gated only."""
        tz = ZoneInfo("Australia/Sydney")
        morning = dt(2026, 11, 10, 8, 15, 0, tzinfo=tz)
        cache = solar_home_manager.read_cache()
        at_cold = make_mock_airtouch_state(ac_power="OFF", living_temp=17.7)
        wx_mild = {
            "outdoor_temp_c": 15.0,
            "cloud_cover_pct": 40.0,
            "forecast_max_c": 20.0,
            "forecast_min_c": 12.0,
        }
        needed, plan = solar_home_manager.evaluate_tier1_immediate_comfort(
            morning, at_cold, wx_mild, cache, self.base_config
        )
        self.assertTrue(needed)
        self.assertEqual(plan["tier"], "TIER1_COMFORT_HEAT")

    def test_fan_circulation_graduated_speeds(self):
        """Daytime heat band drives fan speed without the compressor: 21C LOW, 22C MEDIUM, 23C HIGH."""
        tz = ZoneInfo("Australia/Sydney")
        day_time = dt(2026, 10, 21, 14, 0, 0, tzinfo=tz)
        for temp_c, expected_speed in [(21.0, "LOW"), (22.0, "MEDIUM"), (23.0, "HIGH")]:
            cache = solar_home_manager.read_cache()
            at_state = make_mock_airtouch_state(ac_power="OFF", living_temp=temp_c)
            needed, plan = solar_home_manager.evaluate_tier1_immediate_comfort(
                day_time, at_state, None, cache, self.base_config
            )
            self.assertTrue(needed, f"temp={temp_c}")
            self.assertEqual(plan["tier"], "TIER1_FAN_CIRCULATE")
            self.assertEqual(plan["power"], "ON")
            self.assertEqual(plan["mode"], "FAN")
            self.assertEqual(plan["fan_speed"], expected_speed)
            self.assertFalse(plan["draws_compressor_power"])

    def test_fan_circulation_hysteresis_holds_until_below_differential(self):
        """Once circulating, the fan stays on until temp drops 0.5C below the entry point."""
        tz = ZoneInfo("Australia/Sydney")
        day_time = dt(2026, 10, 21, 14, 0, 0, tzinfo=tz)

        cache = solar_home_manager.read_cache()
        cache["hvac_active_tier"] = "TIER1_FAN_CIRCULATE"
        at_warm = make_mock_airtouch_state(ac_power="ON", ac_mode="FAN", living_temp=20.8)
        needed, plan = solar_home_manager.evaluate_tier1_immediate_comfort(
            day_time, at_warm, None, cache, self.base_config
        )
        self.assertTrue(needed)
        self.assertEqual(plan["tier"], "TIER1_FAN_CIRCULATE")
        self.assertEqual(plan["fan_speed"], "LOW")

        cache2 = solar_home_manager.read_cache()
        cache2["hvac_active_tier"] = "TIER1_FAN_CIRCULATE"
        at_cool = make_mock_airtouch_state(ac_power="ON", ac_mode="FAN", living_temp=20.4)
        needed2, _ = solar_home_manager.evaluate_tier1_immediate_comfort(
            day_time, at_cool, None, cache2, self.base_config
        )
        self.assertFalse(needed2)

    def test_fan_circulation_opens_scheduled_zones_when_all_closed(self):
        """Fan with every damper closed would dead-head: scheduled zones are powered ON (mode/setpoint untouched)."""
        tz = ZoneInfo("Australia/Sydney")
        day_time = dt(2026, 10, 21, 14, 0, 0, tzinfo=tz)
        cache = solar_home_manager.read_cache()
        at_state = make_mock_airtouch_state(ac_power="OFF", living_temp=22.5)
        for z in at_state["zones"].values():
            z["power"] = "OFF"
        needed, plan = solar_home_manager.evaluate_tier1_immediate_comfort(
            day_time, at_state, None, cache, self.base_config
        )
        self.assertTrue(needed)
        self.assertEqual(plan["tier"], "TIER1_FAN_CIRCULATE")
        self.assertTrue(plan["zone_commands"], "expected airflow-path commands")
        self.assertTrue(all(c.get("power") == "ON" for c in plan["zone_commands"]))
        self.assertTrue(
            all("control_mode" not in c and "setpoint_c" not in c for c in plan["zone_commands"]),
            "zone temperatures must not be touched by fan circulation",
        )

    def test_fan_circulation_leaves_open_zones_alone(self):
        """When dampers are already open, fan circulation sends no zone commands at all."""
        tz = ZoneInfo("Australia/Sydney")
        day_time = dt(2026, 10, 21, 14, 0, 0, tzinfo=tz)
        cache = solar_home_manager.read_cache()
        at_state = make_mock_airtouch_state(ac_power="OFF", living_temp=22.5)
        needed, plan = solar_home_manager.evaluate_tier1_immediate_comfort(
            day_time, at_state, None, cache, self.base_config
        )
        self.assertTrue(needed)
        self.assertEqual(plan["zone_commands"], [])

    def test_compressor_guard_blocks_fan_to_cool_restart(self):
        """FAN->COOL within the off-time window is held; the power-toggle guard alone would allow it."""
        tz = ZoneInfo("Australia/Sydney")
        now = dt(2026, 10, 19, 12, 4, 0, tzinfo=tz)

        cache = solar_home_manager.read_cache()
        cache["hvac_compressor_running"] = False
        cache["airtouch_last_compressor_change_time"] = dt(
            2026, 10, 19, 12, 1, 0, tzinfo=tz
        ).isoformat()  # stopped 3 mins ago (< 6 min off-time)
        allowed, reason = solar_home_manager.can_change_compressor(
            True, False, cache, now, self.base_config
        )
        self.assertFalse(allowed)
        self.assertIn("min off-time", reason)

        # Stopping a running compressor too soon is also held.
        cache2 = solar_home_manager.read_cache()
        cache2["hvac_compressor_running"] = True
        cache2["airtouch_last_compressor_change_time"] = dt(
            2026, 10, 19, 12, 0, 0, tzinfo=tz
        ).isoformat()  # started 4 mins ago (< 15 min run-time)
        allowed2, reason2 = solar_home_manager.can_change_compressor(
            False, True, cache2, now, self.base_config
        )
        self.assertFalse(allowed2)
        self.assertIn("min run-time", reason2)

        # No timestamp on record (fresh cache) -> allowed.
        cache3 = solar_home_manager.read_cache()
        allowed3, _ = solar_home_manager.can_change_compressor(
            True, False, cache3, now, self.base_config
        )
        self.assertTrue(allowed3)

    @patch("solar_home_manager.get_tesla_vehicle_data")
    def test_tier3_supersedes_fan_only_circulation(self, mock_vehicle):
        """
        On a hot forecast day with strong solar and SoC >= 90%, Tier 3 pre-cool
        replaces fan-only circulation (compressor need beats airflow comfort).
        """
        self.base_config["ROLLOUT_STAGE"] = 3
        tz = ZoneInfo("Australia/Sydney")
        midday = dt(2026, 10, 19, 12, 0, 0, tzinfo=tz)

        mock_twc = {
            "vehicle_connected": True,
            "contactor_closed": False,
            "vehicle_current_a": 0.0,
            "grid_v": 240.0,
        }
        mock_vehicle.return_value = {
            "latitude": -33.8688,
            "longitude": 151.2093,
            "charging_state": "Stopped",
            "battery_level": 92,
            "charge_limit_soc": 100,
        }
        # 22.5C indoor would only merit fan circulation on its own...
        at_state = make_mock_airtouch_state(ac_power="OFF", living_temp=22.5)
        # ...but a 33C forecast with 3kW export triggers Tier 3 pre-cooling.
        wx_state = {
            "outdoor_temp_c": 27.0,
            "cloud_cover_pct": 0.0,
            "forecast_max_c": 33.0,
            "forecast_min_c": 18.0,
        }

        solar_home_manager.run_solar_loop(
            override_time=midday,
            mock_power=-3000.0,
            mock_twc=mock_twc,
            mock_airtouch=at_state,
            mock_weather=wx_state,
        )

        cache = solar_home_manager.read_cache()
        self.assertEqual(cache["shadow_hvac_plan"]["tier"], "TIER3_PRECOOL")

    def test_zone_level_manual_turn_on_triggers_override_not_shutoff(self):
        """
        If someone is home sick or working on a holiday and turns their zone ON (or changes its setpoint),
        the orchestrator detects the zone-level manual change and does NOT turn their room off 5 minutes later.
        """
        tz = ZoneInfo("Australia/Sydney")
        midday = dt(2026, 10, 21, 11, 0, 0, tzinfo=tz)

        # Automation previously had Zone 0 (Bedroom 1) OFF during the day
        cache = solar_home_manager.read_cache()
        cache["airtouch_last_dispatched"] = {
            "ac": {"power": "ON", "mode": "COOL", "setpoint_c": 22.0},
            "zones": {
                "0": {"power": "OFF", "setpoint_c": 24.0},
                "1": {"power": "ON", "setpoint_c": 22.0},
            },
        }

        # Human turned Zone 0 (Bedroom 1) ON and set to 21.5°C on tablet/app
        live_at = make_mock_airtouch_state(ac_power="ON", ac_mode="COOL", ac_setpoint=22.0, living_temp=22.5)
        live_at["zones"]["0"]["power"] = "ON"
        live_at["zones"]["0"]["setpoint_c"] = 21.5

        override_active, reason = solar_home_manager.detect_manual_airtouch_override(
            live_at, cache, midday, self.base_config
        )
        self.assertTrue(override_active)
        self.assertIn("Bedroom 1 power OFF->ON", reason)
        self.assertIsNotNone(cache["airtouch_manual_override_until"])

    def test_fan_suppressed_on_hot_sunny_day_instead_of_blowing_warm_air(self):
        """29°C-forecast sunny day: 23.2°C indoor must NOT run FAN HIGH (duct air would heat)."""
        tz = ZoneInfo("Australia/Sydney")
        midday = dt(2026, 11, 10, 13, 0, 0, tzinfo=tz)
        cache = solar_home_manager.read_cache()
        # 23.2°C indoor would previously run FAN HIGH; outdoor 28°C means the
        # ceiling-duct supply air is hotter than the rooms.
        at_state = make_mock_airtouch_state(ac_power="OFF", living_temp=23.2)
        wx_state = {
            "outdoor_temp_c": 28.0,
            "cloud_cover_pct": 5.0,
            "forecast_max_c": 29.0,
            "forecast_min_c": 17.0,
        }
        needed, plan = solar_home_manager.evaluate_tier1_immediate_comfort(
            midday, at_state, wx_state, cache, self.base_config
        )
        # Tier 1 cooling trigger pulls back 24 -> 23°C on warm/sunny days, so
        # 23.2°C now fires the compressor (uses peak solar) instead of the fan.
        self.assertTrue(needed)
        self.assertEqual(plan["tier"], "TIER1_COMFORT_COOL")
        self.assertEqual(plan["mode"], "COOL")

    def test_fan_suppressed_idle_when_below_pulled_back_trigger(self):
        """22.5°C indoor on a 29°C sunny day: fan suppressed, AC stays OFF (Tier 3 may bank)."""
        tz = ZoneInfo("Australia/Sydney")
        midday = dt(2026, 11, 10, 13, 0, 0, tzinfo=tz)
        cache = solar_home_manager.read_cache()
        at_state = make_mock_airtouch_state(ac_power="OFF", living_temp=22.5)
        wx_state = {
            "outdoor_temp_c": 28.0,
            "cloud_cover_pct": 5.0,
            "forecast_max_c": 29.0,
            "forecast_min_c": 17.0,
        }
        needed, plan = solar_home_manager.evaluate_tier1_immediate_comfort(
            midday, at_state, wx_state, cache, self.base_config
        )
        self.assertFalse(needed)
        self.assertEqual(plan["tier"], "IDLE")
        self.assertIn("Fan suppressed", plan["reason"])

    def test_fan_still_runs_on_mild_day(self):
        """22.5°C indoor with 20°C outdoor: FAN MEDIUM still runs (no duct-heat risk)."""
        tz = ZoneInfo("Australia/Sydney")
        midday = dt(2026, 10, 21, 14, 0, 0, tzinfo=tz)
        cache = solar_home_manager.read_cache()
        at_state = make_mock_airtouch_state(ac_power="OFF", living_temp=22.5)
        wx_state = {
            "outdoor_temp_c": 20.0,
            "cloud_cover_pct": 40.0,
            "forecast_max_c": 22.0,
            "forecast_min_c": 14.0,
        }
        needed, plan = solar_home_manager.evaluate_tier1_immediate_comfort(
            midday, at_state, wx_state, cache, self.base_config
        )
        self.assertTrue(needed)
        self.assertEqual(plan["tier"], "TIER1_FAN_CIRCULATE")
        self.assertEqual(plan["fan_speed"], "MEDIUM")

    def test_warm_sunny_tier3_precool_fires_below_30_forecast(self):
        """29°C forecast + 27°C outdoor + 3kW solar: Tier 3 banks to 22°C (deep 30°C gate missed)."""
        tz = ZoneInfo("Australia/Sydney")
        midday = dt(2026, 11, 10, 13, 0, 0, tzinfo=tz)
        sunset = dt(2026, 11, 10, 19, 40, 0, tzinfo=tz)
        at_state = make_mock_airtouch_state(ac_power="OFF", living_temp=22.8)
        wx_state = {
            "outdoor_temp_c": 27.0,
            "cloud_cover_pct": 5.0,
            "forecast_max_c": 29.0,
            "forecast_min_c": 17.0,
        }
        cache = solar_home_manager.read_cache()
        active, plan = solar_home_manager.evaluate_tier3_solar_banking(
            midday, sunset, 3000.0, at_state, wx_state, cache, self.base_config
        )
        self.assertTrue(active)
        self.assertEqual(plan["tier"], "TIER3_PRECOOL")
        self.assertEqual(plan["setpoint_c"], 22.0)
        self.assertIn("warm/sunny", plan["reason"])

    def test_warm_sunny_tier3_stays_off_without_solar(self):
        """Same 29°C day but only 800W export: no pre-cool (nothing free to bank)."""
        tz = ZoneInfo("Australia/Sydney")
        midday = dt(2026, 11, 10, 13, 0, 0, tzinfo=tz)
        sunset = dt(2026, 11, 10, 19, 40, 0, tzinfo=tz)
        at_state = make_mock_airtouch_state(ac_power="OFF", living_temp=23.5)
        wx_state = {
            "outdoor_temp_c": 27.0,
            "cloud_cover_pct": 5.0,
            "forecast_max_c": 29.0,
            "forecast_min_c": 17.0,
        }
        cache = solar_home_manager.read_cache()
        active, plan = solar_home_manager.evaluate_tier3_solar_banking(
            midday, sunset, 800.0, at_state, wx_state, cache, self.base_config
        )
        self.assertFalse(active)
        self.assertEqual(plan["tier"], "IDLE")

    def test_current_outdoor_heat_escalates_to_deep_precool(self):
        """31°C outside with only a 29°C forecast still banks deep to 21°C (current heat counts)."""
        tz = ZoneInfo("Australia/Sydney")
        midday = dt(2026, 11, 10, 14, 0, 0, tzinfo=tz)
        sunset = dt(2026, 11, 10, 19, 40, 0, tzinfo=tz)
        # 22.5°C indoor would NOT start the warm/sunny path (entry 22.6°C)...
        at_state = make_mock_airtouch_state(ac_power="OFF", living_temp=22.5)
        wx_state = {
            "outdoor_temp_c": 31.0,
            "cloud_cover_pct": 5.0,
            "forecast_max_c": 29.0,
            "forecast_min_c": 18.0,
        }
        cache = solar_home_manager.read_cache()
        active, plan = solar_home_manager.evaluate_tier3_solar_banking(
            midday, sunset, 4500.0, at_state, wx_state, cache, self.base_config
        )
        self.assertTrue(active)
        self.assertEqual(plan["tier"], "TIER3_PRECOOL")
        self.assertEqual(plan["setpoint_c"], 21.0)
        self.assertIn("currently hot", plan["reason"])

    def test_active_precool_holds_below_target_while_hot(self):
        """Running pre-cool at 21.1°C indoor (33°C forecast) keeps going — banks to 20.5, not 21.2."""
        tz = ZoneInfo("Australia/Sydney")
        midday = dt(2026, 11, 10, 14, 0, 0, tzinfo=tz)
        sunset = dt(2026, 11, 10, 19, 40, 0, tzinfo=tz)
        at_state = make_mock_airtouch_state(ac_power="ON", ac_mode="COOL", living_temp=21.1)
        wx_state = {
            "outdoor_temp_c": 31.0,
            "cloud_cover_pct": 5.0,
            "forecast_max_c": 33.0,
            "forecast_min_c": 18.0,
        }
        cache = solar_home_manager.read_cache()
        cache["hvac_active_tier"] = "TIER3_PRECOOL"
        active, plan = solar_home_manager.evaluate_tier3_solar_banking(
            midday, sunset, 4500.0, at_state, wx_state, cache, self.base_config
        )
        self.assertTrue(active)
        self.assertEqual(plan["tier"], "TIER3_PRECOOL")

    def test_active_precool_releases_once_banked_cold(self):
        """Running pre-cool at 20.4°C indoor finally releases (below 21.0 - 0.5 hold floor)."""
        tz = ZoneInfo("Australia/Sydney")
        midday = dt(2026, 11, 10, 14, 0, 0, tzinfo=tz)
        sunset = dt(2026, 11, 10, 19, 40, 0, tzinfo=tz)
        at_state = make_mock_airtouch_state(ac_power="ON", ac_mode="COOL", living_temp=20.4)
        wx_state = {
            "outdoor_temp_c": 31.0,
            "cloud_cover_pct": 5.0,
            "forecast_max_c": 33.0,
            "forecast_min_c": 18.0,
        }
        cache = solar_home_manager.read_cache()
        cache["hvac_active_tier"] = "TIER3_PRECOOL"
        active, plan = solar_home_manager.evaluate_tier3_solar_banking(
            midday, sunset, 4500.0, at_state, wx_state, cache, self.base_config
        )
        self.assertFalse(active)
        self.assertEqual(plan["tier"], "IDLE")


if __name__ == "__main__":
    unittest.main()
