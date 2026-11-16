# Licensed under the Apache-2.0 license

import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

try:
    import tkinter as tk
except ImportError:
    tk = None

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from usb_stress_config import Settings
from usb_stress_gui import Application
from usb_stress_discovery import DiscoveredDevice


class GuiTests(unittest.TestCase):
    def setUp(self):
        if tk is None:
            self.skipTest("Tkinter not installed")
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"No graphical display: {error}")
        self.root.withdraw()
        self.failure_dialog = patch("tkinter.messagebox.showerror").start()
        self.addCleanup(patch.stopall)
        self.app = Application(self.root)

    def tearDown(self):
        if hasattr(self, "root"):
            self.root.destroy()

    def test_preset_and_generic_behavior_choose_only_compatible_cases(self):
        self.app.preset()
        self.assertEqual(self.app.collect().profile, "cst1")
        self.app.apply(Settings(vendor_id=0x1234, product_id=7, behavior="loopback",
                                allow_data_tests=True, tests=["loopback"]))
        self.app.select_tests()
        settings = self.app.collect()
        self.assertIn("loopback", settings.tests)
        self.assertNotIn("bulk", settings.tests)
        self.assertNotIn("port_cycle", settings.tests)

    def test_switch_to_cst1_applies_protocol_values_without_replacing_device_or_workload(self):
        self.app.apply(Settings(vendor_id=0x1234, product_id=7, bus=2, address=29,
                                interface=3, out_endpoint=2, in_endpoint=0x82, iterations=9))
        self.app.variables["profile"].set("cst1")
        self.app.profile_changed()
        settings = self.app.collect()
        self.assertEqual((settings.alternate, settings.inactive_alternate), (1, 0))
        self.assertEqual((settings.configuration, settings.interface,
                          settings.out_endpoint, settings.in_endpoint), (1, 0, 1, 0x81))
        self.assertEqual((settings.vendor_id, settings.product_id, settings.bus,
                          settings.address, settings.iterations), (0x1234, 7, 2, 29, 9))
        self.assertEqual(settings.tests, ["descriptors"])

    def test_run_uses_snapshot_and_disables_editing_without_usb_access(self):
        self.app.preset()
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            self.app.variables["report"].set(str(report))
            with patch.object(self.app.runner, "start") as start:
                self.app.start()
            self.assertEqual(start.call_args.args[0].vendor_id, 0x424)
            self.assertEqual(start.call_args.args[1], report.resolve())
            self.assertEqual(str(self.app.run_button["state"]), "disabled")

    def test_gui_rejects_success_exit_without_passing_report(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            self.app.report = report
            report.write_text(json.dumps({"schema_version": 1, "overall": "FAIL",
                                          "tests": [{"name": "descriptors", "status": "FAIL"}]}))
            self.app.completed(0)
            self.assertIn("Run failed", self.app.status.get())

    def test_discovered_selection_fills_ids_and_disambiguates_identical_devices(self):
        self.app.discovered = [
            DiscoveredDevice(0x1234, 7, 1, address, "Same product", "") for address in (3, 5)
        ]
        self.app.device_choice.configure(values=[device.label for device in self.app.discovered])
        self.app.device_choice.current(1)
        self.app.select_device()
        settings = self.app.collect()
        self.assertEqual((settings.vendor_id, settings.product_id, settings.bus, settings.address),
                         (0x1234, 7, 1, 5))
        self.app.preset()
        self.assertEqual(self.app.collect().address, 5)
        self.assertEqual(self.app.collect().vendor_id, 0x1234)

    def test_discovery_results_and_errors_are_processed_on_gui_thread(self):
        device = DiscoveredDevice(0x1234, 7, 1, 3, "Device", "")
        self.app.scanning = True
        self.app.discovery_events.put(("devices", [device]))
        self.app.poll()
        self.assertFalse(self.app.scanning)
        self.assertEqual(self.app.device_choice.current(), -1)
        self.assertEqual(len(self.app.device_choice["values"]), 1)
        self.app.discovery_events.put(("error", "backend unavailable"))
        self.app.poll()
        self.assertIn("backend unavailable", self.app.status.get())

    def test_live_status_updates_before_process_exit_and_elapsed_time_advances(self):
        from usb_stress import PROGRESS_PREFIX
        self.app.results.insert("", "end", iid="soak", text="soak", values=("NOT_RUN", ""))
        with patch("usb_stress_gui.time.monotonic", return_value=10):
            self.app.runner.events.put(("log", PROGRESS_PREFIX + json.dumps(
                {"name": "soak", "status": "RUNNING"}) + "\n"))
            self.app.poll()
        self.assertEqual(self.app.results.item("soak", "values")[0], "RUNNING")
        with patch("usb_stress_gui.time.monotonic", return_value=12.5):
            self.app.poll()
        self.assertIn("2.5", self.app.results.item("soak", "values")[1])
        self.app.runner.events.put(("log", PROGRESS_PREFIX + json.dumps(
            {"name": "soak", "status": "PASS", "details": {"iterations": 8}}) + "\n"))
        self.app.poll()
        self.assertEqual(self.app.results.item("soak", "values")[0], "PASS")
        child = self.app.results.get_children("soak")[0]
        self.assertEqual(self.app.results.item(child, "text"), "iterations")
        self.assertEqual(self.app.results.item(child, "values")[1], "8")
        self.assertIsNone(self.app.active_test)

    def test_live_failure_and_malformed_event_do_not_crash_gui(self):
        from usb_stress import PROGRESS_PREFIX
        self.app.results.insert("", "end", iid="bulk", values=("NOT_RUN", ""))
        self.app.live_event(PROGRESS_PREFIX + '{"name":"bulk","status":"FAIL","error":"timeout"}')
        self.assertEqual(self.app.results.item("bulk", "values"), ("FAIL", "timeout"))
        self.assertTrue(self.app.live_event(PROGRESS_PREFIX + "{invalid"))
        self.assertIn("Invalid progress event", self.app.log.get("1.0", "end"))

    def test_progress_bar_tracks_completed_tests_not_estimated_transfer_progress(self):
        from usb_stress import PROGRESS_PREFIX
        for name in ("control", "bulk", "soak"):
            self.app.results.insert("", "end", iid=name, values=("NOT_RUN", ""))
        self.app.live_event(PROGRESS_PREFIX + '{"name":"control","status":"RUNNING"}')
        self.assertEqual(float(self.app.overall_progress["value"]), 0)
        self.app.live_event(PROGRESS_PREFIX + '{"name":"control","status":"PASS","details":{}}')
        self.assertEqual(float(self.app.overall_progress["value"]), 1)
        self.assertEqual(float(self.app.overall_progress["maximum"]), 3)
        self.app.live_event(PROGRESS_PREFIX + '{"name":"bulk","status":"RUNNING"}')
        self.app.live_event(PROGRESS_PREFIX + '{"name":"bulk","status":"FAIL","error":"timeout"}')
        self.assertEqual(self.app.progress_text.get(), "2 / 3 tests completed")
        self.assertEqual(self.app.results.item("soak", "values")[0], "NOT_RUN")

    def test_details_form_nested_key_value_rows_without_changing_test_progress(self):
        self.app.results.insert("", "end", iid="bulk", text="bulk", values=("PASS", ""))
        event = {"elapsed_seconds": 1.5, "details": {
            "out_bytes": 512, "nested": {"packets": 1}, "samples": [64, 128]}}
        self.app.render_details("bulk", event)
        children = self.app.results.get_children("bulk")
        self.assertEqual([self.app.results.item(child, "text") for child in children],
                         ["elapsed_seconds", "out_bytes", "nested", "samples"])
        nested_child = self.app.results.get_children(children[2])[0]
        self.assertEqual(self.app.results.item(nested_child, "text"), "packets")
        self.assertEqual(self.app.results.item(nested_child, "values")[1], "1")
        self.app.update_progress()
        self.assertEqual(self.app.progress_text.get(), "1 / 1 tests completed")
        self.app.render_details("bulk", {"details": {"out_bytes": 1024}})
        self.assertEqual(len(self.app.results.get_children("bulk")), 1)

    def test_discovery_failure_alert_contains_error_and_report_after_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            self.app.report = Path(directory) / "report.json"
            self.app.report.write_text(json.dumps({
                "schema_version": 1, "overall": "FAIL",
                "error": "USB discovery/claim timed out: device not present",
                "tests": [{"name": "control", "status": "NOT_RUN"}],
            }))
            self.app.completed(1)
            self.failure_dialog.assert_called_once()
            message = self.failure_dialog.call_args.args[1]
            self.assertIn("device not present", message)
            self.assertIn(str(self.app.report), message)
            self.assertFalse(self.app.runner.running)
            self.assertEqual(str(self.app.run_button["state"]), "normal")

    def test_successful_run_does_not_display_failure_dialog(self):
        with tempfile.TemporaryDirectory() as directory:
            self.app.report = Path(directory) / "report.json"
            self.app.report.write_text(json.dumps({
                "schema_version": 1, "overall": "PASS",
                "tests": [{"name": "control", "status": "PASS", "details": {}}],
            }))
            self.app.completed(0)
            self.failure_dialog.assert_not_called()

if __name__ == "__main__":
    unittest.main()
