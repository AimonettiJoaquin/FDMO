"""Attempt one published spell activation, then verify its effect or report a prompt."""

import json

from dmo_advisor import zone_cards, visible
from dmo_memory_bot import SummonOption, hand_signature, selection, require_recent
from dmo_turn_bot import ready
from dmo_card_panel import CardPanelController


class SpellController:
    def __init__(self, desktop, config, plan):
        self.plan = dict(plan)
        self.option = SummonOption(desktop, config, kind='spell_candidate')
        self.desktop = desktop
        self.width, self.height = config['screen']['width'], config['screen']['height']
        self.zone_choice = None
        self.zone_samples = 0
        self.zone_clicked_prompt = None
        self.zone_attempts = 0
        self.zone_retry_samples = 0
        self.max_action_attempts = config.get('memory_action_attempts', 3)
        self.monster_points = config['zones']['own_monsters']
        # Optional override for the equipment-specific confirmation control.
        # Its captured default is above the selected monster, unlike attack
        # confirmation, whose offset deselected the monster in this flow.
        self.equip_confirm_point = config.get('memory_equip_confirm_point')
        self.equip_identity = None
        self.equip_samples = 0
        self.equip_clicked = False
        self.equip_confirmed = False
        self.equip_waited = 0
        self.target_identity = None
        self.target_samples = 0
        self.target_clicked = False
        self.target_confirmed = False
        self.target_waited = 0
        self.target_confirm_point = config.get('memory_spell_target_confirm_point')
        self.rival_monster_points = config['zones'].get('memory_opponent_targets',
            config['zones'].get('opponent_monsters'))
        self.rival_monster_confirms = config['zones'].get('memory_opponent_confirms')
        self.monster_target_confirm_offset = config.get('target_confirm_offset', [0, 105])
        self.search_result_name = None
        self.monster_target_point = None
        self.monster_target_probe = 0
        self.monster_target_waited = 0
        self.monster_target_hover = 0
        self.discard_hand = None
        self.player_point = tuple(config['points']['direct_attack'])
        self.player_choice = None
        self.player_samples = 0
        self.player_clicked = False
        self.clear_source_address = None
        self.clear_spent_samples = 0
        self.card_panel = None
        self.baseline = None
        self.stage = 'activate'
        self.waited = 0
        self.pending = None
        self.pending_samples = 0
        self.finished = False

    def handle_search(self, state):
        pointer = state.get('actions', {}).get('prompts', {}).get('CurrentOptionHelper')
        if self.card_panel is None and pointer in (None, '0x0'):
            return None
        if self.card_panel is None:
            self.card_panel = CardPanelController(self.desktop,
                dict(screen=dict(width=self.width, height=self.height)),
                preferred_name=self.plan.get('search_name'),
                discard=self.plan['effect'] == 'draw_discard')
        result = self.card_panel.observe(state)
        if self.card_panel.finished:
            self.record_search_selection(self.card_panel)
            self.card_panel = None
            return None
        return result

    def record_search_selection(self, panel):
        if (self.plan['effect'] == 'search' and self.plan['card'] == 'Reinforcement of the Army' and
                panel.confirmed and panel.cards and panel.desired and len(panel.desired) == 1):
            self.search_result_name = next(c['name'] for c in panel.cards
                                          if c['ordinal'] in panel.desired)

    def handle_player(self, state):
        if self.plan['card'] != 'Raigeki' or self.plan['effect'] != 'clear' or self.zone_choice is None:
            return None
        actions = state.get('actions', {})
        flags, prompts = actions.get('flags', {}), actions.get('prompts', {})
        details = actions.get('prompt_details', {}).get('CurrentChoiceHelper', {})
        expected = dict(Description='Choose a player.', Cancelable=False, Min=0, Max=0,
            UseConfirmations=False, ChoiceType=dict(enum_raw=0), PurposeType=dict(enum_raw=0))
        pointer = prompts.get('CurrentChoiceHelper')
        if (state.get('local_player') is not True or state.get('phase') not in ('MAIN 1', 'MAIN 2') or
                actions.get('status') != 'observed' or pointer in (None, '0x0') or
                prompts.get('CurrentOptionHelper') != '0x0' or
                prompts.get('CurrentConfirmationHelper') != '0x0' or
                flags.get('GameStateOpen') is not False or
                any(flags.get(k) is not False for k in ('Busy', 'SelectingCard', 'ShowChainPrompt')) or
                any(details.get(k) != v for k, v in expected.items())):
            self.player_samples = 0
            return None
        try:
            own, rival = self.fields(state)
            hand_signature(state)
            remaining = {c['address'] for c in self.baseline['hand'] if c['address'] != self.plan['address']}
            source = own['visual_candidates']['MagicCards']['candidates'][self.zone_choice[1]].get('card')
            monsters = zone_cards(rival, 'MonsterCards')
            if (own['address'] != self.baseline['own'] or rival['address'] != self.baseline['rival'] or
                    {c['address'] for c in own['HandCards']} != remaining or
                    not source or source.get('name') != 'Raigeki' or not visible(source) or not monsters):
                return None
            identity = (pointer, rival['address'], source['address'], tuple(c['address'] for c in monsters))
        except (KeyError, TypeError, ValueError, IndexError):
            return None
        if self.player_clicked:
            self.waited += 1
            if self.waited >= 40:
                raise RuntimeError('No se verificó Raigeki después de elegir al rival; no repetir el clic')
            return 'waiting_raigeki_result'
        self.player_samples = self.player_samples + 1 if identity == self.player_choice else 1
        self.player_choice = identity
        if self.player_samples < 2:
            return 'checking_spell_player'
        self.click_equip(state, self.player_point)
        self.player_clicked, self.waited = True, 0
        self.clear_source_address = source['address']
        return 'spell_player_clicked: rival'

    def clear_spell_spent(self, state):
        """The card can resolve or be negated without clearing every monster."""
        if (self.plan['effect'] != 'clear' or self.zone_clicked_prompt is None or
                self.zone_choice is None):
            return False
        try:
            own, rival = self.fields(state)
            hand_signature(state)
            remaining = {c['address'] for c in self.baseline['hand']
                         if c['address'] != self.plan['address']}
            if (own['address'] != self.baseline['own'] or
                    rival['address'] != self.baseline['rival'] or
                    {c['address'] for c in own['HandCards']} != remaining):
                self.clear_spent_samples = 0
                return False
            cards = zone_cards(own, 'MagicCards')
            source = own['visual_candidates']['MagicCards']['candidates'][self.zone_choice[1]].get('card')
            # Raigeki can resolve automatically without a Choose a player
            # prompt. First observe its actual field copy, even while busy;
            # removal from the hand alone is never proof that it was spent.
            if (self.clear_source_address is None and source and visible(source) and
                    source.get('name') == self.plan['card']):
                self.clear_source_address = source['address']
        except (KeyError, TypeError, ValueError, IndexError):
            self.clear_spent_samples = 0
            return False
        actions = state.get('actions', {})
        flags, prompts = actions.get('flags', {}), actions.get('prompts', {})
        if (state.get('phase') not in ('MAIN 1', 'MAIN 2') or
                actions.get('status') != 'observed' or
                flags.get('GameStateOpen') is not True or
                any(flags.get(k) is not False for k in
                    ('Busy', 'SelectingCard', 'ShowChainPrompt')) or
                any(prompts.get(k) != '0x0' for k in
                    ('CurrentChoiceHelper', 'CurrentOptionHelper', 'CurrentConfirmationHelper'))):
            self.clear_spent_samples = 0
            return False
        spent = (self.clear_source_address is not None and
                 all(c['address'] != self.clear_source_address for c in cards))
        self.clear_spent_samples = self.clear_spent_samples + 1 if spent else 0
        return self.clear_spent_samples >= 2

    def equip_choice(self, state):
        if self.plan['card'] != 'Big Bang Shot' or self.plan['effect'] != 'equip':
            return None
        actions = state.get('actions', {})
        flags, prompts = actions.get('flags', {}), actions.get('prompts', {})
        pointer = prompts.get('CurrentChoiceHelper')
        details = actions.get('prompt_details', {}).get('CurrentChoiceHelper', {})
        expected = dict(Description='Select <color=#00FFFFFF>1</color> Card(s)',
            Cancelable=False, Min=1, Max=1, UseConfirmations=True,
            ChoiceType=dict(enum_raw=0), PurposeType=dict(enum_raw=0))
        if (state.get('local_player') is not True or state.get('phase') not in ('MAIN 1', 'MAIN 2') or
                actions.get('status') != 'observed' or pointer in (None, '0x0') or
                prompts.get('CurrentOptionHelper') != '0x0' or
                prompts.get('CurrentConfirmationHelper') != '0x0' or
                flags.get('GameStateOpen') is not False or
                any(flags.get(k) is not False for k in ('Busy', 'SelectingCard', 'ShowChainPrompt')) or
                any(details.get(k) != v for k, v in expected.items()) or self.zone_choice is None):
            return None
        own, _ = self.fields(state)
        hand_signature(state)
        if own['address'] != self.baseline['own']:
            return None
        remaining = {c['address'] for c in self.baseline['hand'] if c['address'] != self.plan['address']}
        if {c['address'] for c in own['HandCards']} != remaining:
            return None
        sources = own['visual_candidates']['MagicCards']['candidates']
        source = sources[self.zone_choice[1]].get('card')
        if not source or source.get('name') != self.plan['card'] or not visible(source):
            return None
        slot = self.plan['target_zone']
        if self.plan['target_side'] != 'own' or type(slot) is not int or not 0 <= slot < 5:
            return None
        target = own['visual_candidates']['MonsterCards']['candidates'][slot].get('card')
        before = next((c for c in self.baseline['own_monsters']
                       if c['address'] == self.plan['target_address']), None)
        if (not target or not before or target.get('address') != self.plan['target_address'] or
                target.get('name') != before.get('name') or not visible(target) or
                target.get('ATK') != before.get('ATK')):
            return None
        elements = state.get('scene', {}).get('field_elements', [])
        links = [e for e in elements if e.get('card_address') == target['address']]
        if (len(links) != 1 or links[0].get('world') is not True or
                links[0].get('visual_slot') != slot or not links[0].get('element') or
                links[0].get('native_element') in (None, '0x0') or
                any(e.get('selected') is True and e.get('card_address') != target['address'] for e in elements)):
            return None
        return (pointer, target['address'], source['address'], links[0]['element']), links[0], tuple(self.monster_points[slot])

    def target_choice(self, state):
        nobleman = self.plan['card'] == 'Nobleman of Crossout' and self.plan['effect'] == 'banish'
        if ((not nobleman and (self.plan['card'] != 'Mystical Space Typhoon' or
                              self.plan['effect'] != 'target')) or
                self.plan['target_side'] != 'rival' or self.zone_choice is None):
            return None
        actions = state.get('actions', {})
        flags, prompts = actions.get('flags', {}), actions.get('prompts', {})
        details = actions.get('prompt_details', {}).get('CurrentChoiceHelper', {})
        pointer = prompts.get('CurrentChoiceHelper')
        expected = dict(Description='Select <color=#00FFFFFF>1</color> Card(s)',
            Cancelable=False, Min=1, Max=1, UseConfirmations=True,
            ChoiceType=dict(enum_raw=0), PurposeType=dict(enum_raw=0))
        if (state.get('my_turn') is not True or state.get('local_player') is not True or
                state.get('phase') not in ('MAIN 1', 'MAIN 2') or
                actions.get('status') != 'observed' or pointer in (None, '0x0') or
                prompts.get('CurrentOptionHelper') != '0x0' or
                prompts.get('CurrentConfirmationHelper') != '0x0' or
                flags.get('GameStateOpen') is not False or
                any(flags.get(k) is not False for k in ('Busy', 'SelectingCard', 'ShowChainPrompt')) or
                any(details.get(k) != v for k, v in expected.items())):
            return None
        own, rival = self.fields(state)
        hand_signature(state)
        remaining = {c['address'] for c in self.baseline['hand'] if c['address'] != self.plan['address']}
        if (own['address'] != self.baseline['own'] or rival['address'] != self.baseline['rival'] or
                {c['address'] for c in own['HandCards']} != remaining):
            return None
        source = own['visual_candidates']['MagicCards']['candidates'][self.zone_choice[1]].get('card')
        slot = self.plan['target_zone']
        if type(slot) is not int or not 0 <= slot < 5:
            return None
        zone = 'MonsterCards' if nobleman else 'MagicCards'
        target = rival['visual_candidates'][zone]['candidates'][slot].get('card')
        if (not source or not visible(source) or source.get('name') != self.plan['card'] or
                not target or target.get('address') != self.plan['target_address'] or
                (nobleman and (target.get('face_down') is not True or
                               target.get('face_position_raw') not in (None, 0, 2))) or
                (not nobleman and target.get('unknown'))):
            return None
        elements = state.get('scene', {}).get('field_elements', [])
        links = [e for e in elements if e.get('card_address') == target['address']]
        if (len(links) != 1 or links[0].get('world') is not True or
                links[0].get('local') is not False or links[0].get('field_address') != rival['address'] or
                links[0].get('zone') != zone or links[0].get('visual_slot') != slot or
                not links[0].get('element') or links[0].get('native_element') in (None, '0x0') or
                any(e.get('selected') is True and e.get('card_address') != target['address'] for e in elements)):
            return None
        # Opponent visual slots run right to left, as in monster targeting.
        # The captured Call of the Mummy at visual slot 0 is at (1190, 235).
        if nobleman:
            if self.rival_monster_points is None or len(self.rival_monster_points) != 5:
                return None
            x, y = self.rival_monster_points[4 - slot]
            point = (x, y)
        else:
            point = (round((1190, 1075, 960, 845, 730)[slot] * self.width / 1920),
                     round(235 * self.height / 1080))
        return (pointer, target['address'], source['address'], links[0]['element']), links[0], point

    def aim_monster_spell(self, state, link, base):
        """Only click the exact linked monster after two fresh hover readings."""
        native = link.get('game_object_link', {})
        if (native.get('verified') is not True or
                native.get('component') != link.get('native_element') or
                native.get('game_object') in (None, '0x0')):
            raise RuntimeError('No se pudo verificar el objeto del monstruo para Nobleman')
        # Stay around the monster row. The old -70 Y offset hit the backrow.
        offsets = ((0, 0), (25, 0), (-25, 0), (0, 20), (0, -20), (25, 20), (-25, 20))
        points = [(base[0] + round(dx * self.width / 1920),
                   base[1] + round(dy * self.height / 1080)) for dx, dy in offsets]
        if self.monster_target_point is None:
            point = points[self.monster_target_probe]
            require_recent(state)
            if not 0 <= point[0] < self.width or not 0 <= point[1] < self.height:
                raise RuntimeError('Punto de Nobleman fuera de pantalla')
            actual = self.desktop.move(point)
            if self.desktop.position() != actual:
                raise RuntimeError('El cursor cambió antes de apuntar con Nobleman')
            self.monster_target_point = actual
            self.monster_target_waited = self.monster_target_hover = 0
            return 'spell_monster_target_aimed'
        self.monster_target_waited += 1
        cursor = self.desktop.position()
        if any(abs(cursor[i] - self.monster_target_point[i]) > 15 for i in (0, 1)):
            raise RuntimeError('El cursor cambió al verificar el objetivo de Nobleman')
        hovered = state.get('scene', {}).get('cursor_references', {}).get('objects', {}).get(
            'HoveredBattleObject', {})
        matches = (hovered.get('class_name') == 'GameObject' and
                   hovered.get('address') not in (None, '0x0') and
                   hovered.get('native_pointer') == native['game_object'])
        self.monster_target_hover = self.monster_target_hover + 1 if matches else 0
        if self.monster_target_hover >= 2:
            return None
        if self.monster_target_waited >= 4 and self.monster_target_hover == 0:
            self.monster_target_probe += 1
            if self.monster_target_probe >= len(points):
                raise RuntimeError('No se localizó el monstruo de Nobleman por hover verificado')
            self.monster_target_point = None
            return self.aim_monster_spell(state, link, base)
        return 'checking_spell_monster_hover'

    def handle_target(self, state):
        try:
            choice = self.target_choice(state)
        except (ValueError, KeyError, TypeError, IndexError):
            choice = None
        if choice is None:
            self.target_samples = 0
            return None
        identity, link, point = choice
        if self.target_clicked and identity != self.target_identity:
            raise RuntimeError('Cambió el objetivo rival después de elegir la mágica')
        if not self.target_clicked and identity != self.target_identity:
            self.monster_target_point = None
            self.monster_target_probe = self.monster_target_waited = self.monster_target_hover = 0
        if self.target_clicked:
            self.target_waited += 1
            if self.target_waited >= 40:
                raise RuntimeError('No se verificó el objetivo de la mágica; no repetir clics')
            if self.target_confirmed:
                return 'waiting_spell_target_result'
            if link.get('selected') is not True:
                self.target_samples = 0
                return 'waiting_spell_target_selection'
        self.target_samples = self.target_samples + 1 if identity == self.target_identity else 1
        self.target_identity = identity
        if self.target_samples < 2:
            return 'checking_spell_target'
        if not self.target_clicked:
            if link.get('selected') is not False:
                raise RuntimeError('El objetivo rival ya estaba seleccionado antes del clic')
            if self.plan['effect'] == 'banish':
                aimed = self.aim_monster_spell(state, link, point)
                if aimed is not None:
                    return aimed
                point = self.monster_target_point
            self.click_equip(state, point, aimed=self.plan['effect'] == 'banish')
            self.target_clicked, self.target_samples = True, 0
            return 'spell_target_clicked: rival'
        # Captured Robbin' Goblin at backrow slot 4: target (730,235),
        # Confirm circle (715,303). This is separate from monster/equip UI.
        confirm = (tuple(self.target_confirm_point) if self.target_confirm_point is not None else
                   (point[0] - round(15 * self.width / 1920),
                    point[1] + round(68 * self.height / 1080)))
        if self.plan['effect'] == 'banish':
            slot = self.plan['target_zone']
            if self.rival_monster_confirms is not None:
                if len(self.rival_monster_confirms) != 5:
                    raise RuntimeError('Confirmaciones de monstruos rivales sin calibrar')
                confirm = tuple(self.rival_monster_confirms[4 - slot])
            else:
                base = self.rival_monster_points[4 - slot]
                confirm = tuple(base[i] + self.monster_target_confirm_offset[i] for i in (0, 1))
        self.click_equip(state, confirm)
        self.target_confirmed = True
        return 'spell_target_confirm_clicked'

    def capture_discard_hand(self, state):
        """Record the three drawn cards before a discard panel removes any."""
        if self.plan['effect'] != 'draw_discard' or self.baseline is None:
            return
        own, _ = self.fields(state)
        signature = hand_signature(state)
        b = self.baseline
        if signature[:2] != b['signature'][:2] or own['address'] != b['own']:
            raise RuntimeError('Cambió el duelo durante el robo y descarte')
        remaining = {c['address'] for c in b['hand'] if c['address'] != self.plan['address']}
        current = {c['address'] for c in own['HandCards']}
        if (type(b['deck']) is int and own.get('DeckCount') == b['deck'] - 3 and
                len(current) == len(b['hand']) + 2 and remaining <= current and
                self.plan['address'] not in current and len(current - remaining) == 3):
            if self.discard_hand is not None and current != self.discard_hand:
                raise RuntimeError('Cambió la mano antes de elegir los descartes')
            self.discard_hand = current

    def handle_equip(self, state):
        try:
            choice = self.equip_choice(state)
        except (ValueError, KeyError, TypeError, IndexError):
            choice = None
        if choice is None:
            self.equip_samples = 0
            return None
        identity, link, point = choice
        if self.equip_clicked and identity != self.equip_identity:
            raise RuntimeError('Cambió el objetivo o prompt después de elegir el equipo')
        if self.equip_clicked:
            self.equip_waited += 1
            if self.equip_waited >= 40:
                raise RuntimeError('No se verificó el equipo; no repetir la elección ni confirmación')
        self.equip_samples = self.equip_samples + 1 if identity == self.equip_identity else 1
        self.equip_identity = identity
        if self.equip_samples < 2:
            return 'checking_equip_target'
        if not self.equip_clicked:
            if link.get('selected') is not False:
                raise RuntimeError('El objetivo de equipo ya estaba seleccionado antes del clic')
            self.click_equip(state, point)
            self.equip_clicked, self.equip_samples, self.equip_waited = True, 0, 0
            return 'equip_target_clicked'
        if self.equip_confirmed:
            return 'waiting_equip_result'
        if link.get('selected') is not True:
            self.equip_samples = 0
            return 'waiting_equip_selection'
        # Live Sabersaurus capture: monster center (960,728), Confirm circle
        # center (960,570). Keep this calibration relative to the target slot.
        confirm = (tuple(self.equip_confirm_point) if self.equip_confirm_point is not None else
                   (point[0], point[1] - round(158 * self.height / 1080)))
        self.click_equip(state, confirm)
        self.equip_confirmed, self.waited = True, 0
        return 'equip_confirm_clicked'

    def click_equip(self, state, point, *, aimed=False):
        if not 0 <= point[0] < self.width or not 0 <= point[1] < self.height:
            raise RuntimeError('El punto de equipo queda fuera de pantalla')
        require_recent(state)
        actual = point if aimed else self.desktop.move(point)
        cursor = self.desktop.position()
        if ((aimed and any(abs(cursor[i] - point[i]) > 15 for i in (0, 1))) or
                (not aimed and cursor != actual)):
            raise RuntimeError('El cursor cambió antes del clic de equipo')
        self.desktop.click()

    def activation_zone(self, state):
        """Recognize the captured Activate prompt and its own placement slot."""
        actions = state.get('actions', {})
        flags, prompts = actions.get('flags', {}), actions.get('prompts', {})
        pointer = prompts.get('CurrentChoiceHelper')
        details = actions.get('prompt_details', {}).get('CurrentChoiceHelper', {})
        if (state.get('local_player') is not True or state.get('my_turn') is not True or
                state.get('phase') not in ('MAIN 1', 'MAIN 2') or
                actions.get('status') != 'observed' or not pointer or pointer == '0x0' or
                prompts.get('CurrentOptionHelper') != '0x0' or
                prompts.get('CurrentConfirmationHelper') != '0x0' or
                flags.get('GameStateOpen') is not False or
                any(flags.get(k) is not False for k in ('Busy', 'SelectingCard', 'ShowChainPrompt')) or
                details.get('Description') != 'Activate\n<color=#00FFFFFF>' + self.plan['card'] or
                details.get('Cancelable') is not True or details.get('Min') != 0 or details.get('Max') != 0 or
                details.get('UseConfirmations') is not False or
                details.get('ChoiceType') != dict(enum_raw=0) or
                details.get('PurposeType') != dict(enum_raw=0)):
            return None
        own, _ = self.fields(state)
        if hand_signature(state) != self.baseline['signature']:
            return None
        if self.plan['effect'] == 'field':
            slot = own.get('field_spell', {})
            if (self.plan['card'] != 'Gaia Power' or slot.get('status') != 'observed' or
                    'card' not in slot or slot['card'] is not None):
                return None
            # Captured Gaia activation view: the single highlighted field
            # spell zone at the left, separate from the five backrow slots.
            point = (round(416 * self.width / 1920), round(707 * self.height / 1080))
            return pointer, 'FieldSpell', point, ('FieldSpell',)
        zones = own.get('MagicCards')
        visual = own.get('visual_candidates', {}).get('MagicCards', {})
        candidates = visual.get('candidates', [])
        if (not isinstance(zones, list) or len(zones) != 5 or
                visual.get('source') != 'MagicVisualHelpers' or len(candidates) != 5):
            return None
        free = []
        for index, (zone, candidate) in enumerate(zip(zones, candidates)):
            if candidate.get('helper_index') != index:
                return None
            # Occupied visual helpers publish an address/card but can omit
            # helper_present. Only explicitly verified vacancies are clickable.
            if (candidate.get('helper_address') not in (None, '0x0') and
                    candidate.get('card') and candidate['card'].get('location') == 'Field' and
                    candidate.get('helper_present') is not False):
                continue
            diagnostic = zone.get('diagnostic', {}) if zone else {}
            if (candidate.get('helper_present') is not False or candidate.get('helper_address') != '0x0' or
                    candidate.get('card') is not None or not zone or not zone.get('unknown') or
                    diagnostic.get('CurrentName') != '' or
                    diagnostic.get('CardLocation') != dict(enum_raw=0) or
                    any(not isinstance(diagnostic.get(k), dict) or diagnostic[k].get('CardID') != 0
                        for k in ('cardFinder', 'OverrideCardfinder'))):
                return None
            free.append(index)
        if not free:
            return None
        index = min(free, key=lambda i: (abs(i - 2), i))
        # Centers inside the highlighted backrow in the captured Pot of Greed
        # placement view; y=855 avoids the overlapping hand at the bottom.
        point = (round((565, 760, 960, 1160, 1365)[index] * self.width / 1920),
                 round(855 * self.height / 1080))
        return pointer, index, point, tuple(free)

    def fields(self, state):
        fields = state.get('board', {}).get('fields', [])
        own = [f for f in fields if f.get('local') is True]
        rival = [f for f in fields if f.get('local') is False]
        if len(own) != 1 or len(rival) != 1 or state.get('board', {}).get('errors'):
            raise ValueError('No se pudieron identificar los campos durante la mágica')
        return own[0], rival[0]

    def record(self, state):
        own, rival = self.fields(state)
        signature = hand_signature(state)
        address = self.plan['address']
        if sum(c['address'] == address and c['name'] == self.plan['card']
               for c in own['HandCards']) != 1:
            raise RuntimeError('Cambió la copia de la mágica antes de activarla')
        self.baseline = dict(signature=signature, own=own['address'], rival=rival['address'],
            deck=own.get('DeckCount'), hand=list(own['HandCards']),
            monsters=zone_cards(rival, 'MonsterCards'), own_monsters=zone_cards(own, 'MonsterCards'),
            backrow=zone_cards(rival, 'MagicCards'), rival_lp=rival.get('life_points_candidate'))

    def resolved(self, state):
        if not ready(state, state.get('phase')) or state.get('phase') not in ('MAIN 1', 'MAIN 2'):
            return False
        own, rival = self.fields(state)
        b = self.baseline
        if ((state['player'], state['manager']) != b['signature'][:2] or
                own['address'] != b['own'] or rival['address'] != b['rival']):
            raise RuntimeError('Cambió el duelo durante la resolución de la mágica')
        hand_signature(state)
        hand = own['HandCards']
        after = {c['address'] for c in hand}
        if self.plan['address'] in after:
            return False
        remaining = {c['address'] for c in b['hand'] if c['address'] != self.plan['address']}
        if self.plan['effect'] != 'draw_discard' and not remaining <= after:
            return False
        name, effect = self.plan['card'], self.plan['effect']
        if effect == 'draw':
            count = 2 if name == 'Pot of Greed' else 1
            return (type(b['deck']) is int and own.get('DeckCount') == b['deck'] - count and
                    len(hand) == len(b['hand']) - 1 + count and len(after - remaining) == count and
                    (name != 'Upstart Goblin' or type(b['rival_lp']) is int and
                     rival.get('life_points_candidate') == b['rival_lp'] + 1000))
        if effect == 'search':
            target = self.search_result_name or self.plan['search_name']
            return (len(hand) == len(b['hand']) and type(b['deck']) is int and
                    own.get('DeckCount') == b['deck'] - 1 and
                    len(after - remaining) == 1 and
                    (target is None or sum(c['name'] == target for c in hand) ==
                     sum(c['name'] == target for c in b['hand']) + 1))
        if effect == 'draw_discard':
            return (self.discard_hand is not None and type(b['deck']) is int and
                    own.get('DeckCount') == b['deck'] - 3 and len(hand) == len(b['hand']) and
                    after <= self.discard_hand and len(self.discard_hand - after) == 2)
        if effect == 'field':
            field_spell = own.get('field_spell', {})
            card = field_spell.get('card')
            return (len(hand) == len(b['hand']) - 1 and
                    field_spell.get('status') == 'observed' and card is not None and
                    visible(card) and card.get('location') == 'Field' and card.get('name') == name)
        theirs = zone_cards(rival, 'MonsterCards')
        if effect == 'clear':
            return bool(b['monsters']) and not theirs
        if effect == 'destroy':
            before = {c['address'] for c in b['monsters'] if visible(c)}
            return bool(before - {c['address'] for c in theirs})
        if effect == 'target':
            return self.plan['target_address'] not in {c['address'] for c in zone_cards(rival, 'MagicCards')}
        if effect == 'banish':
            return (self.target_confirmed and
                    any(c['address'] == self.plan['target_address'] and c.get('face_down') is True
                        for c in b['monsters']) and
                    self.plan['target_address'] not in {c['address'] for c in theirs})
        if effect == 'equip':
            address = self.plan['target_address']
            before = next((c for c in b['own_monsters'] if c['address'] == address), None)
            target = next((c for c in zone_cards(own, 'MonsterCards') if c['address'] == address), None)
            return (before is not None and target is not None and type(before.get('ATK')) is int and
                    target.get('ATK') == before['ATK'] + 400 and
                    any(c.get('name') == name and visible(c) for c in zone_cards(own, 'MagicCards')))
        return False

    def observe(self, state):
        if self.finished:
            return None
        if self.stage == 'activate':
            if self.baseline is None:
                self.record(state)
            candidate = selection(state, kind='spell_candidate')
            if candidate is not None and (candidate['card']['address'] != self.plan['address'] or
                    candidate['signature'] != self.baseline['signature']):
                raise RuntimeError('Cambió la mágica o la mano antes del clic de activación')
            result = self.option.observe(state)
            if self.option.clicked:
                self.stage, self.waited = 'resolve', 0
            return result
        if (state.get('status') != 'duel' or state.get('my_turn') is not True or
                state.get('manager') != self.baseline['signature'][1]):
            raise RuntimeError('El duelo o turno cambió antes de verificar la mágica')
        try:
            self.capture_discard_hand(state)
        except (ValueError, KeyError, TypeError):
            pass
        try:
            if self.resolved(state):
                self.finished = True
                return f'spell_verified: {self.plan["card"]}'
        except (ValueError, KeyError, TypeError):
            pass  # A changing board is not proof of success.
        if self.clear_spell_spent(state):
            self.finished = True
            return f'spell_consumed_without_clear: {self.plan["card"]}'
        search = self.handle_search(state)
        if search is not None:
            return search
        player = self.handle_player(state)
        if player is not None:
            return player
        equip = self.handle_equip(state)
        if equip is not None:
            return equip
        target = self.handle_target(state)
        if target is not None:
            return target
        if self.zone_clicked_prompt is None:
            try:
                zone = self.activation_zone(state)
            except (ValueError, KeyError, TypeError):
                zone = None
            if zone is not None:
                self.zone_samples = self.zone_samples + 1 if zone == self.zone_choice else 1
                self.zone_choice = zone
                if self.zone_samples < 2:
                    return 'checking_spell_zone'
                require_recent(state)
                actual = self.desktop.move(zone[2])
                if self.desktop.position() != actual:
                    raise RuntimeError('El cursor cambió antes del clic en la zona de mágica')
                self.desktop.click()
                self.zone_clicked_prompt = zone[0]
                self.zone_attempts = 1
                self.waited = 0
                self.pending, self.pending_samples = None, 0
                return ('field_spell_zone_clicked' if self.plan['effect'] == 'field'
                        else 'spell_zone_clicked')
            self.zone_choice, self.zone_samples = None, 0
        elif self.zone_attempts < self.max_action_attempts:
            try:
                retry_zone = self.activation_zone(state)
            except (ValueError, KeyError, TypeError):
                retry_zone = None
            # A spell still in the identical complete hand, with the same
            # placement prompt and vacant zone, has not been placed yet.
            if retry_zone == self.zone_choice:
                self.zone_retry_samples += 1
                if self.zone_retry_samples >= 8:
                    require_recent(state)
                    actual = self.desktop.move(retry_zone[2])
                    if self.desktop.position() != actual:
                        raise RuntimeError('El cursor cambió antes del reintento de zona')
                    self.desktop.click()
                    self.zone_attempts += 1
                    self.zone_retry_samples = self.waited = 0
                    return f'spell_zone_retry: {self.zone_attempts}/{self.max_action_attempts}'
            else:
                self.zone_retry_samples = 0
        prompts = state.get('actions', {}).get('prompts', {})
        active = tuple((k, prompts.get(k)) for k in
                       ('CurrentChoiceHelper', 'CurrentOptionHelper', 'CurrentConfirmationHelper')
                       if prompts.get(k) not in (None, '0x0'))
        # A click is not proof of placement. Wait for the result while the
        # same placement prompt persists, without sending another click.
        details = state.get('actions', {}).get('prompt_details', {}).get('CurrentChoiceHelper', {})
        if (active == (('CurrentChoiceHelper', self.zone_clicked_prompt),) and
                details.get('Description') == 'Activate\n<color=#00FFFFFF>' + self.plan['card']):
            active = ()
        if active:
            self.pending_samples = self.pending_samples + 1 if active == self.pending else 1
            self.pending = active
            if self.pending_samples >= 16:
                details = state.get('actions', {}).get('prompt_details', {})
                raise RuntimeError(f'Mágica {self.plan["card"]}: prompt pendiente de implementar '
                                   f'{active}. Detalles: {json.dumps(details, ensure_ascii=False)}')
        else:
            self.pending = None
            self.pending_samples = 0
        self.waited += 1
        if self.waited >= 40:
            raise RuntimeError(f'No se verificó el efecto de {self.plan["card"]}; no repetir la activación')
        return None
