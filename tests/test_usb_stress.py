# Licensed under the Apache-2.0 license

from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import usb_stress
import usb1
from usb_stress_advanced import Completion


class Device:
    def __init__(self, packet=512):
        self.packet = packet
        self.mode = self.alternate = 0
        self.stored = b""
        self.queue = b""
        self.counters = [0, 0, 0, 0]
        self.halts = {1: False, 0x81: False}
        self.corrupt = self.short_write = self.bad_counters = self.halt_timeout = False
        self.closed = False
        self.lock = threading.Lock()
        self.bulk_calls = 0
        self.fail_cancel = self.noop_cycle = self.corrupt_queue = False
        self.maximum_queue = 0

    def control_read(self, request, length):
        if request == 0x5d:
            return struct.pack("<4sBBBBHHI", b"CST1", 1, self.mode, self.alternate,
                               2 if self.packet == 512 else 1, self.packet, 1024, 15)[:length]
        if request == 0x5c:
            return self.stored[:length]
        if request == 0x5f:
            counters = list(self.counters)
            counters[0] += int(self.bad_counters)
            return struct.pack("<IIII", *counters)
        raise usb_stress.StressError("unknown control read")

    def control_write(self, request, data=b"", value=0):
        if request == 0x5b:
            self.stored = data
        elif request == 0x5e:
            self.mode = value
            self.counters = [0, 0, 0, 0]
            self.queue = b""
        else:
            raise usb_stress.StressError("unknown control write")

    def bulk_write(self, data):
        with self.lock:
            self.bulk_calls += 1
            if self.halts[1]:
                if self.halt_timeout:
                    raise usb_stress.StressError("timeout is not STALL")
                raise usb_stress.PipeHalted("OUT stalled")
            if self.mode == 1:
                if len(data) > self.packet:
                    raise usb_stress.StressError("loopback must respect finite device buffer")
                self.queue += data
            self.counters[0] += len(data)
            self.counters[2] += (len(data) + self.packet - 1) // self.packet
            return len(data) - int(self.short_write)

    def bulk_read(self, length):
        with self.lock:
            self.bulk_calls += 1
            if self.halts[0x81]:
                raise usb_stress.PipeHalted("IN stalled")
            if self.mode == 0:
                if length % self.packet:
                    raise usb_stress.StressError("source reads must be packet aligned")
                data = bytes(length)
            else:
                data = self.queue[:length]
                self.queue = self.queue[length:]
            self.counters[1] += len(data)
            self.counters[3] += (len(data) + self.packet - 1) // self.packet
            if self.corrupt and data:
                data = bytes([data[0] ^ 1]) + data[1:]
            return data

    def set_alternate(self, alternate):
        self.alternate = alternate
        self.queue = b""
        self.halts = {1: False, 0x81: False}

    def halt(self, endpoint):
        self.halts[endpoint] = True

    def clear_halt(self, endpoint):
        self.halts[endpoint] = False

    def halted(self, endpoint):
        return self.halts[endpoint]

    def close(self):
        self.closed = True

    def queued(self, requests, cancel=False, prime=None):
        self.maximum_queue = max(self.maximum_queue, len(requests))
        if prime is not None:
            self.bulk_write(prime)
        if cancel:
            return [Completion(usb1.TRANSFER_COMPLETED if self.fail_cancel
                               else usb1.TRANSFER_CANCELLED, 0, b"")
                    for _ in requests]
        results = []
        for request in requests:
            if request.control_request is not None:
                data = self.control_read(request.control_request, request.payload)
                length = len(data)
            elif request.endpoint & 0x80:
                data = self.bulk_read(request.payload)
                length = len(data)
            else:
                data = b""
                length = self.bulk_write(request.payload)
            if self.corrupt_queue and data:
                data = bytes([data[0] ^ 1]) + data[1:]
            results.append(Completion(usb1.TRANSFER_COMPLETED, length, data))
        return results

    def port_cycle(self):
        if not self.noop_cycle:
            self.stored = b""
            self.mode = 0
            self.counters = [0, 0, 0, 0]
            self.set_alternate(1)

    def standard_read(self, request_type, request, value, index, length):
        if (request_type, request) == (0x80, 6):
            device = bytes([18, 1, 0, 2, 0, 0, 0, 64]) + bytes(9) + b"\x01"
            prefix = bytes([9, 2, 41, 0, 1, 1, 0, 0xc0, 50])
            interfaces = bytes([9, 4, 0, 0, 0, 255, 0, 0, 1,
                                9, 4, 0, 1, 2, 255, 0, 0, 1])
            def endpoints(packet):
                return b"".join(bytes([7, 5, ep, 2]) + struct.pack("<H", packet) + b"\0"
                                for ep in (1, 0x81))
            descriptors = {
                (0x100, 0): device,
                (0x200, 0): prefix + interfaces + endpoints(self.packet),
                (0x300, 0): b"\x04\x03\x09\x04",
                (0x301, 0x409): b"\x28\x03" + "Caliptra USB stress".encode("utf-16-le"),
                (0x600, 0): b"\x0a\x06\x00\x02\x00\x00\x00\x40\x01\x00",
                (0x700, 0): bytes([9, 7]) + prefix[2:] + interfaces + endpoints(64),
            }
            if (value, index) in descriptors:
                return descriptors[value, index][:length]
            raise usb_stress.PipeHalted("unsupported descriptor")
        if (request_type, request) == (0x80, 8):
            return b"\x01"
        if request == 0:
            return b"\x01\0" if request_type == 0x80 else b"\0\0"
        if (request_type, request) == (0x81, 10):
            return bytes([self.alternate])
        raise usb_stress.StressError("unimplemented standard request")


class StressTests(unittest.TestCase):
    def suite(self, device):
        return usb_stress.Suite(device, 2, [1, 63, 64, 65, 511, 512, 513, 4096], 19, 0.01)

    def test_all_mvp_cases_at_full_and_high_speed(self):
        for packet in (64, 512):
            device = Device(packet)
            suite = self.suite(device)
            for name in usb_stress.TESTS:
                getattr(suite, name)()
            self.assertGreater(device.bulk_calls, 0)

    def test_capability_handshake_rejects_ocp_or_malformed_device(self):
        for raw in (b"OCP1" + bytes(12), b"CST1", struct.pack(
                "<4sBBBBHHI", b"CST1", 1, 0, 0, 2, 64, 1024, 15)):
            with self.assertRaises(usb_stress.StressError):
                usb_stress.Capabilities.decode(raw)

    def test_random_loopback_detects_corruption_without_replaying(self):
        device = Device()
        device.corrupt = True
        with self.assertRaisesRegex(usb_stress.StressError, "mismatch"):
            self.suite(device).loopback()
        self.assertEqual(device.counters[0], 1)

    def test_short_write_and_wrong_counter_are_failures(self):
        for attribute in ("short_write", "bad_counters"):
            device = Device()
            setattr(device, attribute, True)
            with self.assertRaises(usb_stress.StressError):
                self.suite(device).bulk()

    def test_timeout_is_not_accepted_as_expected_endpoint_stall(self):
        device = Device()
        device.halt_timeout = True
        with self.assertRaisesRegex(usb_stress.StressError, "timeout"):
            self.suite(device).halt()

    def test_long_run_byte_accounting_handles_device_counter_wrap(self):
        device = Device()
        device.counters = [7, 9, 0, 0]
        result = self.suite(device).counters((1 << 32) + 7, (1 << 32) + 9)
        self.assertEqual(result["out_bytes"], (1 << 32) + 7)
        self.assertEqual(result["in_bytes"], (1 << 32) + 9)

    def test_queued_bulk_and_control_check_lengths_data_and_accounting(self):
        for packet in (64, 512):
            device = Device(packet)
            suite = self.suite(device)
            self.assertGreater(suite.queued_bulk()["out_bytes"], 0)
            self.assertEqual(device.maximum_queue, 16)
            self.assertEqual(suite.queued_control()["transfers"], 16)
            device.corrupt_queue = True
            for name in ("queued_bulk", "queued_control"):
                with self.assertRaisesRegex(usb_stress.StressError, "mismatch"):
                    getattr(suite, name)()

    def test_cancel_requires_cancelled_status_and_checks_recovery(self):
        device = Device()
        self.assertEqual(self.suite(device).cancel()["cancelled_transfers"], 36)
        device.fail_cancel = True
        with self.assertRaisesRegex(usb_stress.StressError, "CANCELLED"):
            self.suite(device).cancel()

    def test_port_cycle_rejects_success_without_resetting_firmware(self):
        device = Device()
        self.assertEqual(self.suite(device).port_cycle()["cycles"], 2)
        device.noop_cycle = True
        with self.assertRaisesRegex(usb_stress.StressError, "Port cycle"):
            self.suite(device).port_cycle()

    def test_completed_queue_rejects_missing_short_and_error_completions(self):
        from usb_stress_advanced import Request
        requests = [Request(usb_stress.IN, 64)]
        for completions in ([], [Completion(usb1.TRANSFER_COMPLETED, 63, bytes(63))],
                            [Completion(usb1.TRANSFER_TIMED_OUT, 64, bytes(64))]):
            with self.assertRaises(usb_stress.StressError):
                usb_stress.Suite._completed(requests, completions)

    def test_chapter9_checks_descriptors_stalls_and_post_stall_recovery(self):
        for packet in (64, 512):
            device = Device(packet)
            self.assertGreater(self.suite(device).chapter9()["standard_read_attempts"], 20)
            original = device.standard_read
            def no_stall(request_type, request, value, index, length):
                if value == 0xff00:
                    return b""
                return original(request_type, request, value, index, length)
            device.standard_read = no_stall
            with self.assertRaisesRegex(usb_stress.StressError, "did not STALL"):
                self.suite(device).chapter9()

    def test_cli_report_failure_marks_remaining_cases_not_run(self):
        device = Device()
        device.corrupt = True
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            args = ["usb_stress", "--profile", "cst1", "--vendor-id", "0x424", "--product-id", "7",
                    "--tests", "bulk,loopback", "--iterations", "1",
                    "--sizes", "1,513", "--report", str(output)]
            with patch.object(sys, "argv", args), patch.object(
                    usb_stress.PyUsbTransport, "open", return_value=device):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(usb_stress.main(), 1)
            report = json.loads(output.read_text())
            self.assertEqual(report["overall"], "FAIL")
            self.assertEqual([case["status"] for case in report["tests"]], ["FAIL", "NOT_RUN"])
            self.assertTrue(device.closed)

    def test_cli_report_success_contains_negotiated_speed_and_byte_totals(self):
        device = Device(64)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            args = ["usb_stress", "--profile", "cst1", "--vendor-id", "0x424", "--product-id", "7",
                    "--tests", "loopback", "--iterations", "1",
                    "--sizes", "1,513", "--report", str(output)]
            with patch.object(sys, "argv", args), patch.object(
                    usb_stress.PyUsbTransport, "open", return_value=device):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(usb_stress.main(), 0)
            report = json.loads(output.read_text())
            self.assertEqual(report["overall"], "PASS")
            self.assertEqual(report["capabilities"]["speed"], 1)
            self.assertEqual(report["tests"][0]["details"]["out_bytes"], 514)
            self.assertTrue(device.closed)


if __name__ == "__main__":
    unittest.main()
