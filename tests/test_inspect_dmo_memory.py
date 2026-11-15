import struct
import unittest
from unittest.mock import Mock, patch

from inspect_dmo_memory import Memory, validate_candidate, metadata_types


class MetadataNameTests(unittest.TestCase):
    def metadata(self):
        data = bytearray(1024)
        struct.pack_into('<II', data, 0, 0xFAB11BAF, 31)
        strings = b'\0Misc\0UI\0globalWidgets\0'
        data[200:200 + len(strings)] = strings
        for section, offset, size in ((2, 200, len(strings)), (11, 250, 0), (19, 300, 264)):
            struct.pack_into('<II', data, 8 + section * 8, offset, size)
        for index, namespace in enumerate((0, 6, 9)):
            row = [0] * 26
            row[0], row[1] = 1, namespace
            struct.pack_into('<16i8H2I', data, 300 + index * 88, *row)
        return bytes(data)

    def test_ambiguous_short_name_rejected(self):
        with patch('inspect_dmo_memory.METADATA', Mock(read_bytes=Mock(return_value=self.metadata()))):
            with self.assertRaisesRegex(ValueError, 'Ambiguous'):
                metadata_types(('Misc',))

    def test_qualified_names_resolve_exact_namespace(self):
        with patch('inspect_dmo_memory.METADATA', Mock(read_bytes=Mock(return_value=self.metadata()))):
            result = metadata_types(('global::Misc', 'UI::Misc', 'globalWidgets::Misc'))
        self.assertEqual([x['index'] for x in result.values()], [0, 1, 2])


class MemoryScanTests(unittest.TestCase):
    def memory(self):
        mem = Memory.__new__(Memory)
        mem.page_size = 4096
        mem.pagemap_fd = 99
        mem.maps = [(0x10000, 0x13000, 'rw-p', '')]
        return mem

    def test_unallocated_pages_do_not_consume_scan_budget(self):
        mem = self.memory()
        mem.read = Mock(return_value=struct.pack('<Q', 0x3188de40) + bytes(4088))
        entries = struct.pack('<3Q', 0, 0, 1 << 63)
        with patch('inspect_dmo_memory.os.pread', return_value=entries):
            hits = list(mem.find_pointers({'manager': 0x3188de40}, 4096))
        self.assertEqual(hits, [('manager', 0x12000)])
        mem.read.assert_called_once_with(0x12000, 4096)
        self.assertTrue(mem.scan_complete)
        self.assertEqual(mem.scan_stats['skipped_unallocated_bytes'], 8192)

    def test_swapped_pages_are_included(self):
        mem = self.memory()
        entries = struct.pack('<3Q', 1 << 62, 1 << 63, 0)
        with patch('inspect_dmo_memory.os.pread', return_value=entries):
            self.assertEqual(list(mem.allocated_ranges(0x10000, 12288)), [(0x10000, 8192)])

    def test_pagemap_failure_falls_back_to_reading_range(self):
        mem = self.memory()
        with patch('inspect_dmo_memory.os.pread', side_effect=PermissionError):
            self.assertEqual(list(mem.allocated_ranges(0x10000, 12288)), [(0x10000, 12288)])

    def test_budget_exhaustion_is_reported(self):
        mem = self.memory()
        mem.pagemap_fd = None
        mem.read = Mock()
        self.assertEqual(list(mem.find_pointers({'manager': 123}, 4096)), [])
        self.assertFalse(mem.scan_complete)
        self.assertEqual(mem.scan_stats['stop_reason'], 'budget')
        self.assertEqual(mem.scan_stats['next_address'], '0x10000')
        mem.read.assert_not_called()

    def test_scan_reaches_later_regions_with_sufficient_budget(self):
        mem = self.memory()
        mem.pagemap_fd = None
        mem.maps.append((0x20000, 0x21000, 'rw-p', ''))
        mem.read = Mock(side_effect=[bytes(12288), struct.pack('<Q', 123) + bytes(4088)])
        self.assertEqual(list(mem.find_pointers({'manager': 123}, 16384)),
                         [('manager', 0x20000)])
        self.assertTrue(mem.scan_complete)
        self.assertEqual(mem.scan_stats['stop_reason'], 'completed')
        self.assertEqual(mem.scan_stats['eligible_mapped_bytes'], 16384)
        self.assertEqual(mem.scan_stats['bytes_read'], 16384)

    def test_class_internal_pointer_is_not_an_object(self):
        with self.assertRaisesRegex(ValueError, 'metadata'):
            validate_candidate(Mock(), 'DuelSideHelper', 0x31814ee8, {},
                               {'DuelSideHelper': {'address': 0x31814ea0}})

    def test_scan_prioritizes_live_object_blocks_without_duplicates(self):
        mem = self.memory()
        chunk = 4 * 1024 * 1024
        mem.maps = [(0, chunk * 6, 'rw-p', '')]
        blocks = list(mem.scan_blocks([chunk * 4 + 100, chunk * 4 + 200]))
        self.assertEqual([a for a, _ in blocks],
                         [chunk * 4, chunk * 3, chunk * 5, 0, chunk, chunk * 2])
        self.assertEqual(sum(size for _, size in blocks), chunk * 6)

    def test_unmapped_priority_address_does_not_expand_reads(self):
        mem = self.memory()
        self.assertEqual(list(mem.scan_blocks([0xdeadbeef])), [(0x10000, 12288)])

    def test_wrong_manager_class_is_rejected(self):
        mem = Mock()
        mem.u64.side_effect = [0x20000, 0xdead]
        with self.assertRaisesRegex(ValueError, 'wrong class'):
            validate_candidate(mem, 'DuelistScript', 0x10000,
                               {'duelManager': {'pointer': '0x30000'}},
                               {'DuelManager': {'address': 0x3188de40}})


if __name__ == '__main__':
    unittest.main()
