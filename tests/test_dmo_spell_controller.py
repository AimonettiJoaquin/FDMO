from copy import deepcopy
from unittest import TestCase
from unittest.mock import Mock

from dmo.bot.dmo_spell_controller import SpellController
from dmo.strategy.dmo_spells import spell_plan
from tests.test_dmo_spells import spell_state
from tests.test_dmo_loop_bot import config
from tests.test_dmo_battle import add_monster, monster


class SpellControllerTests(TestCase):
    def test_gaia_requires_faceup_field_spell_not_just_hand_removal(self):
        actor, desktop, state = self.setup_spell('Gaia Power')
        after = deepcopy(state)
        own = after['board']['fields'][0]
        own['HandCards'], own['HandCardCount'] = [], 0
        self.assertFalse(actor.resolved(after))
        card = dict(name='Gaia Power', face_down=False, location='Field')
        own['field_spell'] = dict(status='unknown', card=card)
        self.assertFalse(actor.resolved(after))
        own['field_spell']['status'] = 'observed'
        card['face_down'] = True
        self.assertFalse(actor.resolved(after))
        card['face_down'] = False
        card['location'] = 'Graveyard'
        self.assertFalse(actor.resolved(after))
        card['location'] = 'Field'
        self.assertEqual(actor.observe(after), 'spell_verified: Gaia Power')
        desktop.click.assert_called_once_with()

    def test_gaia_is_not_placed_in_regular_magic_zones(self):
        actor, desktop, state = self.setup_spell('Gaia Power')
        prompt = self.zone_prompt(state)
        prompt['actions']['prompt_details']['CurrentChoiceHelper']['Description'] = (
            'Activate\n<color=#00FFFFFF>Gaia Power')
        zone = actor.activation_zone(prompt)
        self.assertEqual(zone[1:3], ('FieldSpell', (416, 707)))
        desktop.click.assert_called_once_with()

    def test_gaia_places_once_after_two_stable_reads_and_waits_for_effect(self):
        actor, desktop, state = self.setup_spell('Gaia Power')
        prompt = self.zone_prompt(state)
        prompt['actions']['prompt_details']['CurrentChoiceHelper']['Description'] = (
            'Activate\n<color=#00FFFFFF>Gaia Power')
        self.assertEqual(actor.observe(prompt), 'checking_spell_zone')
        self.assertEqual(actor.observe(prompt), 'field_spell_zone_clicked')
        desktop.move.assert_called_with((416, 707))
        self.assertEqual(desktop.click.call_count, 2)  # Activate, then placement.
        for _ in range(5):
            self.assertIsNone(actor.observe(prompt))
        self.assertFalse(actor.finished)
        self.assertEqual(desktop.click.call_count, 2)
        after = deepcopy(state)
        own = after['board']['fields'][0]
        own['HandCards'], own['HandCardCount'] = [], 0
        own['field_spell']['card'] = dict(name='Gaia Power', face_down=False, location='Field')
        self.assertEqual(actor.observe(after), 'spell_verified: Gaia Power')

    def test_gaia_placement_requires_empty_known_slot_exact_prompt_and_same_hand(self):
        actor, desktop, state = self.setup_spell('Gaia Power')
        prompt = self.zone_prompt(state)
        details = prompt['actions']['prompt_details']['CurrentChoiceHelper']
        details['Description'] = 'Activate\n<color=#00FFFFFF>Gaia Power'
        for slot in [dict(status='unknown', card=None), dict(status='observed'),
                     dict(status='observed', card=dict(name='Gaia Power', face_down=False))]:
            invalid = deepcopy(prompt)
            invalid['board']['fields'][0]['field_spell'] = slot
            self.assertIsNone(actor.activation_zone(invalid))
        details['UseConfirmations'] = True
        self.assertIsNone(actor.activation_zone(prompt))
        details['UseConfirmations'] = False
        prompt['board']['fields'][0]['HandCards'][0]['address'] = '0xnewcopy'
        self.assertIsNone(actor.activation_zone(prompt))
        desktop.click.assert_called_once_with()

    def test_gaia_field_zone_scales_with_viewport(self):
        actor, _, state = self.setup_spell('Gaia Power')
        prompt = self.zone_prompt(state)
        prompt['actions']['prompt_details']['CurrentChoiceHelper']['Description'] = (
            'Activate\n<color=#00FFFFFF>Gaia Power')
        actor.width, actor.height = 960, 540
        self.assertEqual(actor.activation_zone(prompt)[2], (208, 354))

    def setup_spell(self, name='Pot of Greed'):
        state = spell_state([name])
        state['actions']['helpers'][0]['options'].append(dict(value=4, name='Set'))
        own = state['board']['fields'][0]
        own.update(owner_identity='0x4000', generation_id=1, DeckCount=30)
        if name == 'Big Bang Shot':
            card = monster('Giant Rex', '0xa000', 2000)
            card['slot'] = 2
            add_monster(state, True, 2, card)
        if name == 'Raigeki':
            add_monster(state, False, 2, monster('Enemy', '0xe000', 1800))
        if name == 'Nobleman of Crossout':
            add_monster(state, False, 0, dict(address='0xe000', hidden=True,
                                             face_down=True, slot=0))
        if name == 'Mystical Space Typhoon':
            state['board']['fields'][1]['visual_candidates']['MagicCards']['candidates'][0].update(
                helper_address='0xe100', card=dict(address='0xe000', name='Call of the Mummy',
                    face_down=False, location='Field', slot=0))
        plan = spell_plan(state)
        state['advice'] = dict(status='suggestions', options=[plan])
        state['scene']['hand_elements'] = [dict(element='0x8000', native_element='0x9000',
            card_address=plan['address'], name=name, selected=True,
            hover=dict(hoverable=True, hovered=True))]
        state['scene']['status'] = 'hand_objects_linked'
        desktop = Mock()
        cursor = [(285, 957)]
        def move(point):
            cursor[0] = point
            return point
        desktop.move.side_effect = move
        desktop.position.side_effect = lambda: cursor[0]
        actor = SpellController(desktop, config(), plan)
        self.assertEqual(actor.observe(state), 'checking_selected_card')
        self.assertEqual(actor.observe(state), 'activate_option_clicked')
        desktop.click.assert_called_once_with()
        desktop.move.assert_called_once_with((215, 825))
        return actor, desktop, state

    def after_draw(self, state, count):
        after = deepcopy(state)
        own = after['board']['fields'][0]
        own['HandCards'] = [dict(address=hex(0x1000 + i * 8), name='New card', slot=i, location='Hand')
                            for i in range(count)]
        own['HandCardCount'] = count
        own['DeckCount'] -= count
        return after

    def zone_prompt(self, state):
        after = deepcopy(state)
        after['actions']['flags']['GameStateOpen'] = False
        after['actions']['prompts']['CurrentChoiceHelper'] = '0xabc'
        after['actions']['prompt_details'] = dict(CurrentChoiceHelper=dict(
            Description='Activate\n<color=#00FFFFFF>Pot of Greed', Cancelable=True,
            Min=0, Max=0, UseConfirmations=False, ChoiceType=dict(enum_raw=0), PurposeType=dict(enum_raw=0)))
        own = after['board']['fields'][0]
        own['MagicCards'] = [dict(unknown=True, diagnostic=dict(CurrentName='',
            CardLocation=dict(enum_raw=0), cardFinder=dict(CardID=0),
            OverrideCardfinder=dict(CardID=0))) for _ in range(5)]
        own['visual_candidates']['MagicCards'] = dict(source='MagicVisualHelpers', candidates=[
            dict(helper_index=i, helper_present=False, helper_address='0x0', card=None) for i in range(5)])
        return after

    def setup_equip(self):
        actor, desktop, state = self.setup_spell('Big Bang Shot')
        prompt = self.zone_prompt(state)
        prompt['actions']['prompt_details']['CurrentChoiceHelper'] = dict(
            Description='Select <color=#00FFFFFF>1</color> Card(s)', Cancelable=False,
            Min=1, Max=1, UseConfirmations=True, ChoiceType=dict(enum_raw=0), PurposeType=dict(enum_raw=0))
        own = prompt['board']['fields'][0]
        own['HandCards'], own['HandCardCount'] = [], 0
        own['visual_candidates']['MagicCards']['candidates'][2].update(helper_address='0xbb0',
            helper_present=True, card=dict(address='0xbb1', name='Big Bang Shot', face_down=False))
        prompt['scene']['field_elements'] = [dict(element='0xc000', native_element='0xd000',
            card_address='0xa000', name='Giant Rex', visual_slot=2, world=True, selected=False)]
        actor.zone_choice = ('0xzone', 2, (960, 855), (0, 1, 2, 3, 4))
        actor.zone_clicked_prompt = '0xzone'
        return actor, desktop, prompt

    def setup_player(self):
        actor, desktop, state = self.setup_spell('Raigeki')
        prompt = self.zone_prompt(state)
        prompt['actions']['prompt_details']['CurrentChoiceHelper'] = dict(
            Description='Choose a player.', Cancelable=False, Min=0, Max=0,
            UseConfirmations=False, ChoiceType=dict(enum_raw=0), PurposeType=dict(enum_raw=0))
        own = prompt['board']['fields'][0]
        own['HandCards'], own['HandCardCount'] = [], 0
        own['visual_candidates']['MagicCards']['candidates'][2].update(helper_address='0xbb0',
            helper_present=True, card=dict(address='0xbb1', name='Raigeki', face_down=False))
        actor.zone_choice = ('0xzone', 2, (960, 855), (0, 1, 2, 3, 4))
        actor.zone_clicked_prompt = '0xzone'
        return actor, desktop, prompt

    def setup_search(self):
        actor, desktop, state = self.setup_spell('Terraforming')
        prompt = self.zone_prompt(state)
        actions = prompt['actions']
        actions['prompts'].update(CurrentChoiceHelper='0x0', CurrentOptionHelper='0x777')
        actions['prompt_details'] = dict(CurrentOptionHelper=dict(
            Title='Choose a Field Spell to add to your hand', Description='(<color=#00FFFFFF>Terraforming</color>)',
            MinimumRequiredOptions=0, MaximumRequiredOptions=1, selected_indices=[], cards=[
                dict(ordinal=i, Index=i+20, name='Gaia Power', address=hex(0x10000+i*8),
                     PlayerID=0, CardLocation=dict(enum_raw=3)) for i in range(3)]))
        own = prompt['board']['fields'][0]
        own['HandCards'], own['HandCardCount'] = [], 0
        own['visual_candidates']['MagicCards']['candidates'][2].update(helper_address='0xbb0',
            helper_present=True, card=dict(address='0xbb1', name='Terraforming', face_down=False))
        actor.zone_choice = ('0xzone', 2, (960, 855), (0, 1, 2, 3, 4))
        actor.zone_clicked_prompt = '0xzone'
        return actor, desktop, state, prompt

    def test_search_selects_one_gaia_copy_and_confirms_after_selected_index(self):
        actor, desktop, state, prompt = self.setup_search()
        self.assertEqual(actor.observe(prompt), 'checking_card_panel')
        self.assertEqual(actor.observe(prompt), 'panel_card_clicked: Gaia Power')
        self.assertEqual(desktop.move.call_args.args, ((960, 890),))
        for _ in range(3):
            self.assertEqual(actor.observe(prompt), 'waiting_panel_selection')
        self.assertEqual(desktop.click.call_count, 2)
        prompt['actions']['prompt_details']['CurrentOptionHelper']['selected_indices'] = [21]
        self.assertEqual(actor.observe(prompt), 'checking_panel_selection')
        self.assertEqual(actor.observe(prompt), 'panel_select_clicked')
        self.assertEqual(desktop.move.call_args.args, ((960, 1038),))
        for _ in range(3):
            self.assertEqual(actor.observe(prompt), 'waiting_panel_result')
        self.assertEqual(desktop.click.call_count, 3)
        after = self.after_draw(state, 1)
        after['board']['fields'][0]['HandCards'][0]['name'] = 'Gaia Power'
        self.assertEqual(actor.observe(after), 'spell_verified: Terraforming')

    def test_search_does_not_confirm_a_wrong_or_multiple_selection(self):
        for selected in ([20], [21, 22]):
            actor, desktop, _, prompt = self.setup_search()
            actor.observe(prompt)
            actor.observe(prompt)
            prompt['actions']['prompt_details']['CurrentOptionHelper']['selected_indices'] = selected
            with self.assertRaisesRegex(RuntimeError, 'distinta del plan'):
                actor.observe(prompt)
            self.assertEqual(desktop.click.call_count, 2)

    def test_search_refuses_unnamed_cards_or_missing_option_details(self):
        for unknown in (True, False):
            actor, desktop, _, prompt = self.setup_search()
            if unknown:
                prompt['actions']['prompt_details']['CurrentOptionHelper']['cards'][0]['name'] = None
            else:
                prompt['actions']['prompt_details'] = {}
            with self.assertRaisesRegex(RuntimeError, 'No se verificó'):
                for _ in range(40):
                    actor.observe(prompt)
            desktop.click.assert_called_once_with()

    def test_raigeki_chooses_rival_once_then_requires_enemy_field_cleared(self):
        actor, desktop, prompt = self.setup_player()
        self.assertEqual(actor.observe(prompt), 'checking_spell_player')
        self.assertEqual(actor.observe(prompt), 'spell_player_clicked: rival')
        self.assertEqual(desktop.move.call_args.args, ((955, 207),))
        for _ in range(4):
            actor.observe(prompt)
        self.assertEqual(desktop.click.call_count, 2)
        prompt['actions']['flags']['GameStateOpen'] = True
        prompt['actions']['prompts']['CurrentChoiceHelper'] = '0x0'
        self.assertIsNone(actor.observe(prompt))
        prompt['board']['fields'][1]['visual_candidates']['MonsterCards']['candidates'][2].update(
            helper_address='0x0', card=None)
        self.assertEqual(actor.observe(prompt), 'spell_verified: Raigeki')

    def test_raigeki_consumed_without_clearing_monster_continues_turn(self):
        actor, desktop, prompt = self.setup_player()
        actor.observe(prompt)
        self.assertEqual(actor.observe(prompt), 'spell_player_clicked: rival')
        prompt['actions']['flags']['GameStateOpen'] = True
        prompt['actions']['prompts']['CurrentChoiceHelper'] = '0x0'
        self.assertIsNone(actor.observe(prompt))
        prompt['board']['fields'][0]['visual_candidates']['MagicCards']['candidates'][2].update(
            helper_address='0x0', card=None)
        self.assertIsNone(actor.observe(prompt))
        self.assertEqual(actor.observe(prompt), 'spell_consumed_without_clear: Raigeki')
        self.assertTrue(actor.finished)
        self.assertEqual(desktop.click.call_count, 2)

    def test_raigeki_spent_without_player_prompt_continues_after_two_free_samples(self):
        actor, desktop, state = self.setup_player()
        state['actions']['prompts']['CurrentChoiceHelper'] = '0x0'
        state['actions']['prompt_details'] = {}
        self.assertIsNone(actor.observe(state))  # Observe the placed Raigeki while closed.
        own = state['board']['fields'][0]
        own['visual_candidates']['MagicCards']['candidates'][2].update(
            helper_address='0x0', card=None)
        for _ in range(3):
            self.assertIsNone(actor.observe(state))
        state['actions']['flags']['GameStateOpen'] = True
        self.assertIsNone(actor.observe(state))
        self.assertEqual(actor.observe(state), 'spell_consumed_without_clear: Raigeki')
        self.assertTrue(actor.finished)
        self.assertFalse(actor.player_clicked)
        desktop.click.assert_called_once_with()

    def test_raigeki_hand_removal_without_observed_field_copy_is_not_consumption(self):
        actor, desktop, state = self.setup_player()
        state['actions']['prompts']['CurrentChoiceHelper'] = '0x0'
        state['actions']['prompt_details'] = {}
        state['actions']['flags']['GameStateOpen'] = True
        state['board']['fields'][0]['visual_candidates']['MagicCards']['candidates'][2].update(
            helper_address='0x0', card=None)
        with self.assertRaisesRegex(RuntimeError, 'No se verificó el efecto de Raigeki'):
            for _ in range(40):
                actor.observe(state)
        self.assertFalse(actor.finished)
        desktop.click.assert_called_once_with()

    def test_raigeki_consumption_requires_complete_hand_and_backrow(self):
        for invalid in ('hand', 'backrow'):
            actor, desktop, state = self.setup_player()
            state['actions']['prompts']['CurrentChoiceHelper'] = '0x0'
            state['actions']['prompt_details'] = {}
            actor.observe(state)
            state['actions']['flags']['GameStateOpen'] = True
            own = state['board']['fields'][0]
            own['visual_candidates']['MagicCards']['candidates'][2].update(
                helper_address='0x0', card=None)
            if invalid == 'hand':
                own['HandCardCount'] = 1
            else:
                own['visual_candidates']['MagicCards']['candidates'][0]['error'] = 'changing'
            for _ in range(3):
                self.assertIsNone(actor.observe(state))
            self.assertFalse(actor.finished)
            desktop.click.assert_called_once_with()

    def test_player_prompt_for_a_different_spell_is_not_clicked(self):
        actor, desktop, prompt = self.setup_player()
        actor.plan['card'] = 'Pot of Greed'
        actor.observe(prompt)
        for _ in range(14):
            actor.observe(prompt)
        with self.assertRaisesRegex(RuntimeError, 'pendiente de implementar'):
            actor.observe(prompt)
        self.assertEqual(desktop.click.call_count, 1)

    def test_equips_exact_own_target_then_confirms_only_after_memory_selection(self):
        actor, desktop, prompt = self.setup_equip()
        # An explicit calibration can override the captured default without
        # sharing the attack confirmation offset.
        actor.equip_confirm_point = (1500, 1000)
        self.assertEqual(actor.observe(prompt), 'checking_equip_target')
        self.assertEqual(actor.observe(prompt), 'equip_target_clicked')
        self.assertEqual(desktop.move.call_args.args, ((960, 728),))
        for _ in range(4):
            actor.observe(prompt)
        self.assertEqual(desktop.click.call_count, 2)
        prompt['scene']['field_elements'][0]['selected'] = True
        self.assertEqual(actor.observe(prompt), 'checking_equip_target')
        self.assertEqual(actor.observe(prompt), 'equip_confirm_clicked')
        self.assertEqual(desktop.move.call_args.args, ((1500, 1000),))
        for _ in range(4):
            actor.observe(prompt)
        self.assertEqual(desktop.click.call_count, 3)
        prompt['actions']['flags']['GameStateOpen'] = True
        prompt['actions']['prompts']['CurrentChoiceHelper'] = '0x0'
        target = prompt['board']['fields'][0]['visual_candidates']['MonsterCards']['candidates'][2]['card']
        self.assertIsNone(actor.observe(prompt))
        target['ATK'] = 2400
        self.assertEqual(actor.observe(prompt), 'spell_verified: Big Bang Shot')

    def test_default_equip_confirmation_uses_captured_button_above_monster(self):
        actor, desktop, prompt = self.setup_equip()
        actor.observe(prompt)
        actor.observe(prompt)
        prompt['scene']['field_elements'][0]['selected'] = True
        self.assertEqual(actor.observe(prompt), 'checking_equip_target')
        self.assertEqual(actor.observe(prompt), 'equip_confirm_clicked')
        self.assertEqual(desktop.click.call_count, 3)  # Activate, target and Confirm.
        desktop.move.assert_called_with((960, 570))
        self.assertTrue(actor.equip_confirmed)
        self.assertFalse(actor.finished)

    def test_default_equip_confirmation_offset_scales_and_follows_target_slot(self):
        actor, desktop, prompt = self.setup_equip()
        actor.width, actor.height = 960, 540
        actor.monster_points[2] = [480, 364]
        actor.observe(prompt)
        actor.observe(prompt)
        prompt['scene']['field_elements'][0]['selected'] = True
        actor.observe(prompt)
        self.assertEqual(actor.observe(prompt), 'equip_confirm_clicked')
        desktop.move.assert_called_with((480, 285))

    def test_default_equip_confirmation_is_not_repeated_without_attack_increase(self):
        actor, desktop, prompt = self.setup_equip()
        actor.observe(prompt)
        actor.observe(prompt)
        prompt['scene']['field_elements'][0]['selected'] = True
        actor.observe(prompt)
        actor.observe(prompt)
        for _ in range(5):
            actor.observe(prompt)
        self.assertEqual(desktop.click.call_count, 3)
        self.assertFalse(actor.finished)
        prompt['actions']['prompts']['CurrentChoiceHelper'] = '0x0'
        prompt['actions']['flags']['GameStateOpen'] = True
        self.assertIsNone(actor.observe(prompt))
        target = prompt['board']['fields'][0]['visual_candidates']['MonsterCards']['candidates'][2]['card']
        target['ATK'] += 400
        self.assertEqual(actor.observe(prompt), 'spell_verified: Big Bang Shot')

    def mst_prompt(self):
        actor, desktop, state = self.setup_spell('Mystical Space Typhoon')
        prompt = self.zone_prompt(state)
        prompt['actions']['prompt_details']['CurrentChoiceHelper'].update(
            Description='Select <color=#00FFFFFF>1</color> Card(s)', Cancelable=False,
            Min=1, Max=1, UseConfirmations=True)
        own, rival = prompt['board']['fields']
        own['HandCards'], own['HandCardCount'] = [], 0
        own['visual_candidates']['MagicCards']['candidates'][2].update(
            helper_address='0xbb0', card=dict(address='0xbb1', name='Mystical Space Typhoon', face_down=False))
        prompt['scene']['field_elements'] = [dict(element='0xc0', native_element='0xd0',
            card_address='0xe000', visual_slot=0, zone='MagicCards', field_address=rival['address'],
            local=False, world=True, selected=False)]
        actor.zone_choice = ('0xzone', 2, (960, 855), (0, 1, 2, 3, 4))
        actor.zone_clicked_prompt = '0xzone'
        return actor, desktop, prompt

    def test_mst_targets_exact_rival_backrow_then_uses_its_own_confirm_button(self):
        actor, desktop, prompt = self.mst_prompt()
        self.assertEqual(actor.observe(prompt), 'checking_spell_target')
        self.assertEqual(actor.observe(prompt), 'spell_target_clicked: rival')
        desktop.move.assert_called_with((1190, 235))
        prompt['scene']['field_elements'][0]['selected'] = True
        actor.observe(prompt)
        self.assertEqual(actor.observe(prompt), 'spell_target_confirm_clicked')
        desktop.move.assert_called_with((1175, 303))
        self.assertEqual(desktop.click.call_count, 3)
        for _ in range(4):
            actor.observe(prompt)
        self.assertEqual(desktop.click.call_count, 3)
        self.assertFalse(actor.finished)

    def test_mst_slot_four_confirmation_matches_live_capture_and_scales(self):
        for width, height, expected in [(1920, 1080, (715, 303)), (960, 540, (357, 152))]:
            actor, desktop, prompt = self.mst_prompt()
            actor.width, actor.height = width, height
            rival = prompt['board']['fields'][1]
            zones = rival['visual_candidates']['MagicCards']['candidates']
            zones[0], zones[4] = zones[4], zones[0]
            for i, entry in enumerate(zones):
                entry['helper_index'] = i
            actor.plan['target_zone'] = 4
            prompt['scene']['field_elements'][0]['visual_slot'] = 4
            actor.observe(prompt)
            actor.observe(prompt)
            prompt['scene']['field_elements'][0]['selected'] = True
            actor.observe(prompt)
            self.assertEqual(actor.observe(prompt), 'spell_target_confirm_clicked')
            desktop.move.assert_called_with(expected)

    def test_mst_rejects_wrong_side_slot_card_and_prompt(self):
        for key, value in [('local', True), ('visual_slot', 4), ('card_address', '0xwrong'),
                           ('zone', 'MonsterCards'), ('field_address', '0xotherfield')]:
            actor, desktop, prompt = self.mst_prompt()
            prompt['scene']['field_elements'][0][key] = value
            self.assertIsNone(actor.target_choice(prompt))
            desktop.click.assert_called_once_with()

    def test_mst_confirms_only_with_independent_calibration_and_destroyed_target(self):
        actor, desktop, prompt = self.mst_prompt()
        actor.target_confirm_point = (1500, 1000)
        actor.observe(prompt)
        actor.observe(prompt)
        prompt['scene']['field_elements'][0]['selected'] = True
        actor.observe(prompt)
        self.assertEqual(actor.observe(prompt), 'spell_target_confirm_clicked')
        prompt['actions']['flags']['GameStateOpen'] = True
        prompt['actions']['prompts']['CurrentChoiceHelper'] = '0x0'
        self.assertIsNone(actor.observe(prompt))
        target = prompt['board']['fields'][1]['visual_candidates']['MagicCards']['candidates'][0]
        target.update(helper_address='0x0', card=None)
        self.assertEqual(actor.observe(prompt), 'spell_verified: Mystical Space Typhoon')
        self.assertEqual(desktop.click.call_count, 3)

    def test_reinforcement_requires_one_new_card_and_deck_decrement(self):
        actor, _, state = self.setup_spell('Reinforcement of the Army')
        after = self.after_draw(state, 1)
        self.assertTrue(actor.resolved(after))
        after['board']['fields'][0]['DeckCount'] += 1
        self.assertFalse(actor.resolved(after))

    def nobleman_prompt(self):
        actor, desktop, state = self.setup_spell('Nobleman of Crossout')
        prompt = self.zone_prompt(state)
        prompt['actions']['prompt_details']['CurrentChoiceHelper'].update(
            Description='Select <color=#00FFFFFF>1</color> Card(s)', Cancelable=False,
            Min=1, Max=1, UseConfirmations=True)
        own, rival = prompt['board']['fields']
        own['HandCards'], own['HandCardCount'] = [], 0
        own['visual_candidates']['MagicCards']['candidates'][2].update(
            helper_address='0xbb0', card=dict(address='0xbb1', name='Nobleman of Crossout', face_down=False))
        prompt['scene']['field_elements'] = [dict(element='0xc0', native_element='0xd0',
            card_address='0xe000', visual_slot=0, zone='MonsterCards', field_address=rival['address'],
            local=False, world=True, selected=False,
            game_object_link=dict(verified=True, component='0xd0', game_object='0xef00'))]
        prompt['scene']['cursor_references'] = dict(objects=dict(HoveredBattleObject=dict(
            address='0xhover', class_name='GameObject', native_pointer='0xef00')))
        actor.zone_choice = ('0xzone', 2, (960, 855), (0, 1, 2, 3, 4))
        actor.zone_clicked_prompt = '0xzone'
        return actor, desktop, prompt

    def click_nobleman_target(self, actor, prompt):
        self.assertEqual(actor.observe(prompt), 'checking_spell_target')
        self.assertEqual(actor.observe(prompt), 'spell_monster_target_aimed')
        self.assertEqual(actor.observe(prompt), 'checking_spell_monster_hover')
        self.assertEqual(actor.observe(prompt), 'spell_target_clicked: rival')

    def test_nobleman_selects_exact_facedown_monster_and_waits_for_selection(self):
        actor, desktop, prompt = self.nobleman_prompt()
        self.click_nobleman_target(actor, prompt)
        self.assertEqual(desktop.move.call_count, 2)  # Activate, aim; preserve the verified hover.
        desktop.move.assert_called_with((1214, 302))
        self.assertEqual(desktop.click.call_count, 2)
        for _ in range(3):
            actor.observe(prompt)
        self.assertEqual(desktop.click.call_count, 2)
        prompt['scene']['field_elements'][0]['selected'] = True
        actor.observe(prompt)
        self.assertEqual(actor.observe(prompt), 'spell_target_confirm_clicked')
        desktop.move.assert_called_with((1214, 407))
        prompt['actions']['flags']['GameStateOpen'] = True
        prompt['actions']['prompts']['CurrentChoiceHelper'] = '0x0'
        self.assertFalse(actor.resolved(prompt))
        prompt['board']['fields'][1]['visual_candidates']['MonsterCards']['candidates'][0].update(
            card=None, helper_address='0x0')
        self.assertEqual(actor.observe(prompt), 'spell_verified: Nobleman of Crossout')
        self.assertEqual(desktop.click.call_count, 3)

    def test_nobleman_refuses_faceup_replaced_or_foreign_target(self):
        for change in ('faceup', 'replacement', 'foreign'):
            actor, desktop, prompt = self.nobleman_prompt()
            target = prompt['board']['fields'][1]['visual_candidates']['MonsterCards']['candidates'][0]['card']
            if change == 'faceup':
                target['face_down'] = False
            elif change == 'replacement':
                target['address'] = '0xother'
            else:
                prompt['scene']['field_elements'][0]['local'] = True
            self.assertIsNone(actor.target_choice(prompt))
            desktop.click.assert_called_once_with()

    def test_nobleman_uses_calibrated_monster_confirmation(self):
        actor, desktop, prompt = self.nobleman_prompt()
        actor.rival_monster_confirms = [(1, 1), (2, 2), (3, 3), (4, 4), (1229, 380)]
        self.click_nobleman_target(actor, prompt)
        prompt['scene']['field_elements'][0]['selected'] = True
        actor.observe(prompt)
        self.assertEqual(actor.observe(prompt), 'spell_target_confirm_clicked')
        desktop.move.assert_called_with((1229, 380))

    def test_nobleman_never_clicks_backrow_hover_and_probes_only_monster_row(self):
        actor, desktop, prompt = self.nobleman_prompt()
        prompt['scene']['cursor_references']['objects']['HoveredBattleObject']['native_pointer'] = '0xbackrow'
        with self.assertRaisesRegex(RuntimeError, 'No se localizó el monstruo de Nobleman'):
            for _ in range(40):
                actor.observe(prompt)
        # The only click was activation; none of the probes may click a card.
        desktop.click.assert_called_once_with()
        points = [c.args[0] for c in desktop.move.call_args_list[1:]]
        self.assertTrue(points)
        self.assertTrue(all(282 <= y <= 322 for _, y in points))

    def test_nobleman_delayed_hover_keeps_aim_until_second_matching_sample(self):
        actor, desktop, prompt = self.nobleman_prompt()
        actor.observe(prompt)
        actor.observe(prompt)
        hover = prompt['scene']['cursor_references']['objects']['HoveredBattleObject']
        hover['native_pointer'] = '0xbackrow'
        for _ in range(3):
            self.assertEqual(actor.observe(prompt), 'checking_spell_monster_hover')
        hover['native_pointer'] = '0xef00'
        self.assertEqual(actor.observe(prompt), 'checking_spell_monster_hover')
        self.assertEqual(actor.observe(prompt), 'spell_target_clicked: rival')
        self.assertEqual(desktop.move.call_count, 2)

    def test_nobleman_requires_verified_game_object_before_aiming(self):
        actor, desktop, prompt = self.nobleman_prompt()
        prompt['scene']['field_elements'][0].pop('game_object_link')
        actor.observe(prompt)
        with self.assertRaisesRegex(RuntimeError, 'No se pudo verificar el objeto'):
            actor.observe(prompt)
        desktop.click.assert_called_once_with()

    def test_rota_verifies_actual_offered_choice_when_assailant_is_not_available(self):
        from types import SimpleNamespace
        actor, _, state = self.setup_spell('Reinforcement of the Army')
        actor.plan['search_name'] = 'D.D. Assailant'
        after = self.after_draw(state, 1)
        after['board']['fields'][0]['HandCards'][0]['name'] = 'Other warrior'
        self.assertFalse(actor.resolved(after))
        panel = SimpleNamespace(confirmed=True, desired={0}, cards=[dict(ordinal=0, name='Other warrior')])
        actor.record_search_selection(panel)
        self.assertTrue(actor.resolved(after))

    def test_graceful_requires_observed_three_draws_and_exactly_two_discards(self):
        actor, _, state = self.setup_spell('Graceful Charity')
        drawn = self.after_draw(state, 3)
        actor.capture_discard_hand(drawn)
        self.assertEqual(len(actor.discard_hand), 3)
        self.assertFalse(actor.resolved(drawn))
        final = deepcopy(drawn)
        own = final['board']['fields'][0]
        own['HandCards'] = own['HandCards'][:1]
        own['HandCardCount'] = 1
        self.assertTrue(actor.resolved(final))
        actor.discard_hand = None
        self.assertFalse(actor.resolved(final))


    def test_equip_refuses_replaced_target_or_other_selected_card(self):
        for replacement in (True, False):
            actor, desktop, prompt = self.setup_equip()
            if replacement:
                prompt['board']['fields'][0]['visual_candidates']['MonsterCards']['candidates'][2]['card']['address'] = '0xother'
            else:
                prompt['scene']['field_elements'].append(dict(card_address='0xother', selected=True))
            actor.observe(prompt)
            for _ in range(14):
                actor.observe(prompt)
            with self.assertRaisesRegex(RuntimeError, 'pendiente de implementar'):
                actor.observe(prompt)
            self.assertEqual(desktop.click.call_count, 1)

    def test_equip_never_reclicks_an_unconfirmed_target(self):
        actor, desktop, prompt = self.setup_equip()
        actor.observe(prompt)
        actor.observe(prompt)
        with self.assertRaisesRegex(RuntimeError, 'no repetir'):
            for _ in range(85):
                actor.observe(prompt)
        self.assertEqual(desktop.click.call_count, 2)

    def test_activation_places_in_free_zone_once_then_requires_draw_evidence(self):
        actor, desktop, state = self.setup_spell()
        prompt = self.zone_prompt(state)
        self.assertEqual(actor.observe(prompt), 'checking_spell_zone')
        self.assertEqual(actor.observe(prompt), 'spell_zone_clicked')
        self.assertEqual(desktop.move.call_args.args, ((960, 855),))
        self.assertEqual(desktop.click.call_count, 2)
        for _ in range(3):
            self.assertIsNone(actor.observe(prompt))
        self.assertEqual(desktop.click.call_count, 2)
        self.assertEqual(actor.observe(self.after_draw(state, 2)), 'spell_verified: Pot of Greed')

    def test_occupied_center_is_avoided(self):
        actor, desktop, state = self.setup_spell()
        prompt = self.zone_prompt(state)
        prompt['board']['fields'][0]['visual_candidates']['MagicCards']['candidates'][2].update(
            helper_present=True, helper_address='0x123', card=dict(address='0x345', name='Tenki', location='Field'))
        actor.observe(prompt)
        self.assertEqual(actor.observe(prompt), 'spell_zone_clicked')
        self.assertEqual(desktop.move.call_args.args, ((760, 855),))

    def test_wrong_card_prompt_or_ambiguous_vacancy_is_not_clicked(self):
        for wrong_description in (True, False):
            actor, desktop, state = self.setup_spell()
            prompt = self.zone_prompt(state)
            if wrong_description:
                prompt['actions']['prompt_details']['CurrentChoiceHelper']['Description'] = 'Choose a target'
            else:
                prompt['board']['fields'][0]['MagicCards'][2]['diagnostic']['cardFinder']['CardID'] = 42
            self.assertIsNone(actor.observe(prompt))
            for _ in range(14):
                actor.observe(prompt)
            with self.assertRaisesRegex(RuntimeError, 'pendiente de implementar'):
                actor.observe(prompt)
            self.assertEqual(desktop.click.call_count, 1)

    def test_pending_placement_retries_three_times_then_stops(self):
        actor, desktop, state = self.setup_spell()
        prompt = self.zone_prompt(state)
        actor.observe(prompt)
        actor.observe(prompt)
        for attempt in (2, 3):
            for _ in range(7):
                self.assertIsNone(actor.observe(prompt))
            self.assertEqual(actor.observe(prompt), f'spell_zone_retry: {attempt}/3')
        for _ in range(39):
            actor.observe(prompt)
        with self.assertRaisesRegex(RuntimeError, 'no repetir'):
            actor.observe(prompt)
        self.assertEqual(desktop.click.call_count, 4)  # Activate + three placements.

    def test_following_search_prompt_is_reported_instead_of_reusing_zone_click(self):
        actor, desktop, state = self.setup_spell()
        prompt = self.zone_prompt(state)
        actor.observe(prompt)
        actor.observe(prompt)
        prompt['actions']['prompt_details']['CurrentChoiceHelper']['Description'] = 'Choose a card from deck'
        actor.observe(prompt)
        for _ in range(14):
            actor.observe(prompt)
        with self.assertRaisesRegex(RuntimeError, 'Choose a card from deck'):
            actor.observe(prompt)
        self.assertEqual(desktop.click.call_count, 2)

    def test_pot_requires_two_cards_and_deck_change_without_second_click(self):
        actor, desktop, state = self.setup_spell()
        after = self.after_draw(state, 1)
        self.assertIsNone(actor.observe(after))
        after = self.after_draw(state, 2)
        self.assertEqual(actor.observe(after), 'spell_verified: Pot of Greed')
        self.assertTrue(actor.finished)
        desktop.click.assert_called_once_with()

    def test_upstart_requires_draw_and_rival_life_increase(self):
        actor, desktop, state = self.setup_spell('Upstart Goblin')
        after = self.after_draw(state, 1)
        self.assertIsNone(actor.observe(after))
        after['board']['fields'][1]['life_points_candidate'] = 9000
        self.assertEqual(actor.observe(after), 'spell_verified: Upstart Goblin')
        desktop.click.assert_called_once_with()

    def test_unknown_prompt_reports_details_without_guessing_a_zone(self):
        actor, desktop, state = self.setup_spell()
        state['actions']['flags']['GameStateOpen'] = False
        state['actions']['prompts']['CurrentChoiceHelper'] = '0xabc'
        state['actions']['prompt_details'] = dict(CurrentChoiceHelper=dict(Description='Choose spell zone'))
        self.assertIsNone(actor.observe(state))
        for _ in range(14):
            actor.observe(state)
        with self.assertRaisesRegex(RuntimeError, 'Choose spell zone'):
            actor.observe(state)
        desktop.click.assert_called_once_with()

    def test_unconfirmed_spell_is_never_retried(self):
        actor, desktop, state = self.setup_spell()
        for _ in range(39):
            self.assertIsNone(actor.observe(state))
        with self.assertRaisesRegex(RuntimeError, 'no repetir'):
            actor.observe(state)
        desktop.click.assert_called_once_with()

    def test_search_requires_requested_card_and_deck_decrease(self):
        actor, desktop, state = self.setup_spell('Terraforming')
        after = self.after_draw(state, 1)
        self.assertIsNone(actor.observe(after))
        after['board']['fields'][0]['HandCards'][0]['name'] = 'Gaia Power'
        self.assertEqual(actor.observe(after), 'spell_verified: Terraforming')
        desktop.click.assert_called_once_with()

    def test_closed_state_never_verifies_draw_mid_animation(self):
        actor, _, state = self.setup_spell()
        after = self.after_draw(state, 2)
        after['actions']['flags']['GameStateOpen'] = False
        self.assertIsNone(actor.observe(after))
        self.assertFalse(actor.finished)

    def test_occupied_helper_without_presence_flag_allows_other_free_zone(self):
        actor, desktop, state = self.setup_spell()
        prompt = self.zone_prompt(state)
        occupied = prompt['board']['fields'][0]['visual_candidates']['MagicCards']['candidates'][2]
        occupied.pop('helper_present')
        occupied.update(helper_address='0x123', card=dict(address='0x345',
            name='Big Bang Shot', location='Field'))
        actor.observe(prompt)
        self.assertEqual(actor.observe(prompt), 'spell_zone_clicked')
        self.assertEqual(desktop.move.call_args.args[0], (760, 855))

    def test_zone_retry_stops_when_hand_changes_or_other_prompt_opens(self):
        for change in ('hand', 'busy', 'prompt'):
            actor, desktop, state = self.setup_spell()
            prompt = self.zone_prompt(state)
            actor.observe(prompt)
            actor.observe(prompt)
            if change == 'hand':
                prompt['board']['fields'][0]['HandCards'][0]['address'] = '0xchanged'
            elif change == 'busy':
                prompt['actions']['flags']['Busy'] = True
            else:
                prompt['actions']['prompts']['CurrentConfirmationHelper'] = '0xconfirm'
            for _ in range(10):
                actor.observe(prompt)
            self.assertEqual(desktop.click.call_count, 2)
