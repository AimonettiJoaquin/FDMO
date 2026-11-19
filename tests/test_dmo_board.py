import struct
import unittest
from unittest.mock import Mock, patch

from dmo.memory.dmo_board import BoardReader, array_refs, list_refs, render_board, render_visual_candidates
from dmo.memory.inspect_dmo_memory import validate_class, validate_generic_class
from tests.test_monitor_dmo import FakeMemory


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.mem = FakeMemory()
        self.next = 0x1000
        self.values = {}
        self.types = {}
        self.monitor = Mock(mem=self.mem, player=0x18000)
        self.monitor.pointer.side_effect = lambda obj, typ, key: self.values[obj, key]
        self.monitor.field.side_effect = lambda obj, typ, key: self.values[obj, key]
        self.monitor.check_object.side_effect = self.check
        self.reader = BoardReader(self.monitor)

    def allocate(self, size=128):
        pointer = self.next
        self.next += size
        return pointer

    def named_object(self, name, size=128):
        obj, klass, text = self.allocate(size), self.allocate(), self.allocate()
        self.mem.put(obj, '<Q', klass)
        self.mem.put(klass + 16, '<Q', text)
        raw = name.encode() + b'\0'
        self.mem.data[text:text + len(raw)] = raw
        self.types[obj] = name
        return obj

    def check(self, obj, typ, unity=True):
        if self.types.get(obj) != typ:
            raise ValueError('wrong object type')

    def array(self, refs, name='NetworkBehaviour[]'):
        obj = self.named_object(name, max(128, 32 + len(refs) * 8))
        self.mem.put(obj + 24, '<Q', len(refs))
        for i, ref in enumerate(refs):
            self.mem.put(obj + 32 + i * 8, '<Q', ref)
        return obj

    def collection(self, refs, extra=()):
        items = self.array([*refs, *extra])
        obj = self.named_object('List`1')
        self.mem.put(obj + 16, '<Q', items)
        self.mem.put(obj + 24, '<i', len(refs))
        return obj

    def board(self):
        manager = self.named_object('DuelManager')
        mid = self.named_object('NetworkIdentity')
        owner = self.named_object('NetworkIdentity')
        fid = self.named_object('NetworkIdentity')
        field = self.named_object('DuelFieldScript')
        self.values[manager, '<netIdentity>k__BackingField'] = mid
        self.values[self.monitor.player, '<netIdentity>k__BackingField'] = owner
        self.values[manager, 'Players'] = self.collection([owner])
        self.values[manager, 'DuelFields'] = self.collection([fid])
        self.values[fid, '<NetworkBehaviours>k__BackingField'] = self.array([field])
        self.values[field, 'DuelManagerIdentity'] = mid
        self.values[field, 'Player'] = owner
        for key, value in dict(PlayerIndex=0, SideIndex=0, AlternativeLifePoints=8000,
                               DeckCount=35, ExtraDeckCount=0, HandCardCount=5, GenerationID=0).items():
            self.values[field, key] = value
        self.values[field, 'MonsterCards'] = self.array([0] * 5, 'GameCard[]')
        self.values[field, 'MagicCards'] = self.array([0] * 5, 'GameCard[]')
        self.values[field, 'HandCards'] = self.collection([])
        self.values[field, 'MonsterVisualHelpers'] = 0
        self.values[field, 'MagicVisualHelpers'] = 0
        self.values[field, 'FieldSpell'] = 0
        self.values[field, 'FieldSpellVisualHelper'] = 0
        return manager, field

    def card(self, name='Skull Servant'):
        card = self.named_object('GameCard')
        for key, value in dict(FaceDown=False, OriginalName=name, CurrentName=name,
                CustomID=42, CardLocation={'enum_raw': 2}, CurrentOwnerID=0,
                FieldSlot=0, Level=1, ATK=300, DEF=200, Attribute=5, CanAttack=True,
                AttackedThisTurn=False, TimesAttackedThisTurn=0,
                FacePosition={'enum_raw': 0}, cardFinder=0, OverrideCardfinder=0).items():
            self.values[card, key] = value
        return card

    def test_list_ignores_spare_capacity(self):
        pointer = self.collection([1, 2], extra=[0xdeadbeef])
        self.assertEqual(list_refs(self.mem, pointer), [1, 2])

    def test_field_spell_reads_separate_visual_card(self):
        _, field = self.board()
        self.assertIsNone(self.reader.field_spell(field, True)['card'])
        raw, visual = self.card(''), self.card('Gaia Power')
        helper = self.named_object('VisualHelper')
        self.values[field, 'FieldSpell'] = raw
        self.values[field, 'FieldSpellVisualHelper'] = helper
        self.values[helper, 'GameCard'] = visual
        result = self.reader.field_spell(field, True)
        self.assertEqual(result['status'], 'observed')
        self.assertEqual(result['card']['name'], 'Gaia Power')
        self.assertEqual(result['source'], 'FieldSpellVisualHelper')

    def test_field_spell_unreadable_card_is_not_empty(self):
        _, field = self.board()
        self.values[field, 'FieldSpell'] = self.card('')
        self.assertEqual(self.reader.field_spell(field, True)['status'], 'unknown')

    def test_field_spell_changed_helper_link_is_rejected(self):
        _, field = self.board()
        helper = self.named_object('VisualHelper')
        card = self.card('Gaia Power')
        self.values[field, 'FieldSpellVisualHelper'] = helper
        original = self.monitor.pointer.side_effect
        calls = []
        def pointer(obj, typ, key):
            if obj == helper and key == 'GameCard':
                calls.append(obj)
                return card if len(calls) == 1 else 0
            return original(obj, typ, key)
        self.monitor.pointer.side_effect = pointer
        with self.assertRaisesRegex(ValueError, 'cambió'):
            self.reader.field_spell(field, True)

    def test_array_bound_prevents_unbounded_read(self):
        pointer = self.array([1, 2, 3])
        with self.assertRaisesRegex(ValueError, 'límite'):
            array_refs(self.mem, pointer, limit=2)

    def test_list_mutation_is_rejected(self):
        pointer = self.collection([1, 2])
        items = self.mem.u64(pointer + 16)
        original = self.mem.read
        def changing_read(address, size):
            block = original(address, size)
            if address == items + 32:
                self.mem.put(pointer + 28, '<i', 1)
            return block
        with patch.object(self.mem, 'read', side_effect=changing_read):
            with self.assertRaisesRegex(ValueError, 'cambió'):
                list_refs(self.mem, pointer)

    def test_reads_only_fields_linked_to_manager_and_player(self):
        manager, field = self.board()
        result = self.reader.read(manager)
        self.assertEqual(result['status'], 'linked')
        self.assertTrue(result['fields'][0]['local'])
        self.assertEqual(result['fields'][0]['life_points_candidate'], 8000)
        self.assertFalse(result['fields'][0]['life_points_verified'])

    def test_card_in_local_hand_is_read_through_field(self):
        manager, field = self.board()
        card = self.named_object('GameCard')
        self.values[field, 'HandCards'] = self.collection([card])
        for key, value in dict(FaceDown=False, CurrentName='Dark Assailant',
                CardLocation={'enum_raw': 1}, CurrentOwnerID=0, CustomID=42, FieldSlot=0,
                Level=4, ATK=1200, DEF=1200, Attribute=5, CanAttack=True, AttackedThisTurn=False,
                TimesAttackedThisTurn=0, FacePosition={'enum_raw': 0}).items():
            self.values[card, key] = value
        result = self.reader.read(manager)
        hand_card = result['fields'][0]['HandCards'][0]
        self.assertEqual(hand_card['name'], 'Dark Assailant')
        self.assertEqual(hand_card['zone'], 'HandCards')
        self.assertEqual(hand_card['ATK'], 1200)
        self.assertTrue(hand_card['CanAttack'])  # raw flag, no legal-action claim

    def test_player_identity_not_in_duel_is_rejected(self):
        manager, field = self.board()
        self.values[field, 'Player'] = self.named_object('NetworkIdentity')
        result = self.reader.read(manager)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['fields'], [])
        self.assertIn('dueño', result['errors'][0]['error'])

    def test_foreign_duel_field_is_rejected(self):
        manager, field = self.board()
        self.values[field, 'DuelManagerIdentity'] = 0xdeadbeef
        result = self.reader.read(manager)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['fields'], [])
        self.assertIn('otro duelo', result['errors'][0]['error'])

    def test_unreadable_zone_is_not_reported_empty(self):
        manager, field = self.board()
        self.values[field, 'HandCards'] = 0
        result = self.reader.read(manager)
        self.assertEqual(result['status'], 'partial')
        self.assertIsNone(result['fields'][0]['HandCards'])
        self.assertIn('HandCards', result['fields'][0]['errors'])

    def test_opponent_hand_is_not_traversed(self):
        manager, field = self.board()
        opponent = self.named_object('NetworkIdentity')
        owner = self.values[self.monitor.player, '<netIdentity>k__BackingField']
        self.values[field, 'Player'] = opponent
        self.values[manager, 'Players'] = self.collection([owner, opponent])
        del self.values[field, 'HandCards']
        result = self.reader.read(manager)
        self.assertFalse(result['fields'][0]['local'])
        self.assertNotIn('HandCards', result['fields'][0])

    def test_face_down_opponent_card_does_not_require_name(self):
        card = self.named_object('GameCard')
        self.values[card, 'FaceDown'] = True
        result = self.reader.card(card, 'MonsterCards', 0, reveal=False)
        self.assertTrue(result['hidden'])
        self.assertNotIn('name', result)
        self.monitor.check_object.assert_called_with(card, 'GameCard', unity=False)

    def test_unnamed_slot_preserves_other_cards_and_slot_numbers(self):
        manager, field = self.board()
        unnamed, known = self.card(''), self.card()
        self.values[field, 'MonsterCards'] = self.array([unnamed, known, 0], 'GameCard[]')
        result = self.reader.read(manager)
        cards = result['fields'][0]['MonsterCards']
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(cards[0]['unknown'])
        self.assertEqual(cards[0]['diagnostic']['CurrentName'], '')
        self.assertEqual(cards[1]['name'], 'Skull Servant')
        self.assertEqual(cards[1]['slot'], 1)
        self.assertIsNone(cards[2])
        output = render_board(result)
        self.assertIn('1: sin lectura', output)
        self.assertIn('2: Skull Servant', output)
        self.assertIn('posición 1', result['fields'][0]['errors']['MonsterCards'])

    def test_all_unnamed_slots_are_unknown_not_empty(self):
        manager, field = self.board()
        self.values[field, 'MagicCards'] = self.array([self.card(None)], 'GameCard[]')
        result = self.reader.read(manager)
        output = render_board(result)
        self.assertEqual(result['status'], 'partial')
        self.assertIn('Magias/trampas: 1: sin lectura', output)
        self.assertNotIn('Magias/trampas: vacío', output)

    def test_bad_pointer_does_not_discard_valid_neighbor(self):
        manager, field = self.board()
        self.values[field, 'MonsterCards'] = self.array([0xdeadbeef, self.card()], 'GameCard[]')
        result = self.reader.read(manager)
        cards = result['fields'][0]['MonsterCards']
        self.assertTrue(cards[0]['unknown'])
        self.assertEqual(cards[0]['diagnostic'], {})
        self.assertEqual(cards[1]['name'], 'Skull Servant')

    def test_original_name_is_diagnostic_not_current_name_fallback(self):
        card = self.card('')
        self.values[card, 'OriginalName'] = 'Skull Servant'
        result = self.reader.card_snapshot(card, 'MonsterCards', 0)
        self.assertTrue(result['unknown'])
        self.assertNotIn('name', result)
        self.assertEqual(result['diagnostic']['OriginalName'], 'Skull Servant')

    def test_diagnostic_read_failure_keeps_unknown_slot(self):
        card = self.card('')
        def read(obj, typ, key):
            if key == 'OriginalName':
                raise OSError('unreadable string')
            return self.values[obj, key]
        self.monitor.field.side_effect = read
        result = self.reader.card_snapshot(card, 'MonsterCards', 0)
        self.assertTrue(result['unknown'])
        self.assertNotIn('OriginalName', result['diagnostic'])
        self.assertEqual(result['diagnostic']['CustomID'], 42)

    def test_snapshot_keeps_opponent_face_down_card_hidden(self):
        card = self.named_object('GameCard')
        self.values[card, 'FaceDown'] = True
        result = self.reader.card_snapshot(card, 'MonsterCards', 0, reveal=False)
        self.assertTrue(result['hidden'])
        self.assertNotIn('diagnostic', result)
        self.assertEqual(self.monitor.field.call_count, 1)

    def test_local_facedown_magic_keeps_name_when_published(self):
        card = self.card('Heavy Storm')
        self.values[card, 'FaceDown'] = True
        result = self.reader.card_snapshot(card, 'MagicVisualHelpers', 2, reveal=True)
        self.assertEqual(result['name'], 'Heavy Storm')
        self.assertEqual(result['CustomID'], 42)
        output = render_visual_candidates(dict(candidates=[dict(helper_index=2, card=result)]),
                                          'Magias/trampas')
        self.assertIn('Heavy Storm', output)
        self.assertIn('(boca abajo)', output)

    def test_local_facedown_magic_without_name_has_state_and_finder_diagnostic(self):
        card = self.card('')
        self.values[card, 'FaceDown'] = True
        finder = self.named_object('CardFinder')
        self.values[card, 'cardFinder'] = finder
        self.values[finder, 'CardType'] = 1
        self.values[finder, 'CardID'] = 123
        result = self.reader.card_snapshot(card, 'MagicVisualHelpers', 2, reveal=True)
        self.assertTrue(result['unknown'])
        self.assertTrue(result['face_down'])
        self.assertNotIn('name', result)
        self.assertEqual(result['diagnostic']['cardFinder']['CardID'], 123)
        self.assertIsNone(result['diagnostic']['OverrideCardfinder'])
        output = render_visual_candidates(dict(candidates=[dict(helper_index=2, card=result)]),
                                          'Magias/trampas')
        self.assertIn('carta boca abajo; identidad sin lectura', output)

    def test_corrupt_finder_does_not_lose_facedown_state(self):
        card = self.card('')
        self.values[card, 'FaceDown'] = True
        self.values[card, 'cardFinder'] = 0xdeadbeef
        result = self.reader.card_snapshot(card, 'MagicVisualHelpers', 0, reveal=True)
        self.assertTrue(result['face_down'])
        self.assertIn('cardFinder', result['diagnostic_errors'])

    def test_opponent_facedown_magic_needs_no_name_or_finder(self):
        card = self.named_object('GameCard')
        self.values[card, 'FaceDown'] = True
        result = self.reader.card_snapshot(card, 'MagicVisualHelpers', 0, reveal=False)
        self.assertTrue(result['hidden'])
        self.assertNotIn('diagnostic', result)
        self.monitor.pointer.assert_not_called()
        self.assertEqual(self.monitor.field.call_count, 1)

    def test_sync_list_uses_validated_runtime_offset(self):
        sync = self.named_object('SyncList`1')
        items = self.collection([10, 20])
        self.mem.put(sync + 48, '<Q', items)
        self.monitor.definitions = {'SyncList`1': {}, 'VisualHelper': {}}
        self.monitor.layout = {'classes': {'SyncList`1': {'fields': {
            'objects': dict(offset=0, type_code=21, attributes=33)}}}}
        schema = {'fields': {'objects': dict(offset=48, type_code=21, attributes=33)}}
        with patch('dmo.memory.dmo_board.validate_generic_class', return_value=schema):
            self.assertEqual(self.reader.sync_refs(sync), [10, 20])
            schema['fields']['objects']['offset'] = 0
            with self.assertRaisesRegex(ValueError, 'Layout'):
                self.reader.sync_refs(sync)

    def test_sync_list_rejects_unvalidated_class(self):
        sync = self.named_object('SyncList`1')
        self.monitor.definitions = {'SyncList`1': {}, 'VisualHelper': {}}
        with patch('dmo.memory.dmo_board.validate_generic_class', side_effect=ValueError('Clase no validada')):
            with self.assertRaisesRegex(ValueError, 'no validada'):
                self.reader.sync_refs(sync)

    def test_visual_candidate_does_not_turn_unknown_field_into_verified_board(self):
        manager, field = self.board()
        self.values[field, 'MonsterCards'] = self.array([self.card('')], 'GameCard[]')
        helper = self.named_object('VisualHelper')
        self.values[field, 'MonsterVisualHelpers'] = 0x19000
        self.values[helper, 'GameCard'] = self.card()
        with patch.object(self.reader, 'sync_refs', return_value=[helper]):
            result = self.reader.read(manager)
        f = result['fields'][0]
        diagnostic = f['visual_candidates']['MonsterCards']
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(f['MonsterCards'][0]['unknown'])
        self.assertFalse(diagnostic['verified'])
        self.assertEqual(diagnostic['candidates'][0]['card']['name'], 'Skull Servant')

    def test_visual_candidate_masks_opponent_face_down_card(self):
        _, field = self.board()
        helper = self.named_object('VisualHelper')
        card = self.named_object('GameCard')
        self.values[card, 'FaceDown'] = True
        self.values[helper, 'GameCard'] = card
        with patch.object(self.reader, 'sync_refs', return_value=[helper]):
            result = self.reader.visual_candidates(field, 'MonsterCards', local=False)
        self.assertTrue(result['candidates'][0]['card']['hidden'])
        self.assertNotIn('name', result['candidates'][0]['card'])

    def test_bad_visual_helper_preserves_neighbor_candidate(self):
        _, field = self.board()
        helper = self.named_object('VisualHelper')
        self.values[helper, 'GameCard'] = self.card()
        with patch.object(self.reader, 'sync_refs', return_value=[0xdeadbeef, helper]):
            result = self.reader.visual_candidates(field, 'MagicCards', local=True)
        self.assertIn('error', result['candidates'][0])
        self.assertEqual(result['candidates'][1]['card']['name'], 'Skull Servant')

    def test_null_visual_helper_is_recorded_without_dereferencing(self):
        _, field = self.board()
        with patch.object(self.reader, 'sync_refs', return_value=[0]):
            result = self.reader.visual_candidates(field, 'MonsterCards', local=True)
        entry = result['candidates'][0]
        self.assertFalse(entry['helper_present'])
        self.assertIsNone(entry['card'])
        self.assertNotIn('error', entry)
        self.monitor.check_object.assert_not_called()
        output = render_visual_candidates(result, 'Monstruos')
        self.assertIn('ocupación sin confirmar', output)
        self.assertNotIn('vacío', output)

    def test_readable_visual_cards_are_displayed_without_claiming_field_position(self):
        manager, field = self.board()
        self.values[field, 'MonsterCards'] = self.array([self.card('')], 'GameCard[]')
        helper = self.named_object('VisualHelper')
        self.values[helper, 'GameCard'] = self.card()
        with patch.object(self.reader, 'sync_refs', return_value=[0, helper]):
            result = self.reader.read(manager)
        output = render_board(result)
        self.assertIn('ref. 2: Skull Servant [ATK 300/DEF 200]', output)
        self.assertIn('memoria del cliente, por validar', output)
        self.assertNotIn('Carta vinculada sin nombre publicado', output)
        self.assertEqual(result['status'], 'partial')

    def test_visual_render_preserves_hidden_and_unreadable_entries(self):
        diagnostic = dict(candidates=[
            dict(helper_index=0, card={'hidden': True}),
            dict(helper_index=1, error='Referencia destruida'),
            dict(helper_index=2, card={'unknown': True, 'error': 'Nombre ilegible'})])
        output = render_visual_candidates(diagnostic, 'Monstruos')
        self.assertIn('ref. 1: carta boca abajo', output)
        self.assertIn('ref. 2: sin lectura (Referencia destruida)', output)
        self.assertIn('ref. 3: sin lectura (Nombre ilegible)', output)

    def test_visual_read_failure_is_not_displayed_as_empty(self):
        output = render_visual_candidates({'error': 'Colección cambió'}, 'Monstruos')
        self.assertIn('sin lectura (Colección cambió)', output)
        self.assertNotIn('sin cartas publicadas', output)


class GenericClassTests(unittest.TestCase):
    def setUp(self):
        self.mem = FakeMemory()
        self.base = 0x10000
        self.klass, self.generic = 0x1000, 0x2000
        self.definition = dict(definition_offset=0x100, name_offset=0x200,
                               fields=[dict(name='objects', name_offset=0x300, token=0x4000001)])
        self.argument = dict(definition_offset=0x400)
        self.mem.put(self.klass + 16, '<Q', self.base + 0x200)
        self.mem.put(self.klass + 32, '<QI', self.generic, 21 << 16)
        self.mem.put(self.klass + 96, '<Q', self.generic)
        # +104 remains null, matching GenericClass::CreateClass.
        self.mem.put(self.klass + 120, '<Q', self.klass)
        self.mem.put(self.klass + 128, '<Q', 0x3000)
        self.mem.put(self.generic, '<4Q', 0x4000, 0x5000, 0, self.klass)
        self.mem.put(0x4000, '<QI', self.base + 0x100, 18 << 16)
        self.mem.put(0x5000, '<I', 1)
        self.mem.put(0x5008, '<Q', 0x6000)
        self.mem.put(0x6000, '<Q', 0x7000)
        self.mem.put(0x7000, '<QI', self.base + 0x400, 18 << 16)
        self.mem.put(0x3000, '<QQQiI', self.base + 0x300, 0x8000, self.klass, 48, 0x4000001)
        self.mem.put(0x8000, '<QI', 0x9000, (21 << 16) | 33)

    def validate(self):
        return validate_generic_class(self.mem, self.klass, self.definition,
                                      self.argument, self.base)

    def test_null_direct_metadata_handle_is_valid_for_generic_instance(self):
        self.assertIsNone(validate_class(self.mem, self.klass, self.definition, self.base))
        self.assertEqual(self.validate()['fields']['objects']['offset'], 48)

    def test_wrong_generic_definition_is_rejected(self):
        self.mem.put(0x4000, '<Q', self.base + 0x999)
        with self.assertRaisesRegex(ValueError, 'definición: metadatos'):
            self.validate()

    def test_wrong_argument_type_is_rejected(self):
        self.mem.put(0x7000, '<Q', self.base + 0x999)
        with self.assertRaisesRegex(ValueError, 'argumento: metadatos'):
            self.validate()

    def test_corrupt_generic_links_are_rejected(self):
        cases = [(self.klass + 120, 0, 'autorreferencia'),
                 (self.klass + 32, 0, 'GENERICINST'),
                 (self.generic + 24, 0, 'cached_class'),
                 (0x5000, 2000, 'cantidad de argumentos'),
                 (self.generic + 16, 0x999, 'contexto de método')]
        for address, value, error in cases:
            with self.subTest(error=error):
                original = self.mem.u64(address)
                self.mem.put(address, '<Q', value)
                with self.assertRaisesRegex(ValueError, error):
                    self.validate()
                self.mem.put(address, '<Q', original)

    def test_generic_field_name_parent_and_token_remain_checked(self):
        for address, fmt in [(0x3000, '<Q'), (0x3010, '<Q'), (0x301c, '<I')]:
            with self.subTest(address=address):
                original = self.mem.read(address, struct.calcsize(fmt))
                self.mem.put(address, fmt, 0)
                with self.assertRaisesRegex(ValueError, 'FieldInfo'):
                    self.validate()
                self.mem.data[address:address + len(original)] = original


if __name__ == '__main__':
    unittest.main()
