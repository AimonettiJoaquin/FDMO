from unittest import TestCase

from dmo_session_metrics import SessionMetrics


def currency_state(money):
    return dict(status='duel', local_player=True, player='0x100', character='JQN',
                process_identity=dict(pid=123, process_start='1', binary_sha256='hash'),
                currency=dict(status='observed', money=money, player='0x100', misc='0x200'))


class SessionMetricTests(TestCase):
    def setUp(self):
        self.now = 0
        self.metrics = SessionMetrics(clock=lambda: self.now)

    def test_net_money_per_minute_includes_setup_and_waiting_time(self):
        self.now = 30
        self.metrics.observe(currency_state(1000))
        self.now = 120
        self.metrics.observe(currency_state(1300))
        report = self.metrics.report()
        self.assertEqual(report['duration_seconds'], 120)
        self.assertEqual(report['duration_hms'], '00:02:00')
        self.assertEqual(report['money_generated'], 300)
        self.assertEqual(report['money_per_minute'], 150)
        self.assertEqual(report['money_first_observation_seconds'], 30)
        self.assertIn('Dinero/min: 150.00', self.metrics.summary(report))

    def test_expenses_are_subtracted_instead_of_counting_only_increases(self):
        self.metrics.observe(currency_state(1000))
        self.now = 60
        self.metrics.observe(currency_state(1200))
        self.now = 120
        self.metrics.observe(currency_state(900))
        report = self.metrics.report()
        self.assertEqual((report['money_generated'], report['money_per_minute']), (-100, -50))

    def test_absent_or_single_reading_is_not_reported_as_zero_earnings(self):
        self.assertIsNone(self.metrics.report()['money_generated'])
        self.metrics.observe(currency_state(1000))
        self.now = 60
        self.assertIsNone(self.metrics.report()['money_per_minute'])
        self.assertIn('no disponible', self.metrics.summary())

    def test_zero_elapsed_time_does_not_divide_by_zero(self):
        self.metrics.observe(currency_state(1000))
        self.metrics.observe(currency_state(1000))
        self.assertIsNone(self.metrics.report()['money_per_minute'])

    def test_missing_intermediate_balance_does_not_reset_initial_balance(self):
        self.metrics.observe(currency_state(1000))
        missing = currency_state(0)
        missing['currency'] = dict(status='unavailable')
        self.now = 30
        self.metrics.observe(missing)
        self.now = 60
        self.metrics.observe(currency_state(1500))
        self.assertEqual(self.metrics.report()['money_per_minute'], 500)
        self.now = 90
        self.assertEqual(self.metrics.report()['money_last_observation_age_seconds'], 30)

    def test_failed_latest_read_does_not_present_old_balance_as_current_profit(self):
        self.metrics.observe(currency_state(1000))
        self.now = 60
        self.metrics.observe(currency_state(1300))
        missing = currency_state(0)
        missing['currency'] = dict(status='unavailable', error='saldo inestable')
        self.metrics.observe(missing)
        report = self.metrics.report()
        self.assertEqual(report['final_money'], 1300)  # Last known balance retained for diagnosis.
        self.assertIsNone(report['money_per_minute'])
        self.assertEqual(report['money_metric_error'], 'saldo inestable')

    def unstable(self):
        state = currency_state(0)
        return {k: state[k] for k in ('character', 'process_identity')} | dict(status='unstable')

    def test_ctrl_c_after_unstable_snapshot_preserves_recent_observed_profit(self):
        self.metrics.observe(currency_state(63867))
        self.now = 31194.43
        self.metrics.observe(currency_state(77937))
        self.now = 31196.33
        self.metrics.observe(self.unstable())
        report = self.metrics.report()
        self.assertEqual(report['money_generated'], 14070)
        self.assertEqual(report['money_per_minute'], 27.06)
        self.assertEqual(report['money_metric_status'], 'observed_recent')
        self.assertTrue(report['money_metric_uses_recent_balance'])
        self.assertEqual(report['money_last_observation_age_seconds'], 1.9)
        self.assertIn('saldo de hace 1.9 s', self.metrics.summary(report))

    def test_repeated_unstable_snapshots_do_not_refresh_balance_age(self):
        self.metrics.observe(currency_state(1000))
        self.now = 60
        self.metrics.observe(currency_state(1300))
        for age in (1, 2, 3):
            self.now = 60 + age
            self.metrics.observe(self.unstable())
            self.assertEqual(self.metrics.report()['money_generated'], 300)
        self.now = 63.01
        self.metrics.observe(self.unstable())
        self.assertIsNone(self.metrics.report()['money_generated'])
        self.assertEqual(self.metrics.observations, 2)

    def test_unstable_snapshot_does_not_override_an_explicit_wallet_failure(self):
        self.metrics.observe(currency_state(1000))
        self.now = 60
        self.metrics.observe(currency_state(1300))
        failed = currency_state(0)
        failed['currency'] = dict(status='unavailable', error='saldo cambió')
        self.metrics.observe(failed)
        self.now = 61
        self.metrics.observe(self.unstable())
        report = self.metrics.report()
        self.assertIsNone(report['money_generated'])
        self.assertEqual(report['money_metric_error'], 'saldo cambió')

    def test_unstable_snapshot_from_changed_process_invalidates_comparison(self):
        self.metrics.observe(currency_state(1000))
        self.metrics.observe(currency_state(1300))
        unstable = self.unstable()
        unstable['process_identity']['process_start'] = 'new'
        self.metrics.observe(unstable)
        self.assertEqual(self.metrics.report()['money_metric_status'], 'invalid')

    def test_unstable_snapshot_with_single_observation_still_has_insufficient_data(self):
        self.metrics.observe(currency_state(1000))
        self.metrics.observe(self.unstable())
        self.assertIsNone(self.metrics.report()['money_generated'])

    def test_changed_process_invalidates_currency_comparison(self):
        self.metrics.observe(currency_state(1000))
        state = currency_state(100000)
        state['process_identity']['process_start'] = 'other'
        self.metrics.observe(state)
        report = self.metrics.report()
        self.assertEqual(report['money_metric_status'], 'invalid')
        self.assertIsNone(report['money_generated'])

    def test_foreign_unstable_malformed_and_invalid_money_are_ignored(self):
        for key, value in [('local_player', False), ('status', 'unstable'),
                           ('process_identity', None), ('currency', None)]:
            state = currency_state(1000)
            state[key] = value
            self.metrics.observe(state)
        for money in (None, True, -1, 2**31):
            self.metrics.observe(currency_state(money))
        state = currency_state(1000)
        state['currency']['player'] = '0xother'
        self.metrics.observe(state)
        self.assertEqual(self.metrics.observations, 0)

    def test_time_does_not_wrap_after_a_day(self):
        self.now = 25 * 3600 + 61
        self.assertEqual(self.metrics.report()['duration_hms'], '25:01:01')
