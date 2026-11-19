from unittest import TestCase
from unittest.mock import Mock

from dmo.bot.dmo_loop_bot import DuelLoop
from dmo.bot.dmo_special_summon import SpecialSummonController
from tests.test_dmo_battle import sample, monster, add_monster
from tests.test_dmo_loop_bot import config


def position_state():
    state = sample()
    for field in state['board']['fields']:
        field['visual_candidates']['MonsterCards']['source'] = 'MonsterVisualHelpers'
        for zone in field['visual_candidates']['MonsterCards']['candidates']:
            zone['helper_present'] = False
    state['actions']['flags']['GameStateOpen'] = False
    state['actions']['prompts'].update(CurrentChoiceHelper='0xabc', CurrentConfirmationHelper='0xdef')
    state['actions']['prompt_details'] = dict(
        CurrentChoiceHelper=dict(Description='Choose a Position\n(<color=#00FFFFFF>Giant Rat</color>)',
            Cancelable=False, Min=0, Max=0, UseConfirmations=False,
            ChoiceType=dict(enum_raw=0), PurposeType=dict(enum_raw=0)),
        CurrentConfirmationHelper=dict(Title='Mode', ConfirmationType=dict(enum_raw=0)))
    return state


def desktop():
    result = Mock()
    cursor = [None]
    def move(point):
        cursor[0] = point
        return point
    result.move.side_effect = move
    result.position.side_effect = lambda: cursor[0]
    return result


class SpecialSummonTests(TestCase):
    def test_position_then_confirm_new_field_card_without_early_repeat(self):
        state, device = position_state(), desktop()
        actor = SpecialSummonController(device, config(), state, 'Giant Rat')
        self.assertEqual(actor.observe(state), 'checking_special_summon')
        self.assertEqual(actor.observe(state), 'special_position_clicked: Attack')
        for _ in range(5):
            actor.observe(state)
        device.click.assert_called_once_with()
        device.move.assert_called_once_with((905, 568))
        add_monster(state, True, 4, monster('Giant Rat', '0xnew', 1400))
        self.assertEqual(actor.observe(state), 'special_summon_verified: Giant Rat')
        self.assertTrue(actor.finished)

    def test_rival_turn_uses_defense_and_scales_position(self):
        state, device = position_state(), desktop()
        state['my_turn'] = False
        cfg = config()
        cfg['screen'] = dict(width=960, height=540)
        actor = SpecialSummonController(device, cfg, state, 'Giant Rat')
        actor.observe(state)
        self.assertEqual(actor.observe(state), 'special_position_clicked: Defense')
        device.move.assert_called_once_with((508, 284))

    def test_wrong_name_or_confirmation_does_not_click(self):
        for change in ('name', 'confirmation', 'busy'):
            state, device = position_state(), desktop()
            actor = SpecialSummonController(device, config(), state, 'Giant Rat')
            if change == 'name':
                state['actions']['prompt_details']['CurrentChoiceHelper']['Description'] = 'Choose a Position\n(Tiger)'
            elif change == 'confirmation':
                state['actions']['prompt_details']['CurrentConfirmationHelper']['Title'] = 'Confirm'
            else:
                state['actions']['flags']['Busy'] = True
            with self.assertRaisesRegex(RuntimeError, 'Invocación especial pendiente'):
                for _ in range(60):
                    actor.observe(state)
            device.click.assert_not_called()

    def test_existing_same_named_monster_is_not_success(self):
        state, device = position_state(), desktop()
        add_monster(state, True, 2, monster('Giant Rat', '0xold', 1400))
        actor = SpecialSummonController(device, config(), state, 'Giant Rat')
        actor.observe(state)
        actor.observe(state)
        self.assertFalse(actor.finished)

    def test_explicit_special_summon_zone_uses_only_vacant_slot(self):
        state, device = position_state(), desktop()
        for i in range(4):
            add_monster(state, True, i, monster('Tiger', hex(0x1000+i), 1700))
        actor = SpecialSummonController(device, config(), state, 'Giant Rat')
        actor.observe(state)
        actor.observe(state)
        state['actions']['prompts']['CurrentConfirmationHelper'] = '0x0'
        state['actions']['prompt_details']['CurrentChoiceHelper']['Description'] = 'Special Summon\n<color=#00FFFFFF>Giant Rat'
        actor.observe(state)
        self.assertEqual(actor.observe(state), 'special_zone_clicked')
        self.assertEqual(device.move.call_args.args[0], (1320, 689))
        for _ in range(5):
            actor.observe(state)
        self.assertEqual(device.click.call_count, 2)

    def test_loop_pauses_battle_until_special_summon_completes(self):
        state, device = position_state(), desktop()
        loop = DuelLoop(device, config())
        loop.manager, loop.stage, loop.summoned = state['manager'], 'turn', True
        loop.actor = Mock()
        loop.card_panel = Mock(identity=('player', 'manager', 'pointer', 1, 1, (),
                                        'Select 1 monster to Special Summon (Spear Cretin)'),
                               desired={0}, cards=[dict(name='Giant Rat')], finished=True)
        loop.card_panel.observe.return_value = 'panel_verified'
        self.assertEqual(loop.observe(state), 'panel_verified')
        loop.observe(state)
        self.assertEqual(loop.observe(state), 'special_position_clicked: Attack')
        loop.actor.observe.assert_not_called()
        self.assertTrue(loop.summoned)
        add_monster(state, True, 4, monster('Giant Rat', '0xnew', 1400))
        self.assertEqual(loop.observe(state), 'special_summon_verified: Giant Rat')
        self.assertIsNone(loop.special_summon)

    def test_changed_duel_identity_stops(self):
        state, device = position_state(), desktop()
        actor = SpecialSummonController(device, config(), state, 'Giant Rat')
        state['manager'] = '0xother'
        with self.assertRaisesRegex(RuntimeError, 'identidad'):
            actor.observe(state)
        device.click.assert_not_called()

    def test_same_position_text_with_new_pointer_is_zone_selection(self):
        state, device = position_state(), desktop()
        add_monster(state, True, 2, monster('Assailant', '0xold', 1700))
        actor = SpecialSummonController(device, config(), state, 'Giant Rat')
        actor.observe(state)
        actor.observe(state)
        state['actions']['prompts'].update(CurrentChoiceHelper='0xnew', CurrentConfirmationHelper='0x0')
        actor.observe(state)
        self.assertEqual(actor.observe(state), 'special_zone_clicked')
        self.assertEqual(device.move.call_args.args[0], (790, 700))
        for attempt in (2, 3):
            for _ in range(7):
                actor.observe(state)
            self.assertEqual(actor.observe(state), f'special_zone_retry: {attempt}/3')
        self.assertEqual(device.click.call_count, 4)
        add_monster(state, True, 1, monster('Giant Rat', '0xnewcard', 1400))
        self.assertEqual(actor.observe(state), 'special_summon_verified: Giant Rat')
        self.assertEqual(device.click.call_count, 4)

    def test_closed_mode_without_new_choice_pointer_is_not_placement(self):
        state, device = position_state(), desktop()
        actor = SpecialSummonController(device, config(), state, 'Giant Rat')
        actor.observe(state)
        actor.observe(state)
        state['actions']['prompts']['CurrentConfirmationHelper'] = '0x0'
        for _ in range(15):
            actor.observe(state)
        device.click.assert_called_once_with()

    def test_pending_mode_retries_only_three_times(self):
        state, device = position_state(), desktop()
        actor = SpecialSummonController(device, config(), state, 'Giant Rat')
        actor.observe(state)
        actor.observe(state)
        for attempt in (2, 3):
            for _ in range(7):
                actor.observe(state)
            self.assertEqual(actor.observe(state), f'special_position_retry: {attempt}/3')
        for _ in range(15):
            actor.observe(state)
        self.assertEqual(device.click.call_count, 3)
