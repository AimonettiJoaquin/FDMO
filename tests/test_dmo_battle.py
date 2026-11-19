import copy
from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import Mock

from dmo.strategy.dmo_battle import battle_decision
from dmo.bot.dmo_turn_bot import TurnController


def sample():
    def field(local):
        return dict(local=local, address='0x100' if local else '0x200',
                    player_index=0 if local else 1, life_points_candidate=8000,
                    visual_candidates=dict(MonsterCards=dict(
                        candidates=[dict(helper_index=i, helper_address='0x0', card=None)
                                    for i in range(5)])))
    return dict(timestamp=datetime.now(timezone.utc).isoformat(), status='duel',
                local_player=True, my_turn=True, player='0x300', manager='0x400',
                own_id=0, turn_id=0, phase='BATTLE',
                board=dict(fields=[field(True), field(False)], errors=[]),
                actions=dict(status='observed', flags=dict(GameStateOpen=True, Busy=False,
                    SelectingCard=False, ShowChainPrompt=False),
                    prompts=dict(CurrentChoiceHelper='0x0', CurrentOptionHelper='0x0',
                                 CurrentConfirmationHelper='0x0'), helpers=[]),
                scene=dict(field_elements=[]))


def monster(name, address, atk, position=1):
    return dict(name=name, address=address, location='Field', face_down=False,
                face_position_raw=position, ATK=atk, DEF=1000, FieldSlot=7,
                CanAttack=True, AttackedThisTurn=False)


def add_monster(state, local, slot, card):
    field = state['board']['fields'][0 if local else 1]
    field['visual_candidates']['MonsterCards']['candidates'][slot] = dict(
        helper_index=slot, helper_address=hex(0x1000 + slot), card=card)


def publish_attack(state, slot=7):
    state['actions']['helpers'].append(dict(CardLocation=dict(enum_raw=2),
        CardType=dict(enum_raw=1), FieldID=0, ID=slot,
        options=[dict(value=5, name='Attack')]))


class BattleDecisionTests(TestCase):
    def test_chooses_largest_damage_against_visible_attack_position(self):
        state = sample()
        add_monster(state, True, 2, monster('Tiger', '0xa', 1700))
        add_monster(state, False, 1, monster('Weak', '0xb', 1000))
        add_monster(state, False, 3, monster('Strong', '0xc', 1500))
        publish_attack(state)
        plan = battle_decision(state)
        self.assertEqual((plan['kind'], plan['attacker_zone'], plan['target_zone'],
                          plan['expected_damage']), ('attack', 2, 1, 700))

    def test_direct_attack_only_when_rival_has_no_monsters(self):
        state = sample()
        add_monster(state, True, 2, monster('Tiger', '0xa', 1700))
        publish_attack(state)
        self.assertEqual(battle_decision(state)['kind'], 'direct')
        add_monster(state, False, 1, dict(address='0xb', hidden=True, face_down=True))
        plan = battle_decision(state)
        self.assertEqual((plan['kind'], plan['target_zone'], plan['uncertain_target']),
                         ('attack', 1, True))

    def test_hidden_target_is_used_when_visible_targets_are_unfavorable(self):
        state = sample()
        add_monster(state, True, 2, monster('Tiger', '0xa', 1700))
        add_monster(state, False, 1, monster('Too strong', '0xb', 2800))
        add_monster(state, False, 4, dict(address='0xc', hidden=True, face_down=True))
        publish_attack(state)
        self.assertEqual(battle_decision(state)['target'], '0xc')

    def test_no_published_attack_ends_and_incomplete_state_refuses(self):
        state = sample()
        add_monster(state, True, 2, monster('Tiger', '0xa', 1700))
        self.assertEqual(battle_decision(state)['kind'], 'end')
        state['actions']['status'] = 'partial'
        with self.assertRaisesRegex(ValueError, 'cliente no está listo'):
            battle_decision(state)

    def test_defense_uses_def_not_atk(self):
        state = sample()
        add_monster(state, True, 2, monster('Tiger', '0xa', 1700))
        defended = monster('Wall', '0xb', 3000, position=2)
        defended['DEF'] = 1500
        add_monster(state, False, 1, defended)
        publish_attack(state)
        self.assertEqual(battle_decision(state)['target'], '0xb')

    def test_verified_attacker_is_not_reused_even_if_action_list_is_stale(self):
        state = sample()
        add_monster(state, True, 2, monster('Tiger', '0xa', 1700))
        publish_attack(state)
        self.assertEqual(battle_decision(state)['kind'], 'direct')
        self.assertEqual(battle_decision(state, {'0xa'})['kind'], 'end')


class TurnControllerTests(TestCase):
    def actor(self):
        desktop = Mock()
        desktop.move.side_effect = lambda point: point
        desktop.position.return_value = (1067, 330)
        config = dict(screen=dict(width=1920, height=1080),
            points=dict(phase_battle=[1067, 330], phase_end=[1434, 343],
                        direct_attack=[955, 207]),
            zones=dict(own_monsters=[[604, 696], [790, 700], [960, 728],
                                     [1138, 686], [1320, 689]],
                       opponent_monsters=[[707, 303], [842, 297], [952, 305],
                                          [1088, 302], [1214, 302]]),
            attack_sword_offset=[0, -120], target_confirm_offset=[0, 105])
        return TurnController(desktop, config), desktop

    def test_main_requests_battle_then_ends_if_unavailable(self):
        actor, desktop = self.actor()
        state = sample()
        state['phase'] = 'MAIN 1'
        self.assertIsNone(actor.observe(state))
        self.assertEqual(actor.observe(state), 'battle_clicked')
        self.assertEqual(desktop.click.call_count, 1)
        # A disabled Battle choice leaves the phase picker open.
        desktop.position.return_value = (1434, 343)
        for _ in range(17):
            self.assertIsNone(actor.observe(state))
        self.assertEqual(actor.observe(state), 'end_clicked')
        self.assertEqual(desktop.key.call_count, 1)
        state['phase'] = 'END'
        self.assertEqual(actor.observe(state), 'end_phase_verified')
        self.assertFalse(actor.finished)
        state['my_turn'] = False
        self.assertEqual(actor.observe(state), 'turn_passed')
        self.assertTrue(actor.finished)

    def test_battle_without_attack_ends_after_stable_decision(self):
        actor, desktop = self.actor()
        state = sample()
        self.assertEqual(actor.observe(state), 'battle_ready')
        self.assertIsNone(actor.observe(state))
        desktop.position.return_value = (1434, 343)
        self.assertEqual(actor.observe(state), 'end_clicked')
        desktop.key.assert_called_once_with('tab')

    def test_new_own_draw_after_end_confirms_turn_even_if_rival_was_missed(self):
        actor, desktop = self.actor()
        state = sample()
        actor.begin(state)
        desktop.position.return_value = (1434, 343)
        self.assertEqual(actor.end(state), 'end_clicked')
        state['phase'] = 'DRAW'
        self.assertEqual(actor.observe(state), 'turn_passed')
        self.assertTrue(actor.finished)

    def test_new_own_draw_after_observed_end_confirms_turn(self):
        actor, desktop = self.actor()
        state = sample()
        actor.begin(state)
        desktop.position.return_value = (1434, 343)
        actor.end(state)
        state['phase'] = 'END'
        self.assertEqual(actor.observe(state), 'end_phase_verified')
        state['phase'] = 'DRAW'
        self.assertEqual(actor.observe(state), 'turn_passed')

    def test_end_turn_mode_waits_for_animation_then_clicks_once(self):
        actor, desktop = self.actor()
        actor.end_turn_only = True
        state = sample()
        state['actions']['flags']['GameStateOpen'] = False
        self.assertIsNone(actor.observe(state))
        desktop.click.assert_not_called()
        state['actions']['flags']['GameStateOpen'] = True
        self.assertIsNone(actor.observe(state))
        desktop.position.return_value = (1434, 343)
        self.assertEqual(actor.observe(state), 'end_clicked')
        desktop.click.assert_called_once_with()
        state['my_turn'] = False
        self.assertEqual(actor.observe(state), 'turn_passed')

    def test_attack_requires_selected_field_element(self):
        actor, desktop = self.actor()
        state = sample()
        add_monster(state, True, 2, monster('Tiger', '0xa', 1700))
        add_monster(state, False, 1, monster('Weak', '0xb', 1000))
        publish_attack(state)
        actor.observe(state)
        actor.observe(state)
        desktop.position.return_value = (960, 728)
        self.assertEqual(actor.observe(state), 'attacker_clicked')
        self.assertEqual(desktop.click.call_count, 1)
        self.assertIsNone(actor.observe(state))
        desktop.position.return_value = (960, 608)
        state['scene']['field_elements'] = [dict(card_address='0xa', visual_slot=2, selected=True)]
        self.assertEqual(actor.observe(state), 'attack_option_clicked')
        self.assertEqual(desktop.click.call_count, 2)

    def test_target_prompt_can_keep_game_state_open(self):
        actor, desktop = self.actor()
        state = sample()
        add_monster(state, True, 2, monster('Tiger', '0xa', 1700))
        publish_attack(state)
        actor.observe(state)
        actor.observe(state)
        desktop.position.return_value = (960, 728)
        actor.observe(state)
        state['scene']['field_elements'] = [dict(card_address='0xa', visual_slot=2, selected=True)]
        desktop.position.return_value = (960, 608)
        self.assertEqual(actor.observe(state), 'attack_option_clicked')
        state['scene']['field_elements'][0]['selected'] = False
        state['actions']['prompts']['CurrentChoiceHelper'] = '0x1234'
        self.assertIsNone(actor.observe(state))
        desktop.position.return_value = (955, 207)
        self.assertEqual(actor.observe(state), 'target_clicked')
        desktop.move.assert_called_with((955, 207))
        self.assertEqual(desktop.click.call_count, 3)

    def test_resume_direct_clicks_only_pending_single_attacker(self):
        actor, desktop = self.actor()
        actor.resume_direct = 'Tiger'
        state = sample()
        add_monster(state, True, 2, monster('Tiger', '0xa', 1700))
        publish_attack(state)
        state['actions']['prompts']['CurrentChoiceHelper'] = '0x1234'
        self.assertIsNone(actor.observe(state))
        desktop.position.return_value = (955, 207)
        self.assertEqual(actor.observe(state), 'target_clicked')
        desktop.move.assert_called_once_with((955, 207))
        desktop.click.assert_called_once_with()
        self.assertIsNone(actor.observe(state))  # No second click while target remains pending.
        after = copy.deepcopy(state)
        after['actions']['prompts']['CurrentChoiceHelper'] = '0x0'
        after['actions']['flags']['GameStateOpen'] = False
        after['board']['fields'][1]['life_points_candidate'] = 6300
        self.assertEqual(actor.observe(after), 'attack_verified')
        desktop.click.assert_called_once_with()
        after['actions']['flags']['GameStateOpen'] = True
        self.assertIsNone(actor.observe(after))
        desktop.position.return_value = (1434, 343)
        self.assertEqual(actor.observe(after), 'end_clicked')

    def test_unrelated_life_change_does_not_verify_direct_attack(self):
        actor, desktop = self.actor()
        actor.resume_direct = 'Tiger'
        state = sample()
        add_monster(state, True, 2, monster('Tiger', '0xa', 1700))
        publish_attack(state)
        state['actions']['prompts']['CurrentChoiceHelper'] = '0x1234'
        actor.observe(state)
        desktop.position.return_value = (955, 207)
        actor.observe(state)
        after = copy.deepcopy(state)
        after['actions']['prompts']['CurrentChoiceHelper'] = '0x0'
        after['actions']['flags']['GameStateOpen'] = False
        after['board']['fields'][1]['life_points_candidate'] = 7900
        self.assertIsNone(actor.observe(after))

    def test_hidden_target_reveal_verifies_attack_without_damage(self):
        actor, _ = self.actor()
        state = sample()
        add_monster(state, True, 2, monster('Tiger', '0xa', 1700))
        add_monster(state, False, 1, dict(address='0xb', hidden=True, face_down=True))
        publish_attack(state)
        actor.plan = battle_decision(state)
        actor.record_attack_baseline(state)
        actor.stage = 'await_target'
        actor.identity = (state['player'], state['manager'], state['own_id'])
        after = copy.deepcopy(state)
        after['actions']['flags']['GameStateOpen'] = False
        after['board']['fields'][1]['visual_candidates']['MonsterCards']['candidates'][1]['card'] = dict(
            address='0xb', name='Revealed defender', face_down=False, face_position_raw=2)
        self.assertEqual(actor.observe(after), 'attack_verified')

    def test_resume_target_requires_selection_instead_of_hover(self):
        actor, desktop = self.actor()
        actor.resume_target = 'Weak'
        state = sample()
        add_monster(state, True, 2, monster('Tiger', '0xa', 1700))
        add_monster(state, False, 1, monster('Weak', '0xb', 1000))
        publish_attack(state)
        state['actions']['prompts']['CurrentChoiceHelper'] = '0x1234'
        state['scene']['cursor_references'] = dict(objects=dict(
            HoveredBattleObject=dict(address='0x9000', class_name='GameObject')))
        for _ in range(3):
            self.assertIsNone(actor.observe(state))
        desktop.click.assert_not_called()
        state['scene']['field_elements'] = [dict(card_address='0xb', visual_slot=1,
            local=False, zone='MonsterCards', world=True, selected=True)]
        self.assertIsNone(actor.observe(state))
        desktop.position.return_value = (1088, 407)
        self.assertEqual(actor.observe(state), 'attack_confirm_clicked')
        desktop.move.assert_called_once_with((1088, 407))
        desktop.click.assert_called_once_with()

    def test_rival_helper_four_clicks_leftmost_screen_zone(self):
        actor, desktop = self.actor()
        state = sample()
        add_monster(state, True, 2, monster('Zombie Master', '0xa', 1800))
        add_monster(state, False, 4, monster('Armored Zombie', '0xb', 1500))
        publish_attack(state)
        actor.plan = battle_decision(state)
        actor.stage = 'await_target_prompt'
        state['actions']['prompts']['CurrentChoiceHelper'] = '0x1234'
        self.assertIsNone(actor.observe(state))
        desktop.position.return_value = (707, 303)
        self.assertEqual(actor.observe(state), 'target_aimed')
        desktop.move.assert_called_once_with((707, 303))
        desktop.click.assert_not_called()
        self.assertEqual(actor.plan['target_zone'], 4)
        self.assertEqual(actor.attack_baseline[-1][0], '0xb')

    def test_rival_helper_zero_clicks_rightmost_screen_zone(self):
        actor, desktop = self.actor()
        state = sample()
        add_monster(state, True, 2, monster('Zombie Master', '0xa', 1800))
        add_monster(state, False, 0, monster('Armored Zombie', '0xb', 1500))
        publish_attack(state)
        actor.plan = battle_decision(state)
        actor.stage = 'await_target_prompt'
        state['actions']['prompts']['CurrentChoiceHelper'] = '0x1234'
        self.assertIsNone(actor.observe(state))
        desktop.position.return_value = (1214, 302)
        self.assertEqual(actor.observe(state), 'target_aimed')
        desktop.move.assert_called_once_with((1214, 302))
        desktop.click.assert_not_called()

    def test_calibrated_target_and_confirm_use_individual_zone_points(self):
        actor, desktop = self.actor()
        actor.config['zones']['memory_opponent_targets'] = [
            [717, 300], [840, 300], [955, 300], [1084, 300], [1210, 300]]
        actor.config['zones']['memory_opponent_confirms'] = [
            [693, 378], [825, 380], [955, 380], [1099, 380], [1229, 380]]
        state = sample()
        add_monster(state, True, 2, monster('Ox', '0xa', 1700))
        add_monster(state, False, 4, dict(address='0xb', hidden=True, face_down=True))
        publish_attack(state)
        actor.plan = battle_decision(state)
        actor.stage = 'await_target_prompt'
        state['actions']['prompts']['CurrentChoiceHelper'] = '0x1234'
        self.assertIsNone(actor.observe(state))
        desktop.position.return_value = (717, 300)
        self.assertEqual(actor.observe(state), 'target_clicked_calibrated')
        desktop.move.assert_called_once_with((717, 300))
        self.assertEqual(desktop.click.call_count, 1)
        actor.stage = 'await_target'
        state['scene']['field_elements'] = [dict(card_address='0xb', visual_slot=4,
            local=False, zone='MonsterCards', world=True, selected=True)]
        self.assertIsNone(actor.observe(state))
        desktop.position.return_value = (693, 378)
        self.assertEqual(actor.observe(state), 'attack_confirm_clicked')
        self.assertEqual(desktop.move.call_args.args, ((693, 378),))

    def target_actor(self):
        actor, desktop = self.actor()
        state = sample()
        add_monster(state, True, 2, monster('Ox', '0xa', 1700))
        add_monster(state, False, 4, dict(address='0xb', hidden=True, face_down=True))
        publish_attack(state)
        actor.plan = battle_decision(state)
        actor.stage = 'await_target_prompt'
        state['actions']['prompts']['CurrentChoiceHelper'] = '0x1234'
        actor.observe(state)
        desktop.position.return_value = (707, 303)
        self.assertEqual(actor.observe(state), 'target_aimed')
        state['scene']['cursor_references'] = dict(objects=dict(
            HoveredBattleObject=dict(address='0x9000', class_name='GameObject')))
        return actor, desktop, state

    def test_target_click_waits_for_hover_and_confirm_waits_for_selection(self):
        actor, desktop, state = self.target_actor()
        self.assertIsNone(actor.observe(state))
        desktop.click.assert_not_called()
        self.assertEqual(actor.observe(state), 'target_clicked')
        for _ in range(3):
            self.assertIsNone(actor.observe(state))
        self.assertEqual(desktop.click.call_count, 1)
        state['scene']['field_elements'] = [dict(card_address='0xb', visual_slot=4,
            local=False, zone='MonsterCards', world=True, selected=True)]
        self.assertIsNone(actor.observe(state))
        desktop.position.return_value = (707, 408)
        self.assertEqual(actor.observe(state), 'attack_confirm_clicked')
        self.assertEqual(desktop.click.call_count, 2)
        for _ in range(3):
            self.assertIsNone(actor.observe(state))
        self.assertEqual(desktop.click.call_count, 2)

    def test_target_changed_while_waiting_for_hover_refuses_click(self):
        actor, desktop, state = self.target_actor()
        actor.observe(state)
        state['board']['fields'][1]['visual_candidates']['MonsterCards']['candidates'][4]['card']['address'] = '0xother'
        with self.assertRaisesRegex(RuntimeError, 'objetivo cambió'):
            actor.observe(state)
        desktop.click.assert_not_called()

    def test_cursor_drift_reaims_without_clicking(self):
        actor, desktop, state = self.target_actor()
        cursor = [1000, 500]
        def move(point):
            cursor[:] = point
            return point
        desktop.move.side_effect = move
        desktop.position.side_effect = lambda: tuple(cursor)
        self.assertEqual(actor.observe(state), 'target_reaimed: 1/3')
        desktop.click.assert_not_called()
        self.assertIsNone(actor.observe(state))
        self.assertEqual(actor.observe(state), 'target_clicked')

    def test_foreign_hover_probes_until_it_matches_defender(self):
        actor, desktop = self.actor()
        cursor = [952, 305]
        def move(point):
            cursor[:] = point
            return point
        desktop.move.side_effect = move
        desktop.position.side_effect = lambda: tuple(cursor)
        state = sample()
        add_monster(state, True, 2, monster('Ox', '0xa', 1700))
        add_monster(state, False, 2, dict(address='0xb', hidden=True, face_down=True))
        publish_attack(state)
        actor.plan = battle_decision(state)
        actor.stage = 'await_target_prompt'
        state['actions']['prompts']['CurrentChoiceHelper'] = '0x1234'
        state['scene']['field_elements'] = [dict(card_address='0xb', visual_slot=2,
            local=False, zone='MonsterCards', world=True, selected=False,
            native_element='0x9300', game_object_link=dict(
                verified=True, component='0x9300', game_object='0x9000'))]
        state['scene']['cursor_references'] = dict(objects=dict(
            HoveredBattleObject=dict(address='0x8000', class_name='GameObject',
                                     native_pointer='0x8800')))
        self.assertIsNone(actor.observe(state))
        self.assertEqual(actor.observe(state), 'target_aimed')
        self.assertEqual(actor.target_point, (952, 305))
        for _ in range(2):
            self.assertIsNone(actor.observe(state))
        self.assertEqual(actor.observe(state), 'target_probe_aimed')
        self.assertEqual(actor.target_point, (977, 305))
        for _ in range(2):
            self.assertIsNone(actor.observe(state))
        self.assertEqual(actor.observe(state), 'target_probe_aimed')
        while actor.target_point != (707, 303):
            for _ in range(2):
                self.assertIsNone(actor.observe(state))
            self.assertEqual(actor.observe(state), 'target_probe_aimed')
        state['scene']['cursor_references']['objects']['HoveredBattleObject'] = dict(
            address='0x9000', class_name='GameObject', native_pointer='0x9000')
        self.assertIsNone(actor.observe(state))
        self.assertEqual(actor.observe(state), 'target_clicked')
        desktop.click.assert_called_once_with()

    def test_hover_uses_actual_game_object_without_fixed_allocation_gap(self):
        actor, desktop, state = self.target_actor()
        state['scene']['field_elements'] = [dict(card_address='0xb', visual_slot=4,
            local=False, zone='MonsterCards', world=True, native_element='0x9300',
            game_object_link=dict(verified=True, component='0x9300', game_object='0xc800'))]
        state['scene']['cursor_references']['objects']['HoveredBattleObject'].update(
            native_pointer='0xc800')
        self.assertIsNone(actor.observe(state))
        self.assertEqual(actor.observe(state), 'target_clicked')
        desktop.click.assert_called_once_with()

    def test_fixed_gap_coincidence_does_not_authorize_target_click(self):
        actor, desktop, state = self.target_actor()
        state['scene']['field_elements'] = [dict(card_address='0xb', visual_slot=4,
            local=False, zone='MonsterCards', world=True, native_element='0x9300',
            game_object_link=dict(verified=True, component='0x9300', game_object='0xc800'))]
        state['scene']['cursor_references']['objects']['HoveredBattleObject'].update(
            native_pointer='0x9000')
        for _ in range(2):
            self.assertIsNone(actor.observe(state))
        desktop.click.assert_not_called()

    def test_native_target_without_verified_object_link_refuses_hover(self):
        actor, desktop, state = self.target_actor()
        state['scene']['field_elements'] = [dict(card_address='0xb', visual_slot=4,
            local=False, zone='MonsterCards', world=True, native_element='0x9300')]
        state['scene']['cursor_references']['objects']['HoveredBattleObject'].update(
            native_pointer='0x9000')
        self.assertFalse(actor.confirmed_target_hover(state))
        self.assertFalse(actor.confirmed_target_hover(state))
        desktop.click.assert_not_called()

    def test_hidden_verified_target_waits_on_monster_row_before_click(self):
        actor, desktop = self.actor()
        actor.config['zones']['memory_opponent_targets'] = [
            [717, 300], [840, 300], [955, 300], [1084, 300], [1210, 300]]
        state = sample()
        add_monster(state, True, 2, monster('Ox', '0xa', 1700))
        add_monster(state, False, 4, dict(address='0xb', hidden=True, face_down=True))
        publish_attack(state)
        actor.plan = battle_decision(state)
        actor.stage = 'await_target_prompt'
        state['actions']['prompts']['CurrentChoiceHelper'] = '0x1234'
        state['scene']['field_elements'] = [dict(card_address='0xb', visual_slot=4,
            local=False, zone='MonsterCards', world=True, native_element='0x9300',
            game_object_link=dict(verified=True, component='0x9300', game_object='0xc800'))]
        desktop.position.return_value = (717, 300)
        self.assertIsNone(actor.observe(state))
        self.assertEqual(actor.observe(state), 'target_aimed')
        desktop.move.assert_called_once_with((717, 300))
        desktop.click.assert_not_called()
        state['scene']['cursor_references'] = dict(objects=dict(HoveredBattleObject=dict(
            address='0x9000', class_name='GameObject', native_pointer='0xc800')))
        self.assertIsNone(actor.observe(state))
        self.assertEqual(actor.observe(state), 'target_clicked')
        desktop.click.assert_called_once_with()
        desktop.move.assert_called_once_with((717, 300))

    def test_verified_hover_click_does_not_send_another_mouse_motion(self):
        actor, desktop, state = self.target_actor()
        def invalidating_move(point):
            state['scene']['cursor_references']['objects']['HoveredBattleObject'] = dict(
                address='0x0', status='null')
            return point
        desktop.move.side_effect = invalidating_move
        self.assertIsNone(actor.observe(state))
        self.assertEqual(actor.observe(state), 'target_clicked')
        desktop.move.assert_called_once_with((707, 303))
        self.assertEqual(state['scene']['cursor_references']['objects'][
            'HoveredBattleObject']['address'], '0x9000')

    def test_first_matching_hover_at_probe_deadline_waits_for_second_sample(self):
        actor, desktop, state = self.target_actor()
        state['scene']['field_elements'] = [dict(card_address='0xb', visual_slot=4,
            local=False, zone='MonsterCards', world=True, native_element='0x9300',
            game_object_link=dict(verified=True, component='0x9300', game_object='0xc800'))]
        state['scene']['cursor_references']['objects']['HoveredBattleObject'].update(
            native_pointer='0xwrong')
        self.assertIsNone(actor.observe(state))
        self.assertIsNone(actor.observe(state))
        state['scene']['cursor_references']['objects']['HoveredBattleObject'].update(
            native_pointer='0xc800')
        self.assertIsNone(actor.observe(state))
        self.assertEqual(actor.observe(state), 'target_clicked')
        desktop.move.assert_called_once_with((707, 303))

    def test_field_spell_and_backrow_hover_never_authorize_a_monster_click(self):
        actor, desktop, state = self.target_actor()
        rival = state['board']['fields'][1]
        rival['field_spell'] = dict(status='observed', card=dict(
            address='0xfield', name='The Sanctuary in the Sky', face_down=False))
        state['scene']['field_elements'] = [dict(card_address='0xb', visual_slot=4,
            local=False, zone='MonsterCards', world=True, native_element='0x9300',
            game_object_link=dict(verified=True, component='0x9300', game_object='0xc800'))]
        state['scene']['cursor_references']['objects']['HoveredBattleObject'].update(
            native_pointer='0xbackrow')
        cursor = [707, 303]
        def move(point):
            cursor[:] = point
            return point
        desktop.move.side_effect = move
        desktop.position.side_effect = lambda: tuple(cursor)
        with self.assertRaisesRegex(RuntimeError, 'No se localizó el objetivo'):
            for _ in range(120):
                actor.observe(state)
        desktop.click.assert_not_called()
        self.assertTrue(all(277 <= c.args[0][1] <= 325 for c in desktop.move.call_args_list))

    def test_busy_sample_clears_hover_confirmation(self):
        actor, desktop, state = self.target_actor()
        self.assertIsNone(actor.observe(state))
        state['actions']['flags']['Busy'] = True
        self.assertIsNone(actor.observe(state))
        desktop.click.assert_not_called()
        state['actions']['flags']['Busy'] = False
        self.assertIsNone(actor.observe(state))
        self.assertEqual(actor.observe(state), 'target_clicked')

    def test_monster_probe_offsets_scale_with_viewport(self):
        actor, _, _ = self.target_actor()
        actor.config['screen'] = dict(width=960, height=540)
        actor.config['zones']['memory_opponent_targets'] = [
            [358, 150], [420, 150], [477, 150], [542, 150], [605, 150]]
        points = actor.target_points()
        self.assertEqual(points[:4], [(358, 150), (370, 150), (346, 150), (358, 160)])
        self.assertTrue(all(140 <= y <= 160 for _, y in points))

    def test_confirmation_helper_without_selected_target_does_not_click(self):
        actor, desktop, state = self.target_actor()
        actor.stage = 'await_target'
        state['actions']['prompts']['CurrentConfirmationHelper'] = '0xconfirm'
        state['scene']['field_elements'] = [dict(card_address='0xother', visual_slot=4,
            local=False, zone='MonsterCards', world=True, selected=True)]
        for _ in range(3):
            self.assertIsNone(actor.observe(state))
        desktop.click.assert_not_called()

    def retryable_target(self):
        actor, desktop, state = self.target_actor()
        state['actions']['prompt_details'] = dict(CurrentChoiceHelper=dict(
            Description='Choose an attack target.', Cancelable=True, Min=0, Max=0,
            UseConfirmations=True))
        actor.observe(state)
        self.assertEqual(actor.observe(state), 'target_clicked')
        return actor, desktop, state

    def retry_click(self, actor, desktop, state, attempt):
        for _ in range(8):
            self.assertIsNone(actor.observe(state))
        if not actor.direct_probe_used:
            desktop.position.return_value = actor.point('direct_attack')
            self.assertEqual(actor.observe(state), 'direct_attack_probe_clicked')
            for _ in range(8):
                self.assertIsNone(actor.observe(state))
            self.assertEqual(actor.observe(state), 'direct_attack_probe_ignored')
            desktop.position.return_value = actor.enemy_point()
        self.assertEqual(actor.observe(state), 'target_retry_aimed')
        self.assertIsNone(actor.observe(state))
        self.assertEqual(actor.observe(state), f'target_retry_clicked: {attempt}/3')

    def test_unselected_target_gets_three_clicks_and_no_blind_confirmation(self):
        actor, desktop, state = self.retryable_target()
        self.retry_click(actor, desktop, state, 2)
        self.retry_click(actor, desktop, state, 3)
        self.assertEqual(desktop.click.call_count, 4)  # Three card clicks plus one direct probe.
        with self.assertRaisesRegex(RuntimeError, 'No se verificó la confirmación'):
            for _ in range(40):
                actor.observe(state)
        self.assertEqual(desktop.click.call_count, 4)

    def test_failed_target_can_switch_to_another_monster_with_same_attacker(self):
        actor, desktop, state = self.retryable_target()
        add_monster(state, False, 0, dict(address='0xc', hidden=True, face_down=True))
        self.retry_click(actor, desktop, state, 2)
        self.retry_click(actor, desktop, state, 3)
        for _ in range(8):
            actor.observe(state)
        desktop.position.return_value = (1214, 302)
        self.assertIn('target_changed:', actor.observe(state))
        self.assertEqual(actor.plan['target'], '0xc')
        self.assertEqual(actor.plan['attacker'], '0xa')
        self.assertEqual(actor.failed_targets, {'0xb'})
        self.assertEqual(actor.stage, 'aim_target')
        self.assertEqual(desktop.click.call_count, 4)

    def direct_probe_actor(self):
        actor, desktop, state = self.retryable_target()
        actor.config['points']['direct_attack'] = [961, 189]
        for _ in range(8):
            self.assertIsNone(actor.observe(state))
        desktop.position.return_value = (961, 189)
        self.assertEqual(actor.observe(state), 'direct_attack_probe_clicked')
        desktop.move.assert_called_with((961, 189))
        self.assertEqual(desktop.click.call_count, 2)
        return actor, desktop, state

    def test_unresponsive_defender_probes_direct_before_scanning(self):
        actor, desktop, state = self.direct_probe_actor()
        # The stale visual defender remains present even after direct damage.
        state['actions']['prompts']['CurrentChoiceHelper'] = '0x0'
        state['board']['fields'][1]['life_points_candidate'] -= actor.plan['expected_damage']
        self.assertEqual(actor.observe(state), 'direct_attack_probe_verified')
        self.assertEqual(actor.used_attackers, {'0xa'})
        self.assertEqual(actor.attacks, 1)
        self.assertEqual(actor.stage, 'battle')
        self.assertEqual(desktop.click.call_count, 2)

    def test_direct_probe_does_not_accept_action_disappearance_without_damage(self):
        actor, desktop, state = self.direct_probe_actor()
        state['actions']['prompts']['CurrentChoiceHelper'] = '0x0'
        state['actions']['helpers'] = []
        with self.assertRaisesRegex(RuntimeError, 'intento de ataque directo'):
            for _ in range(40):
                actor.observe(state)
        self.assertEqual(actor.attacks, 0)
        self.assertEqual(desktop.click.call_count, 2)

    def test_direct_probe_rejects_damage_while_target_prompt_remains(self):
        actor, desktop, state = self.direct_probe_actor()
        state['board']['fields'][1]['life_points_candidate'] -= actor.plan['expected_damage']
        self.assertIsNone(actor.observe(state))
        self.assertEqual(actor.attacks, 0)
        self.assertEqual(desktop.click.call_count, 2)

    def test_ignored_direct_probe_restores_defender_plan_and_baseline(self):
        actor, desktop, state = self.direct_probe_actor()
        previous_plan, previous_baseline = actor.direct_probe_previous
        for _ in range(8):
            self.assertIsNone(actor.observe(state))
        self.assertEqual(actor.observe(state), 'direct_attack_probe_ignored')
        self.assertEqual(actor.plan, previous_plan)
        self.assertEqual(actor.attack_baseline, previous_baseline)
        desktop.position.return_value = actor.enemy_point()
        self.assertEqual(actor.observe(state), 'target_retry_aimed')
        self.assertEqual(desktop.click.call_count, 2)

    def test_another_selected_card_blocks_target_retry(self):
        actor, desktop, state = self.retryable_target()
        state['scene']['field_elements'] = [dict(card_address='0xother', selected=True)]
        for _ in range(15):
            actor.observe(state)
        desktop.click.assert_called_once_with()
        self.assertEqual(actor.stage, 'await_target')

    def test_excluding_all_failed_targets_never_authorizes_direct_attack(self):
        state = sample()
        add_monster(state, True, 2, monster('Tiger', '0xa', 1700))
        add_monster(state, False, 4, dict(address='0xb', hidden=True, face_down=True))
        publish_attack(state)
        self.assertEqual(battle_decision(state, excluded_targets={'0xb'})['kind'], 'end')
        self.assertIsNotNone(state['board']['fields'][1]['visual_candidates']['MonsterCards']['candidates'][4]['card'])

    def test_consumed_attacker_is_not_retried(self):
        actor, desktop, state = self.retryable_target()
        state['board']['fields'][0]['visual_candidates']['MonsterCards']['candidates'][2]['card']['AttackedThisTurn'] = True
        for _ in range(15):
            actor.observe(state)
        desktop.click.assert_called_once_with()
