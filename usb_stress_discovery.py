# Licensed under the Apache-2.0 license
"""Read-only USB discovery; never claim an interface or change configuration."""

from dataclasses import dataclass
import sys

import usb.core
import usb.util


@dataclass(frozen=True)
class DiscoveredDevice:
    vendor_id: int
    product_id: int
    bus: int | None
    address: int | None
    product: str
    serial: str
    warning: str = ""

    @property
    def label(self) -> str:
        location = f"bus {self.bus}, address {self.address}"
        serial = f" | serial {self.serial}" if self.serial else ""
        warning = " | descriptor access limited" if self.warning else ""
        return f"{self.vendor_id:04X}:{self.product_id:04X} | {self.product} | {location}{serial}{warning}"


def discover_devices() -> list[DiscoveredDevice]:
    if sys.platform == "win32":
        import libusb_package
        devices = libusb_package.find(find_all=True)
    else:
        devices = usb.core.find(find_all=True)
    found = []
    for device in devices:
        strings = {}
        warnings = []
        try:
            for name in ("product", "serial_number"):
                try:
                    strings[name] = getattr(device, name) or ""
                except (usb.core.USBError, ValueError, NotImplementedError) as error:
                    strings[name] = ""
                    warnings.append(f"{name}: {error}")
            found.append(DiscoveredDevice(
                device.idVendor, device.idProduct, device.bus, device.address,
                strings["product"] or "USB device", strings["serial_number"], "; ".join(warnings),
            ))
        finally:
            usb.util.dispose_resources(device)
    return sorted(found, key=lambda item: (item.vendor_id, item.product_id,
                                          item.bus or 0, item.address or 0))
