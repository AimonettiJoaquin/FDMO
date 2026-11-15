from copy import deepcopy
import unittest

from dmo_card_memory import OwnCardMemory, render_card_memory


def state():
    empty = [dict(helper_index=i, helper_address='0x0', card=None) for i in range(5)]
    field = dict(local=True, address='field', owner_identity='owner', generation_id=1,
                 HandCardCount=2, HandCards=[
                     dict(address='hand1', name='Raigeki', location='Hand', CustomID=1),
                     dict(address='hand2', name='Giant Rat', location='Hand', CustomID=1)],
                 visual_candidates={z: dict(candidates=deepcopy(empty))
                                    for z in ('MonsterCards', 'MagicCards')})
    return dict(status='duel', player='player', manager='manager', local_player=True,
                my_turn=True, phase='MAIN 1', board=dict(status='partial', fields=[field]))


def place(before):
    after = deepcopy(before)
    field = after['board']['fields'][0]
    field['HandCards'].pop(0)
    field['HandCardCount'] -= 1
    field['visual_candidates']['MagicCards']['candidates'][2] = dict(helper_index=2,
        helper_address='helper1', card=dict(address='card1', face_down=True, unknown=True))
    return after


class OwnCardMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tracker = OwnCardMemory()
        self.before = state()
        self.after = place(self.before)

    def infer(self):
        self.tracker.observe(self.before, 10)
        return self.tracker.observe(self.after, 11)

    def test_simple_transition_is_only_inferred_and_raw_state_is_unchanged(self):
        raw = deepcopy(self.after)
        result = self.infer()
        card = result['cards'][0]
        self.assertEqual(card['name'], 'Raigeki')
        self.assertEqual(card['helper_index'], 2)
        self.assertEqual(card['source'], 'inferred_transition')
        self.assertFalse(card['confirmed'])
        self.assertEqual(self.after, raw)
        self.assertIn('inferida, sin confirmar', render_card_memory(result))

    def test_explicit_confirmation_is_kept_until_card_leaves(self):
        event = self.infer()['cards'][0]['event_id']
        self.tracker.confirm_set(event, 'Raigeki', source='user_confirmation', evidence='Usuario confirmó Raigeki.')
        result = self.tracker.observe(self.after, 12)
        self.assertTrue(result['cards'][0]['confirmed'])
        self.assertEqual(result['cards'][0]['source'], 'user_confirmation')
        self.after['board']['fields'][0]['visual_candidates']['MagicCards']['candidates'][2] = dict(
            helper_index=2, helper_address='0x0', card=None)
        self.assertEqual(self.tracker.observe(self.after, 13)['cards'], [])
        with self.assertRaisesRegex(ValueError, 'ya no'):
            self.tracker.confirm_set(event, 'Raigeki', source='user_confirmation', evidence='Vieja confirmación')

    def test_confirmation_cannot_be_based_on_inference_or_wrong_name(self):
        event = self.infer()['cards'][0]['event_id']
        for source, name, evidence in [('inferred_transition', 'Raigeki', 'Coincidencia'),
                                        ('confirmed_action', 'Raigeki', ''),
                                        ('user_confirmation', 'Heavy Storm', 'Usuario')]:
            with self.subTest(source=source, name=name), self.assertRaises(ValueError):
                self.tracker.confirm_set(event, name, source=source, evidence=evidence)

    def test_two_cards_leaving_hand_are_ambiguous(self):
        self.after['board']['fields'][0].update(HandCards=[], HandCardCount=0)
        self.assertEqual(self.infer()['cards'], [])

    def test_simultaneous_draw_and_set_is_ambiguous(self):
        field = self.after['board']['fields'][0]
        field['HandCards'].append(dict(address='hand3', name='Jar of Greed', location='Hand'))
        field['HandCardCount'] += 1
        self.assertEqual(self.infer()['cards'], [])

    def test_two_field_changes_are_ambiguous(self):
        slots = self.after['board']['fields'][0]['visual_candidates']['MagicCards']['candidates']
        slots[1] = dict(helper_index=1, helper_address='helper2',
                        card=dict(address='card2', face_down=True, unknown=True))
        self.assertEqual(self.infer()['cards'], [])

    def test_replacing_occupied_slot_does_not_infer_set(self):
        self.before['board']['fields'][0]['visual_candidates']['MagicCards']['candidates'][2] = dict(
            helper_index=2, helper_address='oldhelper',
            card=dict(address='oldcard', face_down=True, unknown=True))
        self.assertEqual(self.infer()['cards'], [])

    def test_opponent_turn_does_not_infer_our_set(self):
        self.before['my_turn'] = self.after['my_turn'] = False
        self.assertEqual(self.infer()['cards'], [])

    def test_phase_transition_does_not_infer_set(self):
        self.after['phase'] = 'BATTLE'
        self.assertEqual(self.infer()['cards'], [])

    def test_inconsistent_hand_count_blocks_tracking(self):
        self.after['board']['fields'][0]['HandCardCount'] = 99
        self.assertEqual(self.infer()['status'], 'unavailable')

    def test_duplicate_hand_pointer_blocks_tracking(self):
        field = self.after['board']['fields'][0]
        field['HandCards'].append(deepcopy(field['HandCards'][0]))
        field['HandCardCount'] += 1
        self.assertEqual(self.infer()['status'], 'unavailable')

    def test_unknown_facedown_name_is_not_required_but_state_is(self):
        card = self.after['board']['fields'][0]['visual_candidates']['MagicCards']['candidates'][2]['card']
        card['face_down'] = False
        self.assertEqual(self.infer()['status'], 'unavailable')

    def test_opponent_data_is_not_used(self):
        self.after['board']['fields'].append(dict(local=False))
        self.assertEqual(self.infer()['cards'][0]['name'], 'Raigeki')

    def test_already_set_card_at_start_is_not_identified(self):
        self.assertEqual(self.tracker.observe(self.after, 10)['cards'], [])

    def test_sampling_gap_resets_even_confirmed_memory(self):
        event = self.infer()['cards'][0]['event_id']
        self.tracker.confirm_set(event, 'Raigeki', source='confirmed_action', evidence='Prueba de ejecutor simulado')
        self.assertEqual(self.tracker.observe(self.after, 20)['cards'], [])

    def test_non_increasing_time_resets_tracking(self):
        self.infer()
        self.assertEqual(self.tracker.observe(self.after, 11)['cards'], [])

    def test_unstable_sample_resets_tracking(self):
        self.infer()
        self.tracker.observe(dict(status='unstable'), 12)
        self.assertEqual(self.tracker.observe(self.after, 13)['cards'], [])

    def test_context_change_clears_memory(self):
        for change in ('manager', 'player', 'field', 'generation'):
            with self.subTest(change=change):
                self.tracker = OwnCardMemory()
                self.infer()
                new = deepcopy(self.after)
                if change in ('manager', 'player'):
                    new[change] = 'other'
                elif change == 'field':
                    new['board']['fields'][0]['address'] = 'other'
                else:
                    new['board']['fields'][0]['generation_id'] = 2
                self.assertEqual(self.tracker.observe(new, 12)['cards'], [])

    def test_replaced_or_revealed_card_loses_old_association(self):
        for change in ('address', 'face_down'):
            with self.subTest(change=change):
                self.tracker = OwnCardMemory()
                self.infer()
                new = deepcopy(self.after)
                card = new['board']['fields'][0]['visual_candidates']['MagicCards']['candidates'][2]['card']
                if change == 'address':
                    card['address'] = 'replacement'
                else:
                    card.update(face_down=False, unknown=False, name='Heavy Storm', location='Field')
                self.assertEqual(self.tracker.observe(new, 12)['cards'], [])

    def test_returned_memory_cannot_mutate_tracker(self):
        result = self.infer()
        result['cards'][0]['confirmed'] = True
        self.assertFalse(self.tracker.snapshot()['cards'][0]['confirmed'])


if __name__ == '__main__':
    unittest.main()
