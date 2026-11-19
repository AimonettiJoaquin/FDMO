import copy
import unittest
from unittest.mock import Mock, patch

import numpy as np

from dmo.bot.dmo_bot import Bot, DEFAULT_CONFIG, SafetyStop, Vision


class PhaseUI:
    """Simulate Tab and the game's accepted/rejected phase clicks."""

    def __init__(self, points, phase="my_main", opened=False, battle_enabled=False):
        self.points = points
        self.phase = phase
        self.opened = opened
        self.battle_enabled = battle_enabled
        self.clicks = []
        self.keys = []

    def screenshot(self):
        return self.phase if self.opened or self.phase == "world" else None

    def key(self, name):
        self.keys.append(name)
        if name == "tab":
            self.opened = not self.opened

    def click(self, point):
        self.clicks.append(point)
        if not self.opened:
            raise AssertionError("Phase click with the selector closed")
        if point == self.points["phase_battle"] and self.battle_enabled:
            self.phase = "my_battle"
            self.opened = False
        elif point == self.points["phase_end"]:
            self.phase = "enemy_turn"
            self.opened = False

    def save_debug(self, *args):
        return "debug.png"


class PhaseRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.bot = Bot(copy.deepcopy(DEFAULT_CONFIG))
        self.ui = PhaseUI(self.bot.points)
        self.bot.desktop = self.ui
        self.bot.vision = Mock()
        self.bot.vision.is_state.side_effect = lambda state, image: state == image
        self.bot.settle = Mock()
        self.sleep = patch("dmo.bot.dmo_bot.time.sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def test_wait_recognizes_selector_already_open(self):
        self.ui.opened = True
        self.assertEqual(self.bot.wait_for_own_phase(), "my_main")
        self.assertEqual(self.ui.keys, [])
        self.assertTrue(self.ui.opened)

    def test_recovery_recognizes_selector_in_either_position(self):
        for opened in (False, True):
            with self.subTest(opened=opened):
                self.ui.opened = opened
                self.assertEqual(self.bot.recover_stalled_phase(), "my_main")
                self.assertTrue(self.ui.opened)

    def test_disabled_battle_can_fall_back_to_end(self):
        self.assertFalse(self.bot.go_to_battle())
        self.assertTrue(self.bot.end_turn())
        self.assertEqual(self.ui.phase, "enemy_turn")
        self.assertEqual(self.ui.clicks[-1], self.bot.points["phase_end"])

    def test_available_battle_is_confirmed(self):
        self.ui.battle_enabled = True
        self.assertTrue(self.bot.go_to_battle())
        self.assertEqual(self.ui.phase, "my_battle")
        self.assertFalse(self.ui.opened)
        self.assertNotIn(self.bot.points["phase_end"], self.ui.clicks)

    def test_end_does_not_click_during_opponent_turn(self):
        self.ui.phase = "enemy_turn"
        self.assertFalse(self.bot.end_turn())
        self.assertEqual(self.ui.clicks, [])

    def test_unknown_screen_is_not_duel_end(self):
        self.ui.phase = None
        self.bot.config["timing"]["duel_end_no_phase_timeout"] = 0
        with self.assertRaises(SafetyStop):
            self.bot.wait_for_own_phase()

    def test_world_confirms_duel_end(self):
        self.ui.phase = "world"
        self.bot.config["timing"]["duel_end_no_phase_timeout"] = 0
        self.assertIsNone(self.bot.wait_for_own_phase())

    @patch("dmo.bot.dmo_bot.switch_to_game_delay")
    def test_run_requests_end_after_rejected_battle(self, switch_delay):
        # Reproduce the log: an initial Bonz detection, then our Draw/Main.
        self.bot.desktop = Mock()
        self.bot.start_duel = Mock()
        self.bot.wait_for_own_phase = Mock(side_effect=[
            "enemy_turn", "my_draw", "my_main", None,
        ])
        self.bot.draw_card = Mock()
        self.bot.summon_once = Mock()
        self.bot.go_to_battle = Mock(return_value=False)
        self.bot.end_turn = Mock(return_value=True)
        self.bot.attack_all = Mock()
        self.bot.run(1)
        self.bot.go_to_battle.assert_called_once()
        self.bot.end_turn.assert_called_once()
        self.bot.attack_all.assert_not_called()
        self.bot.start_duel.assert_called_once()


class WorldHUDTests(unittest.TestCase):
    @staticmethod
    def world_frame(background, arrow=True):
        frame = np.full((300, 345, 3), background, dtype=np.int16)
        if arrow:
            for row in range(16):
                half_width = row // 2
                frame[149 + row, 209 - half_width:210 + half_width] = (100, 255, 0)
        return frame

    def test_world_with_changed_scenery_and_moved_compass(self):
        reference = self.world_frame(20)
        current = self.world_frame(170)
        # Compass details change outside the central player marker.
        reference[265:285, 200:220] = 255
        current[120:140, 80:100] = 255
        self.assertGreater(np.abs(reference - current).mean() / 255, 0.13)
        self.assertLessEqual(Vision._world_hud_score(current, reference), 0.25)

    def test_missing_arrow_is_not_world(self):
        reference = self.world_frame(20)
        for background in (0, 100, 255):
            with self.subTest(background=background):
                current = np.full_like(reference, background)
                self.assertGreater(Vision._world_hud_score(current, reference), 0.25)

    def test_green_patch_is_not_an_arrow(self):
        reference = self.world_frame(20)
        current = self.world_frame(170, arrow=False)
        current[149:165, 201:217] = (100, 255, 0)
        self.assertGreater(Vision._world_hud_score(current, reference), 0.25)

    def test_arrow_elsewhere_is_not_world(self):
        reference = self.world_frame(20)
        current = np.roll(self.world_frame(170), 70, axis=1)
        self.assertGreater(Vision._world_hud_score(current, reference), 0.25)

    def test_calibration_without_arrow_is_not_accepted(self):
        current = self.world_frame(170)
        reference = np.zeros_like(current)
        self.assertGreater(Vision._world_hud_score(current, reference), 0.25)


if __name__ == "__main__":
    unittest.main()
