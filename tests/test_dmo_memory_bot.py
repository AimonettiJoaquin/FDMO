import copy
from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import Mock

from dmo_memory_bot import (Desktop, MonsterPlacement, Selector, SummonOption,
                            monster_zone_choice, probe_points, selection)


def state():
    cards = [dict(address='0x1000', name='Weak Card', slot=0),
             dict(address='0x2000', name='Good Card', slot=1)]
    field = dict(local=True, address='0x3000', owner_identity='0x4000',
                 generation_id=1, player_index=0, HandCards=cards, HandCardCount=2)
    helpers = [dict(CardLocation={'enum_raw': 1}, FieldID=0, ID=i,
                    options=[dict(value=2, name='Summon')]) for i in range(2)]
    links = [dict(card_address=c['address'], name=c['name'],
                  element=hex(0x5000+i*0x100), native_element=hex(0x6000+i*0x100),
                  hover=dict(hoverable=True, hovered=False)) for i, c in enumerate(cards)]
    return dict(timestamp=datetime.now(timezone.utc).isoformat(), status='duel',
                local_player=True, my_turn=True, phase='MAIN 1', player='0x7000',
                manager='0x8000', board=dict(fields=[field]),
                actions=dict(status='observed', flags=dict(GameStateOpen=True,
                            Busy=False, SelectingCard=False, ShowChainPrompt=False), helpers=helpers),
                advice=dict(status='suggestions', options=[dict(kind='summon_candidate',
                            address='0x2000', card='Good Card')]),
                scene=dict(status='hand_objects_linked', report_hints=dict(total=2, accepted=2),
                           hand_elements=links))


class SelectionTests(TestCase):
    def test_selects_exact_suggested_card_with_published_action(self):
        result = selection(state())
        self.assertEqual(result['card']['name'], 'Good Card')
        self.assertEqual(result['link']['card_address'], '0x2000')

    def test_stale_options_and_ambiguous_links_do_not_select(self):
        sample = state()
        sample['actions']['helpers'][1]['options'] = []
        self.assertIsNone(selection(sample))
        sample = state()
        sample['scene']['hand_elements'].append(copy.deepcopy(sample['scene']['hand_elements'][1]))
        self.assertIsNone(selection(sample))

    def test_foreign_turn_and_busy_state_do_not_select(self):
        sample = state()
        sample['my_turn'] = False
        self.assertIsNone(selection(sample))
        sample = state()
        sample['actions']['flags']['SelectingCard'] = True
        self.assertIsNone(selection(sample))

    def test_probe_points_are_bounded(self):
        points = probe_points(1920, 1080, [285, 957], 1, 7)
        self.assertGreater(len(points), 50)
        self.assertTrue(all(0 <= x < 1920 and 0 <= y < 1080 for x, y in points))
        self.assertLessEqual(len(points), 81)
        self.assertEqual(points[0], (395, 957))


class SelectorTests(TestCase):
    def desktop(self, point=(100, 900)):
        desktop = Mock()
        desktop.move.side_effect = lambda p: p
        desktop.position.return_value = point
        return desktop

    def test_only_target_hover_can_verify(self):
        desktop = self.desktop()
        actor = Selector(desktop, [(100, 900), (200, 900)])
        sample = state()
        self.assertEqual(actor.observe(sample), 'probing')
        for _ in range(5):
            sample['scene']['hand_elements'][0]['hover']['hovered'] = True
            self.assertNotEqual(actor.observe(sample), 'verified')
        self.assertEqual(actor.probes, 2)
        desktop.position.return_value = (200, 900)
        sample['scene']['hand_elements'][0]['hover']['hovered'] = False
        sample['scene']['hand_elements'][1]['hover']['hovered'] = True
        for _ in range(3):
            self.assertNotEqual(actor.observe(sample), 'verified')
        self.assertEqual(actor.observe(sample), 'verified')
        desktop.click.assert_not_called()

    def test_clicks_once_after_two_confirmations(self):
        desktop = self.desktop()
        actor = Selector(desktop, [(100, 900)], click=True)
        sample = state()
        actor.observe(sample)
        sample['scene']['hand_elements'][1]['hover']['hovered'] = True
        for _ in range(3):
            self.assertNotEqual(actor.observe(sample), 'clicked')
        self.assertEqual(actor.observe(sample), 'clicked')
        self.assertEqual(actor.observe(sample), 'observing_click')
        sample['scene']['hand_elements'][1]['selected'] = True
        self.assertEqual(actor.observe(sample), 'selected')
        self.assertIsNone(actor.observe(sample))
        desktop.click.assert_called_once_with()

    def test_click_without_selection_signal_reports_uncertainty(self):
        desktop = self.desktop()
        actor = Selector(desktop, [(100, 900)], click=True)
        sample = state()
        actor.observe(sample)
        sample['scene']['hand_elements'][1]['hover']['hovered'] = True
        for _ in range(4):
            actor.observe(sample)
        for _ in range(7):
            self.assertEqual(actor.observe(sample), 'observing_click')
        self.assertEqual(actor.observe(sample), 'click_unconfirmed')
        desktop.click.assert_called_once_with()

    def test_stale_timestamp_blocks_click(self):
        desktop = self.desktop()
        actor = Selector(desktop, [(100, 900)], click=True)
        sample = state()
        actor.observe(sample)
        sample['scene']['hand_elements'][1]['hover']['hovered'] = True
        for _ in range(3):
            actor.observe(sample)
        sample['timestamp'] = '2020-01-01T00:00:00+00:00'
        with self.assertRaisesRegex(RuntimeError, 'ya no es reciente'):
            actor.observe(sample)
        desktop.click.assert_not_called()

    def test_changed_hand_stops_before_click(self):
        desktop = self.desktop()
        actor = Selector(desktop, [(100, 900)], click=True)
        sample = state()
        actor.observe(sample)
        sample['board']['fields'][0]['HandCards'][1]['address'] = '0x9999'
        with self.assertRaisesRegex(RuntimeError, 'cambió'):
            actor.observe(sample)
        desktop.click.assert_not_called()

    def test_exhausted_search_stops_without_click(self):
        desktop = self.desktop()
        actor = Selector(desktop, [(100, 900)], click=True)
        sample = state()
        actor.observe(sample)
        for _ in range(4):
            actor.observe(sample)
        with self.assertRaisesRegex(RuntimeError, 'No se encontró'):
            actor.observe(sample)
        desktop.click.assert_not_called()

    def test_manual_cursor_move_invalidates_hover(self):
        desktop = self.desktop()
        actor = Selector(desktop, [(100, 900)], click=True)
        sample = state()
        actor.observe(sample)
        sample['scene']['hand_elements'][1]['hover']['hovered'] = True
        desktop.position.return_value = (600, 900)
        with self.assertRaisesRegex(RuntimeError, 'cursor se movió'):
            actor.observe(sample)
        desktop.click.assert_not_called()

    def test_transient_advice_wait_pauses_same_binding_without_input(self):
        desktop = self.desktop()
        actor = Selector(desktop, [(100, 900)], click=True)
        sample = state()
        actor.observe(sample)
        sample['scene']['hand_elements'][1]['hover']['hovered'] = True
        for _ in range(3):
            actor.observe(sample)
        paused = copy.deepcopy(sample)
        paused['advice'] = dict(status='wait', options=[])
        self.assertEqual(actor.observe(paused), 'waiting_advice')
        desktop.move.assert_called_once()
        desktop.click.assert_not_called()
        self.assertEqual(actor.observe(sample), 'checking')
        self.assertEqual(actor.observe(sample), 'clicked')
        desktop.click.assert_called_once_with()

    def test_advice_wait_does_not_hide_real_binding_or_action_change(self):
        for change in ('hand', 'element', 'native', 'action', 'turn', 'phase', 'prompt', 'busy'):
            with self.subTest(change=change):
                desktop = self.desktop()
                actor = Selector(desktop, [(100, 900)], click=True)
                sample = state()
                actor.observe(sample)
                sample['advice'] = dict(status='wait', options=[])
                if change == 'hand':
                    sample['board']['fields'][0]['HandCards'][1]['address'] = '0xother'
                elif change == 'element':
                    sample['scene']['hand_elements'][1]['element'] = '0xother'
                elif change == 'native':
                    sample['scene']['hand_elements'][1]['native_element'] = '0xother'
                elif change == 'action':
                    sample['actions']['helpers'][1]['options'] = []
                elif change == 'turn':
                    sample['my_turn'] = False
                elif change == 'phase':
                    sample['phase'] = 'BATTLE'
                elif change == 'prompt':
                    sample['actions']['prompts'] = dict(CurrentChoiceHelper='0xpending')
                else:
                    sample['actions']['flags']['Busy'] = True
                with self.assertRaisesRegex(RuntimeError, 'cambió'):
                    actor.observe(sample)
                desktop.click.assert_not_called()

    def test_advice_wait_keeps_cursor_guard_and_is_bounded(self):
        desktop = self.desktop()
        actor = Selector(desktop, [(100, 900)], click=True)
        sample = state()
        actor.observe(sample)
        sample['advice'] = dict(status='wait', options=[])
        desktop.position.return_value = (600, 900)
        with self.assertRaisesRegex(RuntimeError, 'cursor se movió'):
            actor.observe(sample)
        desktop.position.return_value = (100, 900)
        with self.assertRaisesRegex(RuntimeError, 'No se estabilizó la recomendación'):
            for _ in range(8):
                actor.observe(sample)
        desktop.click.assert_not_called()


class DesktopTests(TestCase):
    def test_warp_then_relative_motion_and_readback(self):
        desktop = Desktop()
        with __import__('unittest').mock.patch('dmo_memory_bot.subprocess.run') as run:
            run.side_effect = [Mock(), Mock(), Mock(stdout='{"x": 102, "y": 900}')]
            self.assertEqual(desktop.move((100, 900)), (102, 900))
        self.assertEqual(run.call_args_list[0].args[0][:2], ['hyprctl', 'dispatch'])
        self.assertEqual(run.call_args_list[1].args[0],
                         ['ydotool', 'mousemove', '-x', '15', '-y', '0'])
        self.assertEqual(run.call_args_list[2].args[0], ['hyprctl', '-j', 'cursorpos'])

    def test_bad_position_refuses_probe(self):
        desktop = Desktop()
        with __import__('unittest').mock.patch('dmo_memory_bot.subprocess.run') as run:
            run.side_effect = [Mock(), Mock(), Mock(stdout='{"x": 250, "y": 900}')] * 3
            with self.assertRaisesRegex(RuntimeError, 'lejos'):
                desktop.move((100, 900))
        self.assertEqual(run.call_count, 9)

    def test_transient_cursor_error_retries_before_click(self):
        desktop = Desktop()
        with __import__('unittest').mock.patch('dmo_memory_bot.subprocess.run') as run:
            run.side_effect = [Mock(), Mock(), Mock(stdout='{"x": 934, "y": 569}'),
                               Mock(), Mock(), Mock(stdout='{"x": 963, "y": 561}')]
            self.assertEqual(desktop.move((963, 561)), (963, 561))
        self.assertEqual(run.call_count, 6)

    def test_scroll_up_uses_relative_wheel_event(self):
        desktop = Desktop()
        with __import__('unittest').mock.patch('dmo_memory_bot.subprocess.run') as run:
            desktop.scroll_up(12)
        self.assertEqual(run.call_args.args[0],
                         ['ydotool', 'mousemove', '--wheel', '-x', '0', '-y', '12'])


class SummonOptionTests(TestCase):
    def setup_spell_option(self):
        _, desktop, sample = self.setup_option()
        sample['advice']['options'][0]['kind'] = 'spell_candidate'
        for helper in sample['actions']['helpers']:
            helper['options'] = [dict(value=4, name='Set'), dict(value=0, name='ActivateEffect')]
        option = SummonOption(desktop, dict(screen=dict(width=1920, height=1080),
            points=dict(hand_first=[285, 957], summon=[165, 808])), kind='spell_candidate')
        return option, desktop, sample

    def test_spell_uses_captured_two_button_layout_instead_of_summon(self):
        option, desktop, sample = self.setup_spell_option()
        desktop.position.side_effect = [(835, 957), (835, 957), (765, 825)]
        desktop.move.return_value = (765, 825)
        self.assertEqual(option.observe(sample), 'checking_selected_card')
        self.assertEqual(option.observe(sample), 'activate_option_clicked')
        desktop.move.assert_called_once_with((765, 825))
        desktop.click.assert_called_once_with()

    def test_spell_refuses_an_uncalibrated_menu(self):
        option, desktop, sample = self.setup_spell_option()
        sample['actions']['helpers'][1]['options'].append(dict(value=1, name='SpecialSummon'))
        desktop.position.return_value = (835, 957)
        option.observe(sample)
        with self.assertRaisesRegex(RuntimeError, 'no coincide'):
            option.observe(sample)
        desktop.move.assert_not_called()
        desktop.click.assert_not_called()

    def setup_option(self):
        config = dict(screen=dict(width=1920, height=1080),
                      points=dict(hand_first=[285, 957], summon=[165, 808]))
        desktop = Mock()
        option = SummonOption(desktop, config)
        sample = state()
        sample['scene']['hand_elements'][1]['selected'] = True
        sample['scene']['hand_elements'][1]['hover']['hovered'] = True
        return option, desktop, sample

    def test_clicks_relative_option_once_and_records_followup(self):
        option, desktop, sample = self.setup_option()
        desktop.position.side_effect = [(628, 957), (628, 957), (508, 808)]
        desktop.move.return_value = (508, 808)
        self.assertEqual(option.observe(sample), 'checking_selected_card')
        self.assertEqual(option.observe(sample), 'summon_option_clicked')
        desktop.move.assert_called_once_with((508, 808))
        desktop.click.assert_called_once_with()
        for _ in range(15):
            self.assertEqual(option.observe(sample), 'observing_option')
        self.assertEqual(option.observe(sample), 'option_click_recorded')
        desktop.click.assert_called_once_with()

    def test_unselected_or_unhovered_card_is_not_clicked(self):
        option, desktop, sample = self.setup_option()
        sample['scene']['hand_elements'][1]['selected'] = False
        self.assertEqual(option.observe(sample), 'waiting_selected_card')
        sample['scene']['hand_elements'][1]['selected'] = True
        sample['scene']['hand_elements'][1]['hover']['hovered'] = False
        self.assertEqual(option.observe(sample), 'waiting_selected_card')
        desktop.click.assert_not_called()

    def test_option_mode_does_not_wait_forever_for_an_old_menu(self):
        option, desktop, sample = self.setup_option()
        sample['scene']['hand_elements'][1]['selected'] = False
        for _ in range(59):
            self.assertEqual(option.observe(sample), 'waiting_selected_card')
        with self.assertRaisesRegex(RuntimeError, '60 muestras'):
            option.observe(sample)
        desktop.click.assert_not_called()

    def test_manual_motion_resets_confirmation(self):
        option, desktop, sample = self.setup_option()
        desktop.position.side_effect = [(628, 957), (700, 957), (700, 957), (580, 808)]
        desktop.move.return_value = (580, 808)
        self.assertEqual(option.observe(sample), 'checking_selected_card')
        self.assertEqual(option.observe(sample), 'checking_selected_card')
        self.assertEqual(option.observe(sample), 'summon_option_clicked')
        desktop.move.assert_called_once_with((580, 808))

    def test_old_state_and_wrong_position_block_option_click(self):
        option, desktop, sample = self.setup_option()
        desktop.position.return_value = (628, 957)
        option.observe(sample)
        sample['timestamp'] = '2020-01-01T00:00:00+00:00'
        with self.assertRaisesRegex(RuntimeError, 'ya no es reciente'):
            option.observe(sample)
        desktop.click.assert_not_called()
        option, desktop, sample = self.setup_option()
        desktop.position.side_effect = [(628, 957), (628, 957), (700, 808)]
        desktop.move.return_value = (508, 808)
        option.observe(sample)
        with self.assertRaisesRegex(RuntimeError, 'cursor cambió'):
            option.observe(sample)
        desktop.click.assert_not_called()


class MonsterPlacementTests(TestCase):
    def sample(self):
        sample = state()
        field = sample['board']['fields'][0]
        field['MonsterCards'] = [dict(unknown=True, diagnostic=dict(CurrentName='',
            CardLocation=dict(enum_raw=0), cardFinder=dict(CardID=0),
            OverrideCardfinder=dict(CardID=0))) for _ in range(5)]
        field['visual_candidates'] = dict(MonsterCards=dict(source='MonsterVisualHelpers',
            candidates=[dict(helper_index=i, helper_present=False, card=None)
                        for i in range(5)]))
        sample['actions']['prompts'] = dict(CurrentChoiceHelper='0xaaaa',
            CurrentOptionHelper='0x0', CurrentConfirmationHelper='0x0')
        sample['actions']['flags']['GameStateOpen'] = False
        return sample

    def actor(self, *, card_address=None):
        desktop = Mock()
        desktop.move.return_value = (971, 696)
        desktop.position.return_value = (971, 696)
        return MonsterPlacement(desktop, dict(screen=dict(width=1920, height=1080),
            points=dict(summon_slot=[971, 696]), zones=dict(own_monsters=[
                [604, 696], [790, 700], [960, 728], [1138, 686], [1320, 689]])),
            'Good Card', card_address=card_address), desktop

    def test_duplicate_names_require_exact_copy_and_verify_its_removal(self):
        sample = self.sample()
        sample['board']['fields'][0]['HandCards'][0]['name'] = 'Good Card'
        self.assertIsNone(monster_zone_choice(sample, 'Good Card'))
        identity = monster_zone_choice(sample, 'Good Card', card_address='0x2000')
        self.assertEqual(identity[4], '0x2000')
        self.assertIsNone(monster_zone_choice(sample, 'Good Card', card_address='0xdead'))
        actor, desktop = self.actor(card_address='0x2000')
        self.assertEqual(actor.observe(sample), 'checking_zone_choice')
        self.assertEqual(actor.observe(sample), 'zone_clicked')
        after = copy.deepcopy(sample)
        field = after['board']['fields'][0]
        field['HandCardCount'] = 1
        field['HandCards'] = field['HandCards'][1:]  # Wrong copy removed.
        field['visual_candidates']['MonsterCards']['candidates'][2] = dict(
            helper_index=2, helper_present=True,
            card=dict(name='Good Card', address='0x9000', location='Field'))
        self.assertEqual(actor.observe(after), 'observing_placement')
        field['HandCards'] = sample['board']['fields'][0]['HandCards'][:1]
        self.assertEqual(actor.observe(after), 'summon_verified')
        desktop.click.assert_called_once_with()

    def test_choice_requires_empty_field_and_pending_prompt(self):
        sample = self.sample()
        self.assertIsNotNone(monster_zone_choice(sample, 'Good Card'))
        sample['actions']['prompts']['CurrentChoiceHelper'] = '0x0'
        self.assertIsNone(monster_zone_choice(sample, 'Good Card'))

    def test_occupied_center_selects_nearest_free_zone(self):
        sample = self.sample()
        sample['board']['fields'][0]['visual_candidates']['MonsterCards']['candidates'][2] = dict(
            helper_index=2, card=dict(name='Tiger', address='0x9000', location='Field'))
        self.assertEqual(monster_zone_choice(sample, 'Good Card')[-1], 1)
        actor, desktop = self.actor()
        desktop.move.return_value = (790, 700)
        desktop.position.return_value = (790, 700)
        self.assertEqual(actor.observe(sample), 'checking_zone_choice')
        self.assertEqual(actor.observe(sample), 'zone_clicked')
        desktop.move.assert_called_once_with((790, 700))
        sample = self.sample()
        sample['board']['fields'][0]['visual_candidates']['MonsterCards']['candidates'][2]['helper_present'] = True
        self.assertIsNone(monster_zone_choice(sample, 'Good Card'))

    def test_one_click_then_visual_field_card_confirms(self):
        sample = self.sample()
        actor, desktop = self.actor()
        self.assertEqual(actor.observe(sample), 'checking_zone_choice')
        self.assertEqual(actor.observe(sample), 'zone_clicked')
        desktop.click.assert_called_once_with()
        self.assertEqual(actor.observe(sample), 'observing_placement')
        after = copy.deepcopy(sample)
        field = after['board']['fields'][0]
        field['HandCards'] = field['HandCards'][:1]
        field['HandCardCount'] = 1
        field['visual_candidates']['MonsterCards']['candidates'][2] = dict(
            helper_index=2, helper_present=True,
            card=dict(name='Good Card', address='0x9000', location='Field'))
        self.assertEqual(actor.observe(after), 'summon_verified')
        desktop.click.assert_called_once_with()

    def test_hand_change_without_matching_field_card_is_not_verified(self):
        sample = self.sample()
        actor, _ = self.actor()
        actor.observe(sample)
        actor.observe(sample)
        after = copy.deepcopy(sample)
        field = after['board']['fields'][0]
        field['HandCards'] = field['HandCards'][:1]
        field['HandCardCount'] = 1
        self.assertEqual(actor.observe(after), 'observing_placement')

    def test_changed_prompt_and_stale_sample_prevent_click(self):
        sample = self.sample()
        actor, desktop = self.actor()
        actor.observe(sample)
        sample['actions']['prompts']['CurrentChoiceHelper'] = '0xbbbb'
        self.assertEqual(actor.observe(sample), 'checking_zone_choice')
        desktop.click.assert_not_called()
        sample['timestamp'] = '2020-01-01T00:00:00+00:00'
        with self.assertRaisesRegex(RuntimeError, 'ya no es reciente'):
            actor.observe(sample)
        desktop.click.assert_not_called()
