#!/usr/bin/env python3
# Licensed under the Apache-2.0 license
"""Configurable USB stress tests for generic and CST1-compatible devices."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import struct
import sys
import threading
import time
import signal
from typing import Callable, Protocol, TypeVar, TYPE_CHECKING
from usb_stress_config import Settings, CST1_TESTS, GENERIC_TESTS

if TYPE_CHECKING:
    from usb.core import Device
    from usb_stress_advanced import Completion, Request

OUT = 0x01
IN = 0x81
TESTS = ("control", "bulk", "loopback", "halt", "alternate", "duplex", "soak")
ADVANCED_TESTS = ("chapter9", "queued_bulk", "queued_control", "cancel", "port_cycle", "descriptors")
SUPPORTED_TESTS = TESTS + ADVANCED_TESTS
T = TypeVar("T")


class StressError(RuntimeError):
    pass


class PipeHalted(StressError):
    pass


@dataclass(frozen=True)
class Capabilities:
    version: int
    mode: int
    alternate: int
    speed: int
    packet_size: int
    control_max: int
    features: int

    @classmethod
    def decode(cls, raw: bytes) -> Capabilities:
        if len(raw) != 16:
            raise StressError(f"Malformed CST1 capability length: {len(raw)}")
        magic, version, mode, alternate, speed, packet, control, features = struct.unpack(
            "<4sBBBBHHI", raw
        )
        if magic != b"CST1" or version != 1:
            raise StressError("Device is not the CST1 stress ROM; no bulk tests will be sent")
        if mode not in (0, 1) or alternate not in (0, 1):
            raise StressError("Invalid stress mode/alternate setting")
        if speed not in (1, 2) or packet != (64 if speed == 1 else 512):
            raise StressError(f"Invalid speed/packet size: {speed}/{packet}")
        if control != 1024 or features & 0x0f != 0x0f:
            raise StressError("Device lacks the required CST1 test features")
        return cls(version, mode, alternate, speed, packet, control, features)


class Transport(Protocol):
    def control_read(self, request: int, length: int) -> bytes: ...
    def control_write(self, request: int, data: bytes = b"", value: int = 0) -> None: ...
    def bulk_read(self, length: int) -> bytes: ...
    def bulk_write(self, data: bytes) -> int: ...
    def set_alternate(self, alternate: int) -> None: ...
    def halt(self, endpoint: int) -> None: ...
    def clear_halt(self, endpoint: int) -> None: ...
    def halted(self, endpoint: int) -> bool: ...
    def standard_read(self, request_type: int, request: int, value: int,
                      index: int, length: int) -> bytes: ...
    def queued(self, requests: list[Request], cancel: bool = False,
               prime: bytes | None = None) -> list[Completion]: ...
    def port_cycle(self) -> None: ...


class PyUsbTransport:
    def __init__(self, device: Device, timeout_ms: int):
        import usb.core
        import usb.util

        self.device = device
        self.timeout_ms = timeout_ms
        self.usb_core = usb.core
        self.usb_util = usb.util
        self.settings = Settings.cst1(vendor_id=device.idVendor, product_id=device.idProduct)

    @classmethod
    def open(cls, vendor: int, product: int, discovery_timeout: float, timeout_ms: int,
             settings: Settings | None = None) -> PyUsbTransport:
        import usb.core
        import usb.util

        settings = settings or Settings.cst1(vendor_id=vendor, product_id=product)
        deadline = time.monotonic() + discovery_timeout
        last_error = "device not present"
        while time.monotonic() < deadline:
            if sys.platform == "win32":
                import libusb_package
                devices = list(libusb_package.find(find_all=True, idVendor=vendor, idProduct=product))
            else:
                devices = list(usb.core.find(find_all=True, idVendor=vendor, idProduct=product))
            devices = [device for device in devices
                       if (settings.bus is None or device.bus == settings.bus)
                       and (settings.address is None or device.address == settings.address)]
            if settings.serial_number:
                try:
                    devices = [device for device in devices if device.serial_number == settings.serial_number]
                except usb.core.USBError as error:
                    raise StressError(f"Cannot read USB serial numbers: {error}") from error
            if len(devices) > 1:
                raise StressError("Multiple devices match VID/PID; disconnect the unrelated test device")
            if not devices:
                time.sleep(0.1)
                continue
            device = devices[0]
            try:
                try:
                    configuration = device.get_active_configuration()
                except usb.core.USBError as error:
                    if getattr(error, "backend_error_code", None) != -5:
                        raise
                    if settings.profile == "generic":
                        raise StressError("Generic device is unconfigured; configure it before testing") from error
                    device.set_configuration(settings.configuration)
                    configuration = device.get_active_configuration()
                if configuration.bConfigurationValue != settings.configuration:
                    raise StressError("Active configuration does not match selection; configure the device first")
                interface = configuration[(settings.interface, settings.alternate)]
                if settings.profile == "cst1" and interface.bInterfaceClass != 0xff:
                    raise StressError("CST1 requires a vendor-class interface")
                usb.util.claim_interface(device, settings.interface)
                device.ctrl_transfer(0x80, 6, 0x0100, 0, 18, timeout=timeout_ms)
                transport = cls(device, timeout_ms)
                transport.settings = settings
                if settings.profile == "cst1":
                    caps = Capabilities.decode(transport.control_read(0x5d, 16))
                endpoints = {ep.bEndpointAddress: ep for ep in interface}
                if settings.needs_bulk or settings.profile == "cst1":
                    selected = [endpoints.get(settings.out_endpoint), endpoints.get(settings.in_endpoint)]
                    if any(ep is None or ep.bmAttributes & 3 != 2 for ep in selected):
                        raise StressError("Selected bulk IN/OUT endpoints are missing or not bulk")
                    if selected[0].wMaxPacketSize != selected[1].wMaxPacketSize:
                        raise StressError("This profile requires equal IN/OUT bulk maximum packet sizes")
                    if settings.profile == "cst1" and selected[0].wMaxPacketSize != caps.packet_size:
                        raise StressError("Bulk descriptors do not match negotiated CST1 capabilities")
                if settings.needs_bulk or settings.profile == "cst1":
                    transport.set_alternate(settings.alternate)
                return transport
            except StressError:
                usb.util.dispose_resources(device)
                raise
            except (usb.core.USBError, NotImplementedError) as error:
                last_error = str(error)
                usb.util.dispose_resources(device)
                time.sleep(0.1)
            except (ValueError, KeyError) as error:
                usb.util.dispose_resources(device)
                raise StressError(f"Invalid selected interface/alternate: {error}") from error
        raise StressError(f"USB discovery/claim timed out: {last_error}")

    def _call(self, operation: Callable[[], T]) -> T:
        try:
            return operation()
        except self.usb_core.USBError as error:
            if getattr(error, "backend_error_code", None) == -9 or error.errno == 32:
                raise PipeHalted(str(error)) from error
            raise StressError(f"USB transfer failed: {error}") from error
        except NotImplementedError as error:
            raise StressError(f"USB backend does not support the operation: {error}") from error
        except ValueError as error:
            raise StressError(f"USB backend rejected the operation: {error}") from error

    def control_read(self, request: int, length: int) -> bytes:
        return bytes(self._call(lambda: self.device.ctrl_transfer(
            0xc0, request, 0, 0, length, timeout=self.timeout_ms
        )))

    def control_write(self, request: int, data: bytes = b"", value: int = 0) -> None:
        actual = self._call(lambda: self.device.ctrl_transfer(
            0x40, request, value, 0, data, timeout=self.timeout_ms
        ))
        if actual != len(data):
            raise StressError(f"Short control write: {actual}/{len(data)}")

    def bulk_read(self, length: int) -> bytes:
        return bytes(self._call(lambda: self.device.read(
            self.settings.in_endpoint, length, timeout=self.timeout_ms)))

    def bulk_write(self, data: bytes) -> int:
        return self._call(lambda: self.device.write(
            self.settings.out_endpoint, data, timeout=self.timeout_ms))

    def set_alternate(self, alternate: int) -> None:
        self._call(lambda: self.device.set_interface_altsetting(
            interface=self.settings.interface, alternate_setting=alternate))

    def halt(self, endpoint: int) -> None:
        self._call(lambda: self.device.ctrl_transfer(
            2, 3, 0, endpoint, b"", timeout=self.timeout_ms
        ))

    def clear_halt(self, endpoint: int) -> None:
        self._call(lambda: self.device.clear_halt(endpoint))

    def halted(self, endpoint: int) -> bool:
        raw = bytes(self._call(lambda: self.device.ctrl_transfer(
            0x82, 0, 0, endpoint, 2, timeout=self.timeout_ms
        )))
        if len(raw) != 2 or raw[1] != 0 or raw[0] not in (0, 1):
            raise StressError(f"Invalid endpoint status: {raw.hex()}")
        return bool(raw[0])

    def close(self) -> None:
        self._call(lambda: self.usb_util.dispose_resources(self.device))

    def standard_read(self, request_type: int, request: int, value: int,
                      index: int, length: int) -> bytes:
        try:
            return bytes(self._call(lambda: self.device.ctrl_transfer(
                request_type, request, value, index, length, timeout=self.timeout_ms
            )))
        except StressError as error:
            raise type(error)(
                f"Standard IN {request_type:#x}/{request:#x}, value={value:#x}, "
                f"index={index:#x}, length={length}: {error}"
            ) from error

    def queued(self, requests: list[Request], cancel: bool = False,
               prime: bytes | None = None) -> list[Completion]:
        from usb_stress_advanced import AdvancedError, AsyncDevice
        import usb1

        vendor, product = self.device.idVendor, self.device.idProduct
        self.close()
        try:
            with AsyncDevice(vendor, product, self.timeout_ms, self.settings) as asynchronous:
                return asynchronous.run(requests, cancel, prime)
        except (AdvancedError, usb1.USBError, OSError) as error:
            raise StressError(f"Async USB operation failed: {error}") from error

    def port_cycle(self) -> None:
        from usb_stress_advanced import AdvancedError, port_cycle

        vendor, product = self.device.idVendor, self.device.idProduct
        import libusb_package
        # The native API selects by VID/PID only, not by serial or interface.
        devices = list(libusb_package.find(find_all=True, idVendor=vendor, idProduct=product))
        if len(devices) != 1:
            raise StressError("Port cycle requires a unique VID/PID even when a serial number is selected")
        self.close()
        try:
            port_cycle(vendor, product)
        except (AdvancedError, OSError) as error:
            raise StressError(f"Native libusbK port cycle failed: {error}") from error
        # Port cycling can assign a new USB address; VID/PID was checked unique above.
        reconnect = replace(self.settings, bus=None, address=None)
        replacement = type(self).open(vendor, product, 15, self.timeout_ms, reconnect)
        self.device = replacement.device
        self.settings.bus = self.device.bus
        self.settings.address = self.device.address


def pattern(seed: int, length: int) -> bytes:
    return random.Random(seed).randbytes(length)


def check_data(actual: bytes, expected: bytes, context: str) -> None:
    if actual != expected:
        mismatch = next((i for i, (a, b) in enumerate(zip(actual, expected)) if a != b),
                        min(len(actual), len(expected)))
        raise StressError(f"{context}: mismatch at offset {mismatch}; length {len(actual)}/{len(expected)}")


def subtest_names(name: str, settings: Settings) -> list[str]:
    size_cases = [f"{size} byte workload" for size in dict.fromkeys(settings.sizes)]
    return {
        "control": [f"{size} byte control read-back / short response" for size in (0, 1, 63, 64, 65, 255, 512, 1024)],
        "bulk": ["Select source/sink mode", *size_cases, "Byte accounting"],
        "loopback": ["Select loopback mode", *size_cases, "Byte accounting"],
        "halt": ["Select source/sink mode", "OUT halt / STALL / recovery",
                 "IN halt / STALL / recovery", "Clear non-halted IN"],
        "alternate": ["Select source/sink mode", "Alternate transitions / read-back", "Traffic after alternate change"],
        "duplex": ["Select source/sink mode", "Concurrent IN / OUT integrity", "Byte accounting"],
        "soak": ["Select loopback mode", "Timed loopback integrity", "Byte accounting"],
        "chapter9": ["Device descriptor", "Truncated device descriptors",
                     "Configuration / alternate descriptors", "Bulk endpoint descriptors",
                     "GET_CONFIGURATION", "Device GET_STATUS", "Interface / EP0 GET_STATUS",
                     "Language descriptor", "Interface string", "Device qualifier",
                     "Other-speed configuration", "SET_INTERFACE / GET_INTERFACE",
                     "Invalid descriptor type / STALL recovery", "Invalid device descriptor index / STALL recovery",
                     "Invalid string index / STALL recovery", "Post-check bulk loopback"],
        "queued_bulk": ["Select source/sink mode", *size_cases, "Byte accounting"],
        "queued_control": ["Select source/sink mode", "Store control pattern", "Queued EP0 read-back integrity"],
        "cancel": [f"{direction} cancellation (depth {depth}) / recovery"
                   for direction in ("IN", "OUT") for depth in (1, settings.queue_depth)],
        "port_cycle": ["Pre-cycle marker", "Native port cycle", "Reset state / cleared marker",
                       "Post-cycle control", "Post-cycle loopback / accounting"],
        "descriptors": ["Device descriptor / identity", "Configuration descriptor chains"],
    }[name]


class Suite:
    def __init__(self, transport: Transport, iterations: int, sizes: list[int], seed: int,
                 duration: float, queue_depth: int = 8, settings: Settings | None = None):
        self.transport = transport
        self.iterations = iterations
        self.sizes = sizes
        self.seed = seed
        self.duration = duration
        self.queue_depth = queue_depth
        self.settings = settings or Settings.cst1()
        self.out_endpoint = self.settings.out_endpoint
        self.in_endpoint = self.settings.in_endpoint
        self.subtests: list[dict] = []
        self.subtest_callback: Callable[[dict], None] | None = None
        self._in_subtest = False
        if self.settings.profile == "cst1":
            self.caps = Capabilities.decode(transport.control_read(0x5d, 16))
        else:
            if not isinstance(transport, PyUsbTransport):
                raise StressError("Generic profile requires descriptor-aware transport")
            interface = transport.device.get_active_configuration()[
                self.settings.interface, self.settings.alternate
            ]
            packets = [ep.wMaxPacketSize for ep in interface if ep.bEndpointAddress == self.in_endpoint]
            packet = packets[0] if packets else 0
            if self.settings.needs_bulk and packet not in (8, 16, 32, 64, 512, 1024):
                raise StressError(f"Unsupported bulk packet size: {packet}")
            self.caps = Capabilities(0, int(self.settings.behavior == "loopback"),
                                     self.settings.alternate,
                                     0 if packet == 0 else 3 if packet == 1024 else 2 if packet == 512 else 1,
                                     packet, 0, 0)

    @contextmanager
    def subtest(self, name: str):
        if self._in_subtest:
            yield
            return
        record = next((item for item in self.subtests if item["name"] == name), None)
        if record is None:
            record = dict(name=name, status="NOT_RUN", executions=0, elapsed_seconds=0.0)
            self.subtests.append(record)
        record["status"] = "RUNNING"
        started = time.monotonic()
        self._in_subtest = True
        if self.subtest_callback:
            self.subtest_callback(dict(record))
        try:
            yield
        except (StressError, ImportError, OSError, KeyboardInterrupt) as error:
            record.update(status="FAIL", error=str(error) or "interrupted")
            raise
        else:
            record["status"] = "PASS"
            record["executions"] += 1
        finally:
            self._in_subtest = False
            record["elapsed_seconds"] += time.monotonic() - started
            if self.subtest_callback:
                self.subtest_callback(dict(record))

    def skip_subtest(self, name: str, reason: str) -> None:
        record = next((item for item in self.subtests if item["name"] == name), None)
        if record is not None:
            record.update(status="SKIP", reason=reason)
            if self.subtest_callback:
                self.subtest_callback(dict(record))

    def select_mode(self, mode: int) -> None:
        with self.subtest("Select loopback mode" if mode else "Select source/sink mode"):
            self.mode(mode)

    def checked_counters(self, out_bytes: int, in_bytes: int) -> dict[str, int]:
        with self.subtest("Byte accounting"):
            return self.counters(out_bytes, in_bytes)

    def mode(self, mode: int) -> None:
        if self.settings.profile == "generic":
            if mode != int(self.settings.behavior == "loopback"):
                raise StressError("Test behavior does not match the declared generic device behavior")
            return
        self.transport.set_alternate(0)
        self.transport.control_write(0x5e, value=mode)
        self.transport.set_alternate(1)
        self.caps = Capabilities.decode(self.transport.control_read(0x5d, 16))
        if self.caps.mode != mode or self.caps.alternate != 1:
            raise StressError("Device did not commit the requested mode/alternate setting")

    def write(self, data: bytes) -> None:
        actual = self.transport.bulk_write(data)
        if actual != len(data):
            raise StressError(f"Short bulk OUT write: {actual}/{len(data)}")

    def stats(self) -> tuple[int, int, int, int]:
        raw = self.transport.control_read(0x5f, 16)
        if len(raw) != 16:
            raise StressError("Malformed device counters")
        return struct.unpack("<IIII", raw)

    def counters(self, out_bytes: int, in_bytes: int) -> dict[str, int]:
        if self.settings.profile == "generic":
            return dict(out_bytes=out_bytes, in_bytes=in_bytes)
        actual_out, actual_in, out_packets, in_packets = self.stats()
        if (actual_out, actual_in) != (out_bytes & 0xffffffff, in_bytes & 0xffffffff):
            raise StressError(f"Device byte counters disagree: OUT/IN={actual_out}/{actual_in}, "
                              f"expected {out_bytes & 0xffffffff}/{in_bytes & 0xffffffff}")
        return dict(out_bytes=out_bytes, in_bytes=in_bytes,
                    device_out_packets=out_packets, device_in_packets=in_packets)

    def control(self) -> dict[str, int]:
        for iteration in range(self.iterations):
            for length in (0, 1, 63, 64, 65, 255, 512, 1024):
                with self.subtest(f"{length} byte control read-back / short response"):
                    data = pattern(self.seed + iteration + length, length)
                    self.transport.control_write(0x5b, data)
                    check_data(self.transport.control_read(0x5c, length), data, "control read-back")
                    if 0 < length < self.caps.control_max:
                        check_data(self.transport.control_read(0x5c, length + 1), data,
                                   "control short/ZLP termination")
        return {"iterations": self.iterations}

    def bulk(self) -> dict[str, int]:
        self.select_mode(0)
        out_bytes = in_bytes = 0
        packet = self.caps.packet_size
        for iteration in range(self.iterations):
            for size in self.sizes:
                with self.subtest(f"{size} byte workload"):
                    data = pattern(self.seed + iteration + size, size)
                    self.write(data)
                    out_bytes += size
                    length = ((size + packet - 1) // packet) * packet
                    check_data(self.transport.bulk_read(length), bytes(length), "bulk source")
                    in_bytes += length
        return self.checked_counters(out_bytes, in_bytes)

    def loopback(self) -> dict[str, int]:
        self.select_mode(1)
        total = 0
        for iteration in range(self.iterations):
            for size in self.sizes:
                with self.subtest(f"{size} byte workload"):
                    data = pattern(self.seed + iteration + size, size)
                    for offset in range(0, size, self.caps.packet_size):
                        chunk = data[offset:offset + self.caps.packet_size]
                        self.write(chunk)
                        check_data(self.transport.bulk_read(len(chunk)), chunk, f"loopback {size}@{offset}")
                        total += len(chunk)
        return self.checked_counters(total, total)

    def halt(self) -> dict[str, int]:
        self.select_mode(0)
        packet = self.caps.packet_size
        for endpoint in (self.out_endpoint, self.in_endpoint):
            with self.subtest("IN halt / STALL / recovery" if endpoint == self.in_endpoint
                              else "OUT halt / STALL / recovery"):
                self.check_halt(endpoint, packet)
        with self.subtest("Clear non-halted IN"):
            self.transport.clear_halt(self.in_endpoint)
            check_data(self.transport.bulk_read(packet), bytes(packet), "clear non-halted IN")
        return {"endpoints_tested": 2}

    def check_halt(self, endpoint: int, packet: int) -> None:
        self.transport.halt(endpoint)
        if not self.transport.halted(endpoint):
            raise StressError(f"Endpoint {endpoint:#x} did not enter HALT")
        try:
            if endpoint == self.in_endpoint:
                self.transport.bulk_read(packet)
            else:
                self.transport.bulk_write(bytes(packet))
        except PipeHalted:
            pass
        else:
            raise StressError(f"Endpoint {endpoint:#x} did not STALL traffic")
        self.transport.clear_halt(endpoint)
        if self.transport.halted(endpoint):
            raise StressError(f"Endpoint {endpoint:#x} remained halted")
        if endpoint == self.in_endpoint:
            check_data(self.transport.bulk_read(packet), bytes(packet), "clear IN halt")
        else:
            self.write(pattern(self.seed, packet))

    def alternate(self) -> dict[str, int]:
        self.select_mode(0)
        for _ in range(self.iterations):
            with self.subtest("Alternate transitions / read-back"):
                self.check_alternates()
            with self.subtest("Traffic after alternate change"):
                self.write(bytes(self.caps.packet_size))
                check_data(self.transport.bulk_read(self.caps.packet_size),
                           bytes(self.caps.packet_size), "traffic after alternate change")
        return {"cycles": self.iterations}

    def check_alternates(self) -> None:
        for alternate in (self.settings.inactive_alternate, self.settings.alternate):
            self.transport.set_alternate(alternate)
            if self.settings.profile == "cst1":
                actual = Capabilities.decode(self.transport.control_read(0x5d, 16)).alternate
            else:
                raw = self.transport.standard_read(0x81, 10, 0, self.settings.interface, 1)
                if len(raw) != 1:
                    raise StressError("Malformed GET_INTERFACE reply")
                actual = raw[0]
            if actual != alternate:
                raise StressError("Selected alternate setting was not committed")

    def duplex(self) -> dict[str, int]:
        self.select_mode(0)
        stop = threading.Event()
        length = self.caps.packet_size * 4

        def worker(input_direction: bool) -> int:
            total = 0
            try:
                for iteration in range(self.iterations):
                    if stop.is_set():
                        break
                    if input_direction:
                        check_data(self.transport.bulk_read(length), bytes(length), "concurrent bulk IN")
                    else:
                        self.write(pattern(self.seed + iteration, length))
                    total += length
            except StressError:
                stop.set()
                raise
            return total

        with self.subtest("Concurrent IN / OUT integrity"), ThreadPoolExecutor(max_workers=2) as pool:
            writer = pool.submit(worker, False)
            reader = pool.submit(worker, True)
            try:
                out_bytes, in_bytes = writer.result(), reader.result()
            finally:
                stop.set()
        return self.checked_counters(out_bytes, in_bytes)

    def soak(self) -> dict[str, int | float]:
        self.select_mode(1)
        start = time.monotonic()
        total = iterations = 0
        with self.subtest("Timed loopback integrity"):
            while time.monotonic() - start < self.duration:
                data = pattern(self.seed + iterations, self.caps.packet_size)
                self.write(data)
                check_data(self.transport.bulk_read(len(data)), data, "soak loopback")
                total += len(data)
                iterations += 1
            if iterations == 0:
                raise StressError("Soak did not execute any transfers")
        elapsed = time.monotonic() - start
        return dict(self.checked_counters(total, total), iterations=iterations, elapsed_seconds=elapsed,
                    aggregate_mib_per_second=2 * total / elapsed / (1024 * 1024))

    def chapter9(self) -> dict[str, int]:
        checks = 0

        def read(request_type: int, request: int, value: int, index: int, length: int) -> bytes:
            nonlocal checks
            checks += 1
            return self.transport.standard_read(request_type, request, value, index, length)

        for _ in range(self.iterations):
            with self.subtest("Device descriptor"):
                device = read(0x80, 6, 0x100, 0, 18)
                if len(device) != 18 or device[:2] != b"\x12\x01" or device[7] != 64 or device[17] != 1:
                    raise StressError("Invalid device descriptor")
            with self.subtest("Truncated device descriptors"):
                for length in (1, 8, 18, 64):
                    check_data(read(0x80, 6, 0x100, 0, length), device[:length],
                               "truncated device descriptor")
            with self.subtest("Configuration / alternate descriptors"):
                header = read(0x80, 6, 0x200, 0, 9)
                tree = read(0x80, 6, 0x200, 0, 255)
                if (len(header) != 9 or header[:2] != b"\x09\x02"
                        or len(tree) != 41 or int.from_bytes(header[2:4], "little") != len(tree)
                        or tree[:9] != header or tree[4:6] != b"\x01\x01"
                        or tree[9:18] != bytes([9, 4, 0, 0, 0, 255, 0, 0, 1])
                        or tree[18:27] != bytes([9, 4, 0, 1, 2, 255, 0, 0, 1])):
                    raise StressError("Invalid configuration/alternate descriptor tree")
            with self.subtest("Bulk endpoint descriptors"):
                for offset, endpoint in ((27, OUT), (34, IN)):
                    expected = bytes([7, 5, endpoint, 2]) + struct.pack("<H", self.caps.packet_size) + b"\0"
                    check_data(tree[offset:offset + 7], expected, "bulk endpoint descriptor")
            with self.subtest("GET_CONFIGURATION"):
                check_data(read(0x80, 8, 0, 0, 1), b"\x01", "GET_CONFIGURATION")
            with self.subtest("Device GET_STATUS"):
                status = read(0x80, 0, 0, 0, 2)
                check_data(status, bytes([int(bool(header[7] & 0x40)), 0]), "device GET_STATUS")
            with self.subtest("Interface / EP0 GET_STATUS"):
                for recipient, index in ((0x81, 0), (0x82, 0), (0x82, 0x80)):
                    check_data(read(recipient, 0, 0, index, 2), b"\0\0", "GET_STATUS")
            with self.subtest("Language descriptor"):
                languages = read(0x80, 6, 0x300, 0, 255)
                check_data(languages, b"\x04\x03\x09\x04", "language descriptor")
            with self.subtest("Interface string"):
                text = read(0x80, 6, 0x301, 0x409, 255)
                expected_text = "Caliptra USB stress".encode("utf-16-le")
                check_data(text, bytes([len(expected_text) + 2, 3]) + expected_text, "interface string")
            if self.caps.speed == 2:
                with self.subtest("Device qualifier"):
                    qualifier = read(0x80, 6, 0x600, 0, 10)
                    check_data(qualifier, b"\x0a\x06\x00\x02\x00\x00\x00\x40\x01\x00", "device qualifier")
                with self.subtest("Other-speed configuration"):
                    other = read(0x80, 6, 0x700, 0, 255)
                    expected_other = bytearray(tree)
                    expected_other[1] = 7
                    for offset in (31, 38):
                        expected_other[offset:offset + 2] = struct.pack("<H", 64)
                    check_data(other, bytes(expected_other), "other-speed configuration")
            else:
                for name in ("Device qualifier", "Other-speed configuration"):
                    self.skip_subtest(name, "This check is only exercised at high speed by this suite")
            with self.subtest("SET_INTERFACE / GET_INTERFACE"):
                for alternate in (0, 1):
                    self.transport.set_alternate(alternate)
                    check_data(read(0x81, 10, 0, 0, 1), bytes([alternate]), "GET_INTERFACE")
            invalid_reads = ((0x80, 6, 0xff00, 0, 8), (0x80, 6, 0x101, 0, 18),
                             (0x80, 6, 0x363, 0x409, 8))
            negative_names = ("Invalid descriptor type / STALL recovery",
                              "Invalid device descriptor index / STALL recovery",
                              "Invalid string index / STALL recovery")
            for name, arguments in zip(negative_names, invalid_reads):
                with self.subtest(name):
                    try:
                        read(*arguments)
                    except PipeHalted:
                        pass
                    else:
                        raise StressError(f"Invalid standard request did not STALL: {arguments}")
                    check_data(read(0x80, 8, 0, 0, 1), b"\x01", "EP0 recovery after STALL")
        with self.subtest("Post-check bulk loopback"):
            self.loopback()
        return {"iterations": self.iterations, "standard_read_attempts": checks}

    def queued_bulk(self) -> dict[str, int]:
        from usb_stress_advanced import Request
        self.select_mode(0)
        out_bytes = in_bytes = 0
        for iteration in range(self.iterations):
            for size in self.sizes:
                with self.subtest(f"{size} byte workload"):
                    read_length = ((size + self.caps.packet_size - 1) // self.caps.packet_size) * self.caps.packet_size
                    requests = []
                    for index in range(self.queue_depth):
                        requests.extend((Request(self.out_endpoint, pattern(self.seed + iteration + index, size)),
                                         Request(self.in_endpoint, read_length)))
                    results = self.transport.queued(requests)
                    self._completed(requests, results)
                    out_bytes += size * self.queue_depth
                    in_bytes += read_length * self.queue_depth
        return dict(self.checked_counters(out_bytes, in_bytes), queue_depth_per_direction=self.queue_depth)

    @staticmethod
    def _completed(requests: list[Request], results: list[Completion]) -> None:
        import usb1
        if len(results) != len(requests):
            raise StressError("Async completion count does not match submissions")
        for request, result in zip(requests, results):
            length = request.payload if isinstance(request.payload, int) else len(request.payload)
            if result.status != usb1.TRANSFER_COMPLETED or result.actual_length != length:
                raise StressError(f"Async transfer status/length: {result.status}/{result.actual_length}, "
                                  f"expected COMPLETED/{length}")
            if request.endpoint & 0x80 and request.control_request is None:
                check_data(result.data, bytes(length), "queued bulk source")

    def queued_control(self) -> dict[str, int]:
        from usb_stress_advanced import Request
        self.select_mode(0)
        data = pattern(self.seed, self.caps.control_max)
        with self.subtest("Store control pattern"):
            self.transport.control_write(0x5b, data)
        lengths = (1, 63, 64, 65, 512, 1024)
        total = 0
        for _ in range(self.iterations):
            with self.subtest("Queued EP0 read-back integrity"):
                requests = [Request(IN, lengths[index % len(lengths)], 0x5c)
                            for index in range(self.queue_depth)]
                results = self.transport.queued(requests)
                self._completed(requests, results)
                for result in results:
                    check_data(result.data, data[:result.actual_length], "queued control read-back")
                    total += result.actual_length
        return {"transfers": self.iterations * self.queue_depth, "in_bytes": total,
                "queue_depth": self.queue_depth}

    def cancel(self) -> dict[str, int]:
        cancelled = 0
        for _ in range(self.iterations):
            for endpoint in (IN, OUT):
                for depth in (1, self.queue_depth):
                    direction = "IN" if endpoint == IN else "OUT"
                    with self.subtest(f"{direction} cancellation (depth {depth}) / recovery"):
                        self.check_cancellation(endpoint, depth)
                        cancelled += depth
        return {"cancelled_transfers": cancelled, "queue_depth": self.queue_depth,
                "directions_tested": 2}

    def check_cancellation(self, endpoint: int, depth: int) -> None:
        from usb_stress_advanced import Request
        import usb1
        self.mode(1)
        packet = pattern(self.seed, self.caps.packet_size)
        requests = [Request(endpoint, self.caps.packet_size if endpoint == IN else packet)
                    for _ in range(depth)]
        # Prime after the async handle selects alternate 1 and resets its pipes.
        results = self.transport.queued(requests, cancel=True,
                                        prime=packet if endpoint == OUT else None)
        if len(results) != depth or any(
                result.status != usb1.TRANSFER_CANCELLED or result.actual_length != 0
                for result in results):
            raise StressError("NAK-blocked transfers did not complete as CANCELLED/zero bytes")
        if endpoint == OUT:
            self.counters(len(packet), 0)
        else:
            self.counters(0, 0)
        self.mode(1)
        self.write(packet)
        check_data(self.transport.bulk_read(len(packet)), packet, "loopback after cancellation")

    def port_cycle(self) -> dict[str, int | str]:
        for iteration in range(self.iterations):
            with self.subtest("Pre-cycle marker"):
                self.mode(1)
                marker = pattern(self.seed + iteration, 25)
                self.transport.control_write(0x5b, marker)
                check_data(self.transport.control_read(0x5c, len(marker)), marker, "pre-cycle marker")
            with self.subtest("Native port cycle"):
                self.transport.port_cycle()
            with self.subtest("Reset state / cleared marker"):
                caps = Capabilities.decode(self.transport.control_read(0x5d, 16))
                if caps.mode != 0 or caps.speed != self.caps.speed:
                    raise StressError("Port cycle did not restore the source/sink session and negotiated speed")
                # Drivers can restore alternate 1, but SET_CONFIGURATION does not clear this storage.
                check_data(self.transport.control_read(0x5c, len(marker)), b"", "reset-only stored marker")
            with self.subtest("Post-cycle control"):
                self.transport.control_write(0x5b, marker)
                check_data(self.transport.control_read(0x5c, len(marker)), marker, "post-cycle control")
            with self.subtest("Post-cycle loopback / accounting"):
                self.mode(1)
                packet = pattern(self.seed + iteration, self.caps.packet_size)
                self.write(packet)
                check_data(self.transport.bulk_read(len(packet)), packet, "post-cycle loopback")
                self.counters(len(packet), len(packet))
        return {"cycles": self.iterations, "mechanism": "native UsbK_ResetDevice port cycle",
                "isolated_bus_reset": "not tested"}

    def descriptors(self) -> dict[str, int]:
        read = self.transport.standard_read
        with self.subtest("Device descriptor / identity"):
            device = self.check_device_descriptor()
        with self.subtest("Configuration descriptor chains"):
            endpoint_count = self.check_configuration_chains(read, device[17])
        return {"configurations": device[17], "endpoint_descriptors": endpoint_count}

    def check_device_descriptor(self) -> bytes:
        read = self.transport.standard_read
        device = read(0x80, 6, 0x100, 0, 18)
        if len(device) != 18 or device[:2] != b"\x12\x01":
            raise StressError("Malformed device descriptor")
        if (int.from_bytes(device[8:10], "little") != self.settings.vendor_id
                or int.from_bytes(device[10:12], "little") != self.settings.product_id):
            raise StressError("Device descriptor VID/PID does not match selection")
        if device[17] == 0:
            raise StressError("Device advertises no configurations")
        return device

    def check_configuration_chains(self, read: Callable[[int, int, int, int, int], bytes],
                                   configurations: int) -> int:
        endpoint_count = 0
        for index in range(configurations):
            header = read(0x80, 6, 0x200 | index, 0, 9)
            if len(header) != 9 or header[:2] != b"\x09\x02":
                raise StressError("Malformed configuration header")
            length = int.from_bytes(header[2:4], "little")
            if length < 9:
                raise StressError("Invalid total configuration length")
            tree = read(0x80, 6, 0x200 | index, 0, length)
            if len(tree) != length or tree[:9] != header:
                raise StressError("Truncated or inconsistent configuration descriptor")
            offset = 0
            while offset < length:
                if length - offset < 2 or tree[offset] < 2 or offset + tree[offset] > length:
                    raise StressError("Malformed descriptor chain")
                endpoint_count += int(tree[offset + 1] == 5)
                offset += tree[offset]
        return endpoint_count


PROGRESS_PREFIX = "USBSTRESS_EVENT "


def run(settings: Settings, report_path: Path | None = None, progress_events: bool = False) -> int:
    settings.validate()
    tests = settings.tests
    now = datetime.now(timezone.utc)
    report_path = report_path or Path.cwd() / "results" / f"{now:%Y%m%d-%H%M%S-%f}.json"
    if report_path.exists():
        raise ValueError(f"Report already exists: {report_path}")
    report: dict = {
        "schema_version": 1, "started_utc": now.isoformat(),
        "parameters": asdict(settings),
        "tests": [dict(name=name, status="NOT_RUN", subtests=[
            dict(name=child, status="NOT_RUN", executions=0, elapsed_seconds=0.0)
            for child in subtest_names(name, settings)]) for name in tests], "overall": "FAIL",
        "unsupported_coverage": ["Linux kernel scatter-gather/pre-mapped DMA",
                                 "isolated device bus reset (port cycle is separate)",
                                 "selective unlink within a running queue",
                                 "raw Chapter 9 requests intercepted/rewritten by the OS driver",
                                 "interrupt/isochronous and suspend/remote wakeup"],
    }
    transport = None
    result = 1

    def progress(case: dict) -> None:
        if progress_events:
            print(PROGRESS_PREFIX + json.dumps(case), flush=True)

    try:
        # validate() guarantees IDs before opening a device.
        if settings.vendor_id is None or settings.product_id is None:
            raise StressError("Select a device VID/PID")
        transport = PyUsbTransport.open(settings.vendor_id, settings.product_id,
                                        settings.discovery_timeout, settings.timeout_ms, settings)
        suite = Suite(transport, settings.iterations, settings.sizes, settings.seed,
                      settings.duration, settings.queue_depth, settings)
        report["capabilities"] = asdict(suite.caps)
        report["capability_source"] = ("CST1 firmware handshake" if settings.profile == "cst1"
                                       else "Endpoint descriptor; behavior declared by user, no device counters")
        print(f"{settings.profile.upper()} device: packet={suite.caps.packet_size}", flush=True)
        for index, name in enumerate(tests):
            started = time.monotonic()
            suite.subtests = report["tests"][index]["subtests"]
            suite.subtest_callback = lambda child, parent=name: progress(
                dict(name=parent, subtest=child["name"], **{
                    key: value for key, value in child.items() if key != "name"}))
            progress(dict(name=name, status="RUNNING"))
            try:
                details = getattr(suite, name)()
            except (StressError, ImportError, OSError) as error:
                report["tests"][index] = dict(name=name, status="FAIL",
                                             elapsed_seconds=time.monotonic() - started, error=str(error),
                                             subtests=suite.subtests)
                progress(report["tests"][index])
                raise
            report["tests"][index] = dict(name=name, status="PASS",
                                         elapsed_seconds=time.monotonic() - started, details=details,
                                         subtests=suite.subtests)
            progress(report["tests"][index])
            print(f"PASS {name}: {details}", flush=True)
        report["overall"] = "PASS"
        result = 0
    except (StressError, ImportError, OSError) as error:
        report["error"] = str(error)
        print(f"USB_STRESS_FAILED: {error}", file=sys.stderr)
    except KeyboardInterrupt:
        report["error"] = "interrupted"
        if "index" in locals() and report["tests"][index]["status"] == "NOT_RUN":
            report["tests"][index].update(status="FAIL", error="interrupted")
            progress(report["tests"][index])
        result = 130
    finally:
        if transport is not None:
            try:
                transport.close()
            except StressError as error:
                report["overall"] = "FAIL"
                report["cleanup_error"] = str(error)
                print(f"USB cleanup failed: {error}", file=sys.stderr)
                result = 1
        try:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            with report_path.open("x", encoding="utf-8") as output:
                json.dump(report, output, indent=2)
                output.write("\n")
        except OSError as error:
            print(f"Could not write result report: {error}", file=sys.stderr)
            result = 1
        else:
            print(f"Report: {report_path}")
    return result


def main() -> int:
    if sys.platform == "win32":
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Saved GUI/CLI JSON configuration")
    parser.add_argument("--progress-events", action="store_true",
                        help="Emit flushed JSON per-test status events for GUI consumers")
    parser.add_argument("--profile", choices=("generic", "cst1"))
    parser.add_argument("--vendor-id", type=lambda text: int(text, 0))
    parser.add_argument("--product-id", type=lambda text: int(text, 0))
    parser.add_argument("--serial-number")
    for name in ("configuration", "interface", "alternate", "inactive-alternate",
                 "out-endpoint", "in-endpoint", "iterations", "seed", "timeout-ms", "queue-depth",
                 "bus", "address"):
        parser.add_argument("--" + name, type=lambda text: int(text, 0))
    for name in ("duration", "discovery-timeout"):
        parser.add_argument("--" + name, type=float)
    parser.add_argument("--behavior", choices=("source_sink", "loopback"))
    parser.add_argument("--allow-data-tests", action="store_true", default=None,
                        help="Authorize data writes and state changes to a cooperative generic device")
    parser.add_argument("--sizes")
    parser.add_argument("--tests", help="Comma-separated tests: " + ",".join(SUPPORTED_TESTS))
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        settings = Settings.load(args.config) if args.config else (
            Settings.cst1() if args.profile == "cst1" else Settings()
        )
        for name, value in vars(args).items():
            if name not in ("config", "report", "sizes", "tests", "progress_events") and value is not None:
                setattr(settings, name, value)
        if args.sizes is not None:
            settings.sizes = [int(value.strip(), 0) for value in args.sizes.split(",")]
        if args.tests is not None:
            settings.tests = [value.strip() for value in args.tests.split(",")]
        elif not args.config and settings.profile == "cst1":
            settings.tests = list(TESTS)
        return run(settings, args.report, progress_events=args.progress_events)
    except (ValueError, OSError, json.JSONDecodeError) as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
