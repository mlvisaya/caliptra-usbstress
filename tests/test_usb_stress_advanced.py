# Licensed under the Apache-2.0 license

from pathlib import Path
import sys
import unittest

import usb1

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from usb_stress_advanced import AdvancedError, AsyncDevice, Request


class Transfer:
    def __init__(self, fail=False):
        self.fail = fail
        self.pending = self.cancelled = self.closed = False
        self.callback = None

    def setBulk(self, endpoint, payload, callback, user_data, timeout):
        self.payload = payload
        self.callback = callback
        self.index = user_data

    def submit(self):
        if self.fail:
            raise usb1.USBErrorIO()
        self.pending = True

    def cancel(self):
        self.cancelled = True

    def isSubmitted(self):
        return self.pending

    def getStatus(self):
        return usb1.TRANSFER_CANCELLED if self.cancelled else usb1.TRANSFER_COMPLETED

    def getUserData(self):
        return self.index

    def getActualLength(self):
        return 0 if self.cancelled else self.payload

    def getBuffer(self):
        return bytes(self.payload)

    def close(self):
        if self.pending:
            raise AssertionError("Transfer freed before cancellation completion")
        self.closed = True


class Handle:
    def __init__(self, fail_index=None):
        self.fail_index = fail_index
        self.transfers = []

    def getTransfer(self):
        transfer = Transfer(len(self.transfers) == self.fail_index)
        self.transfers.append(transfer)
        return transfer


class Context:
    def __init__(self, handle, blocked):
        self.handle = handle
        self.blocked = blocked
        self.maximum_pending = 0

    def handleEventsTimeout(self, timeout):
        self.maximum_pending = max(
            self.maximum_pending, sum(t.pending for t in self.handle.transfers)
        )
        for transfer in self.handle.transfers:
            if transfer.pending and (not self.blocked or transfer.cancelled):
                transfer.pending = False
                transfer.callback(transfer)


class AsyncTests(unittest.TestCase):
    def device(self, blocked=False, fail_index=None):
        handle = Handle(fail_index)
        device = AsyncDevice.__new__(AsyncDevice)
        device.handle = handle
        device.context = Context(handle, blocked)
        device.timeout_ms = 100
        return device

    def test_submits_full_queue_before_dispatching_callbacks(self):
        device = self.device()
        result = device.run([Request(0x81, 64) for _ in range(8)])
        self.assertEqual(device.context.maximum_pending, 8)
        self.assertEqual(len(result), 8)
        self.assertTrue(all(t.closed for t in device.handle.transfers))

    def test_cancel_waits_for_callbacks_before_freeing_transfers(self):
        device = self.device(blocked=True)
        result = device.run([Request(0x81, 64) for _ in range(4)], cancel=True)
        self.assertTrue(all(r.status == usb1.TRANSFER_CANCELLED for r in result))
        self.assertTrue(all(t.closed and not t.pending for t in device.handle.transfers))

    def test_submit_failure_cancels_and_drains_prior_requests(self):
        device = self.device(blocked=True, fail_index=2)
        with self.assertRaises(usb1.USBErrorIO):
            device.run([Request(0x81, 64) for _ in range(4)])
        self.assertTrue(all(t.closed and not t.pending for t in device.handle.transfers))
        self.assertTrue(device.handle.transfers[0].cancelled)

    def test_cancel_rejects_transfers_which_already_completed(self):
        device = self.device()
        with self.assertRaisesRegex(AdvancedError, "NAK-blocked"):
            device.run([Request(0x81, 64)], cancel=True)
        self.assertTrue(device.handle.transfers[0].closed)


if __name__ == "__main__":
    unittest.main()
