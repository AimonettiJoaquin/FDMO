import copy
from unittest import TestCase

from dmo_match_stats import MatchStats
from tests.test_dmo_battle import sample


class MatchStatsTests(TestCase):
    def settled(self, own_lp, rival_lp, *, forfeit=False):
        stats = MatchStats()
        stats.start()
        state = sample()
        state['board']['fields'][0]['life_points_candidate'] = own_lp
        state['board']['fields'][1]['life_points_candidate'] = rival_lp
        stats.observe(state)
        stats.observe(state, 'fallback_forfeit_confirm_clicked' if forfeit else None)
        state['status'] = 'outside_duel'
        stats.observe(state, 'duel_finished')
        stats.finish()
        return stats

    def test_win_and_loss_require_two_stable_zero_life_readings_and_exit(self):
        win = self.settled(4500, 0)
        self.assertEqual((win.started, win.wins, win.losses, win.unknown), (1, 1, 0, 0))
        loss = self.settled(0, 6300)
        self.assertEqual((loss.started, loss.wins, loss.losses, loss.unknown), (1, 0, 1, 0))
        self.assertIn('Ganadas: 1', win.summary())
        self.assertIn('Perdidas: 1', loss.summary())

    def test_verified_forfeit_is_separate_from_loss(self):
        stats = self.settled(0, 6300, forfeit=True)
        self.assertEqual((stats.forfeits, stats.losses), (1, 0))
        self.assertIn('Abandonadas: 1', stats.summary())

    def test_forfeit_click_without_duel_exit_is_unknown(self):
        stats = MatchStats()
        stats.start()
        state = sample()
        stats.observe(state, 'fallback_forfeit_confirm_clicked')
        stats.finish()
        self.assertEqual((stats.forfeits, stats.unknown), (0, 1))

    def test_single_zero_sample_and_both_zero_remain_unknown(self):
        stats = MatchStats()
        stats.start()
        state = sample()
        state['board']['fields'][1]['life_points_candidate'] = 0
        stats.observe(state)
        state['status'] = 'outside_duel'
        stats.observe(state)
        stats.finish()
        self.assertEqual(stats.unknown, 1)
        both = self.settled(0, 0)
        self.assertEqual(both.unknown, 1)

    def test_interrupted_or_never_observed_match_is_counted_as_unknown(self):
        stats = MatchStats()
        stats.start()
        stats.finish()
        self.assertEqual((stats.started, stats.unknown), (1, 1))
        stats.start()
        stats.observe(sample())
        stats.finish()
        self.assertEqual((stats.started, stats.unknown), (2, 2))

    def test_outside_before_duel_and_other_manager_do_not_create_result(self):
        stats = MatchStats()
        stats.start()
        stats.observe(dict(status='outside_duel'))
        state = sample()
        stats.observe(state)
        changed = copy.deepcopy(state)
        changed['manager'] = '0xother'
        changed['board']['fields'][1]['life_points_candidate'] = 0
        stats.observe(changed)
        stats.observe(changed)
        stats.finish()
        self.assertEqual(stats.unknown, 1)

    def test_multiple_matches_accumulate_without_double_counting(self):
        stats = MatchStats()
        for own, rival in ((7000, 0), (0, 8000), (3400, 0)):
            stats.start()
            state = sample()
            state['board']['fields'][0]['life_points_candidate'] = own
            state['board']['fields'][1]['life_points_candidate'] = rival
            stats.observe(state)
            stats.observe(state)
            state['status'] = 'outside_duel'
            stats.observe(state)
            stats.finish()
            stats.finish()
        self.assertEqual((stats.started, stats.wins, stats.losses, stats.forfeits,
                          stats.unknown), (3, 2, 1, 0, 0))
