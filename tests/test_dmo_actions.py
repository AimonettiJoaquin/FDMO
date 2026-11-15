import unittest
from unittest.mock import patch

from tests import test_dmo_board as fixtures
from dmo_actions import ActionReader, option_values, render_actions, selected_options


class ActionTests(unittest.TestCase):
    def card_option_prompt(self):
        option = self.f.named_object('OptionHelper')
        for key, value in dict(Title='Choose a Field Spell to add to your hand', Description='Terraforming',
                MinimumRequiredOptions=0, MaximumRequiredOptions=1, OptionHelperType=dict(enum_raw=0)).items():
            self.f.values[option, key] = value
        helpers = []
        for i in range(3):
            helper = self.f.named_object('CardOptionHelper')
            for key, value in dict(gameCard=self.f.card('Gaia Power'), Index=i, PlayerID=0,
                                   CardLocation=dict(enum_raw=3)).items():
                self.f.values[helper, key] = value
            helpers.append(helper)
        self.f.values[option, 'CardOptionHelpers'] = self.f.collection(helpers)
        self.f.values[self.f.monitor.player, 'CurrentOptionHelper'] = option
        selected = self.f.named_object('List`1')
        items = self.f.array([0], 'Int32[]')
        self.f.mem.put(items + 32, '<i', 1)
        self.f.mem.put(selected + 16, '<Q', items)
        self.f.mem.put(selected + 24, '<i', 1)
        self.f.values[self.f.monitor.player, 'ClientOptions'] = selected
        return option, selected

    def test_reads_search_names_indices_and_selection_without_scanning_memory(self):
        self.reader.describe_prompts = True
        self.card_option_prompt()
        result = self.reader.read(self.manager)
        details = result['prompt_details']['CurrentOptionHelper']
        self.assertEqual([c['name'] for c in details['cards']], ['Gaia Power'] * 3)
        self.assertEqual([c['ordinal'] for c in details['cards']], [0, 1, 2])
        self.assertEqual(details['selected_indices'], [1])
        self.assertEqual(result['status'], 'observed')

    def test_search_selection_storage_type_is_checked(self):
        with self.assertRaisesRegex(ValueError, 'Int32'):
            selected_options(self.f.mem, self.options([1]))

    def test_option_prompt_changed_during_read_is_not_published(self):
        self.reader.describe_prompts = True
        option, _ = self.card_option_prompt()
        original = self.f.monitor.pointer.side_effect
        calls = [0]
        def pointer(obj, typ, key):
            value = original(obj, typ, key)
            if obj == self.f.monitor.player and key == 'CurrentOptionHelper':
                calls[0] += 1
                return option if calls[0] == 1 else 0
            return value
        self.f.monitor.pointer.side_effect = pointer
        result = self.reader.read(self.manager)
        self.assertIn('error', result['prompt_details']['CurrentOptionHelper'])

    def test_spell_mode_captures_choice_description_and_type(self):
        self.reader.describe_prompts = True
        choice = self.f.named_object('ChoiceHelper')
        self.f.values[self.f.monitor.player, 'CurrentChoiceHelper'] = choice
        values = dict(Description='Choose a spell zone', Cancelable=True, Min=1, Max=1,
                      UseConfirmations=False, ChoiceType=dict(enum_raw=3), PurposeType=dict(enum_raw=2))
        for key, value in values.items():
            self.f.values[choice, key] = value
        result = self.reader.read(self.manager)
        self.assertEqual(result['status'], 'observed')
        self.assertEqual(result['prompt_details']['CurrentChoiceHelper'], values)

    def setUp(self):
        self.f = fixtures.BoardTests()
        self.f.setUp()
        self.manager, _ = self.f.board()
        self.reader = ActionReader(self.f.monitor, {2: 'Summon', 4: 'Set', 5: 'Attack'})
        for key in ('Busy', 'SelectingCard', 'ShowChainPrompt', 'CanReact', 'DrewForTurn'):
            self.f.values[self.f.monitor.player, key] = False
        self.f.values[self.manager, 'GameStateOpen'] = True
        for key in ('CurrentChoiceHelper', 'CurrentOptionHelper', 'CurrentConfirmationHelper'):
            self.f.values[self.f.monitor.player, key] = 0
        self.helper = self.f.named_object('UsableHelper')
        for key, value in dict(CardLocation={'enum_raw': 1}, FieldID=0, ID=2,
                               CardType={'enum_raw': 1}).items():
            self.f.values[self.helper, key] = value
        self.f.values[self.helper, 'Options'] = self.options([2, 4])
        self.f.values[self.f.monitor.player, 'CurrentUsableHelpers'] = self.f.collection([self.helper])

    def options(self, values):
        pointer = self.f.collection(values)
        items = self.f.mem.u64(pointer + 16)
        klass = self.f.mem.u64(items)
        name = self.f.mem.u64(klass + 16)
        raw = b'OptionTypes[]\0'
        self.f.mem.data[name:name + len(raw)] = raw
        self.f.mem.put(items + 32, f'<{len(values)}i', *values)
        return pointer

    def test_reads_enum_ints_not_references(self):
        self.assertEqual(option_values(self.f.mem, self.options([2, 4, 5])), [2, 4, 5])

    def test_diagnostic_does_not_claim_legal_actions(self):
        result = self.reader.read(self.manager)
        self.assertEqual(result['status'], 'observed')
        self.assertFalse(result['semantics_verified'])
        self.assertFalse(result['executable'])
        self.assertEqual([o['name'] for o in result['helpers'][0]['options']], ['Summon', 'Set'])

    def test_empty_list_does_not_mean_no_legal_actions(self):
        self.f.values[self.f.monitor.player, 'CurrentUsableHelpers'] = self.f.collection([])
        result = self.reader.read(self.manager)
        self.assertEqual(result['status'], 'observed')
        self.assertIn('no implica', render_actions(result))

    def test_unknown_enum_is_partial(self):
        self.f.values[self.helper, 'Options'] = self.options([999])
        result = self.reader.read(self.manager)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['helpers'][0]['options'][0]['name'], 'UNKNOWN')

    def test_null_collection_is_unknown_not_empty(self):
        self.f.values[self.f.monitor.player, 'CurrentUsableHelpers'] = 0
        self.assertEqual(self.reader.read(self.manager)['status'], 'partial')

    def test_options_bound_and_storage_type_are_checked(self):
        with self.assertRaises(ValueError):
            option_values(self.f.mem, self.options([2] * 33))
        with self.assertRaises(ValueError):
            option_values(self.f.mem, self.f.collection([2]))

    def test_list_mutation_rejects_options(self):
        pointer = self.options([2])
        items = self.f.mem.u64(pointer + 16)
        original = self.f.mem.read
        def read(address, size):
            value = original(address, size)
            if address == items + 32:
                self.f.mem.put(pointer + 28, '<i', 99)
            return value
        with patch.object(self.f.mem, 'read', side_effect=read):
            with self.assertRaisesRegex(ValueError, 'cambiaron'):
                option_values(self.f.mem, pointer)

    def test_bad_helper_preserves_valid_neighbor(self):
        self.f.values[self.f.monitor.player, 'CurrentUsableHelpers'] = self.f.collection([0xdeadbeef, self.helper])
        result = self.reader.read(self.manager)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(len(result['helpers']), 1)


if __name__ == '__main__':
    unittest.main()
