import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import verify_services


class TestPresenceNetHelpers(unittest.TestCase):
    def test_normalize_mac_formats(self):
        self.assertEqual(verify_services.normalize_mac("AA:BB:CC:DD:EE:FF"), "aa:bb:cc:dd:ee:ff")
        self.assertEqual(verify_services.normalize_mac("aa-bb-cc-dd-ee-ff"), "aa:bb:cc:dd:ee:ff")
        self.assertEqual(verify_services.normalize_mac("aabb.ccdd.eeff"), "aa:bb:cc:dd:ee:ff")
        # macOS `arp -a` prints unpadded groups
        self.assertEqual(verify_services.normalize_mac("0:11:32:8e:7f:5f"), "00:11:32:8e:7f:5f")
        self.assertEqual(verify_services.normalize_mac("a4:7e:fa:35:3b:2"), "a4:7e:fa:35:3b:02")
        self.assertIsNone(verify_services.normalize_mac("not-a-mac"))
        self.assertIsNone(verify_services.normalize_mac(""))
        self.assertIsNone(verify_services.normalize_mac(None))

    def test_parse_arp_a_macos_and_linux(self):
        macos = "? (192.168.1.50) at aa:bb:cc:dd:ee:ff on en0 ifscope [ethernet]"
        linux = "? (192.168.1.51) at AA:BB:CC:DD:EE:00 [ether] on eth0"
        unpadded = "? (192.168.1.10) at 0:11:32:8e:7f:5f on en0 ifscope [ethernet]"
        incomplete = "? (192.168.1.52) at (incomplete) on en0"
        entries = verify_services.parse_arp_a_output(
            "\n".join([macos, linux, unpadded, incomplete])
        )
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0], {"ip": "192.168.1.50", "mac": "aa:bb:cc:dd:ee:ff"})
        self.assertEqual(entries[1], {"ip": "192.168.1.51", "mac": "aa:bb:cc:dd:ee:00"})
        self.assertEqual(entries[2], {"ip": "192.168.1.10", "mac": "00:11:32:8e:7f:5f"})

    def test_parse_proc_net_arp(self):
        text = (
            "IP address       HW type     Flags       HW address            Mask     Device\n"
            "192.168.1.50     0x1         0x2         aa:bb:cc:dd:ee:ff     *        eth0\n"
            "192.168.1.51     0x1         0x0         00:00:00:00:00:00     *        eth0\n"
        )
        entries = verify_services.parse_proc_net_arp(text)
        self.assertEqual(entries, [{"ip": "192.168.1.50", "mac": "aa:bb:cc:dd:ee:ff"}])

    def test_parse_ip_neigh(self):
        text = (
            "192.168.1.50 dev eth0 lladdr aa:bb:cc:dd:ee:ff REACHABLE\n"
            "192.168.1.51 dev eth0 lladdr 0:11:32:8e:7f:5f STALE\n"
            "192.168.1.52 dev eth0  FAILED\n"
        )
        entries = verify_services.parse_ip_neigh(text)
        self.assertEqual(
            entries,
            [
                {"ip": "192.168.1.50", "mac": "aa:bb:cc:dd:ee:ff"},
                {"ip": "192.168.1.51", "mac": "00:11:32:8e:7f:5f"},
            ],
        )

    def test_extract_form_fields(self):
        html = (
            '<html><body><form>'
            '<input type="text" name="username"/>'
            '<input type="password" name="password"/>'
            '<input type="hidden" name="operation" value="login"/>'
            '<input type="password" name="password"/>'
            "</form></body></html>"
        )
        self.assertEqual(
            verify_services.extract_form_fields(html),
            ["username", "password", "operation"],
        )
        self.assertEqual(verify_services.extract_form_fields("<html></html>"), [])

    def test_sweep_subnet_mocked(self):
        import socket as _socket

        with unittest.mock.patch.object(
            verify_services, "ping_host_once",
            side_effect=lambda ip, timeout_s=1.0: (
                {"ok": ip.endswith(".50"), "ms": 1.0 if ip.endswith(".50") else None}
            ),
        ), unittest.mock.patch.object(
            verify_services, "read_local_arp_table",
            return_value={"method": "arp -a",
                          "entries": [{"ip": "192.168.1.50", "mac": "aa:bb:cc:dd:ee:ff"}]},
        ), unittest.mock.patch.object(
            _socket, "gethostbyaddr", return_value=("Test-Laptop.local", [], ["192.168.1.50"])
        ):
            sw = verify_services.sweep_subnet(
                base="192.168.1", start=50, end=51, timeout_s=0.1,
                workers=4, resolve_names=True,
            )
        self.assertEqual(sw["status"], "OK")
        self.assertEqual(sw["subnet"], "192.168.1.0/24")
        self.assertEqual(sw["alive_count"], 1)
        self.assertEqual(
            sw["rows"],
            [{"ip": "192.168.1.50", "mac": "aa:bb:cc:dd:ee:ff",
              "hostname": "Test-Laptop.local"}],
        )

    def test_verdict_conservative(self):
        now = "2026-10-26T10:00:00+11:00"
        # Found now -> weak present even without history
        self.assertEqual(
            verify_services.verdict_for_device(True, None, 45, now), "PRESENT_WEAK"
        )
        # Never seen -> unknown (fail safe to schedule/ITC logic)
        self.assertEqual(
            verify_services.verdict_for_device(False, None, 45, now), "UNKNOWN"
        )
        # Missing for 60 mins with 45-min threshold -> absent hint
        self.assertEqual(
            verify_services.verdict_for_device(
                False, "2026-10-26T09:00:00+11:00", 45, now
            ),
            "ABSENT_LONG",
        )
        # Missing for 10 mins -> still unknown
        self.assertEqual(
            verify_services.verdict_for_device(
                False, "2026-10-26T09:50:00+11:00", 45, now
            ),
            "UNKNOWN",
        )

    def test_summarize_log_counts(self):
        import json
        import tempfile

        snaps = [
            {"timestamp": "2026-10-26T10:00:00+11:00",
             "devices": [{"label": "Office-Laptop", "found_now": True}]},
            {"timestamp": "2026-10-26T10:05:00+11:00",
             "devices": [{"label": "Office-Laptop", "found_now": False}]},
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            for s in snaps:
                f.write(json.dumps(s) + "\n")
            path = f.name
        try:
            summary = verify_services.summarize_presence_log(path)
            dev = summary["devices"]["Office-Laptop"]
            self.assertEqual(dev["snapshots"], 2)
            self.assertEqual(dev["present_weak"], 1)
            self.assertEqual(dev["present_fraction"], 0.5)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
