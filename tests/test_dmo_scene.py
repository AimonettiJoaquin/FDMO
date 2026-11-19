import unittest
from unittest.mock import Mock, patch

from tests import test_dmo_board as fixtures
from dmo.memory.dmo_scene import SceneReader, render_scene


class SceneTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.BoardTests()
        self.f.setUp()
        manager, self.field = self.f.board()
        self.card = self.f.card('Raigeki')
        self.f.values[self.card, 'CardLocation'] = {'enum_raw': 1}
        self.f.values[self.field, 'HandCards'] = self.f.collection([self.card])
        self.f.values[self.field, 'HandCardCount'] = 1
        self.board = self.f.reader.read(manager)
        self.element = self.f.named_object('SelectableDuelElement')
        self.f.mem.put(self.element + 16, '<Q', 0x18000)
        for key, value in dict(gameCard=self.card, World=False, Selected=False,
                                ElementType={'enum_raw': 0}, cardHover=0).items():
            self.f.values[self.element, key] = value
        self.f.values[self.field, 'DuelElements'] = self.f.collection([self.element])
        self.f.values[self.f.monitor.player, 'TargetableElements'] = self.f.collection([self.element])
        self.reader = SceneReader(self.f.monitor)
        self.f.types[self.f.monitor.player] = 'DuelistScript'
        for key in ('CurrentDuelElement', 'CurrentCardPreviewSource'):
            self.f.values[self.f.monitor.player, key] = 0
        self.misc = self.f.named_object('global::Misc')
        self.f.values[self.f.monitor.player, 'misc'] = self.misc
        self.f.values[self.misc, 'duelistScript'] = self.f.monitor.player
        for key in ('HighlightedObject', 'HoveredBattleObject', 'HoveredSlot'):
            self.f.values[self.misc, key] = 0

    def test_cursor_object_is_only_a_candidate(self):
        obj = self.f.named_object('GameObject')
        self.f.values[self.misc, 'HoveredBattleObject'] = obj
        result = self.reader.cursor_references()
        self.assertFalse(result['card_link_verified'])
        item = result['objects']['HoveredBattleObject']
        self.assertEqual(item['address'], hex(obj))
        self.assertEqual(item['class_name'], 'GameObject')
        self.assertEqual(item['status'], 'unvalidated_candidate')

    def test_preview_is_reported_when_misc_hover_is_null(self):
        obj = self.f.named_object('GameObject')
        self.f.values[self.f.monitor.player, 'CurrentCardPreviewSource'] = obj
        result = self.reader.read(self.board)
        preview = result['preview_references']
        self.assertFalse(preview['card_link_verified'])
        self.assertEqual(preview['objects']['CurrentCardPreviewSource']['address'], hex(obj))
        self.assertEqual(preview['objects']['CurrentCardPreviewSource']['class_name'], 'GameObject')
        self.assertEqual(result['cursor_references']['objects']['HoveredBattleObject']['status'], 'null')

    def test_preview_mutation_rejected(self):
        original = self.f.monitor.pointer.side_effect
        calls = 0
        def changing(obj, typ, key):
            nonlocal calls
            if key == 'CurrentDuelElement':
                calls += 1
                return 0 if calls == 1 else 123
            return original(obj, typ, key)
        self.f.monitor.pointer.side_effect = changing
        with self.assertRaisesRegex(ValueError, 'vista previa cambiaron'):
            self.reader.preview_references()

    def test_preview_failure_does_not_hide_other_diagnostics(self):
        self.f.values[self.f.monitor.player, 'CurrentDuelElement'] = 0xdeadbeef
        result = self.reader.read(self.board)
        self.assertNotIn('preview_references', result)
        self.assertTrue(any('Vista previa:' in e for e in result['errors']))
        self.assertIn('cursor_references', result)
        self.assertTrue(result['hand_elements'])

    def test_cursor_rejects_wrong_player(self):
        self.f.values[self.misc, 'duelistScript'] = 123
        with self.assertRaisesRegex(ValueError, 'jugador local'):
            self.reader.cursor_references()

    def test_cursor_mutation_is_rejected(self):
        original = self.f.monitor.pointer.side_effect
        calls = 0
        def changing(obj, typ, key):
            nonlocal calls
            if key == 'HoveredSlot':
                calls += 1
                return 0 if calls == 1 else 123
            return original(obj, typ, key)
        self.f.monitor.pointer.side_effect = changing
        with self.assertRaisesRegex(ValueError, 'cambiaron'):
            self.reader.cursor_references()

    def test_links_exact_hand_object_without_claiming_coordinates(self):
        result = self.reader.read(self.board)
        self.assertEqual(result['status'], 'hand_objects_linked')
        self.assertEqual(len(result['hand_elements']), 1)
        item = result['hand_elements'][0]
        self.assertEqual(item['name'], 'Raigeki')
        self.assertEqual(item['card_address'], hex(self.card))
        self.assertIsNone(item['screen_position'])
        self.assertFalse(result['executable'])
        self.assertFalse(result['screen_coordinates_available'])
        self.assertIn('sin resolver', render_scene(result))

    def test_same_named_different_card_is_not_linked(self):
        self.f.values[self.element, 'gameCard'] = self.f.card('Raigeki')
        result = self.reader.read(self.board)
        self.assertEqual(result['hand_elements'], [])

    def test_world_element_publishes_revalidated_native_game_object(self):
        self.f.values[self.element, 'World'] = True
        reader = self.reader.native_object_reader = Mock()
        reader.component_game_object.return_value = dict(verified=True,
            component='0x18000', game_object='0x28000')
        result = self.reader.read(self.board)
        link = result['hand_elements'][0]['game_object_link']
        self.assertEqual(link['game_object'], '0x28000')
        self.assertTrue(link['verified'])

    def test_failed_native_link_preserves_scene_but_cannot_claim_object(self):
        self.f.values[self.element, 'World'] = True
        self.reader.native_object_reader = Mock()
        self.reader.native_object_reader.component_game_object.side_effect = ValueError('changed')
        result = self.reader.read(self.board)
        item = result['hand_elements'][0]
        self.assertNotIn('game_object_link', item)
        self.assertEqual(item['game_object_link_error'], 'changed')

    def test_local_field_element_links_visual_card_and_selected_flag(self):
        field_card = self.f.card('King Tiger Wanghu')
        self.f.values[self.element, 'gameCard'] = field_card
        self.f.values[self.element, 'Selected'] = True
        self.board['fields'][0]['visual_candidates'] = dict(MonsterCards=dict(candidates=[
            dict(helper_index=2, card=dict(address=hex(field_card),
                                           name='King Tiger Wanghu', slot=2))]))
        result = self.reader.read(self.board)
        self.assertEqual(len(result['field_elements']), 1)
        self.assertEqual(result['field_elements'][0]['visual_slot'], 2)
        self.assertTrue(result['field_elements'][0]['selected'])
        self.assertEqual(result['field_elements'][0]['card_address'], hex(field_card))

    def test_missing_collection_does_not_hide_other_source(self):
        self.f.values[self.field, 'DuelElements'] = 0
        result = self.reader.read(self.board)
        self.assertEqual(len(result['hand_elements']), 1)
        self.assertTrue(result['errors'])

    def test_links_rival_magic_to_its_own_field_and_zone(self):
        rival = self.f.named_object('DuelFieldScript')
        card = self.f.card('Call of the Mummy')
        self.f.values[self.element, 'gameCard'] = card
        self.f.values[self.element, 'World'] = True
        self.f.values[rival, 'DuelElements'] = self.f.collection([self.element])
        self.f.values[self.field, 'DuelElements'] = self.f.collection([])
        self.board['fields'].append(dict(address=hex(rival), local=False,
            visual_candidates=dict(MagicCards=dict(candidates=[dict(helper_index=0,
                card=dict(address=hex(card), name='Call of the Mummy', slot=0))]))))
        result = self.reader.read(self.board)
        self.assertEqual(len(result['field_elements']), 1)
        link = result['field_elements'][0]
        self.assertEqual((link['local'], link['zone'], link['visual_slot'], link['field_address']),
                         (False, 'MagicCards', 0, hex(rival)))

    def test_hidden_field_card_link_does_not_read_its_name(self):
        card = self.f.card('Hidden identity')
        self.f.values[self.element, 'gameCard'] = card
        self.board['fields'][0]['visual_candidates'] = dict(MagicCards=dict(candidates=[
            dict(helper_index=2, card=dict(address=hex(card), hidden=True, face_down=True, slot=2))]))
        self.f.monitor.field.reset_mock()
        result = self.reader.read(self.board)
        self.assertIsNone(result['field_elements'][0]['name'])
        self.assertFalse(any(call.args[1] == 'GameCard' for call in self.f.monitor.field.call_args_list))

    def test_hand_change_rejects_old_links(self):
        self.f.values[self.field, 'HandCards'] = self.f.collection([])
        with self.assertRaisesRegex(ValueError, 'mano cambió'):
            self.reader.read(self.board)

    def test_incomplete_hand_is_not_traversed(self):
        self.board['fields'][0]['HandCards'] = None
        with self.assertRaisesRegex(ValueError, 'mano no está lista'):
            self.reader.read(self.board)

    def test_bad_hover_still_preserves_verified_card_link(self):
        self.f.values[self.element, 'cardHover'] = 0xdeadbeef
        result = self.reader.read(self.board)
        self.assertEqual(result['hand_elements'][0]['name'], 'Raigeki')
        self.assertIn('hover_error', result['hand_elements'][0])

    def test_collection_mutation_discards_that_source(self):
        self.f.values[self.f.monitor.player, 'TargetableElements'] = self.f.collection([])
        with patch('dmo.memory.dmo_scene.list_refs', side_effect=[[self.element], [], [], [], [self.card]]):
            result = self.reader.read(self.board)
        self.assertEqual(result['hand_elements'], [])
        self.assertTrue(result['errors'])

    def discovery_fixture(self, hits=None, complete=True):
        state = dict(status='duel', player=hex(self.f.monitor.player),
                     manager='0xabc', board=self.board)
        self.f.monitor.layout = {'classes': {'SelectableDuelElement': {'fields': {
            'gameCard': dict(offset=48, type_code=18, attributes=6)}}}}
        self.f.monitor.snapshot.return_value = state
        self.f.mem.scan_complete = complete
        self.f.mem.scan_stats = dict(stop_reason='completed' if complete else 'budget')
        addresses = hits if hits is not None else [self.element + 48]
        self.f.mem.find_pointers = Mock(side_effect=lambda needles, budget, **kwargs:
                                       ((hex(self.card), x) for x in addresses))
        return state

    def test_discovery_finds_hand_element_outside_collections(self):
        state = self.discovery_fixture()
        self.f.values[self.field, 'DuelElements'] = self.f.collection([])
        self.f.values[self.f.monitor.player, 'TargetableElements'] = self.f.collection([])
        result = self.reader.discover_hand_elements(state, 4096)
        self.assertEqual(result['status'], 'hand_objects_linked')
        self.assertEqual(result['hand_elements'][0]['card_address'], hex(self.card))
        self.assertTrue(result['scan']['complete'])
        self.assertFalse(result['executable'])
        self.f.mem.find_pointers.assert_called_once_with({hex(self.card): self.card}, 4096,
                                                        preferred_addresses=(self.card,))

    def test_discovery_rejects_arbitrary_pointer_and_different_card(self):
        state = self.discovery_fixture([0xdead, self.element + 48])
        self.f.values[self.element, 'gameCard'] = self.f.card('Raigeki')
        result = self.reader.discover_hand_elements(state, 4096)
        self.assertEqual(result['hand_elements'], [])
        self.assertEqual(result['rejected_candidates'], 2)

    def test_discovery_rejects_context_change(self):
        state = self.discovery_fixture()
        self.f.monitor.snapshot.return_value = dict(state, manager='0xdef')
        result = self.reader.discover_hand_elements(state, 4096)
        self.assertEqual(result['status'], 'context_changed')
        self.assertEqual(result['hand_elements'], [])

    def test_discovery_rechecks_candidate_after_scan(self):
        state = self.discovery_fixture()
        def after():
            self.f.values[self.element, 'gameCard'] = 0
            return state
        self.f.monitor.snapshot.side_effect = after
        result = self.reader.discover_hand_elements(state, 4096)
        self.assertEqual(result['hand_elements'], [])

    def test_discovery_reports_partial_scan(self):
        state = self.discovery_fixture(complete=False)
        result = self.reader.discover_hand_elements(state, 4096)
        self.assertFalse(result['scan']['complete'])
        self.assertEqual(result['scan']['stop_reason'], 'budget')

    def test_discovery_limits_pointer_hits(self):
        state = self.discovery_fixture([self.element + 48] * 5)
        result = self.reader.discover_hand_elements(state, 4096, max_hits=2)
        self.assertEqual(result['pointer_hits'], 2)
        self.assertEqual(len(result['hand_elements']), 1)
        self.assertFalse(result['scan']['complete'])
        self.assertEqual(result['scan']['stop_reason'], 'pointer_hit_limit')

    def test_discovery_requires_stable_duel(self):
        state = self.discovery_fixture()
        with self.assertRaisesRegex(ValueError, 'duelo estable'):
            self.reader.discover_hand_elements(dict(state, status='unstable'), 4096)
        self.f.mem.find_pointers.assert_not_called()

    def test_fast_discovery_stops_after_target_and_revalidates(self):
        state = self.discovery_fixture([self.element + 48, 0xdead])
        original = self.reader.element
        def interactive(obj, hand):
            item = original(obj, hand)
            if item is not None:
                item['hover'] = dict(hoverable=True)
            return item
        with patch.object(self.reader, 'element', side_effect=interactive):
            result = self.reader.discover_hand_elements(state, 4096,
                stop_when_linked=True, target_card=hex(self.card))
        self.assertEqual(result['pointer_hits'], 1)
        self.assertEqual(result['scan']['stop_reason'], 'hand_linked')
        self.assertFalse(result['scan']['complete'])
        self.assertEqual(result['status'], 'hand_objects_linked')
        self.assertEqual(self.f.monitor.snapshot.call_count, 2)

    def test_fast_discovery_does_not_accept_foreign_target(self):
        state = self.discovery_fixture()
        with self.assertRaisesRegex(ValueError, 'mano actual'):
            self.reader.discover_hand_elements(state, 4096, target_card='0xdead')
        self.f.mem.find_pointers.assert_not_called()

    def test_fast_discovery_does_not_wait_for_other_hand_cards(self):
        state = self.discovery_fixture([self.element + 48, 0xdead])
        own = next(f for f in state['board']['fields'] if f.get('local') is True)
        own['HandCards'].append(dict(address='0x90000', name='Other', slot=1))
        own['HandCardCount'] = 2
        original = self.reader.element
        def interactive(obj, hand):
            item = original(obj, hand)
            if item:
                item['hover'] = dict(hoverable=True)
            return item
        with patch.object(self.reader, 'element', side_effect=interactive):
            result = self.reader.discover_hand_elements(state, 4096,
                stop_when_linked=True, target_card=hex(self.card))
        self.assertEqual(result['pointer_hits'], 1)
        self.assertEqual(result['scan']['stop_reason'], 'hand_linked')
        self.assertEqual(len(result['hand_elements']), 1)
        self.assertEqual(self.f.mem.find_pointers.call_args.args[0], {hex(self.card): self.card})

    def test_fast_discovery_discards_links_if_hand_changes(self):
        state = self.discovery_fixture()
        self.f.monitor.snapshot.return_value = dict(state, manager='0xdef')
        original = self.reader.element
        def interactive(obj, hand):
            item = original(obj, hand)
            if item:
                item['hover'] = dict(hoverable=True)
            return item
        with patch.object(self.reader, 'element', side_effect=interactive):
            result = self.reader.discover_hand_elements(state, 4096,
                stop_when_linked=True, target_card=hex(self.card))
        self.assertEqual(result['status'], 'context_changed')
        self.assertEqual(result['hand_elements'], [])

    def hint_report(self):
        return dict(pid=123, scene=dict(hand_elements=[dict(element=hex(self.element),
                    card_address=hex(self.card), native_element='0x18000', name='fake saved name')]))

    def test_report_hints_revalidate_and_use_live_hover(self):
        self.reader.load_hints(self.hint_report(), 123)
        self.f.values[self.field, 'DuelElements'] = self.f.collection([])
        self.f.values[self.f.monitor.player, 'TargetableElements'] = self.f.collection([])
        hover = self.f.named_object('CardHover')
        self.f.values[self.element, 'cardHover'] = hover
        for key, val in dict(Hovered=True, Hoverable=True, TargetImage=0, TargetCanvasGroup=0).items():
            self.f.values[hover, key] = val
        result = self.reader.read(self.board)
        item = result['hand_elements'][0]
        self.assertEqual(item['name'], 'Raigeki')
        self.assertTrue(item['hover']['hovered'])
        self.assertEqual(item['source'], 'revalidated_scene_report')
        self.assertEqual(result['report_hints']['accepted'], 1)
        self.assertIn('cursor encima', render_scene(result))

    def test_report_rejects_other_process_and_malformed_data(self):
        report = self.hint_report()
        for invalid in (dict(report, pid=456), dict(report, process_start='old'),
                        dict(report, binary_sha256='other'), {'pid': 123},
                        dict(report, scene=dict(hand_elements=[{}]))):
            with self.assertRaises(ValueError):
                self.reader.load_hints(invalid, 123, 'current', 'binary')
            self.assertEqual(self.reader.element_hints, ())

    def test_report_skips_replaced_native_object(self):
        self.reader.load_hints(self.hint_report(), 123)
        self.f.mem.put(self.element + 16, '<Q', 0x18008)
        result = self.reader.read(self.board)
        self.assertEqual(result['report_hints']['accepted'], 0)

    def test_report_skips_object_reassigned_to_different_hand_card(self):
        self.reader.load_hints(self.hint_report(), 123)
        self.f.values[self.element, 'gameCard'] = self.f.card('Raigeki')
        result = self.reader.read(self.board)
        self.assertEqual(result['hand_elements'], [])
        self.assertEqual(result['report_hints']['skipped'], 1)

    def test_discovery_prioritizes_recent_ui_hints_in_long_session(self):
        state = self.discovery_fixture()
        self.reader.element_hints = tuple((0x90000 + i * 8, self.card, 0x18000)
                                          for i in range(100))
        anchors = self.reader.discovery_anchors(state)
        self.assertEqual(anchors[0], self.reader.element_hints[-1][0])
        self.assertIn(self.card, anchors)
        self.assertNotIn(self.reader.element_hints[0][0], anchors)

    def test_just_played_card_keeps_its_ui_allocation_ahead_of_old_cache(self):
        self.reader.read(self.board)
        card = self.f.card('Pot of Greed')
        self.f.values[self.field, 'HandCards'] = self.f.collection([card])
        self.board['fields'][0]['HandCards'] = [dict(address=hex(card), name='Pot of Greed', slot=0)]
        result = self.reader.read(self.board)
        self.assertEqual(result['hand_elements'], [])
        self.reader.element_hints = tuple((0x90000 + i * 8, card, 0x18000)
                                          for i in range(100))
        anchors = self.reader.discovery_anchors(dict(status='duel', board=self.board))
        self.assertEqual(anchors[0], self.element)
        self.assertEqual(anchors[1], self.reader.element_hints[-1][0])

    def test_recent_hand_anchors_are_bounded_and_ordered_by_observation(self):
        self.reader.recent_hand_elements = (self.element,) + tuple(range(0x90000, 0x90100, 8))
        self.reader.read(self.board)
        self.assertEqual(len(self.reader.recent_hand_elements), 32)
        self.assertEqual(self.reader.recent_hand_elements[-1], self.element)
        self.assertEqual(self.reader.recent_hand_elements.count(self.element), 1)

    def test_changed_hand_does_not_record_unstable_ui_as_recent(self):
        self.f.values[self.field, 'HandCards'] = self.f.collection([])
        with self.assertRaisesRegex(ValueError, 'mano cambió'):
            self.reader.read(self.board)
        self.assertEqual(self.reader.recent_hand_elements, ())

    def test_loading_scene_report_resets_recent_hand_allocations(self):
        self.reader.read(self.board)
        self.assertTrue(self.reader.recent_hand_elements)
        self.reader.load_hints(self.hint_report(), 123)
        self.assertEqual(self.reader.recent_hand_elements, ())

    def test_report_skips_destroyed_object(self):
        self.reader.load_hints(self.hint_report(), 123)
        del self.f.types[self.element]
        result = self.reader.read(self.board)
        self.assertEqual(result['hand_elements'], [])
        self.assertEqual(result['report_hints']['skipped'], 1)

    def test_transform_failure_preserves_card_link(self):
        self.reader.transform_reader = Mock()
        self.reader.transform_reader.read.side_effect = ValueError('unverified transform')
        result = self.reader.read(self.board)
        self.assertEqual(result['hand_elements'][0]['name'], 'Raigeki')
        self.assertEqual(result['hand_elements'][0]['transform_error'], 'unverified transform')

    def test_transform_rechecks_native_component(self):
        self.reader.transform_reader = Mock()
        def replaced(native):
            self.f.mem.put(self.element + 16, '<Q', native + 8)
            return {'local_position': [1, 2, 3]}
        self.reader.transform_reader.read.side_effect = replaced
        result = self.reader.element(self.element, {self.card: self.board['fields'][0]['HandCards'][0]})
        self.assertNotIn('transform', result)
        self.assertIn('cambió', result['transform_error'])


if __name__ == '__main__':
    unittest.main()
