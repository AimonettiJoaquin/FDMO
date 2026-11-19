from unittest import TestCase
from unittest.mock import Mock, patch

from dmo.bot.dmo_end_fallback import EndFallback
from dmo.bot.dmo_loop_bot import DuelLoop
from tests.test_dmo_special_summon import desktop
from tests.test_dmo_battle import sample
from tests.test_dmo_loop_bot import config


class EndFallbackTests(TestCase):
    def test_open_phase_attempts_end_and_requires_rival_turn(self):
        state, device = sample(), desktop()
        actor = EndFallback(device, config(), state)
        actor.observe(state)
        self.assertEqual(actor.observe(state), 'fallback_end_menu: 1/3')
        self.assertEqual(actor.observe(state), 'fallback_waiting_menu')
        self.assertEqual(actor.observe(state), 'fallback_end_clicked: 1/3')
        device.key.assert_called_once_with('tab')
        device.move.assert_called_once_with((1434, 343))
        self.assertFalse(actor.finished)
        state['phase'] = 'END'
        self.assertEqual(actor.observe(state), 'fallback_end_verified')
        self.assertFalse(actor.finished)
        state['my_turn'] = False
        self.assertEqual(actor.observe(state), 'turn_passed')
        self.assertTrue(actor.finished)

    def test_cancelable_prompt_does_not_press_escape(self):
        state, device = sample(), desktop()
        state['actions']['prompts']['CurrentChoiceHelper'] = '0xpending'
        state['actions']['prompt_details'] = dict(CurrentChoiceHelper=dict(Cancelable=True))
        actor = EndFallback(device, config(), state)
        actor.observe(state)
        self.assertEqual(actor.observe(state), 'fallback_end_menu: 1/3')
        device.key.assert_called_once_with('tab')

    def test_mandatory_prompt_does_not_try_cancel(self):
        state, device = sample(), desktop()
        state['actions']['prompts']['CurrentChoiceHelper'] = '0xpending'
        state['actions']['prompt_details'] = dict(CurrentChoiceHelper=dict(Cancelable=False))
        actor = EndFallback(device, config(), state)
        actor.observe(state)
        self.assertEqual(actor.observe(state), 'fallback_waiting_mandatory_choice')
        device.key.assert_not_called()

    def test_end_retries_are_bounded_and_no_success_is_invented(self):
        state, device = sample(), desktop()
        actor = EndFallback(device, config(), state)
        with self.assertRaisesRegex(RuntimeError, 'Forfeit no confirmó'):
            for _ in range(180):
                actor.observe(state)
        self.assertEqual(device.click.call_count, 4)
        self.assertEqual(device.key.call_count, 4)
        self.assertFalse(actor.finished)

    def test_busy_and_rival_states_never_click(self):
        state, device = sample(), desktop()
        actor = EndFallback(device, config(), state)
        state['actions']['flags']['Busy'] = True
        for _ in range(10):
            actor.observe(state)
        device.key.assert_not_called()
        device.click.assert_not_called()
        state['my_turn'] = False
        self.assertEqual(actor.observe(state), 'turn_passed')
        device.click.assert_not_called()

    def test_identity_change_and_stale_sample_refuse_input(self):
        state, device = sample(), desktop()
        actor = EndFallback(device, config(), state)
        state['manager'] = '0xother'
        with self.assertRaisesRegex(RuntimeError, 'verificar el duelo'):
            actor.observe(state)
        device.key.assert_not_called()
        state = sample()
        actor = EndFallback(device, config(), state)
        actor.observe(state)
        state['timestamp'] = '2020-01-01T00:00:00+00:00'
        with self.assertRaisesRegex(RuntimeError, 'reciente'):
            actor.observe(state)
        device.key.assert_not_called()

    def test_exhausted_recovery_enters_fallback_and_continues_next_turn(self):
        state, device = sample(), desktop()
        loop = DuelLoop(device, config())
        loop.manager = state['manager']
        loop.stage = 'recovery'
        loop.recovery_samples = loop.recovery_sample_limit
        loop.recovery_error = 'No se verificó el ataque'
        loop.summoned = True
        loop.card_panel = Mock()
        # Force the recovery exception itself rather than an active panel.
        loop.card_panel = None
        self.assertIn('último recurso END', loop.observe(state))
        self.assertEqual(loop.stage, 'end_fallback')
        loop.observe(state)
        loop.observe(state)
        loop.observe(state)
        self.assertEqual(loop.observe(state), 'fallback_end_clicked: 1/3')
        state['my_turn'] = False
        self.assertEqual(loop.observe(state), 'turn_passed: 1')
        self.assertEqual(loop.stage, 'waiting')
        self.assertFalse(loop.finished)
        self.assertFalse(loop.summoned)

    def test_unresolved_special_summon_can_fall_back_without_reentering_it(self):
        state, device = sample(), desktop()
        loop = DuelLoop(device, config())
        loop.manager, loop.stage = state['manager'], 'turn'
        loop.special_summon = Mock()
        loop.special_summon.observe.side_effect = RuntimeError('Invocación especial pendiente: Rex')
        self.assertIn('último recurso END', loop.observe(state))
        self.assertIsNone(loop.special_summon)
        self.assertEqual(loop.stage, 'end_fallback')

    def test_backspace_key_uses_evdev_backspace_without_changing_legacy_bot(self):
        from dmo.bot.dmo_memory_bot import Desktop
        with patch('dmo.bot.dmo_memory_bot.subprocess.run') as run:
            Desktop().key('backspace')
        run.assert_called_once_with(['ydotool', 'key', '14:1', '14:0'], check=True)

    def test_mandatory_prompt_never_claims_end_without_transition(self):
        state, device = sample(), desktop()
        state['actions']['prompts']['CurrentChoiceHelper'] = '0xmandatory'
        state['actions']['prompt_details'] = dict(CurrentChoiceHelper=dict(Cancelable=False))
        actor = EndFallback(device, config(), state)
        with self.assertRaisesRegex(RuntimeError, 'selección obligatoria'):
            for _ in range(40):
                actor.observe(state)
        device.key.assert_not_called()
        device.click.assert_not_called()
        self.assertFalse(actor.finished)

    def test_mandatory_choice_that_resolves_can_return_to_end_recovery(self):
        state, device = sample(), desktop()
        state['actions']['prompts']['CurrentChoiceHelper'] = '0xmandatory'
        state['actions']['prompt_details'] = dict(CurrentChoiceHelper=dict(Cancelable=False))
        actor = EndFallback(device, config(), state)
        for _ in range(5):
            self.assertEqual(actor.observe(state), 'fallback_waiting_mandatory_choice')
        state['actions']['prompts']['CurrentChoiceHelper'] = '0x0'
        actor.observe(state)
        self.assertEqual(actor.observe(state), 'fallback_end_menu: 1/3')
        self.assertEqual(actor.mandatory_samples, 0)
        device.key.assert_called_once_with('tab')

    def draw_state(self):
        state = sample()
        state['phase'] = 'DRAW'
        state['actions']['flags'].update(GameStateOpen=False, DrewForTurn=False)
        own = state['board']['fields'][0]
        own.update(owner_identity='0xowner', generation_id=0, HandCardCount=1, DeckCount=30,
                   HandCards=[dict(address='0xcard', name='Panda', slot=0, location='Hand')])
        return state

    def test_draw_is_retried_before_any_end_menu_or_forfeit(self):
        state, device = self.draw_state(), desktop()
        actor = EndFallback(device, config(), state)
        actor.observe(state)
        self.assertEqual(actor.observe(state), 'fallback_draw_clicked: 1/3')
        device.move.assert_called_once_with((963, 561))
        device.key.assert_not_called()
        for _ in range(11):
            actor.observe(state)
        self.assertEqual(actor.observe(state), 'fallback_draw_clicked: 2/3')
        state['board']['fields'][0]['DeckCount'] -= 1
        state['board']['fields'][0]['HandCards'].append(dict(address='0xnew', name='Rat'))
        state['board']['fields'][0]['HandCardCount'] += 1
        for _ in range(15):
            actor.observe(state)
        self.assertEqual(device.click.call_count, 2)
        state['phase'] = 'MAIN 1'
        actor.observe(state)
        self.assertEqual(actor.observe(state), 'fallback_phase_recovered: MAIN 1')
        self.assertTrue(actor.resume)
        device.key.assert_not_called()

    def test_new_draw_during_failed_end_completes_previous_turn(self):
        state, device = sample(), desktop()
        actor = EndFallback(device, config(), state)
        actor.observe(state)
        actor.observe(state)
        actor.observe(state)
        actor.observe(state)
        state = self.draw_state()
        self.assertEqual(actor.observe(state), 'turn_passed')
        self.assertTrue(actor.finished)
        self.assertEqual(device.click.call_count, 1)

    def test_new_own_draw_after_end_click_completes_previous_turn(self):
        state, device = sample(), desktop()
        actor = EndFallback(device, config(), state)
        actor.observe(state)
        actor.observe(state)
        actor.observe(state)
        self.assertEqual(actor.observe(state), 'fallback_end_clicked: 1/3')
        state['phase'] = 'DRAW'
        self.assertEqual(actor.observe(state), 'turn_passed')
        self.assertTrue(actor.finished)
        self.assertEqual(device.click.call_count, 1)

    def test_forfeit_uses_user_point_once_and_waits_for_actual_exit(self):
        state, device = sample(), desktop()
        actor = EndFallback(device, config(), state)
        self.assertEqual(actor.begin_forfeit(state), 'fallback_forfeit_requested')
        device.key.assert_called_once_with('backspace')
        for _ in range(3):
            self.assertEqual(actor.observe(state), 'fallback_waiting_forfeit_dialog')
        self.assertEqual(actor.observe(state), 'fallback_forfeit_confirm_clicked')
        device.move.assert_called_once_with((1125, 633))
        state['my_turn'] = False
        for _ in range(10):
            self.assertEqual(actor.observe(state), 'fallback_waiting_duel_exit')
        self.assertFalse(actor.finished)
        device.click.assert_called_once_with()

    def test_loop_resumes_normal_main_after_draw_recovery(self):
        state, device = self.draw_state(), desktop()
        loop = DuelLoop(device, config())
        loop.manager, loop.stage = state['manager'], 'end_fallback'
        loop.actor = EndFallback(device, config(), state)
        loop.summoned = True
        loop.observe(state)
        loop.observe(state)
        state['phase'] = 'MAIN 1'
        loop.observe(state)
        self.assertEqual(loop.observe(state), 'fallback_phase_recovered: MAIN 1')
        self.assertEqual(loop.stage, 'waiting')
        self.assertFalse(loop.summoned)
        self.assertEqual(loop.observe(state), 'main_ready')

    def test_forfeit_exit_finishes_match_for_runner_to_start_next_one(self):
        state, device = sample(), desktop()
        loop = DuelLoop(device, config())
        loop.manager, loop.stage = state['manager'], 'end_fallback'
        loop.actor = EndFallback(device, config(), state)
        loop.actor.begin_forfeit(state)
        state['status'] = 'outside_duel'
        self.assertEqual(loop.observe(state), 'duel_finished')
        self.assertTrue(loop.finished)

    def test_forfeit_confirmation_scales_with_reference_viewport(self):
        state, device = sample(), desktop()
        cfg = config()
        cfg['screen'] = dict(width=960, height=540)
        actor = EndFallback(device, cfg, state)
        actor.begin_forfeit(state)
        for _ in range(4):
            actor.observe(state)
        device.move.assert_called_once_with((562, 316))

    def test_forfeit_confirmation_does_not_click_after_identity_change(self):
        state, device = sample(), desktop()
        actor = EndFallback(device, config(), state)
        actor.begin_forfeit(state)
        state['manager'] = '0xother'
        with self.assertRaisesRegex(RuntimeError, 'verificar el duelo'):
            actor.observe(state)
        device.click.assert_not_called()

    def test_standby_waits_for_main_without_trying_end(self):
        state, device = sample(), desktop()
        state['phase'] = 'STANDBY'
        actor = EndFallback(device, config(), state)
        actor.observe(state)
        self.assertEqual(actor.observe(state), 'fallback_waiting_standby')
        state['phase'] = 'MAIN 1'
        actor.observe(state)
        self.assertEqual(actor.observe(state), 'fallback_phase_recovered: MAIN 1')
        device.key.assert_not_called()
