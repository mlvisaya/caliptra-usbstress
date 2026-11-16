# Licensed under the Apache-2.0 license
"""Real libusb asynchronous transfers and the native Windows port-cycle API."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import struct
import sys
import time

import usb1
from usb_stress_config import Settings


class AdvancedError(RuntimeError):
    pass


@dataclass(frozen=True)
class Request:
    endpoint: int
    payload: bytes | int
    control_request: int | None = None


@dataclass(frozen=True)
class Completion:
    status: int
    actual_length: int
    data: bytes


class AsyncDevice:
    def __init__(self, vendor: int, product: int, timeout_ms: int,
                 settings: Settings | None = None):
        self.vendor = vendor
        self.product = product
        self.timeout_ms = timeout_ms
        self.context = usb1.USBContext()
        self.handle: usb1.USBDeviceHandle | None = None
        self.claimed = False
        self.settings = settings or Settings.cst1(vendor_id=vendor, product_id=product)

    def __enter__(self) -> AsyncDevice:
        try:
            self.context.open()
            devices = [device for device in self.context.getDeviceList()
                       if device.getVendorID() == self.vendor and device.getProductID() == self.product
                       and (self.settings.bus is None or device.getBusNumber() == self.settings.bus)
                       and (self.settings.address is None or device.getDeviceAddress() == self.settings.address)]
            if self.settings.serial_number:
                selected = []
                for device in devices:
                    with device.open() as handle:
                        if handle.getSerialNumber() == self.settings.serial_number:
                            selected.append(device)
                devices = selected
            if len(devices) != 1:
                raise AdvancedError(f"Async discovery expected one selected device, found {len(devices)}")
            self.handle = handle = devices[0].open()
            if handle.getConfiguration() != self.settings.configuration:
                raise AdvancedError("Async tests require the already-configured stress device")
            if self.settings.profile == "cst1":
                raw = bytes(handle.controlRead(0xc0, 0x5d, 0, 0, 16, timeout=self.timeout_ms))
                if len(raw) != 16 or raw[:5] != b"CST1\x01":
                    raise AdvancedError("Async device did not return the CST1 handshake")
                _, _, _, alternate, speed, packet, maximum, features = struct.unpack("<4sBBBBHHI", raw)
                if (alternate != 1 or speed not in (1, 2)
                        or packet != (64 if speed == 1 else 512)
                        or maximum != 1024 or features & 15 != 15):
                    raise AdvancedError("Async device capabilities do not match the selected stress session")
            handle.claimInterface(self.settings.interface)
            self.claimed = True
            handle.setInterfaceAltSetting(self.settings.interface, self.settings.alternate)
            return self
        except (usb1.USBError, AdvancedError):
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_args) -> None:
        try:
            if self.handle is not None:
                try:
                    if self.claimed:
                        self.handle.releaseInterface(self.settings.interface)
                finally:
                    self.handle.close()
                    self.handle = None
        finally:
            self.context.close()

    def _events(self) -> None:
        try:
            self.context.handleEventsTimeout(0.05)
        except usb1.USBErrorInterrupted:
            pass

    def run(self, requests: list[Request], cancel: bool = False,
            prime: bytes | None = None) -> list[Completion]:
        if self.handle is None:
            raise AdvancedError("Async handle is not open")
        transfers: list[usb1.USBTransfer] = []
        results: dict[int, Completion] = {}

        def completed(transfer: usb1.USBTransfer) -> None:
            index = transfer.getUserData()
            length = transfer.getActualLength()
            results[index] = Completion(
                transfer.getStatus(), length, bytes(transfer.getBuffer()[:length])
            )

        try:
            if prime is not None:
                written = self.handle.bulkWrite(1, prime, timeout=self.timeout_ms)
                if written != len(prime):
                    raise AdvancedError(f"Short cancellation priming write: {written}/{len(prime)}")
            for index, request in enumerate(requests):
                transfer = self.handle.getTransfer()
                transfers.append(transfer)
                if request.control_request is None:
                    transfer.setBulk(request.endpoint, request.payload, callback=completed,
                                     user_data=index, timeout=self.timeout_ms)
                else:
                    transfer.setControl(0xc0, request.control_request, 0, 0, request.payload,
                                        callback=completed, user_data=index, timeout=self.timeout_ms)
                transfer.submit()
            if cancel:
                self._events()
                if results:
                    raise AdvancedError("Cancellation setup did not leave all transfers NAK-blocked")
                for transfer in transfers:
                    transfer.cancel()
            deadline = time.monotonic() + self.timeout_ms / 1000 + 2
            while len(results) != len(transfers):
                if time.monotonic() >= deadline:
                    raise AdvancedError("Async completion deadline exceeded")
                self._events()
            return [results[index] for index in range(len(requests))]
        finally:
            for transfer in transfers:
                if transfer.isSubmitted():
                    try:
                        transfer.cancel()
                    except usb1.USBErrorNotFound:
                        # Completion may already be queued, but must still be drained.
                        pass
            deadline = time.monotonic() + self.timeout_ms / 1000 + 2
            while any(transfer.isSubmitted() for transfer in transfers):
                if time.monotonic() >= deadline:
                    raise AdvancedError("Async cleanup deadline exceeded; transfers are still pending")
                self._events()
            for transfer in transfers:
                transfer.close()


def port_cycle(vendor: int, product: int) -> None:
    if sys.platform != "win32":
        raise AdvancedError("port_cycle requires Windows and the libusbK device driver")
    dll = ctypes.WinDLL("libusbK.dll", use_last_error=True)
    pointer = ctypes.c_void_p
    for name, arguments in (
        ("LstK_Init", [ctypes.POINTER(pointer), ctypes.c_int]),
        ("LstK_FindByVidPid", [pointer, ctypes.c_int, ctypes.c_int, ctypes.POINTER(pointer)]),
        ("LstK_Free", [pointer]),
        ("UsbK_Init", [ctypes.POINTER(pointer), pointer]),
        ("UsbK_ResetDevice", [pointer]),
        ("UsbK_Free", [pointer]),
    ):
        function = getattr(dll, name)
        function.argtypes = arguments
        function.restype = ctypes.c_int

    def check(ok: int) -> None:
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    device_list = pointer()
    handle = pointer()
    check(dll.LstK_Init(ctypes.byref(device_list), 0))
    try:
        info = pointer()
        check(dll.LstK_FindByVidPid(device_list, vendor, product, ctypes.byref(info)))
        check(dll.UsbK_Init(ctypes.byref(handle), info))
        try:
            check(dll.UsbK_ResetDevice(handle))
        finally:
            check(dll.UsbK_Free(handle))
    finally:
        check(dll.LstK_Free(device_list))
