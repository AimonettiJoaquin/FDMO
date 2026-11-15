from unittest import TestCase
from unittest.mock import Mock, patch

from dmo_loop_bot import DuelLoop
from tests.test_dmo_battle import sample
from tests.test_dmo_memory_bot import state as hand_state


def config():
    return dict(screen=dict(width=1920, height=1080),
                points=dict(deck=[963, 561], hand_first=[285, 957], summon=[165, 808],
                            summon_slot=[971, 696], phase_battle=[1067, 330],
                            phase_end=[1434, 343], direct_attack=[955, 207]),
                zones=dict(own_monsters=[[604, 696], [790, 700], [960, 728],
                                         [1138, 686], [1320, 689]],
                           opponent_monsters=[[707, 303], [842, 297], [952, 305],
                                              [1088, 302], [1214, 302]]),
                attack_sword_offset=[0, -120], target_confirm_offset=[0, 105])


class DuelLoopTests(TestCase):
    def late_summon(self, stage='recovery'):
        from dmo_memory_bot import hand_signature
        from tests.test_dmo_memory_bot import MonsterPlacementTests
        state = MonsterPlacementTests().sample()
        state['actions']['prompt_details'] = dict(CurrentChoiceHelper=dict(
            Description='Normal Summon\n<color=#00FFFFFF>Good Card', Cancelable=True,
            Min=0, Max=0, UseConfirmations=False, ChoiceType=dict(enum_raw=0), PurposeType=dict(enum_raw=0)))
        desktop = self.desktop()
        desktop.move.side_effect = lambda point: point
        desktop.position.return_value = (971, 696)
        loop = DuelLoop(desktop, config())
        loop.manager, loop.stage = state['manager'], stage
        loop.actor = Mock(finished=False, resume=False)
        loop.pending_normal_summon = ('Good Card', '0x2000', hand_signature(state))
        return loop, desktop, state

    def test_late_exact_summon_prompt_resumes_placement_before_end_or_forfeit(self):
        for stage in ('recovery', 'turn', 'end_fallback'):
            with self.subTest(stage=stage):
                loop, desktop, state = self.late_summon(stage)
                self.assertEqual(loop.observe(state), 'recovery_summon_zone_ready')
                desktop.click.assert_not_called()
                self.assertEqual(loop.observe(state), 'checking_zone_choice')
                self.assertEqual(loop.observe(state), 'zone_clicked')
                desktop.click.assert_called_once_with()
                desktop.key.assert_not_called()

    def test_late_summon_rejects_changed_hand_or_different_prompt(self):
        for mutation in ('hand', 'prompt'):
            loop, desktop, state = self.late_summon()
            if mutation == 'hand':
                state['board']['fields'][0]['HandCards'][0]['address'] = '0xother'
            else:
                state['actions']['prompt_details']['CurrentChoiceHelper']['Description'] = 'Choose an attack target.'
            self.assertNotEqual(loop.observe(state), 'recovery_summon_zone_ready')
            desktop.click.assert_not_called()

    def test_pending_summon_is_forgotten_when_the_turn_changes(self):
        loop, desktop, state = self.late_summon('turn')
        state['my_turn'] = False
        loop.observe(state)
        self.assertIsNone(loop.pending_normal_summon)
        desktop.click.assert_not_called()

    def test_unlocated_attack_target_uses_end_fallback(self):
        loop = DuelLoop(self.desktop(), config())
        state = sample()
        loop.manager = state['manager']
        loop.stage = 'turn'
        loop.observe_state = Mock(side_effect=RuntimeError(
            'No se localizó el objetivo enemigo por hover verificado'))
        with patch('dmo_loop_bot.EndFallback'):
            self.assertIn('último recurso END', loop.observe(state))
        self.assertEqual(loop.stage, 'end_fallback')

    def failed_draw(self):
        desktop = self.desktop()
        cursor = [None]
        def move(point):
            cursor[0] = point
            return point
        desktop.move.side_effect = move
        desktop.position.side_effect = lambda: cursor[0]
        cfg = config()
        cfg.update(memory_action_attempts=1, memory_recovery_attempts=2, memory_recovery_samples=40,
                   memory_end_fallback=False)
        loop = DuelLoop(desktop, cfg)
        state = self.draw_state(open_state=False)
        loop.observe(state)
        loop.observe(state)
        loop.observe(state)
        loop.waited = 39
        self.assertIn('recovery_started: 1/2', loop.observe(state))
        return loop, desktop, state

    def test_failed_draw_rechecks_phase_and_retries_only_once_before_end(self):
        loop, desktop, state = self.failed_draw()
        self.assertEqual(loop.observe(state), 'recovery_reading_phase')
        self.assertEqual(loop.observe(state), 'recovery_retry_draw')
        self.assertIsNone(loop.observe(state))
        self.assertEqual(loop.observe(state), 'draw_clicked')
        self.assertEqual(desktop.click.call_count, 2)
        loop.waited = 39
        self.assertIn('recovery_started: 2/2', loop.observe(state))
        loop.observe(state)
        self.assertEqual(loop.observe(state), 'recovery_end_clicked')
        desktop.key.assert_called_once_with('tab')
        self.assertEqual(desktop.click.call_count, 3)
        state['my_turn'] = False
        self.assertEqual(loop.observe(state), 'turn_passed: 1')
        self.assertEqual(loop.recovery_attempts, 0)

    def test_late_draw_change_is_never_clicked_again(self):
        loop, desktop, state = self.failed_draw()
        state['board']['fields'][0]['HandCardCount'] += 1
        loop.observe(state)
        self.assertEqual(loop.observe(state), 'recovery_draw_already_changed')
        desktop.click.assert_called_once_with()

    def test_phase_recovery_can_resume_main_and_preserves_summon_limit(self):
        loop, desktop, state = self.failed_draw()
        loop.summoned = True
        state['phase'] = 'MAIN 1'
        state['actions']['flags']['GameStateOpen'] = True
        loop.observe(state)
        self.assertEqual(loop.observe(state), 'recovery_main_ready')
        self.assertTrue(loop.summoned)
        desktop.click.assert_called_once_with()

    def test_recovery_does_not_click_through_a_pending_prompt(self):
        loop, desktop, state = self.failed_draw()
        state['actions']['prompts']['CurrentChoiceHelper'] = '0xabc'
        with self.assertRaisesRegex(RuntimeError, 'Recuperación agotada'):
            for _ in range(40):
                loop.observe(state)
        desktop.click.assert_called_once_with()
        desktop.key.assert_not_called()

    def test_recovery_of_unstable_phases_has_a_total_sample_limit(self):
        loop, desktop, state = self.failed_draw()
        with self.assertRaisesRegex(RuntimeError, 'Recuperación agotada'):
            for i in range(41):
                state['phase'] = 'UNASSIGNED' if i % 2 else 'UNKNOWN'
                loop.observe(state)
        desktop.click.assert_called_once_with()

    def test_end_recovery_requires_a_confirmed_transition_and_remains_bounded(self):
        loop, desktop, state = self.failed_draw()
        loop.recovery_attempts = 2
        loop.observe(state)
        loop.observe(state)
        loop.actor.waited = 23
        with self.assertRaisesRegex(RuntimeError, 'paso a END'):
            loop.observe(state)
        self.assertFalse(loop.finished)
        self.assertEqual(desktop.click.call_count, 2)

    def test_recovery_recognizes_new_draw_after_missed_rival_turn(self):
        state = self.draw_state(open_state=False)
        loop = DuelLoop(self.desktop(), config())
        loop.manager, loop.stage = state['manager'], 'recovery'
        loop.recovery_attempts = 1
        loop.recovery_origin = 'turn'
        loop.last_draw = (state['manager'], state['board']['fields'][0]['address'],
                          4, 31, ('0xold',))
        # Recovery only trusts a real phase controller that clicked END.
        from dmo_turn_bot import TurnController
        loop.actor = TurnController(loop.desktop, loop.config)
        loop.actor.stage, loop.actor.end_origin_phase = 'await_end', 'BATTLE'
        self.assertEqual(loop.observe(state), 'recovery_reading_phase')
        self.assertEqual(loop.observe(state), 'turn_passed: 1')
        self.assertEqual(loop.stage, 'waiting')
        self.assertIsNone(loop.last_draw)
        self.assertEqual(loop.observe(state), 'draw_ready')
        loop.desktop.key.assert_not_called()

    def test_end_to_new_draw_starts_fresh_draw_without_recovery(self):
        from dmo_turn_bot import TurnController
        state = self.draw_state(open_state=False)
        loop = DuelLoop(self.desktop(), config())
        loop.manager, loop.stage = state['manager'], 'turn'
        loop.last_draw = (state['manager'], state['board']['fields'][0]['address'],
                          4, 31, ('0xold',))
        loop.actor = TurnController(loop.desktop, loop.config)
        loop.actor.identity = (state['player'], state['manager'], state['own_id'])
        loop.actor.stage, loop.actor.end_origin_phase = 'await_end', 'BATTLE'
        self.assertEqual(loop.observe(state), 'turn_passed: 1')
        self.assertEqual(loop.observe(state), 'draw_ready')
        self.assertIsNone(loop.observe(state))
        self.assertEqual(loop.observe(state), 'draw_clicked')
        self.assertEqual(loop.recovery_attempts, 0)

    def test_completed_turn_clears_previous_draw_baseline(self):
        loop = DuelLoop(self.desktop(), config())
        loop.last_draw = ('manager', 'field', 4, 31, ('0xold',))
        loop.draw_attempts = 2
        self.assertEqual(loop.complete_turn(), 'turn_passed: 1')
        self.assertIsNone(loop.last_draw)
        self.assertEqual(loop.draw_attempts, 0)

    def test_unstable_snapshot_does_not_end_match(self):
        loop = DuelLoop(self.desktop(), config())
        loop.observe(sample())
        self.assertIsNone(loop.observe(dict(status='unstable')))
        self.assertFalse(loop.finished)
        self.assertEqual(loop.observe(dict(status='outside_duel')), 'duel_finished')

    def test_completed_spell_returns_to_main_preserving_normal_summon(self):
        loop = DuelLoop(self.desktop(), config(), spells=True)
        loop.stage, loop.summoned = 'spell', True
        loop.actor = Mock(finished=True)
        loop.actor.observe.return_value = 'spell_verified: Pot of Greed'
        self.assertEqual(loop.observe(sample()), 'spell_verified: Pot of Greed')
        self.assertEqual(loop.stage, 'main')
        self.assertTrue(loop.summoned)
        self.assertEqual(loop.spell_count, 1)

    def test_discard_panel_retains_drawn_hand_before_pausing_spell_actor(self):
        loop = DuelLoop(self.desktop(), config(), spells=True)
        loop.stage = 'spell'
        loop.actor = Mock(finished=False)
        loop.card_panel = Mock(finished=False)
        state = sample()
        self.assertEqual(loop.observe(state), loop.card_panel.observe.return_value)
        loop.actor.capture_discard_hand.assert_called_once_with(state)
        loop.actor.observe.assert_not_called()

    def test_spell_probe_selects_exact_published_spell_then_stops_at_menu(self):
        desktop = self.desktop()
        desktop.position.return_value = (395, 957)
        loop = DuelLoop(desktop, config(), spell_probe=True)
        state = hand_state()
        state['advice']['options'][0]['kind'] = 'spell_candidate'
        for helper in state['actions']['helpers']:
            helper['options'] = [dict(value=0, name='ActivateEffect')]
        loop.stage = 'main'
        self.assertEqual(loop.observe(state), 'spell_candidate: Good Card')
        self.assertEqual(loop.observe(state), 'probing')
        state['scene']['hand_elements'][1]['hover']['hovered'] = True
        for _ in range(3):
            loop.observe(state)
        self.assertEqual(loop.observe(state), 'clicked')
        state['scene']['hand_elements'][1]['selected'] = True
        self.assertEqual(loop.observe(state), 'spell_menu_ready: Good Card · 0x2000')
        self.assertTrue(loop.finished)
        desktop.click.assert_called_once_with()

    def test_placement_receives_selected_card_address(self):
        loop = DuelLoop(self.desktop(), config())
        loop.stage, loop.card_name, loop.card_address = 'option', 'Duplicate', '0x2000'
        loop.actor = Mock(clicked=True, finished=False)
        state = sample()
        state['phase'] = 'MAIN 1'
        state['actions']['prompts']['CurrentChoiceHelper'] = '0xaaaa'
        with patch('dmo_loop_bot.MonsterPlacement') as placement:
            self.assertEqual(loop.observe(state), 'summon_zone_ready')
        placement.assert_called_once_with(loop.desktop, loop.config, 'Duplicate',
                                           card_address='0x2000')

    def draw_state(self, *, open_state=True):
        state = sample()
        state['phase'] = 'DRAW'
        state['actions']['flags'].update(GameStateOpen=open_state, DrewForTurn=False)
        state['board']['fields'][0].update(HandCardCount=5, DeckCount=35,
            HandCards=[dict(address=hex(0x500 + i)) for i in range(5)])
        return state

    def desktop(self):
        desktop = Mock()
        desktop.move.side_effect = lambda p: p
        desktop.position.return_value = (963, 561)
        return desktop

    def test_draw_once_then_waits_for_hand_change(self):
        desktop = self.desktop()
        loop = DuelLoop(desktop, config())
        state = self.draw_state()
        self.assertEqual(loop.observe(state), 'draw_ready')
        self.assertIsNone(loop.observe(state))
        self.assertEqual(loop.observe(state), 'draw_clicked')
        desktop.click.assert_called_once_with()
        self.assertIsNone(loop.observe(state))
        state['board']['fields'][0]['HandCardCount'] = 6
        self.assertEqual(loop.observe(state), 'draw_verified')
        desktop.click.assert_called_once_with()
        # The capture showed the hand increase before DRAW/DrewForTurn changed.
        state['board']['fields'][0]['DeckCount'] = 34
        state['board']['fields'][0]['HandCards'].append(dict(address='0x600'))
        for _ in range(5):
            self.assertIsNone(loop.observe(state))
        desktop.click.assert_called_once_with()
        state['phase'] = 'STANDBY'
        self.assertEqual(loop.observe(state), 'draw_advanced')
        state['phase'] = 'MAIN 1'
        self.assertEqual(loop.observe(state), 'main_ready')

    def test_initial_draw_can_click_closed_game_state_once(self):
        desktop = self.desktop()
        loop = DuelLoop(desktop, config())
        state = self.draw_state(open_state=False)
        self.assertEqual(loop.observe(state), 'draw_ready')
        self.assertIsNone(loop.observe(state))
        self.assertEqual(loop.observe(state), 'draw_clicked')
        for _ in range(10):
            self.assertIsNone(loop.observe(state))
        desktop.click.assert_called_once_with()
        state['board']['fields'][0]['HandCardCount'] = 6
        self.assertEqual(loop.observe(state), 'draw_verified')

    def test_closed_draw_still_refuses_busy_prompts_or_already_drawn(self):
        cases = [('flags', k, True) for k in
                 ('Busy', 'SelectingCard', 'ShowChainPrompt', 'DrewForTurn')]
        cases += [('prompts', k, '0x999') for k in
                  ('CurrentChoiceHelper', 'CurrentOptionHelper', 'CurrentConfirmationHelper')]
        cases += [('flags', 'GameStateOpen', None), ('flags', 'DrewForTurn', None)]
        for group, key, value in cases:
            with self.subTest(key=key, value=value):
                desktop = self.desktop()
                loop = DuelLoop(desktop, config())
                state = self.draw_state(open_state=False)
                state['actions'][group][key] = value
                loop.observe(state)
                for _ in range(3):
                    result = loop.observe(state)
                    if group == 'prompts' and key == 'CurrentOptionHelper':
                        self.assertEqual(result, 'waiting_card_panel')
                    else:
                        self.assertIsNone(result)
                desktop.click.assert_not_called()

    def test_changed_hand_requires_two_new_draw_samples(self):
        desktop = self.desktop()
        loop = DuelLoop(desktop, config())
        state = self.draw_state(open_state=False)
        loop.observe(state)
        loop.observe(state)
        state['board']['fields'][0]['HandCards'][0]['address'] = '0x900'
        self.assertIsNone(loop.observe(state))
        desktop.click.assert_not_called()
        self.assertEqual(loop.observe(state), 'draw_clicked')

    def test_busy_sample_breaks_draw_stability(self):
        desktop = self.desktop()
        loop = DuelLoop(desktop, config())
        state = self.draw_state(open_state=False)
        loop.observe(state)
        loop.observe(state)
        state['actions']['flags']['Busy'] = True
        loop.observe(state)
        state['actions']['flags']['Busy'] = False
        self.assertIsNone(loop.observe(state))
        desktop.click.assert_not_called()
        self.assertEqual(loop.observe(state), 'draw_clicked')

    def test_incomplete_hand_or_empty_deck_prevents_draw(self):
        for key, value in [('HandCards', []), ('DeckCount', 0)]:
            with self.subTest(key=key):
                desktop = self.desktop()
                loop = DuelLoop(desktop, config())
                state = self.draw_state(open_state=False)
                state['board']['fields'][0][key] = value
                loop.observe(state)
                self.assertIn('último recurso END', loop.observe(state))
                self.assertEqual(loop.stage, 'end_fallback')
                desktop.click.assert_not_called()

    def test_repeats_after_opponent_receives_turn(self):
        desktop = self.desktop()
        loop = DuelLoop(desktop, config(), max_turns=2)
        state = sample()
        state['phase'] = 'MAIN 1'
        state['advice'] = dict(status='no_suggestion', options=[])
        self.assertEqual(loop.observe(state), 'main_ready')
        with patch('dmo_loop_bot.selection', return_value=None):
            self.assertEqual(loop.observe(state), 'turn_actions_started')
        loop.actor = Mock(finished=True)
        loop.actor.observe.return_value = 'turn_passed'
        self.assertEqual(loop.observe(state), 'turn_passed: 1')
        self.assertFalse(loop.finished)
        state['my_turn'] = False
        self.assertIsNone(loop.observe(state))
        state['my_turn'] = True
        state['phase'] = 'DRAW'
        self.assertEqual(loop.observe(state), 'draw_ready')

    def test_draw_retries_when_hand_deck_and_phase_are_unchanged(self):
        loop, desktop, state = self.failed_draw()
        loop.stage, loop.waited = 'await_draw', 0
        loop.max_action_attempts, loop.draw_attempts = 3, 1
        for attempt in (2, 3):
            for _ in range(11):
                self.assertIsNone(loop.observe(state))
            self.assertEqual(loop.observe(state), f'draw_retry: {attempt}/3')
        self.assertEqual(desktop.click.call_count, 3)
        state['board']['fields'][0]['HandCardCount'] += 1
        self.assertEqual(loop.observe(state), 'draw_verified')
        for _ in range(15):
            loop.observe(state)
        self.assertEqual(desktop.click.call_count, 3)

    def test_draw_retry_refuses_changed_deck_or_pending_prompt(self):
        for change in ('deck', 'prompt'):
            loop, desktop, state = self.failed_draw()
            loop.stage, loop.waited = 'await_draw', 0
            loop.max_action_attempts, loop.draw_attempts = 3, 1
            if change == 'deck':
                state['board']['fields'][0]['DeckCount'] -= 1
            else:
                state['actions']['prompts']['CurrentChoiceHelper'] = '0xpending'
            for _ in range(15):
                loop.observe(state)
            desktop.click.assert_called_once_with()

    def test_default_recovery_budget_is_five_with_longer_observation(self):
        loop = DuelLoop(self.desktop(), config())
        self.assertEqual(loop.max_recoveries, 5)
        self.assertEqual(loop.recovery_sample_limit, 80)
