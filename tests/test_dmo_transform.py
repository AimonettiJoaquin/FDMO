import math
import struct
import unittest
from unittest.mock import Mock, patch

from dmo_transform import TransformReader, UNITY_PLAYER, apply_trs, verify_engine, engine_sections
from tests.test_monitor_dmo import FakeMemory


class TransformTests(unittest.TestCase):
    def setUp(self):
        self.mem = FakeMemory()
        self.mem.maps += [(0x18000, 0x18100, 'r--p', str(UNITY_PLAYER)),
                          (0x19000, 0x19100, 'r-xp', str(UNITY_PLAYER))]
        self.mem.put(0x18000, '<Q', 0x19000)
        for obj in (0x1000, 0x2000, 0x4000):
            self.mem.put(obj, '<Q', 0x18000)
        self.mem.put(0x1020, '<Q', 0x2000)
        self.mem.put(0x2020, '<Q', 0x3000)
        self.mem.put(0x3008, '<Q', 0x4000)
        self.mem.put(0x4020, '<Q', 0x2000)
        self.mem.put(0x4028, '<Q', 0x5000)
        self.mem.put(0x4030, '<i', 1)
        self.mem.put(0x5018, '<Q', 0x6000)
        self.mem.put(0x5020, '<Q', 0x7000)
        self.mem.put(0x7000, '<2i', -1, 0)
        self.mem.put(0x6000, '<12f', 10, 20, 0, 0, 0, 0, 0, 1, 2, 2, 2, 0)
        self.mem.put(0x6030, '<12f', 3, 4, 0, 0, 0, 0, 0, 1, 1, 1, 1, 0)
        with (patch('dmo_transform.verify_engine'),
              patch('dmo_transform.engine_sections', return_value=[
                  (0x18000, 0x18100, False), (0x19000, 0x19100, True)])):
            self.reader = TransformReader(self.mem)

    def test_reads_local_and_composes_hierarchy_without_claiming_pixels(self):
        result = self.reader.read(0x1000)
        self.assertEqual(result['local_position'], [3, 4, 0])
        self.assertEqual(result['world_position_candidate'], [16, 28, 0])
        self.assertEqual(len(result['hierarchy']), 2)
        self.assertIsNone(result['screen_position'])
        self.assertFalse(result['live_validated'])
        self.assertFalse(result['executable'])

    def test_parent_rotation_and_scale(self):
        h = math.sqrt(0.5)
        result = apply_trs([1, 0, 0], [10, 20, 0], [0, 0, h, h], [2, 3, 1])
        for actual, expected in zip(result, [10, 22, 0]):
            self.assertAlmostEqual(actual, expected)

    def test_component_object_link_uses_pointer_not_allocation_distance(self):
        result = self.reader.component_game_object(0x1000)
        self.assertTrue(result['verified'])
        self.assertEqual(result['game_object'], '0x2000')

    def test_component_object_link_rejects_foreign_game_object(self):
        self.mem.put(0x2000, '<Q', 0x17000)
        with self.assertRaisesRegex(ValueError, 'UnityPlayer'):
            self.reader.component_game_object(0x1000)

    def test_component_object_link_rejects_change(self):
        original = self.mem.read
        calls = 0
        def changing(address, size):
            nonlocal calls
            if address == 0x1020:
                calls += 1
                if calls == 2:
                    self.mem.put(address, '<Q', 0x4000)
            return original(address, size)
        self.mem.read = changing
        with self.assertRaisesRegex(ValueError, 'cambió'):
            self.reader.component_game_object(0x1000)

    def test_rejects_parent_cycles(self):
        self.mem.put(0x7000, '<i', 1)
        with self.assertRaisesRegex(ValueError, 'cíclica'):
            self.reader.read(0x1000)

    def test_rejects_wrong_game_object(self):
        self.mem.put(0x4020, '<Q', 0x2008)
        with self.assertRaisesRegex(ValueError, 'no pertenece'):
            self.reader.read(0x1000)

    def test_rejects_pending_native_job(self):
        self.mem.put(0x5000, '<Q', 123)
        with self.assertRaisesRegex(ValueError, 'trabajo pendiente'):
            self.reader.read(0x1000)

    def test_rejects_invalid_index_and_nonfinite_position(self):
        self.mem.put(0x4030, '<i', 1000000)
        with self.assertRaisesRegex(ValueError, 'límites'):
            self.reader.read(0x1000)
        self.mem.put(0x4030, '<i', 1)
        self.mem.put(0x6030, '<f', float('nan'))
        with self.assertRaisesRegex(ValueError, 'no finitos'):
            self.reader.read(0x1000)

    def test_rejects_changed_data(self):
        original = self.mem.read
        calls = 0
        def changing(address, size):
            nonlocal calls
            if address == 0x6030:
                calls += 1
                if calls == 2:
                    self.mem.put(address, '<f', 7)
            return original(address, size)
        self.mem.read = changing
        with self.assertRaisesRegex(ValueError, 'cambió'):
            self.reader.read(0x1000)

    def test_rejects_foreign_vtable(self):
        self.mem.put(0x4000, '<Q', 0x17000)
        with self.assertRaisesRegex(ValueError, 'UnityPlayer'):
            self.reader.read(0x1000)

    def test_accepts_verified_sections_with_anonymous_maps(self):
        self.mem.maps = [(a, b, perms, '') for a, b, perms, _ in self.mem.maps]
        self.assertEqual(self.reader.read(0x1000)['local_position'], [3, 4, 0])

    def test_code_pointer_requires_executable_mapping(self):
        self.mem.maps = [(a, b, perms.replace('x', '-'), path)
                         for a, b, perms, path in self.mem.maps]
        with self.assertRaisesRegex(ValueError, 'UnityPlayer'):
            self.reader.read(0x1000)

    def test_rejects_different_engine_build(self):
        with self.assertRaisesRegex(ValueError, 'cambió'):
            verify_engine(Mock(read_bytes=Mock(return_value=b'another build')))


class WineImageTests(unittest.TestCase):
    def setUp(self):
        self.data = bytearray(0x400)
        self.data[:2] = b'MZ'
        struct.pack_into('<I', self.data, 60, 0x80)
        self.data[0x80:0x84] = b'PE\0\0'
        struct.pack_into('<H', self.data, 0x86, 2)
        struct.pack_into('<H', self.data, 0x94, 0xf0)
        struct.pack_into('<H', self.data, 0x98, 0x20b)
        struct.pack_into('<Q', self.data, 0xb0, 0x180000000)
        struct.pack_into('<I', self.data, 0xd4, 0x400)
        for i, (rva, flags) in enumerate(((0x1000, 0x60000000), (0x2000, 0x40000000))):
            off = 0x80 + 24 + 0xf0 + i*40
            struct.pack_into('<3I', self.data, off + 8, 0x100, rva, 0x100)
            struct.pack_into('<I', self.data, off + 36, flags)
        self.mem = FakeMemory()
        self.mem.data[0x1000:0x1400] = self.data
        self.mem.maps = [(0x1000, 0x2000, 'r--p', str(UNITY_PLAYER)),
                         (0x2000, 0x3000, 'r-xp', ''), (0x3000, 0x4000, 'r--p', '')]

    def test_header_identifies_anonymous_wine_sections(self):
        self.assertEqual(engine_sections(self.mem, bytes(self.data)),
                         [(0x2000, 0x2100, True), (0x3000, 0x3100, False)])

    def test_wine_relocated_image_base_is_accepted(self):
        self.mem.put(0x10b0, '<Q', 0x1000)
        self.assertEqual(engine_sections(self.mem, bytes(self.data)),
                         [(0x2000, 0x2100, True), (0x3000, 0x3100, False)])

    def test_unrelated_image_base_is_rejected(self):
        self.mem.put(0x10b0, '<Q', 0x9990000)
        with self.assertRaisesRegex(ValueError, 'ImageBase .* no coincide'):
            engine_sections(self.mem, bytes(self.data))

    def test_relocation_does_not_hide_other_changes(self):
        self.mem.put(0x10b0, '<Q', 0x1000)
        self.mem.data[0x128c] ^= 1  # Section table must remain identical.
        with self.assertRaisesRegex(ValueError, 'fuera de ImageBase; offsets 0x28c'):
            engine_sections(self.mem, bytes(self.data))

    def test_different_or_ambiguous_header_rejected(self):
        self.mem.data[0x1002] = 1
        with self.assertRaisesRegex(ValueError, 'cabecera'):
            engine_sections(self.mem, bytes(self.data))
        self.mem.data[0x1002] = 0
        self.mem.data[0x5000:0x5400] = self.data
        self.mem.maps.append((0x5000, 0x6000, 'r--p', str(UNITY_PLAYER)))
        with self.assertRaisesRegex(ValueError, 'cabecera'):
            engine_sections(self.mem, bytes(self.data))

    def test_wrong_header_path_rejected(self):
        self.mem.maps[0] = (0x1000, 0x2000, 'r--p', '/other/UnityPlayer.dll')
        with self.assertRaisesRegex(ValueError, 'cabecera'):
            engine_sections(self.mem, bytes(self.data))


if __name__ == '__main__':
    unittest.main()
