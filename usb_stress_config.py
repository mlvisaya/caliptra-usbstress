# Licensed under the Apache-2.0 license
"""Validated, portable application settings shared by the CLI and GUI."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
import json
import math
from pathlib import Path


CST1_TESTS = ("control", "bulk", "loopback", "halt", "alternate", "duplex", "soak",
              "chapter9", "queued_bulk", "queued_control", "cancel", "port_cycle", "descriptors")
GENERIC_TESTS = ("descriptors", "bulk", "loopback", "halt", "alternate", "duplex",
                 "soak", "queued_bulk")
SOURCE_TESTS = {"bulk", "halt", "alternate", "duplex", "queued_bulk"}
LOOPBACK_TESTS = {"loopback", "soak"}


@dataclass
class Settings:
    profile: str = "generic"
    vendor_id: int | None = None
    product_id: int | None = None
    serial_number: str = ""
    bus: int | None = None
    address: int | None = None
    configuration: int = 1
    interface: int = 0
    alternate: int = 0
    inactive_alternate: int = 1
    out_endpoint: int = 0x01
    in_endpoint: int = 0x81
    behavior: str = "source_sink"
    allow_data_tests: bool = False
    tests: list[str] = field(default_factory=lambda: ["descriptors"])
    iterations: int = 4
    sizes: list[int] = field(default_factory=lambda: [1, 63, 64, 65, 511, 512, 513, 4096])
    seed: int = 1
    duration: float = 10
    timeout_ms: int = 5000
    discovery_timeout: float = 15
    queue_depth: int = 8

    @classmethod
    def cst1(cls, **values) -> Settings:
        return cls(profile="cst1", alternate=1, inactive_alternate=0, **values)

    @property
    def needs_bulk(self) -> bool:
        return any(test != "descriptors" for test in self.tests)

    def validate(self) -> None:
        if self.profile not in ("generic", "cst1"):
            raise ValueError("Profile must be generic or cst1")
        integer_ranges = {
            "vendor_id": (1, 0xffff), "product_id": (0, 0xffff),
            "configuration": (1, 255), "interface": (0, 255),
            "alternate": (0, 255), "inactive_alternate": (0, 255),
            "iterations": (1, 1000000), "timeout_ms": (1, 60000),
            "queue_depth": (2, 64), "seed": (0, 0xffffffff),
        }
        for name, (minimum, maximum) in integer_ranges.items():
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"{name} must be an integer in {minimum}..{maximum}")
        for name in ("bus", "address"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or not 0 <= value <= 255):
                raise ValueError(f"{name} must be null or an integer in 0..255")
        for name, minimum, maximum in (("out_endpoint", 1, 15), ("in_endpoint", 0x81, 0x8f)):
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"{name} has an invalid direction or endpoint number")
        if not isinstance(self.serial_number, str):
            raise ValueError("serial_number must be a string")
        if type(self.allow_data_tests) is not bool:
            raise ValueError("allow_data_tests must be boolean")
        if self.behavior not in ("source_sink", "loopback"):
            raise ValueError("behavior must be source_sink or loopback")
        for name in ("duration", "discovery_timeout"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 86400:
                raise ValueError(f"{name} must be finite and between 0 and 86400 seconds")
        supported = CST1_TESTS if self.profile == "cst1" else GENERIC_TESTS
        if (not isinstance(self.tests, list) or not self.tests
                or any(not isinstance(test, str) or test not in supported for test in self.tests)
                or len(set(self.tests)) != len(self.tests)):
            raise ValueError(f"Select unique supported tests: {', '.join(supported)}")
        if (not isinstance(self.sizes, list) or not self.sizes
                or any(type(size) is not int or not 1 <= size <= 1048576 for size in self.sizes)):
            raise ValueError("sizes must contain integers in 1..1048576")
        if self.profile == "generic" and self.needs_bulk:
            if not self.allow_data_tests:
                raise ValueError("Generic data tests require explicit allow_data_tests consent")
            if self.behavior == "source_sink" and set(self.tests) & LOOPBACK_TESTS:
                raise ValueError("Loopback/soak require a declared loopback device")
            if self.behavior == "loopback" and set(self.tests) & SOURCE_TESTS:
                raise ValueError("Bulk/halt/alternate/duplex/queued_bulk require a zero-source/OUT-sink device")
        if "alternate" in self.tests and self.alternate == self.inactive_alternate:
            raise ValueError("alternate and inactive_alternate must differ for alternate testing")
        if self.profile == "cst1" and (
                self.interface != 0 or self.configuration != 1 or self.alternate != 1
                or self.inactive_alternate != 0 or self.out_endpoint != 1 or self.in_endpoint != 0x81):
            raise ValueError("CST1 protocol requires configuration 1, interface 0, alternates 0/1, endpoints 01/81")

    @classmethod
    def load(cls, path: Path) -> Settings:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise ValueError("Configuration must be a schema_version 1 JSON object")
        values = raw.get("settings")
        if not isinstance(values, dict):
            raise ValueError("Configuration requires a settings object")
        unknown = set(values) - {item.name for item in fields(cls)}
        if unknown:
            raise ValueError(f"Unknown configuration fields: {', '.join(sorted(unknown))}")
        settings = cls(**values)
        settings.validate()
        return settings

    def save(self, path: Path) -> None:
        self.validate()
        path.write_text(json.dumps({"schema_version": 1, "settings": asdict(self)}, indent=2) + "\n",
                        encoding="utf-8")
