import struct
import contextlib
import io
import unittest
from unittest.mock import Mock, patch

from inspect_dmo_memory import METADATA
from monitor_dmo import DuelMonitor, main, render, resolve_identity


class FakeMemory:
    def __init__(self):
        self.data = bytearray(0x20000)
        self.maps = [(0x100, 0x20000, 'rw-p', str(METADATA))]

    def put(self, address, fmt, *values):
        struct.pack_into(fmt, self.data, address, *values)

    def read(self, address, size):
        if not 0x100 <= address <= address + size <= len(self.data):
            raise ValueError('Unmapped memory')
        return bytes(self.data[address:address + size])

    def u64(self, address):
        return struct.unpack('<Q', self.read(address, 8))[0]


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.mem = FakeMemory()
        self.player, self.manager, self.identity = 0x4000, 0x5000, 0x6000
        fields = {
            'DuelistScript': {'Name': (0x80, 14), 'PlayerName': (0x108, 14),
                              'NPC': (0x248, 2), 'Dueling': (0x88, 2),
                              'StartingDuel': (0x89, 2), 'duelManager': (0x90, 18),
                              'DuelID': (0xa8, 8)},
            'DuelManager': {'CurrentPhase': (0xb8, 17), 'CurrentTurnPlayerID': (0xb4, 8),
                            'Players': (0x90, 21)},
            'NetworkBehaviour': {'<netIdentity>k__BackingField': (0x40, 18)},
            'NetworkIdentity': {'<isLocalPlayer>k__BackingField': (0x22, 2)},
        }
        layout = {'classes': {name: {'fields': {key: dict(offset=o, type_code=t, attributes=6)
                    for key, (o, t) in members.items()}} for name, members in fields.items()}}
        self.monitor = DuelMonitor(self.mem, 'JQN', layout, {}, 1000)
        self.monitor.classes = {name: {'address': addr} for name, addr in
                                [('DuelistScript', 0x1000), ('DuelManager', 0x1100),
                                 ('NetworkIdentity', 0x1200), ('NetworkBehaviour', 0x1300)]}
        for obj, klass in [(self.player, 0x1000), (self.manager, 0x1100), (self.identity, 0x1200)]:
            self.mem.put(obj, '<Q', klass)
            self.mem.put(obj + 16, '<Q', 0x15000)
        self.mem.put(self.player + 0x80, '<Q', 0x7000)
        self.mem.put(0x7010, '<i', 3)
        self.mem.data[0x7014:0x701a] = 'JQN'.encode('utf-16-le')
        self.mem.put(self.player + 0x40, '<Q', self.identity)
        self.mem.put(self.identity + 0x22, '<B', 1)
        self.mem.put(self.player + 0x88, '<B', 1)
        self.mem.put(self.player + 0x90, '<Q', self.manager)
        self.mem.put(self.manager + 0xb8, '<i', 2)
        self.mem.put(self.manager + 0x90, '<Q', 0x9000)
        self.mem.put(0x9000, '<Q', 0xb000)
        self.mem.put(0xb010, '<Q', 0xc000)
        self.mem.data[0xc000:0xc007] = b'List`1\0'
        self.mem.put(0x9010, '<Q', 0xa000)
        self.mem.put(0x9018, '<i', 2)
        self.mem.put(0xa018, '<Q', 4)
        self.monitor.player = self.player

    def test_identifies_jqn_and_own_main_phase(self):
        state = self.monitor.snapshot()
        self.assertEqual(state['status'], 'duel')
        self.assertEqual(state['phase'], 'MAIN 1')
        self.assertTrue(state['my_turn'])
        self.assertIn('JQN · tu turno · MAIN 1', render(state))

    def test_currency_unavailable_does_not_invalidate_duel_snapshot(self):
        self.monitor.read_currency = True
        with patch('monitor_dmo.read_wallet', side_effect=ValueError('saldo cambió')):
            state = self.monitor.snapshot()
        self.assertEqual(state['status'], 'duel')
        self.assertEqual(state['currency']['status'], 'unavailable')

    def test_currency_is_included_only_after_stable_player_snapshot(self):
        self.monitor.read_currency = True
        wallet = dict(status='observed', money=1200, player=hex(self.player))
        with patch('monitor_dmo.read_wallet', return_value=wallet) as read:
            state = self.monitor.snapshot()
        self.assertEqual(state['currency']['money'], 1200)
        read.assert_called_once_with(self.monitor)

    def test_currency_is_available_outside_duel_too(self):
        self.monitor.read_currency = True
        self.mem.put(self.player + 0x88, '<B', 0)
        wallet = dict(status='observed', money=1400, player=hex(self.player))
        with patch('monitor_dmo.read_wallet', return_value=wallet):
            state = self.monitor.snapshot()
        self.assertEqual(state['status'], 'outside_duel')
        self.assertEqual(state['currency']['money'], 1400)

    def test_currency_player_mismatch_does_not_change_duel_state(self):
        self.monitor.read_currency = True
        with patch('monitor_dmo.read_wallet', return_value=dict(player='0xother')):
            state = self.monitor.snapshot()
        self.assertEqual(state['status'], 'duel')
        self.assertEqual(state['currency']['status'], 'unavailable')

    def test_same_name_remote_player_is_rejected(self):
        self.mem.put(self.identity + 0x22, '<B', 0)
        self.assertFalse(self.monitor.matches_player(self.player))

    def test_local_player_with_different_name_is_rejected(self):
        self.mem.data[0x7014:0x701a] = 'ABC'.encode('utf-16-le')
        self.assertFalse(self.monitor.matches_player(self.player))

    def test_playername_fallback(self):
        self.mem.put(self.player + 0x108, '<Q', 0x7000)
        self.mem.put(self.player + 0x80, '<Q', 0)
        self.assertTrue(self.monitor.matches_player(self.player))

    def test_opponent_turn_and_unknown_turn(self):
        self.mem.put(self.manager + 0xb4, '<i', 1)
        self.assertFalse(self.monitor.snapshot()['my_turn'])
        self.mem.put(self.manager + 0xb4, '<i', -1)
        self.assertIsNone(self.monitor.snapshot()['my_turn'])

    def test_bad_secondary_name_does_not_hide_valid_identity(self):
        self.mem.put(self.player + 0x108, '<Q', 0xdeadbeef)
        result = self.monitor.identify(self.player)
        self.assertTrue(result['accepted'])
        self.assertIn('PlayerName', result['errors'])

    def test_diagnostic_distinguishes_locality_from_name(self):
        self.monitor.player = None
        self.mem.put(self.identity + 0x22, '<B', 0)
        self.monitor.inspect_candidate(self.player)
        result = self.monitor.diagnostic_report()
        self.assertEqual(result['reasons'], {'not_local_player': 1})
        self.assertEqual(result['candidates'][0]['names']['Name'], 'JQN')
        self.assertFalse(result['candidates'][0]['is_local_player'])

    def test_name_mismatch_still_reports_local_player_flag(self):
        self.mem.data[0x7014:0x701a] = 'ABC'.encode('utf-16-le')
        result = self.monitor.identify(self.player)
        self.assertEqual(result['reason'], 'name_mismatch')
        self.assertTrue(result['is_local_player'])

    def test_owned_flag_cannot_replace_local_identity(self):
        self.monitor.layout['classes']['NetworkIdentity']['fields']['<isOwned>k__BackingField'] = dict(
            offset=0x23, type_code=2, attributes=6)
        self.mem.put(self.identity + 0x22, '<B', 0)
        self.mem.put(self.identity + 0x23, '<B', 1)
        result = self.monitor.identify(self.player)
        self.assertTrue(result['<isOwned>k__BackingField'])
        self.assertFalse(result['accepted'])

    def test_empty_names_accept_verified_local_player(self):
        self.mem.put(0x7010, '<i', 0)
        self.mem.put(self.player + 0x108, '<Q', 0x7000)
        state = self.monitor.snapshot()
        self.assertEqual(state['status'], 'duel')
        self.assertFalse(state['name_verified'])
        self.assertIn('Jugador local · tu turno · MAIN 1', render(state))

    def test_empty_names_do_not_accept_remote_player(self):
        self.mem.put(0x7010, '<i', 0)
        self.mem.put(self.identity + 0x22, '<B', 0)
        self.assertFalse(self.monitor.identify(self.player)['accepted'])

    def test_invalid_names_are_not_treated_as_empty(self):
        self.mem.put(self.player + 0x80, '<Q', 0xdeadbeef)
        self.mem.put(self.player + 0x108, '<Q', 0xdeadbeef)
        result = self.monitor.identify(self.player)
        self.assertTrue(result['is_local_player'])
        self.assertFalse(result['accepted'])

    def test_replay_reported_local_identity_with_empty_names(self):
        # Values from /tmp/dmo-jqn-identity.json, independent of mutable addresses.
        result = resolve_identity(dict(names={'Name': '', 'PlayerName': ''},
            npc=False, name_matches=False, is_local_player=True,
            **{'<isOwned>k__BackingField': True, '<isClient>k__BackingField': True,
               '<isServer>k__BackingField': False}))
        self.assertTrue(result['accepted'])
        self.assertFalse(result['name_verified'])
        self.assertEqual(result['reason'], 'local_player_without_name')

    def test_outside_duel_does_not_use_old_manager(self):
        self.mem.put(self.player + 0x88, '<B', 0)
        self.mem.put(self.player + 0x90, '<Q', 0xdeadbeef)
        self.assertEqual(self.monitor.snapshot()['status'], 'outside_duel')

    def test_new_duel_follows_changed_manager(self):
        new_manager = 0xd000
        self.mem.data[new_manager:new_manager + 0x200] = self.mem.data[self.manager:self.manager + 0x200]
        self.mem.put(new_manager + 0xb8, '<i', 5)
        self.mem.put(self.player + 0x90, '<Q', new_manager)
        state = self.monitor.snapshot()
        self.assertEqual(state['manager'], hex(new_manager))
        self.assertEqual(state['phase'], 'END')

    def test_destroyed_player_never_reports_a_turn(self):
        self.mem.put(self.player + 16, '<Q', 0)
        self.assertEqual(self.monitor.snapshot()['status'], 'unstable')

    def test_invalid_phase_is_not_guessed(self):
        self.mem.put(self.manager + 0xb8, '<i', 99)
        self.assertEqual(self.monitor.snapshot()['status'], 'unstable')

    def test_disagreeing_samples_do_not_emit_stale_phase(self):
        with patch.object(self.monitor, 'read_state', side_effect=[{'phase': 'DRAW'}, {'phase': 'MAIN 1'}] * 3):
            self.assertEqual(self.monitor.snapshot()['status'], 'unstable')

    def test_foreign_report_is_ignored(self):
        self.monitor.load_hints({'pid': 999}, pid=123)
        self.assertEqual(self.monitor.player, self.player)


class MonitorCommandTests(unittest.TestCase):
    def run_command(self, arguments, monitor=None):
        monitor = monitor if monitor is not None else Mock(player=None)
        monitor.discover.return_value = False
        monitor.diagnostic_report.return_value = {'found': False, 'candidates': []}
        with (patch('sys.argv', ['monitor_dmo.py', '--pid', '123', *arguments]),
              patch('monitor_dmo.process_start', return_value='generation'),
              patch('monitor_dmo.native_layout', return_value={}),
              patch('monitor_dmo.metadata_types', return_value={}),
              patch('monitor_dmo.Memory'),
              patch('monitor_dmo.DuelMonitor', return_value=monitor),
              patch('monitor_dmo.Path.read_text', return_value='{}'),
              patch('monitor_dmo.time.sleep') as sleep,
              contextlib.redirect_stdout(io.StringIO()),
              contextlib.redirect_stderr(io.StringIO())):
            result = main()
        sleep.assert_not_called()
        return result, monitor

    def test_failed_discovery_stops_after_one_scan(self):
        result, monitor = self.run_command([])
        self.assertEqual(result, 3)
        monitor.discover.assert_called_once()

    def test_unidentified_optional_scene_cache_does_not_stop_discovery(self):
        monitor = Mock(player=None)
        with patch('monitor_dmo.Path.stat', return_value=Mock(st_size=10)):
            result, monitor = self.run_command(['--scene-cache', '/tmp/cache.json'], monitor)
        self.assertEqual(result, 3)  # Failed identity discovery, not a cache failure.
        monitor.discover.assert_called_once()
        monitor.scene_reader.load_hints.assert_not_called()

    def test_hints_only_never_scans(self):
        result, monitor = self.run_command(['--diagnose', '--hints-only'])
        self.assertEqual(result, 0)
        monitor.discover.assert_not_called()
        monitor.load_hints.assert_called_once_with({}, 123, inspect_all=True)

    def test_scene_discovery_scans_once_and_exits_without_polling(self):
        monitor = Mock(player=123, budget=4096)
        state = dict(status='duel')
        monitor.snapshot.return_value = state
        monitor.scene_reader.discover_hand_elements.return_value = dict(
            status='investigating', hand_elements=[])
        result, monitor = self.run_command(['--scene-discover'], monitor)
        self.assertEqual(result, 0)
        monitor.snapshot.assert_called_once()
        monitor.scene_reader.discover_hand_elements.assert_called_once_with(state, 4096)
        monitor.discover.assert_not_called()


if __name__ == '__main__':
    unittest.main()
