from unittest import TestCase
from unittest.mock import Mock

from dmo.bot.dmo_card_panel import CardPanelController
from dmo.bot.dmo_loop_bot import DuelLoop
from tests import test_dmo_spell_controller as spell_fixtures
from tests.test_dmo_loop_bot import config


class CardPanelTests(TestCase):
    def test_rota_prefers_offered_assailant_even_when_another_warrior_has_higher_attack(self):
        panel, _, state, _ = self.panel(('D.D. Assailant', 'Strong', 'Medium'),
                                       'Reinforcement of the Army')
        panel.preferred_name = 'D.D. Assailant'
        panel.observe(state)
        self.assertEqual(panel.desired, {0})

    def test_rota_falls_back_to_best_offered_warrior_if_assailant_is_missing(self):
        panel, _, state, _ = self.panel(title='Reinforcement of the Army')
        panel.preferred_name = 'D.D. Assailant'
        panel.observe(state)
        self.assertEqual(panel.desired, {1})

    def rota_nine(self):
        panel, desktop, state, details = self.panel(title='Reinforcement of the Army')
        details.update(MinimumRequiredOptions=1, MaximumRequiredOptions=1)
        details['cards'] = [dict(ordinal=i, Index=i, name='Warrior', address=hex(0x10000 + i * 8),
            CardLocation=dict(enum_raw=3), ATK=1600, DEF=1000) for i in range(9)]
        details['cards'][4]['name'] = 'D.D. Assailant'
        details['cards'][4]['ATK'] = 1700
        return panel, desktop, state, details

    def test_nine_warrior_search_uses_visible_card_and_verifies_selection(self):
        panel, desktop, state, details = self.rota_nine()
        panel.preferred_name = 'D.D. Assailant'
        self.assertEqual(panel.observe(state), 'checking_card_panel')
        self.assertEqual(panel.observe(state), 'panel_card_clicked: D.D. Assailant')
        desktop.move.assert_called_with((1111, 865))
        self.assertEqual(panel.observe(state), 'waiting_panel_selection')
        details['selected_indices'] = [4]
        self.assertEqual(panel.observe(state), 'checking_panel_selection')
        self.assertEqual(panel.observe(state), 'panel_select_clicked')
        self.assertEqual(desktop.click.call_count, 3)  # Fixture, card, Select.

    def test_nine_warrior_search_does_not_click_a_clipped_preferred_card(self):
        panel, desktop, state, details = self.rota_nine()
        details['cards'][4]['name'] = 'Warrior'
        details['cards'][8]['name'] = 'D.D. Assailant'
        panel.preferred_name = 'D.D. Assailant'
        panel.observe(state)
        self.assertTrue(panel.desired <= set(range(7)))
        self.assertEqual(panel.observe(state), 'panel_card_clicked: Warrior')
        x, y = desktop.move.call_args.args[0]
        self.assertTrue(380 < x < 1540)
        self.assertEqual(y, 865)

    def test_nine_warrior_search_never_confirms_a_different_selected_card(self):
        panel, desktop, state, details = self.rota_nine()
        panel.observe(state)
        panel.observe(state)
        details['selected_indices'] = [0]
        with self.assertRaisesRegex(RuntimeError, 'distinta del plan'):
            panel.observe(state)
        self.assertFalse(panel.confirmed)
        self.assertEqual(desktop.click.call_count, 2)

    def test_valid_terraforming_panel_remains_usable_with_optional_chain_control(self):
        panel, desktop, state, details = self.panel(('Gaia Power', 'Gaia Power', 'Gaia Power'),
                                                   'Terraforming')
        state['actions']['flags']['ShowChainPrompt'] = True
        self.assertEqual(panel.observe(state), 'checking_card_panel')
        self.assertEqual(panel.observe(state), 'panel_card_clicked: Gaia Power')
        details['selected_indices'] = [1]
        self.assertEqual(panel.observe(state), 'checking_panel_selection')
        self.assertEqual(panel.observe(state), 'panel_select_clicked')
        self.assertEqual(desktop.click.call_count, 3)  # fixture, card, Select.

    def test_optional_chain_control_does_not_bypass_other_busy_or_modal_checks(self):
        for key, value in [('Busy', True), ('SelectingCard', True), ('ShowChainPrompt', None)]:
            panel, desktop, state, _ = self.panel()
            state['actions']['flags']['ShowChainPrompt'] = True
            state['actions']['flags'][key] = value
            self.assertEqual(panel.observe(state), 'waiting_card_panel')
            desktop.click.assert_called_once_with()
        for key in ('CurrentChoiceHelper', 'CurrentConfirmationHelper'):
            panel, desktop, state, _ = self.panel()
            state['actions']['flags']['ShowChainPrompt'] = True
            state['actions']['prompts'][key] = '0xother'
            self.assertEqual(panel.observe(state), 'waiting_card_panel')
            desktop.click.assert_called_once_with()

    def test_chain_control_without_readable_option_panel_never_sends_input(self):
        panel, desktop, state, details = self.panel()
        state['actions']['flags']['ShowChainPrompt'] = True
        details['cards'] = []
        self.assertEqual(panel.observe(state), 'waiting_card_panel')
        desktop.click.assert_called_once_with()

    def panel(self, names=('Weak', 'Strong', 'Medium'), title='Giant Rat'):
        _, desktop, _, state = spell_fixtures.SpellControllerTests().setup_search()
        details = state['actions']['prompt_details']['CurrentOptionHelper']
        details.update(Title='Choose a monster', Description=title)
        cards = [dict(ordinal=i, Index=i, name=name, address=hex(0x10000+i*8),
            PlayerID=0, CardLocation=dict(enum_raw=4), ATK=(1000, 1500, 1200)[i], DEF=1000)
            for i, name in enumerate(names)]
        details['cards'] = cards
        state.update(my_turn=False, phase='BATTLE')
        return CardPanelController(desktop, config()), desktop, state, details

    def test_rival_turn_revival_chooses_highest_attack_and_confirms_once(self):
        panel, desktop, state, details = self.panel(title='Spear Cretin')
        panel.observe(state)
        self.assertEqual(panel.observe(state), 'panel_card_clicked: Strong')
        details['selected_indices'] = [1]
        self.assertEqual(panel.observe(state), 'checking_panel_selection')
        self.assertEqual(panel.observe(state), 'panel_select_clicked')
        for _ in range(3):
            panel.observe(state)
        self.assertEqual(desktop.click.call_count, 3)  # includes fixture activation
        state['actions']['prompts']['CurrentOptionHelper'] = '0x0'
        self.assertEqual(panel.observe(state), 'panel_verified')
        self.assertTrue(panel.finished)

    def test_tenki_prefers_ox_among_different_offered_cards(self):
        panel, _, state, _ = self.panel(('Weak', 'Strong', 'Enraged Battle Ox'), 'Fire Formation - Tenki')
        panel.observe(state)
        self.assertEqual(panel.observe(state), 'panel_card_clicked: Enraged Battle Ox')

    def test_explicit_discard_prefers_weakest_card(self):
        panel, _, state, _ = self.panel(title='Discard a card')
        panel.observe(state)
        self.assertEqual(panel.observe(state), 'panel_card_clicked: Weak')

    def test_graceful_discards_two_while_preserving_search_and_draw_spells(self):
        panel, _, state, details = self.panel(('Weak', 'Pot of Greed', 'Medium'), 'Discard (Graceful Charity)')
        details.update(MinimumRequiredOptions=2, MaximumRequiredOptions=2)
        details['cards'][1]['ATK'] = 0
        panel.observe(state)
        self.assertEqual(panel.desired, {0, 2})
        self.assertEqual(panel.observe(state), 'panel_card_clicked: Weak')
        details['selected_indices'] = [0]
        self.assertEqual(panel.observe(state), 'panel_card_clicked: Medium')
        details['selected_indices'] = [0, 2]
        self.assertEqual(panel.observe(state), 'checking_panel_selection')
        self.assertEqual(panel.observe(state), 'panel_select_clicked')

    def test_graceful_discard_context_does_not_depend_on_panel_wording(self):
        panel, _, state, details = self.panel(('Weak', 'Pot of Greed', 'Medium'), 'Graceful Charity')
        panel.discard = True
        details.update(MinimumRequiredOptions=2, MaximumRequiredOptions=2)
        for card in details['cards']:
            card['CardLocation'] = dict(enum_raw=1)
        details['cards'][1]['ATK'] = 0
        panel.observe(state)
        self.assertEqual(panel.desired, {0, 2})
        details['cards'][0]['CardLocation'] = dict(enum_raw=3)
        self.assertIsNone(panel.parse(state))

    def graceful_eight(self):
        panel, desktop, state, details = self.panel()
        panel.discard = True
        names = ['Smashing Ground', 'Sabersaurus', 'Gaia Power', 'Enraged Battle Ox',
                 'Giant Rat', 'Terraforming', 'Fire Formation - Tenki', 'Raigeki']
        details.update(Title='Discard 2 cards. (Graceful Charity)', Description=None,
                       MinimumRequiredOptions=2, MaximumRequiredOptions=2)
        details['cards'] = [dict(ordinal=i, Index=i, name=name, address=hex(0x11000 + i * 8),
            CardLocation=dict(enum_raw=0), ATK=[0, 1900, 0, 1700, 1400, 0, 0, 0][i], DEF=0)
                           for i, name in enumerate(names)]
        own = state['board']['fields'][0]
        own['HandCards'] = [dict(name=name, address=hex(0x22000 + i * 8), location='Hand', slot=i)
                            for i, name in enumerate(names)]
        own['HandCardCount'] = 8
        return panel, desktop, state, details

    def test_graceful_eight_ui_copies_select_two_and_confirm_after_memory(self):
        panel, desktop, state, details = self.graceful_eight()
        self.assertEqual(panel.observe(state), 'checking_card_panel')
        self.assertEqual(panel.desired, {3, 4})
        self.assertEqual(panel.observe(state), 'panel_card_clicked: Enraged Battle Ox')
        desktop.move.assert_called_with((948, 865))
        self.assertEqual(panel.observe(state), 'waiting_panel_selection')
        details['selected_indices'] = [3]
        self.assertEqual(panel.observe(state), 'panel_card_clicked: Giant Rat')
        desktop.move.assert_called_with((1111, 865))
        details['selected_indices'] = [3, 4]
        self.assertEqual(panel.observe(state), 'checking_panel_selection')
        self.assertEqual(panel.observe(state), 'panel_select_clicked')
        desktop.move.assert_called_with((960, 1038))
        self.assertEqual(desktop.click.call_count, 4)  # fixture activation + 2 cards + Select.

    def test_graceful_ui_copies_require_complete_matching_live_hand(self):
        for failure in ('name', 'count', 'location', 'unknown'):
            panel, desktop, state, _ = self.graceful_eight()
            own = state['board']['fields'][0]
            if failure == 'count':
                own['HandCardCount'] = 7
            else:
                own['HandCards'][0][failure] = {'name': 'Other card', 'location': 'Deck',
                                              'unknown': True}[failure]
            self.assertIsNone(panel.parse(state))
            desktop.click.assert_called_once_with()

    def test_nine_options_still_require_scroll_calibration(self):
        panel, _, state, details = self.graceful_eight()
        details['cards'].append(dict(details['cards'][-1], ordinal=8, Index=8, address='0xff'))
        with self.assertRaisesRegex(RuntimeError, 'más de 8'):
            panel.parse(state)

    def test_eighth_option_scrolls_once_before_click_and_requires_memory_selection(self):
        panel, desktop, state, details = self.graceful_eight()
        details['cards'][7]['name'] = 'Weak card'
        details['cards'][7]['ATK'] = 0
        state['board']['fields'][0]['HandCards'][7]['name'] = 'Weak card'
        panel.observe(state)
        # The other planned discard is visible and can be selected first.
        panel.observe(state)
        details['selected_indices'] = [4]
        self.assertEqual(panel.observe(state), 'panel_scrolled_right')
        desktop.move.assert_called_with((1495, 992))
        for _ in range(2):
            self.assertEqual(panel.observe(state), 'waiting_panel_scroll')
        self.assertEqual(desktop.click.call_count, 3)  # fixture, visible card, scrollbar.
        self.assertEqual(panel.observe(state), 'panel_card_clicked: Weak card')
        desktop.move.assert_called_with((1464, 865))
        self.assertEqual(panel.observe(state), 'waiting_panel_selection')
        self.assertFalse(panel.confirmed)
        details['selected_indices'] = [4, 7]
        panel.observe(state)
        self.assertEqual(panel.observe(state), 'panel_select_clicked')
        self.assertEqual(desktop.click.call_count, 5)

    def test_required_two_cards_are_selected_before_select(self):
        panel, desktop, state, details = self.panel()
        details.update(MinimumRequiredOptions=2, MaximumRequiredOptions=2)
        panel.observe(state)
        self.assertEqual(panel.observe(state), 'panel_card_clicked: Strong')
        details['selected_indices'] = [1]
        self.assertEqual(panel.observe(state), 'panel_card_clicked: Medium')
        details['selected_indices'] = [1, 2]
        self.assertEqual(panel.observe(state), 'checking_panel_selection')
        self.assertEqual(panel.observe(state), 'panel_select_clicked')
        self.assertEqual(desktop.click.call_count, 4)

    def test_loop_intercepts_panel_on_opponent_turn_without_running_attack_actor(self):
        _, desktop, state, _ = self.panel()
        loop = DuelLoop(desktop, config())
        loop.stage, loop.actor = 'turn', Mock(finished=False)
        actor = loop.actor
        self.assertEqual(loop.observe(state), 'checking_card_panel')
        self.assertEqual(loop.observe(state), 'panel_card_clicked: Strong')
        actor.observe.assert_not_called()
        self.assertEqual(loop.stage, 'turn')

    def test_changed_panel_and_ambiguous_selection_do_not_confirm(self):
        panel, desktop, state, details = self.panel()
        panel.observe(state)
        panel.observe(state)
        # UI ordinal 1 and source Index 1 would indicate different names.
        details['cards'][0]['Index'], details['cards'][1]['Index'] = 1, 0
        with self.assertRaisesRegex(RuntimeError, 'Cambió el contenido'):
            panel.observe(state)
        self.assertFalse(panel.confirmed)

    def test_missing_selection_times_out_without_reclicking(self):
        panel, desktop, state, _ = self.panel()
        panel.observe(state)
        panel.observe(state)
        with self.assertRaisesRegex(RuntimeError, 'No se verificó'):
            for _ in range(40):
                panel.observe(state)
        self.assertEqual(desktop.click.call_count, 2)

    def test_loop_resumes_existing_actor_after_panel_closes(self):
        _, desktop, state, details = self.panel()
        loop = DuelLoop(desktop, config())
        loop.stage, loop.actor = 'turn', Mock(finished=False)
        actor = loop.actor
        loop.observe(state)
        loop.observe(state)
        details['selected_indices'] = [1]
        loop.observe(state)
        loop.observe(state)
        state['actions']['prompts']['CurrentOptionHelper'] = '0x0'
        self.assertEqual(loop.observe(state), 'panel_verified')
        self.assertIsNone(loop.card_panel)
        loop.observe(state)
        actor.observe.assert_called_once_with(state)

    def test_ambiguous_source_and_ui_indices_block_confirmation(self):
        panel, desktop, state, details = self.panel()
        details['cards'][0]['Index'], details['cards'][1]['Index'] = 1, 0
        panel.observe(state)
        panel.observe(state)
        details['selected_indices'] = [1]
        with self.assertRaisesRegex(RuntimeError, 'ambiguo'):
            panel.observe(state)
        self.assertFalse(panel.confirmed)
