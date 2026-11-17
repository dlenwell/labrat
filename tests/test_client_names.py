# Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
"""Check client naming and distrust of catch-all controller fingerprints."""
import unittest

from labrat.collector.clients import client_name, distrust_catch_all_fingerprints, unifi_product_labels

MAC = '02:00:00:00:12:6c'


def record(**changes):
    """Make one sanitized client record with a synthetic MAC.

    Args:
        **changes: Fields to add or override.

    Returns:
        dict: The client record.
    """
    return {'mac_address': MAC, 'classic_available': True, **changes}


class ClientNameTests(unittest.TestCase):
    """Names never carry UniFi's MAC-octet suffix and prefer real identity."""

    vendors = {'phonico', 'consolia'}

    def test_alias_wins(self):
        self.assertEqual(client_name(record(hostname='host'), 'Desk lamp', 'host 12:6c', self.vendors), 'Desk lamp')

    def test_hostname_without_suffix(self):
        self.assertEqual(client_name(record(hostname='laptop'), None, 'laptop 12:6c', self.vendors), 'laptop')

    def test_foreign_vendor_hostname_yields_to_model(self):
        console = record(hostname='Phonico', device_model='Example Console', device_vendor='Consolia')
        self.assertEqual(client_name(console, None, None, self.vendors), 'Example Console')
        phone = record(hostname='Phonico', device_model='Example Phone', device_vendor='Phonico Inc')
        self.assertEqual(client_name(phone, None, None, self.vendors), 'Phonico')

    def test_fallbacks(self):
        self.assertEqual(client_name(record(device_model='Example Hub'), None, 'Example Hub 12:6c', self.vendors), 'Example Hub')
        self.assertEqual(client_name(record(manufacturer='Example Maker'), None, 'Example Maker 12:6c', self.vendors), 'Example Maker')
        self.assertEqual(client_name(record(classic_available=False), None, 'reported 12:6c', self.vendors), 'reported')
        self.assertEqual(client_name(record(), None, None, self.vendors), MAC)


class CatchAllFingerprintTests(unittest.TestCase):
    """One fingerprint across three or more MAC vendors is not trusted."""

    def test_catch_all_labels_dropped(self):
        labels = {'fingerprint_id': 7, 'device_model': 'Example UPS card', 'device_vendor': 'Example Power',
                  'device_family': 'Network', 'device_category': 'Peripheral', 'device_vendor_id': '9'}
        rows = [record(manufacturer=maker, **labels) for maker in ('Alpha Inc', 'Beta Ltd', 'Gamma GmbH', '')]
        single = record(manufacturer='Delta Co', fingerprint_id=8, device_model='Example Console')
        distrust_catch_all_fingerprints([*rows, single])
        for row in rows:
            self.assertNotIn('device_model', row)
            self.assertNotIn('device_vendor_id', row)
            self.assertTrue(row['fingerprint_ambiguous'])
            self.assertEqual(row['fingerprint_id'], 7)
        self.assertEqual(single['device_model'], 'Example Console')

    def test_two_vendors_kept(self):
        rows = [record(manufacturer=maker, fingerprint_id=7, device_model='Example Plug') for maker in ('Alpha Inc', 'Alpha Systems', 'Beta Ltd')]
        distrust_catch_all_fingerprints(rows)
        self.assertTrue(all(row['device_model'] == 'Example Plug' for row in rows))


class UniFiProductTests(unittest.TestCase):
    """UniFi's own product record identifies its cameras."""

    def test_protect_camera(self):
        labels, name = unifi_product_labels({'unifi_device_info_from_ucore': {
            'product_line': 'PROTECT', 'product_model': 'Example Cam', 'product_shortname': 'UVC Example Cam',
            'name': 'Porch camera', 'icons': [{'url': 'https://example.test/icon.png'}], 'fw_version': 'x'}})
        self.assertEqual(name, 'Porch camera')
        self.assertEqual(labels, {'product_line': 'PROTECT', 'device_model': 'Example Cam', 'device_category': 'Camera',
                                  'device_family': 'Camera', 'device_vendor': 'Ubiquiti'})

    def test_other_or_missing(self):
        self.assertEqual(unifi_product_labels({}), ({}, None))
        labels, _ = unifi_product_labels({'unifi_device_info_from_ucore': {'product_line': 'PROTECT', 'product_shortname': 'UP Chime'}})
        self.assertNotIn('device_category', labels)


if __name__ == '__main__':
    unittest.main()
