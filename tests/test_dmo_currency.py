from unittest import TestCase
from unittest.mock import Mock

from dmo_currency import read_wallet


class WalletTests(TestCase):
    def monitor(self):
        monitor = Mock(player=0x100)
        monitor.matches_player.return_value = True
        monitor.pointer.side_effect = lambda obj, cls, key: 0x200 if key == 'misc' else 0x100
        monitor.field.return_value = 1200
        return monitor

    def test_reads_stable_wallet_without_recursing_into_snapshot(self):
        monitor = self.monitor()
        result = read_wallet(monitor)
        self.assertEqual((result['status'], result['money'], result['player']),
                         ('observed', 1200, '0x100'))
        monitor.snapshot.assert_not_called()

    def test_rejects_foreign_player_wallet_and_changing_money(self):
        monitor = self.monitor()
        monitor.matches_player.return_value = False
        with self.assertRaisesRegex(ValueError, 'jugador local'):
            read_wallet(monitor)
        monitor = self.monitor()
        monitor.pointer.side_effect = lambda *args: 0x200
        with self.assertRaisesRegex(ValueError, 'vinculado'):
            read_wallet(monitor)
        monitor = self.monitor()
        monitor.field.side_effect = [1200, 1201]
        with self.assertRaisesRegex(ValueError, 'cambió'):
            read_wallet(monitor)

    def test_rejects_invalid_balances(self):
        for money in (True, -1, 2**31, None, '1200'):
            monitor = self.monitor()
            monitor.field.return_value = money
            with self.assertRaisesRegex(ValueError, 'Saldo inválido'):
                read_wallet(monitor)
