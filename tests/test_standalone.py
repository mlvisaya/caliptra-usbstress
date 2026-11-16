# Licensed under the Apache-2.0 license

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import usb_stress
from usb_stress_config import Settings
from usb_stress_runner import Runner
from test_usb_stress import Device


class GenericTransport(Device, usb_stress.PyUsbTransport):
    def __init__(self, settings):
        Device.__init__(self, packet=64)
        self.settings = settings
        self.vendor_controls = 0
        endpoint = SimpleNamespace(bEndpointAddress=settings.in_endpoint, wMaxPacketSize=64)
        self.device = SimpleNamespace(get_active_configuration=lambda: {(settings.interface, settings.alternate): [endpoint]})
        self.mode = int(settings.behavior == "loopback")

    def control_read(self, *args):
        self.vendor_controls += 1
        raise AssertionError("Generic mode must not send CST1 vendor requests")

    def set_alternate(self, alternate):
        self.alternate = alternate

    def standard_read(self, request_type, request, value, index, length):
        if request == 10:
            return bytes([self.alternate])
        if value == 0x100:
            raw = bytearray(super().standard_read(request_type, request, value, index, 18))
            raw[8:10] = self.settings.vendor_id.to_bytes(2, "little")
            raw[10:12] = self.settings.product_id.to_bytes(2, "little")
            return bytes(raw[:length])
        return super().standard_read(request_type, request, value, index, length)


class StandaloneTests(unittest.TestCase):
    def settings(self, **changes):
        return Settings(vendor_id=0x1234, product_id=0x5678, **changes)

    def test_configuration_round_trip_preserves_device_tests_and_workload(self):
        settings = self.settings(serial_number="test-device", in_endpoint=0x83, out_endpoint=3,
                                 interface=2, tests=["loopback", "soak"], behavior="loopback",
                                 allow_data_tests=True, seed=17)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            settings.save(path)
            self.assertEqual(asdict(Settings.load(path)), asdict(settings))
            raw = json.loads(path.read_text())
            raw["settings"]["unknown"] = True
            path.write_text(json.dumps(raw))
            with self.assertRaisesRegex(ValueError, "Unknown"):
                Settings.load(path)

    def test_invalid_or_dangerous_profiles_rejected_before_usb_discovery(self):
        settings_list = [
            Settings(), self.settings(out_endpoint=0x81), self.settings(in_endpoint=1),
            self.settings(duration=float("nan")), self.settings(queue_depth=1),
            self.settings(tests=["control"]),
            self.settings(tests=["bulk"], allow_data_tests=False),
            self.settings(tests=["bulk"], behavior="loopback", allow_data_tests=True),
            self.settings(tests=["soak"], behavior="source_sink", allow_data_tests=True),
            Settings.cst1(vendor_id=0x424, product_id=7, interface=2),
        ]
        for settings in settings_list:
            with self.subTest(settings=settings), patch.object(usb_stress.PyUsbTransport, "open") as opened:
                with self.assertRaises(ValueError):
                    usb_stress.run(settings)
                opened.assert_not_called()

    def test_generic_source_and_loopback_never_send_vendor_requests(self):
        for behavior, cases in (("source_sink", ["bulk", "alternate", "duplex", "queued_bulk"]),
                                ("loopback", ["loopback", "soak"])):
            settings = self.settings(tests=cases, allow_data_tests=True, behavior=behavior,
                                     out_endpoint=3, in_endpoint=0x83, interface=2, iterations=1,
                                     sizes=[1, 63, 64, 65], duration=0.01)
            settings.validate()
            transport = GenericTransport(settings)
            suite = usb_stress.Suite(transport, 1, settings.sizes, 1, 0.01, settings=settings)
            for case in cases:
                self.assertGreater(getattr(suite, case)()["out_bytes"] if case != "alternate"
                                   else getattr(suite, case)()["cycles"], 0)
            self.assertEqual(transport.vendor_controls, 0)

    def test_generic_descriptors_validate_chain_and_selected_identity(self):
        settings = self.settings()
        transport = GenericTransport(settings)
        suite = usb_stress.Suite(transport, 1, [64], 1, 1, settings=settings)
        self.assertEqual(suite.descriptors()["configurations"], 1)
        original = transport.standard_read
        def malformed(request_type, request, value, index, length):
            raw = bytearray(original(request_type, request, value, index, length))
            if value == 0x200 and length > 9:
                raw[9] = 0
            return bytes(raw)
        transport.standard_read = malformed
        with self.assertRaisesRegex(usb_stress.StressError, "descriptor chain"):
            suite.descriptors()

    def test_cli_config_overrides_and_missing_ids_do_not_access_usb(self):
        with patch.object(sys, "argv", ["usbstress"]), patch.object(
                usb_stress.PyUsbTransport, "open") as opened, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                usb_stress.main()
            self.assertEqual(error.exception.code, 2)
            opened.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            self.settings().save(path)
            with patch.object(sys, "argv", ["usbstress", "--config", str(path), "--iterations", "9",
                                           "--vendor-id", "0x9999"]), patch.object(usb_stress, "run", return_value=0) as run:
                self.assertEqual(usb_stress.main(), 0)
            self.assertEqual(run.call_args.args[0].iterations, 9)
            self.assertEqual(run.call_args.args[0].vendor_id, 0x9999)

    def test_gui_runner_owns_only_child_and_cleans_snapshot(self):
        runner = Runner()
        process = Mock()
        process.stdout = iter([])
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as directory, patch(
                "usb_stress_runner.subprocess.Popen", return_value=process) as popen, patch(
                "usb_stress_runner.threading.Thread") as thread:
            report = Path(directory) / "report.json"
            runner.start(self.settings(), report)
            snapshot = runner.config_path
            self.assertTrue(snapshot.is_file())
            self.assertEqual(Settings.load(snapshot).vendor_id, 0x1234)
            command = popen.call_args.args[0]
            self.assertIsInstance(command, list)
            self.assertIn("--config", command)
            self.assertNotIn("shell", popen.call_args.kwargs)
            thread.return_value.start.assert_called_once()
            with self.assertRaises(RuntimeError):
                runner.start(self.settings(), report)
            runner.stop(grace_seconds=0)
            process.send_signal.assert_called_once()
            runner.tick()
            process.kill.assert_called_once()
            self.assertTrue(runner.forced)
            process.poll.return_value = 1
            runner.finish()
            self.assertFalse(snapshot.exists())
            self.assertFalse(runner.running)

    def test_progress_stream_announces_running_before_test_and_failure_immediately(self):
        output = io.StringIO()
        settings = Settings.cst1(vendor_id=0x424, product_id=7, tests=["control", "bulk"])
        def fail():
            lines = [json.loads(line[len(usb_stress.PROGRESS_PREFIX):])
                     for line in output.getvalue().splitlines()
                     if line.startswith(usb_stress.PROGRESS_PREFIX)]
            self.assertEqual(lines[-1], {"name": "control", "status": "RUNNING"})
            raise usb_stress.StressError("injected failure")
        with tempfile.TemporaryDirectory() as directory, patch.object(
                usb_stress.PyUsbTransport, "open", return_value=Device()), patch.object(
                usb_stress.Suite, "control", side_effect=fail), redirect_stdout(output), redirect_stderr(io.StringIO()):
            report = Path(directory) / "report.json"
            self.assertEqual(usb_stress.run(settings, report, progress_events=True), 1)
            self.assertEqual([case["status"] for case in json.loads(report.read_text())["tests"]],
                             ["FAIL", "NOT_RUN"])
        events = [json.loads(line[len(usb_stress.PROGRESS_PREFIX):])
                  for line in output.getvalue().splitlines() if line.startswith(usb_stress.PROGRESS_PREFIX)]
        self.assertEqual([event["status"] for event in events], ["RUNNING", "FAIL"])
        self.assertEqual(events[-1]["error"], "injected failure")

    def test_subtest_report_covers_all_executed_groups_and_full_speed_exclusions(self):
        names = [name for name in usb_stress.SUPPORTED_TESTS if name != "descriptors"]
        settings = Settings.cst1(vendor_id=0x424, product_id=7, tests=names,
                                iterations=2, sizes=[1, 64, 65], duration=0.01)
        with tempfile.TemporaryDirectory() as directory, patch.object(
                usb_stress.PyUsbTransport, "open", return_value=Device(64)), redirect_stdout(io.StringIO()):
            path = Path(directory) / "report.json"
            self.assertEqual(usb_stress.run(settings, path), 0)
            result = json.loads(path.read_text())
        for case in result["tests"]:
            self.assertEqual([item["name"] for item in case["subtests"]],
                             usb_stress.subtest_names(case["name"], settings))
            self.assertTrue(all(item["status"] in ("PASS", "SKIP") for item in case["subtests"]))
        chapter9 = next(case for case in result["tests"] if case["name"] == "chapter9")
        records = {item["name"]: item for item in chapter9["subtests"]}
        self.assertEqual(records["GET_CONFIGURATION"]["executions"], 2)
        self.assertEqual(records["Device qualifier"]["status"], "SKIP")
        self.assertIn("high speed", records["Device qualifier"]["reason"])
        self.assertEqual(records["Post-check bulk loopback"]["executions"], 1)

    def test_failing_chapter9_subtest_preserves_completed_and_not_run_items_and_live_events(self):
        device = Device()
        original = device.standard_read
        def malformed_qualifier(request_type, request, value, index, length):
            if value == 0x600:
                return bytes(10)
            return original(request_type, request, value, index, length)
        device.standard_read = malformed_qualifier
        settings = Settings.cst1(vendor_id=0x424, product_id=7, tests=["chapter9", "bulk"])
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, patch.object(
                usb_stress.PyUsbTransport, "open", return_value=device), redirect_stdout(output), redirect_stderr(io.StringIO()):
            path = Path(directory) / "report.json"
            self.assertEqual(usb_stress.run(settings, path, progress_events=True), 1)
            result = json.loads(path.read_text())
        records = {item["name"]: item for item in result["tests"][0]["subtests"]}
        self.assertEqual(records["Interface string"]["status"], "PASS")
        self.assertEqual(records["Device qualifier"]["status"], "FAIL")
        self.assertIn("mismatch", records["Device qualifier"]["error"])
        self.assertEqual(records["Other-speed configuration"]["status"], "NOT_RUN")
        self.assertTrue(all(item["status"] == "NOT_RUN" for item in result["tests"][1]["subtests"]))
        events = [json.loads(line[len(usb_stress.PROGRESS_PREFIX):])
                  for line in output.getvalue().splitlines() if line.startswith(usb_stress.PROGRESS_PREFIX)]
        qualifier = [event for event in events if event.get("subtest") == "Device qualifier"]
        self.assertEqual([event["status"] for event in qualifier], ["RUNNING", "FAIL"])
        self.assertEqual(events[-1]["status"], "FAIL")
        self.assertNotIn("subtest", events[-1])


if __name__ == "__main__":
    unittest.main()
