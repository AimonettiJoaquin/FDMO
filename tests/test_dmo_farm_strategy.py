from copy import deepcopy
from unittest import TestCase

from dmo_advisor import suggestions
from dmo_battle import battle_decision
from dmo_farm_strategy import FarmCombat, damage_route
from dmo_spells import spell_plan
from tests.test_dmo_battle import sample, monster, add_monster, publish_attack
from tests.test_dmo_spells import spell_state


def hand_monster(state, name, atk, *, attribute=0, summon=True):
    own = state['board']['fields'][0]
    index = len(own['HandCards'])
    card = dict(monster(name, hex(0x9000 + index * 8), atk), Level=4,
                location='Hand', slot=index, attribute_raw=attribute)
    own['HandCards'].append(card)
    own['HandCardCount'] += 1
    state['actions']['helpers'].append(dict(CardLocation=dict(enum_raw=1), FieldID=0,
        ID=index, options=[dict(value=2, name='Summon')] if summon else []))
    return card


class FarmStrategyTests(TestCase):
    def strong_enemy(self, state, *, atk=3000, defense=2000, position=1, slot=0):
        card = dict(monster('Strong rival', '0xf000', atk, position), DEF=defense,
                    slot=slot, attribute_raw=5)
        add_monster(state, False, slot, card)
        return card

    def test_summons_assailant_over_higher_printed_attack_against_unbeatable_enemy(self):
        state = spell_state([])
        hand_monster(state, 'D.D. Assailant', 1700)
        hand_monster(state, 'Sabersaurus', 1900)
        self.strong_enemy(state)
        plan = suggestions(state)['options'][0]
        self.assertEqual((plan['card'], plan['intent']), ('D.D. Assailant', 'banish_trade'))

    def test_does_not_force_assailant_if_gaia_makes_normal_battle_sufficient(self):
        state = spell_state(['Gaia Power'])
        hand_monster(state, 'D.D. Assailant', 1700)
        hand_monster(state, 'Sabersaurus', 1900)
        self.strong_enemy(state, atk=2300)
        plan = next(p for p in suggestions(state)['options'] if p['kind'] == 'summon_candidate')
        self.assertEqual((plan['card'], plan['estimated_attack']), ('Sabersaurus', 2400))

    def test_pending_gaia_also_boosts_earth_opponent(self):
        state = spell_state(['Gaia Power'])
        hand_monster(state, 'D.D. Assailant', 1700)
        hand_monster(state, 'Sabersaurus', 1900)
        self.strong_enemy(state, atk=2300)['attribute_raw'] = 0
        plan = next(p for p in suggestions(state)['options'] if p['kind'] == 'summon_candidate')
        self.assertEqual(plan['card'], 'D.D. Assailant')

    def test_active_gaia_is_not_added_twice_to_field_monsters(self):
        state = spell_state(['Raigeki'])
        own = state['board']['fields'][0]
        own['field_spell']['card'] = dict(name='Gaia Power', face_down=False)
        add_monster(state, True, 0, dict(monster('Sabersaurus', '0xa', 2400), attribute_raw=0))
        self.strong_enemy(state, atk=2600)
        self.assertEqual(FarmCombat(state).strongest(), 2400)
        self.assertEqual(spell_plan(state)['card'], 'Raigeki')

    def test_gaia_bonus_is_applied_to_earth_hand_only(self):
        state = spell_state([])
        state['board']['fields'][0]['field_spell']['card'] = dict(name='Gaia Power', face_down=False)
        earth = hand_monster(state, 'Earth warrior', 1800)
        dark = hand_monster(state, 'Dark warrior', 2000, attribute=5)
        combat = FarmCombat(state)
        self.assertEqual(combat.attack(earth, hand=True), 2300)
        self.assertEqual(combat.attack(dark, hand=True), 2000)

    def test_raigeki_and_smashing_are_reserved_for_battle_when_we_are_stronger(self):
        for name in ('Raigeki', 'Smashing Ground'):
            state = spell_state([name])
            hand_monster(state, 'Sabersaurus', 1900)
            self.strong_enemy(state, atk=1800)
            self.assertIsNone(spell_plan(state))

    def test_removal_is_saved_when_assailant_can_make_the_trade(self):
        state = spell_state(['Raigeki', 'Smashing Ground'])
        hand_monster(state, 'D.D. Assailant', 1700)
        self.strong_enemy(state)
        self.assertIsNone(spell_plan(state))

    def test_no_assailant_suicide_with_lethal_incoming_damage(self):
        state = spell_state(['Raigeki'])
        hand_monster(state, 'D.D. Assailant', 1700)
        self.strong_enemy(state)
        state['board']['fields'][0]['life_points_candidate'] = 1300
        self.assertIsNone(FarmCombat(state).trade())
        self.assertEqual(spell_plan(state)['card'], 'Raigeki')

    def test_assailant_does_not_trade_against_defense_position(self):
        state = spell_state(['Raigeki'])
        hand_monster(state, 'D.D. Assailant', 1700)
        self.strong_enemy(state, defense=3000, position=2)
        self.assertIsNone(FarmCombat(state).trade())
        self.assertEqual(spell_plan(state)['card'], 'Raigeki')

    def test_high_attack_but_low_defense_can_be_destroyed_without_removal(self):
        state = spell_state(['Raigeki', 'Smashing Ground'])
        hand_monster(state, 'Sabersaurus', 1900)
        self.strong_enemy(state, defense=1000, position=2)
        self.assertIsNone(spell_plan(state))

    def test_smashing_does_not_pretend_to_remove_the_highest_attack(self):
        state = spell_state(['Smashing Ground', 'Raigeki'])
        hand_monster(state, 'Sabersaurus', 1900)
        self.strong_enemy(state, defense=1000)
        add_monster(state, False, 1, dict(monster('High DEF', '0xf100', 1000), DEF=2500))
        self.assertIsNone(FarmCombat(state).removal_reason('Smashing Ground'))
        self.assertEqual(spell_plan(state)['card'], 'Raigeki')

    def test_nobleman_targets_only_rival_facedown_monster(self):
        state = spell_state(['Nobleman of Crossout'])
        self.assertIsNone(spell_plan(state))
        add_monster(state, False, 3, dict(address='0xfd', hidden=True, face_down=True))
        plan = spell_plan(state)
        self.assertEqual((plan['effect'], plan['target_address'], plan['target_zone'], plan['target_side']),
                         ('banish', '0xfd', 3, 'rival'))
        state['board']['fields'][1]['visual_candidates']['MonsterCards']['candidates'][3]['card']['face_position_raw'] = 1
        self.assertIsNone(spell_plan(state))

    def test_removal_opens_lethal_even_if_defender_is_weaker(self):
        for name in ('Raigeki', 'Smashing Ground'):
            state = spell_state([name])
            add_monster(state, True, 0, monster('Ours', '0xa', 2000))
            self.strong_enemy(state, atk=1000)
            state['board']['fields'][1]['life_points_candidate'] = 1500
            self.assertEqual(FarmCombat(state).removal_reason(name), 'lethal')
            self.assertEqual(spell_plan(state)['card'], name)

    def test_lethal_removal_takes_priority_over_search_draw_and_assailant_trade(self):
        state = spell_state(['Reinforcement of the Army', 'Upstart Goblin', 'Raigeki'])
        hand_monster(state, 'D.D. Assailant', 1700)
        self.strong_enemy(state)
        state['board']['fields'][1]['life_points_candidate'] = 1000
        self.assertEqual(spell_plan(state)['card'], 'Raigeki')

    def test_does_not_spend_removal_or_heal_rival_when_battle_is_already_lethal(self):
        state = spell_state(['Raigeki', 'Upstart Goblin'])
        add_monster(state, True, 0, monster('Ours', '0xa', 3000))
        self.strong_enemy(state, atk=1000)
        state['board']['fields'][1]['life_points_candidate'] = 1500
        self.assertIsNone(spell_plan(state))

    def test_lethal_with_pending_gaia_activates_gaia_first(self):
        state = spell_state(['Gaia Power', 'Raigeki', 'Upstart Goblin'])
        add_monster(state, True, 0, dict(monster('Sabersaurus', '0xa', 1900), attribute_raw=0))
        self.strong_enemy(state, atk=1000)
        state['board']['fields'][1]['life_points_candidate'] = 2200
        self.assertEqual(spell_plan(state)['card'], 'Gaia Power')

    def test_panda_loses_enemy_count_bonus_after_raigeki(self):
        state = spell_state(['Raigeki'])
        add_monster(state, True, 0, monster('Gyaku-Gire Panda', '0xa', 1800))
        self.strong_enemy(state, atk=1400)
        state['board']['fields'][1]['life_points_candidate'] = 1500
        combat = FarmCombat(state)
        self.assertEqual(combat.damage({'0xf000'}), 1300)
        self.assertIsNone(spell_plan(state))

    def test_main_two_never_counts_a_future_battle_as_lethal(self):
        state = spell_state(['Raigeki'])
        state['phase'] = 'MAIN 2'
        add_monster(state, True, 0, monster('Ours', '0xa', 3000))
        self.strong_enemy(state, atk=1000)
        state['board']['fields'][1]['life_points_candidate'] = 1500
        self.assertIsNone(spell_plan(state))

    def test_unavailable_summon_cannot_supply_imaginary_lethal(self):
        state = spell_state(['Raigeki'])
        hand_monster(state, 'Sabersaurus', 1900, summon=False)
        self.strong_enemy(state, atk=1000)
        state['board']['fields'][1]['life_points_candidate'] = 1500
        self.assertEqual(FarmCombat(state).damage({'0xf000'}), 0)

    def test_rota_prefers_assailant_when_threat_requires_it(self):
        state = spell_state(['Reinforcement of the Army'])
        self.strong_enemy(state)
        self.assertEqual(spell_plan(state)['search_name'], 'D.D. Assailant')

    def test_battle_assailant_attacks_the_strong_threat_before_other_targets(self):
        state = sample()
        add_monster(state, True, 0, monster('D.D. Assailant', '0xa', 1700))
        add_monster(state, True, 1, dict(monster('Ours', '0xb', 1900), FieldSlot=8))
        self.strong_enemy(state)
        add_monster(state, False, 1, monster('Easy', '0xf100', 1000))
        publish_attack(state)
        publish_attack(state, slot=8)
        plan = battle_decision(state)
        self.assertEqual((plan['attacker'], plan['target'], plan['intent']), ('0xa', '0xf000', 'banish_trade'))
        self.assertEqual(plan['expected_self_damage'], 1300)

    def test_battle_lethal_line_precedes_assailant_sacrifice(self):
        state = sample()
        add_monster(state, True, 0, monster('D.D. Assailant', '0xa', 1700))
        self.strong_enemy(state)
        add_monster(state, False, 1, monster('Easy', '0xf100', 500))
        state['board']['fields'][1]['life_points_candidate'] = 1000
        publish_attack(state)
        plan = battle_decision(state)
        self.assertEqual((plan['target'], plan['intent']), ('0xf100', 'lethal'))

    def test_planning_does_not_mutate_observed_state(self):
        state = spell_state(['Raigeki', 'Gaia Power'])
        hand_monster(state, 'D.D. Assailant', 1700)
        self.strong_enemy(state)
        before = deepcopy(state)
        spell_plan(state)
        suggestions(state, spells=True)
        self.assertEqual(state, before)

    def test_damage_route_finds_attack_order_and_preserves_unknown_blockers(self):
        ours = [monster('Weak', '0xa', 1000), monster('Strong', '0xb', 2000)]
        theirs = [monster('Defender', '0xf', 1500)]
        damage, route = damage_route(ours, theirs)
        self.assertEqual(damage, 1500)
        self.assertEqual(route, (('0xb', '0xf'), ('0xa', None)))
        theirs.append(dict(address='0xfd', hidden=True, face_down=True))
        self.assertEqual(damage_route(ours, theirs)[0], 500)
