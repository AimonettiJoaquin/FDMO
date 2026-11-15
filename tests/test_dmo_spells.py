from copy import deepcopy
from unittest import TestCase

from dmo_advisor import suggestions
from dmo_spells import spell_plan
from tests.test_dmo_battle import sample, monster, add_monster


def spell_state(names):
    state = sample()
    state['phase'] = 'MAIN 1'
    for field in state['board']['fields']:
        field['field_spell'] = dict(status='observed', card=None)
        field['visual_candidates']['MagicCards'] = dict(candidates=[
            dict(helper_index=i, helper_address='0x0', card=None) for i in range(5)])
    own = state['board']['fields'][0]
    own['HandCards'] = [dict(address=hex(0x500 + i * 8), name=name, slot=i, location='Hand')
                        for i, name in enumerate(names)]
    own['HandCardCount'] = len(names)
    state['actions']['helpers'] = [dict(CardLocation=dict(enum_raw=1), FieldID=0, ID=i,
        options=[dict(value=0, name='ActivateEffect')]) for i in range(len(names))]
    return state


class SpellRulesTests(TestCase):
    def test_searches_precede_draws_and_graceful_charity_is_last(self):
        names = ['Graceful Charity', 'Upstart Goblin', 'Pot of Greed', 'Terraforming',
                 'Fire Formation - Tenki', 'Reinforcement of the Army']
        for expected in reversed(names):
            self.assertEqual(spell_plan(spell_state(names))['card'], expected)
            names.remove(expected)

    def test_reinforcement_uses_the_offered_warriors_not_a_fixed_name(self):
        plan = spell_plan(spell_state(['Reinforcement of the Army']))
        self.assertEqual(plan['effect'], 'search')
        self.assertIsNone(plan['search_name'])
        self.assertEqual(spell_plan(spell_state(['Graceful Charity']))['effect'], 'draw_discard')

    def test_all_search_copies_must_be_used_before_graceful(self):
        names = ['Graceful Charity', 'Terraforming', 'Terraforming', 'Fire Formation - Tenki']
        while len(names) > 1:
            plan = spell_plan(spell_state(names))
            self.assertNotEqual(plan['card'], 'Graceful Charity')
            names.remove(plan['card'])
        self.assertEqual(spell_plan(spell_state(names))['card'], 'Graceful Charity')

    def test_new_search_drawn_by_upstart_precedes_graceful(self):
        self.assertEqual(spell_plan(spell_state(['Upstart Goblin', 'Graceful Charity']))['card'],
                         'Upstart Goblin')
        self.assertEqual(spell_plan(spell_state(['Graceful Charity', 'Terraforming']))['card'],
                         'Terraforming')

    def test_search_without_published_activation_does_not_block_graceful(self):
        state = spell_state(['Graceful Charity', 'Terraforming'])
        state['actions']['helpers'][1]['options'] = []
        self.assertEqual(spell_plan(state)['card'], 'Graceful Charity')

    def test_gaia_can_be_activated_before_summoning(self):
        state = spell_state(['Gaia Power', 'Gaia Power'])
        plan = spell_plan(state)
        self.assertEqual((plan['card'], plan['effect'], plan['address']),
                         ('Gaia Power', 'field', '0x500'))
        state['actions']['helpers'][0]['options'] = []
        self.assertEqual(spell_plan(state)['address'], '0x508')

    def test_gaia_does_not_replace_an_already_active_copy(self):
        state = spell_state(['Gaia Power'])
        slot = state['board']['fields'][0]['field_spell']
        slot['card'] = dict(name='Gaia Power', face_down=False, location='Field')
        self.assertIsNone(spell_plan(state))
        slot['card']['face_down'] = True
        self.assertEqual(spell_plan(state)['card'], 'Gaia Power')
        slot['status'] = 'unknown'
        self.assertIsNone(spell_plan(state))
        del state['board']['fields'][0]['field_spell']
        self.assertIsNone(spell_plan(state))

    def test_draw_spells_have_priority_and_exact_copy(self):
        state = spell_state(['Raigeki', 'Upstart Goblin', 'Pot of Greed', 'Pot of Greed'])
        plan = spell_plan(state)
        self.assertEqual((plan['card'], plan['address']), ('Pot of Greed', '0x510'))
        self.assertEqual(spell_plan(spell_state(['Upstart Goblin']))['effect'], 'draw')

    def test_raigeki_is_not_spent_on_unknown_facedown_stats_without_lethal(self):
        state = spell_state(['Raigeki'])
        self.assertIsNone(spell_plan(state))
        add_monster(state, False, 3, dict(address='0xa', face_down=True, hidden=True))
        self.assertIsNone(spell_plan(state))

    def test_smashing_requires_faceup_and_does_not_click_a_target(self):
        state = spell_state(['Smashing Ground'])
        add_monster(state, False, 0, dict(address='0xa', face_down=True, hidden=True))
        self.assertIsNone(spell_plan(state))
        add_monster(state, False, 1, monster('Enemy', '0xb', 1800))
        plan = spell_plan(state)
        self.assertEqual(plan['effect'], 'destroy')
        self.assertIsNone(plan['target_address'])

    def test_mst_uses_rival_backrow_including_facedown(self):
        state = spell_state(['Mystical Space Typhoon'])
        self.assertIsNone(spell_plan(state))
        state['board']['fields'][1]['visual_candidates']['MagicCards']['candidates'][4] = dict(
            helper_index=4, card=dict(address='0xa', hidden=True, face_down=True, slot=4))
        plan = spell_plan(state)
        self.assertEqual((plan['target_address'], plan['target_zone'], plan['target_side']),
                         ('0xa', 4, 'rival'))

    def test_equip_chooses_strongest_faceup_own_monster(self):
        state = spell_state(['Big Bang Shot'])
        self.assertIsNone(spell_plan(state))
        weak, strong = monster('Weak', '0xa', 1600), monster('Strong', '0xb', 1900)
        weak['slot'], strong['slot'] = 1, 3
        add_monster(state, True, 1, weak)
        add_monster(state, True, 3, strong)
        self.assertEqual(spell_plan(state)['target_address'], '0xb')
        strong['face_down'] = True
        self.assertEqual(spell_plan(state)['target_address'], '0xa')

    def test_search_targets_match_requested_deck(self):
        for name, target in [('Terraforming', 'Gaia Power'),
                             ('Fire Formation - Tenki', 'Enraged Battle Ox')]:
            with self.subTest(name=name):
                self.assertEqual(spell_plan(spell_state([name]))['search_name'], target)

    def test_requires_published_activation_with_complete_hand_mapping(self):
        state = spell_state(['Pot of Greed'])
        state['actions']['helpers'][0]['options'] = [dict(value=2, name='Summon')]
        self.assertIsNone(spell_plan(state))
        state['actions']['helpers'] = []
        with self.assertRaisesRegex(ValueError, 'toda la mano'):
            spell_plan(state)

    def test_opponent_turn_busy_prompts_and_wrong_phase_never_offer_spell(self):
        for key, value in [('my_turn', False), ('phase', 'BATTLE')]:
            state = spell_state(['Pot of Greed'])
            state[key] = value
            self.assertIsNone(spell_plan(state))
        state = spell_state(['Pot of Greed'])
        state['actions']['flags']['Busy'] = True
        self.assertIsNone(spell_plan(state))
        state['actions']['flags']['Busy'] = False
        state['actions']['prompts']['CurrentChoiceHelper'] = '0xaaa'
        self.assertIsNone(spell_plan(state))

    def test_incomplete_backrow_is_not_treated_as_empty(self):
        state = spell_state(['Mystical Space Typhoon'])
        del state['board']['fields'][1]['visual_candidates']['MagicCards']
        with self.assertRaises(ValueError):
            spell_plan(state)

    def test_advisor_spell_mode_is_opt_in_and_does_not_mutate_state(self):
        state = spell_state(['Pot of Greed'])
        before = deepcopy(state)
        self.assertEqual(suggestions(state)['options'], [])
        self.assertEqual(suggestions(state, spells=True)['options'][0]['kind'], 'spell_candidate')
        self.assertEqual(state, before)
