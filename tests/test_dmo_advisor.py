from copy import deepcopy
import unittest

from dmo.strategy.dmo_advisor import Advisor, suggestions, render_advice
from tests.test_dmo_card_memory import state


class AdvisorTests(unittest.TestCase):
    def setUp(self):
        self.state = state()
        own = self.state['board']['fields'][0]
        rival = deepcopy(own)
        rival['local'] = False
        rival.pop('HandCards')
        self.state['board']['fields'].append(rival)
        self.own, self.rival = own, rival
        self.own['HandCards'] = [self.card('spell', 'Raigeki', 0, 0),
                                  self.card('monster', 'Giant Rat', 1400, 4),
                                  self.card('tribute', 'Green Baboon', 2600, 7)]
        self.own['HandCardCount'] = 3
        self.advisor = Advisor()

    def card(self, address, name, atk, level=4):
        return dict(address=address, name=name, ATK=atk, DEF=1000, Level=level,
                    location='Hand', face_down=False, CanAttack=True, AttackedThisTurn=False)

    def field_card(self, field, card, zone='MonsterCards'):
        card['location'] = 'Field'
        field['visual_candidates'][zone]['candidates'][0] = dict(
            helper_index=0, helper_address='helper', card=card)

    def stable(self):
        self.advisor.observe(self.state, 10)
        return self.advisor.observe(self.state, 11)

    def test_main_ranks_low_level_cards_without_claiming_legality(self):
        result = self.stable()
        self.assertFalse(result['executable'])
        self.assertEqual(result['options'][0]['card'], 'Giant Rat')
        self.assertIn('invocación normal disponible', render_advice(result))

    def test_first_observation_and_changed_state_require_wait(self):
        self.assertEqual(self.advisor.observe(self.state, 10)['status'], 'wait')
        self.own['HandCards'][1]['ATK'] = 1500
        self.assertEqual(self.advisor.observe(self.state, 11)['status'], 'wait')

    def test_gap_requires_new_stable_pair(self):
        self.advisor.observe(self.state, 10)
        self.assertEqual(self.advisor.observe(self.state, 20)['status'], 'wait')

    def test_identical_snapshots_just_over_three_seconds_remain_stable(self):
        self.advisor.observe(self.state, 10)
        self.assertEqual(self.advisor.observe(self.state, 13.01)['status'], 'suggestions')

    def test_opponent_turn_or_unverified_identity_gets_no_suggestions(self):
        for key in ('my_turn', 'local_player'):
            with self.subTest(key=key):
                self.state[key] = False
                self.assertEqual(self.stable()['status'], 'wait')
                self.state[key] = True

    def test_incomplete_hand_or_missing_zone_blocks_advice(self):
        self.own['HandCardCount'] = 99
        self.assertEqual(self.stable()['status'], 'insufficient_data')
        self.own['HandCardCount'] = 3
        del self.rival['visual_candidates']['MagicCards']
        self.assertEqual(self.stable()['status'], 'insufficient_data')

    def test_battle_comparison_uses_both_defender_stats(self):
        self.state['phase'] = 'BATTLE'
        self.field_card(self.own, self.card('a', 'Attacker', 1900))
        self.field_card(self.rival, self.card('b', 'Defender', 1200))
        option = self.stable()['options'][0]
        self.assertEqual(option['numerical_margin'], 700)
        self.assertIn('no es una estimación de daño', option['requires'][0])

    def test_no_direct_attack_from_empty_visual_references(self):
        self.state['phase'] = 'BATTLE'
        self.field_card(self.own, self.card('a', 'Attacker', 1900))
        result = self.stable()
        self.assertEqual(result['options'], [])
        self.assertIn('no implica que debas terminar', result['reason'])

    def test_hidden_target_and_inferred_identity_are_not_used(self):
        self.state['phase'] = 'BATTLE'
        self.field_card(self.own, self.card('a', 'Attacker', 1900))
        self.field_card(self.rival, dict(address='b', hidden=True, face_down=True))
        self.state['own_card_memory'] = dict(cards=[dict(name='Imaginary attacker', ATK=99999)])
        self.assertEqual(self.stable()['options'], [])

    def test_attack_flag_alone_does_not_override_already_attacked(self):
        self.state['phase'] = 'BATTLE'
        attacker = self.card('a', 'Attacker', 1900)
        attacker['AttackedThisTurn'] = True
        self.field_card(self.own, attacker)
        self.field_card(self.rival, self.card('b', 'Defender', 1200))
        self.assertEqual(self.stable()['options'], [])

    def test_high_defense_prevents_positive_comparison(self):
        self.state['phase'] = 'BATTLE'
        self.field_card(self.own, self.card('a', 'Attacker', 1900))
        defender = self.card('b', 'Defender', 1200)
        defender['DEF'] = 2500
        self.field_card(self.rival, defender)
        self.assertEqual(self.stable()['options'], [])

    def test_backrow_is_not_ignored(self):
        self.state['phase'] = 'BATTLE'
        self.field_card(self.own, self.card('a', 'Attacker', 1900))
        self.field_card(self.rival, self.card('b', 'Defender', 1200))
        self.field_card(self.rival, dict(address='c', hidden=True, face_down=True), 'MagicCards')
        self.assertIn('magias/trampas', render_advice(self.stable()))

    def test_advisor_does_not_modify_raw_state(self):
        original = deepcopy(self.state)
        suggestions(self.state)
        self.assertEqual(self.state, original)

    def test_busy_or_closed_state_suspends_suggestions(self):
        for flags in ({'Busy': True}, {'SelectingCard': True}, {'ShowChainPrompt': True},
                      {'GameStateOpen': False}):
            with self.subTest(flags=flags):
                self.state['actions'] = dict(status='observed', flags=flags)
                self.assertEqual(self.stable()['status'], 'wait')

    def test_partial_actions_do_not_enable_suggestions(self):
        self.state['actions'] = dict(status='partial')
        self.assertEqual(self.stable()['status'], 'insufficient_data')

    def publish_actions(self):
        self.own['player_index'] = 0
        helpers = []
        for i, card in enumerate(self.own['HandCards']):
            card['slot'] = i
            helpers.append(dict(CardLocation={'enum_raw': 1}, FieldID=0, ID=i,
                                options=[dict(value=2, name='Summon')] if i == 1 else []))
        self.state['actions'] = dict(status='observed', flags={'GameStateOpen': True}, helpers=helpers)
        return helpers

    def test_published_summon_filters_candidates(self):
        helpers = self.publish_actions()
        self.assertEqual(self.stable()['options'][0]['card'], 'Giant Rat')
        helpers[1]['options'] = []
        result = self.stable()
        self.assertEqual(result['options'], [])
        self.assertIn('no publica Summon', result['reason'])

    def test_stale_hand_coverage_rejects_mapping(self):
        self.publish_actions().pop()
        self.assertEqual(self.stable()['status'], 'insufficient_data')

    def test_empty_list_does_not_prove_no_summons(self):
        self.publish_actions().clear()
        self.assertEqual(self.stable()['status'], 'insufficient_data')

    def test_duplicate_ids_reject_mapping(self):
        helpers = self.publish_actions()
        helpers[2]['ID'] = 1
        self.assertEqual(self.stable()['status'], 'insufficient_data')

    def test_foreign_field_cannot_supply_summon(self):
        self.publish_actions()[1]['FieldID'] = 1
        self.assertEqual(self.stable()['status'], 'insufficient_data')

    def test_raw_enum_and_name_must_agree(self):
        self.publish_actions()[1]['options'] = [dict(value=5, name='Summon')]
        self.assertEqual(self.stable()['options'], [])

    def publish_attack(self):
        self.state['phase'] = 'BATTLE'
        self.own['player_index'] = 0
        attacker = self.card('a', 'Enraged Battle Ox', 2700)
        attacker['FieldSlot'] = 7
        self.field_card(self.own, attacker)
        self.field_card(self.rival, self.card('b', 'Defender', 1200))
        helper = dict(CardLocation={'enum_raw': 2}, FieldID=0, ID=7,
                      CardType={'enum_raw': 1}, options=[dict(value=5, name='Attack')])
        self.state['actions'] = dict(status='observed', flags={'GameStateOpen': True}, helpers=[helper])
        return helper

    def test_attack_disappears_even_when_raw_flags_remain_unchanged(self):
        helper = self.publish_attack()
        self.assertEqual(self.stable()['options'][0]['card'], 'Enraged Battle Ox')
        helper['options'] = []
        result = self.stable()
        self.assertEqual(result['options'], [])
        self.assertIn('Attack publicado', result['reason'])

    def test_attack_uses_fieldslot_not_visual_index(self):
        self.publish_attack()['ID'] = 0
        self.assertEqual(self.stable()['options'], [])

    def test_foreign_attack_does_not_enable_our_monster(self):
        self.publish_attack()['FieldID'] = 1
        self.assertEqual(self.stable()['options'], [])

    def test_duplicate_attack_mapping_is_rejected(self):
        helper = self.publish_attack()
        self.state['actions']['helpers'].append(deepcopy(helper))
        self.assertEqual(self.stable()['status'], 'insufficient_data')

    def test_stale_attack_in_closed_state_is_not_used(self):
        self.publish_attack()
        self.state['actions']['flags']['GameStateOpen'] = False
        self.assertEqual(self.stable()['status'], 'wait')

    def test_attack_enum_and_name_must_match(self):
        self.publish_attack()['options'] = [dict(value=2, name='Attack')]
        self.assertEqual(self.stable()['options'], [])

    def test_panda_uses_estimated_attack_against_visible_and_facedown_monsters(self):
        self.own['HandCards'] = [self.card('panda', 'Gyaku-Gire Panda', 800),
                                 self.card('saber', 'Sabersaurus', 1900)]
        self.own['HandCardCount'] = 2
        for i in range(3):
            self.rival['visual_candidates']['MonsterCards']['candidates'][i] = dict(
                helper_index=i, helper_address='helper',
                card=dict(address=f'enemy{i}', hidden=True, face_down=True))
        result = self.stable()
        self.assertEqual(result['options'][0]['card'], 'Gyaku-Gire Panda')
        self.assertEqual(result['options'][0]['estimated_attack'], 2300)
        self.assertEqual(self.own['HandCards'][0]['ATK'], 800)

    def test_panda_does_not_win_ranking_when_enemy_field_is_empty(self):
        self.own['HandCards'] = [self.card('panda', 'Gyaku-Gire Panda', 800),
                                 self.card('saber', 'Sabersaurus', 1900)]
        self.own['HandCardCount'] = 2
        self.assertEqual(self.stable()['options'][0]['card'], 'Sabersaurus')


if __name__ == '__main__':
    unittest.main()
