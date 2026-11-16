# Licensed under the Apache-2.0 license

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import usb.core

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from usb_stress_discovery import discover_devices


class DiscoveryTests(unittest.TestCase):
    def test_lists_duplicate_ids_with_locations_and_releases_descriptor_handles(self):
        devices = [SimpleNamespace(idVendor=0x424, idProduct=7, bus=1, address=address,
                                   product="Test device", serial_number="") for address in (3, 5)]
        with patch("usb_stress_discovery.sys.platform", "linux"), patch(
                "usb.core.find", return_value=devices) as find, patch("usb.util.dispose_resources") as dispose:
            found = discover_devices()
        find.assert_called_once_with(find_all=True)
        self.assertEqual(len(found), 2)
        self.assertNotEqual(found[0].label, found[1].label)
        self.assertEqual(dispose.call_count, 2)

    def test_inaccessible_strings_remain_visible_with_explicit_warning(self):
        class Inaccessible:
            idVendor, idProduct, bus, address = 0x1234, 7, 1, 3
            @property
            def product(self):
                raise usb.core.USBError("Access denied")
            @property
            def serial_number(self):
                raise usb.core.USBError("Access denied")
        with patch("usb_stress_discovery.sys.platform", "linux"), patch(
                "usb.core.find", return_value=[Inaccessible()]), patch("usb.util.dispose_resources"):
            found = discover_devices()
        self.assertEqual(found[0].product, "USB device")
        self.assertIn("Access denied", found[0].warning)
        self.assertIn("descriptor access limited", found[0].label)
