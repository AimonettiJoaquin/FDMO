#!/usr/bin/env python3
"""Advance one own turn using read-only game state and bounded UI actions.

The old visual bot is unaffected. Run as the desktop user with monitor JSONL
on stdin. Every click is followed by a memory-observed transition; uncertain
states stop the script instead of probing another position.
"""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

from dmo.strategy.dmo_battle import battle_decision
from dmo.bot.dmo_memory_bot import Desktop, ROOT, require_recent


def ready(state, phase):
    actions = state.get('actions', {})
    flags, prompts = actions.get('flags', {}), actions.get('prompts', {})
    return (state.get('status') == 'duel' and state.get('local_player') is True and
            state.get('my_turn') is True and state.get('phase') == phase and
            actions.get('status') == 'observed' and flags.get('GameStateOpen') is True and
            all(flags.get(k) is False for k in ('Busy', 'SelectingCard', 'ShowChainPrompt')) and
            all(prompts.get(k) == '0x0' for k in
                ('CurrentChoiceHelper', 'CurrentOptionHelper', 'CurrentConfirmationHelper')))


def attack_available(state, field_id, field_slot):
    actions = state.get('actions', {})
    return any(h.get('ID') == field_slot and h.get('FieldID') == field_id and
               h.get('CardLocation', {}).get('enum_raw') == 2 and
               h.get('CardType', {}).get('enum_raw') == 1 and
               any(o.get('name') == 'Attack' and o.get('value') == 5
                   for o in h.get('options', [])) for h in actions.get('helpers', []))


class TurnController:
    def __init__(self, desktop, config, *, resume_direct=None, resume_target=None,
                 end_turn=False):
        self.desktop, self.config = desktop, config
        self.resume_direct = resume_direct
        self.resume_target = resume_target
        self.end_turn_only = end_turn
        self.stage = 'initial'
        self.identity = None
        self.stable = self.waited = self.attacks = 0
        self.last = None
        self.plan = None
        self.finished = False
        self.used_attackers = set()
        self.target_choice = None
        self.target_choice_confirmations = 0
        self.target_hover = None
        self.target_hover_confirmations = 0
        self.attack_baseline = None
        self.target_selection_stable = 0
        self.target_attempts = 0
        self.max_target_attempts = config.get('memory_action_attempts', 3)
        self.failed_targets = set()
        self.target_probe_index = 0
        self.target_point = None
        self.target_drift_attempts = 0
        self.end_origin_phase = None
        self.direct_probe_used = False
        self.direct_probe_previous = None

    def point(self, key):
        point = tuple(self.config['points'][key])
        self.check_point(point)
        return point

    def enemy_point(self):
        # Rival helper indices follow the rival's perspective. Calibrated screen
        # points are left-to-right: helper 4 is the leftmost enemy monster.
        zones = self.config['zones'].get('memory_opponent_targets',
                                         self.config['zones']['opponent_monsters'])
        slot = self.plan['target_zone']
        if len(zones) != 5 or type(slot) is not int or not 0 <= slot < 5:
            raise RuntimeError('Zona del objetivo enemigo inválida')
        point = tuple(zones[4 - slot])
        self.check_point(point)
        return point

    def calibrated_target(self):
        return 'memory_opponent_targets' in self.config['zones']

    def target_points(self):
        """Stay on the monster row; the old -70 Y probes hit the backrow."""
        zones = [tuple(p) for p in self.config['zones'].get(
            'memory_opponent_targets', self.config['zones']['opponent_monsters'])]
        preferred = self.enemy_point()
        offsets = ((0, 0), (25, 0), (-25, 0), (0, 20), (0, -20),
                   (25, 20), (-25, 20))
        def around(point):
            return [(point[0] + round(dx * self.config['screen']['width'] / 1920),
                     point[1] + round(dy * self.config['screen']['height'] / 1080))
                    for dx, dy in offsets]
        points = around(preferred)
        # Other slots remain hover-only probes: only the exact target's
        # verified GameObject can authorize a click at one of these points.
        points += zones
        return list(dict.fromkeys(p for p in points
                                  if 0 <= p[0] < self.config['screen']['width'] and
                                  0 <= p[1] < self.config['screen']['height']))

    def aim_target(self, state, *, next_point=False):
        if next_point:
            self.target_probe_index += 1
        points = self.target_points()
        if self.target_probe_index >= len(points):
            raise RuntimeError('No se localizó el objetivo enemigo por hover verificado')
        point = points[self.target_probe_index]
        require_recent(state)
        actual = self.desktop.move(point)
        if self.desktop.position() != actual:
            raise RuntimeError('El cursor cambió antes de apuntar al objetivo')
        self.target_point = actual
        self.stage, self.waited = 'aim_target', 0
        self.target_hover = None
        self.target_hover_confirmations = self.target_selection_stable = 0

    def target_native_pointer(self, state):
        links = [e for e in state.get('scene', {}).get('field_elements', [])
                 if e.get('card_address') == self.plan['target'] and
                 e.get('local') is False and e.get('zone') == 'MonsterCards' and
                 e.get('visual_slot') == self.plan['target_zone'] and
                 e.get('world') is True]
        if len(links) != 1:
            return None
        try:
            return int(links[0]['native_element'], 16)
        except (KeyError, TypeError, ValueError):
            return None

    def target_game_object(self, state):
        links = [e for e in state.get('scene', {}).get('field_elements', [])
                 if e.get('card_address') == self.plan['target'] and
                 e.get('local') is False and e.get('zone') == 'MonsterCards' and
                 e.get('visual_slot') == self.plan['target_zone'] and
                 e.get('world') is True]
        if len(links) != 1:
            return None
        link = links[0].get('game_object_link', {})
        if link.get('verified') is not True or link.get('component') != links[0].get('native_element'):
            return None
        try:
            value = int(link['game_object'], 16)
            return value if value > 0 else None
        except (KeyError, TypeError, ValueError):
            return None

    def check_point(self, point):
        if (len(point) != 2 or not all(type(v) is int for v in point) or
                not 0 <= point[0] < self.config['screen']['width'] or
                not 0 <= point[1] < self.config['screen']['height']):
            raise RuntimeError(f'Punto fuera de pantalla: {point}')

    def click(self, state, point):
        self.check_point(point)
        require_recent(state)
        actual = self.desktop.move(point)
        if self.desktop.position() != actual:
            raise RuntimeError('El cursor cambió antes del clic')
        self.desktop.click()

    def click_aimed_target(self, state):
        """Preserve the mouse position whose hover Unity already processed."""
        require_recent(state)
        point = self.target_point or self.enemy_point()
        self.check_point(point)
        cursor = self.desktop.position()
        if any(abs(cursor[i] - point[i]) > 15 for i in (0, 1)):
            raise RuntimeError('El cursor cambió antes del clic en el objetivo enemigo')
        # Desktop.move emits a real motion event even at the same point.
        # Moving again here invalidated the two observed hover samples.
        self.desktop.click()

    def begin(self, state):
        identity = (state['player'], state['manager'], state['own_id'])
        if self.identity is None:
            self.identity = identity
        elif self.identity != identity:
            raise RuntimeError('Cambió el duelo o jugador durante el turno')

    def wait(self, limit, reason):
        self.waited += 1
        if self.waited >= limit:
            raise RuntimeError(reason)
        return None

    def end(self, state, *, menu_open=False):
        if not menu_open:
            require_recent(state)
            self.desktop.key('tab')
        self.click(state, self.point('phase_end'))
        self.end_origin_phase = state.get('phase')
        self.stage, self.waited = 'await_end', 0
        return 'end_clicked'

    def record_attack_baseline(self, state):
        fields = state.get('board', {}).get('fields', [])
        own = [f for f in fields if f.get('local') is True]
        rival = [f for f in fields if f.get('local') is False]
        if len(own) != 1 or len(rival) != 1:
            raise RuntimeError('No se pudieron identificar ambos campos antes del ataque')
        own_lp = own[0].get('life_points_candidate')
        rival_lp = rival[0].get('life_points_candidate')
        if (type(own_lp) is not int or type(rival_lp) is not int or
                not 0 <= own_lp <= 99999 or not 0 <= rival_lp <= 99999):
            raise RuntimeError('Puntos de vida previos al ataque inválidos')
        target_before = None
        if self.plan['kind'] == 'attack':
            entries = rival[0].get('visual_candidates', {}).get('MonsterCards', {}).get('candidates', [])
            if len(entries) != 5:
                raise RuntimeError('No se pudo leer la zona objetivo antes del ataque')
            card = entries[self.plan['target_zone']].get('card')
            if not card or card.get('address') != self.plan['target']:
                raise RuntimeError('El objetivo cambió antes del clic')
            target_before = (card.get('address'), card.get('face_down'),
                             card.get('face_position_raw'), card.get('name'))
        self.attack_baseline = (state['manager'], own[0]['address'], rival[0]['address'],
                                own_lp, rival_lp, target_before)

    def confirmed_target_hover(self, state):
        if self.plan['kind'] != 'attack':
            return False
        actions = state.get('actions', {})
        prompts, flags = actions.get('prompts', {}), actions.get('flags', {})
        if (state.get('phase') != 'BATTLE' or state.get('my_turn') is not True or
                actions.get('status') != 'observed' or
                any(flags.get(k) is not False for k in ('Busy', 'SelectingCard', 'ShowChainPrompt')) or
                prompts.get('CurrentChoiceHelper') != self.target_choice or
                prompts.get('CurrentOptionHelper') != '0x0' or
                prompts.get('CurrentConfirmationHelper') != '0x0'):
            self.target_hover = None
            self.target_hover_confirmations = 0
            return False
        target = self.target_point or self.enemy_point()
        cursor = self.desktop.position()
        if abs(cursor[0] - target[0]) > 15 or abs(cursor[1] - target[1]) > 15:
            self.target_hover = None
            self.target_hover_confirmations = 0
            return False
        hovered = (state.get('scene', {}).get('cursor_references', {})
                   .get('objects', {}).get('HoveredBattleObject', {}))
        address = hovered.get('address')
        if (not address or address == '0x0' or
                hovered.get('class_name') != 'GameObject'):
            self.target_hover = None
            self.target_hover_confirmations = 0
            return False
        native = self.target_native_pointer(state)
        if native is not None:
            try:
                expected = self.target_game_object(state)
                matches = expected is not None and int(hovered.get('native_pointer'), 16) == expected
            except (TypeError, ValueError):
                matches = False
            if not matches:
                self.target_hover = None
                self.target_hover_confirmations = 0
                return False
        self.target_hover_confirmations = (
            self.target_hover_confirmations + 1 if address == self.target_hover else 1)
        self.target_hover = address
        return self.target_hover_confirmations >= 2

    def confirm_target(self, state):
        confirms = self.config['zones'].get('memory_opponent_confirms')
        if confirms is not None:
            slot = self.plan['target_zone']
            if len(confirms) != 5 or type(slot) is not int or not 0 <= slot < 5:
                raise RuntimeError('Confirmaciones enemigas sin calibrar')
            point = tuple(confirms[4 - slot])
        else:
            target = self.target_point or self.enemy_point()
            offset = self.config['target_confirm_offset']
            point = (target[0] + offset[0], target[1] + offset[1])
        self.click(state, point)
        self.stage, self.waited = 'await_attack', 0
        return 'attack_confirm_clicked'

    def selected_target(self, state):
        """Hover alone does not prove that the target's Confirm control exists."""
        actions = state.get('actions', {})
        prompts, flags = actions.get('prompts', {}), actions.get('flags', {})
        if (state.get('phase') != 'BATTLE' or state.get('my_turn') is not True or
                actions.get('status') != 'observed' or
                prompts.get('CurrentChoiceHelper') != self.target_choice or
                prompts.get('CurrentOptionHelper') != '0x0' or
                any(flags.get(k) is not False for k in ('Busy', 'SelectingCard', 'ShowChainPrompt'))):
            self.target_selection_stable = 0
            return False
        links = state.get('scene', {}).get('field_elements', [])
        matching = [e for e in links if e.get('card_address') == self.plan['target'] and
                    e.get('local') is False and e.get('zone') == 'MonsterCards' and
                    e.get('visual_slot') == self.plan['target_zone'] and
                    e.get('world') is True]
        selected = (len(matching) == 1 and matching[0].get('selected') is True and
                    not any(e.get('selected') is True and
                            e.get('card_address') != self.plan['target'] for e in links))
        self.target_selection_stable = self.target_selection_stable + 1 if selected else 0
        return self.target_selection_stable >= 2

    def retry_target(self, state):
        """Retry only an unchanged target prompt with no selected card."""
        actions = state.get('actions', {})
        prompts, flags = actions.get('prompts', {}), actions.get('flags', {})
        choice = actions.get('prompt_details', {}).get('CurrentChoiceHelper', {})
        if (self.plan['kind'] != 'attack' or state.get('phase') != 'BATTLE' or
                actions.get('status') != 'observed' or flags.get('GameStateOpen') is not True or
                prompts.get('CurrentChoiceHelper') != self.target_choice or
                prompts.get('CurrentOptionHelper') != '0x0' or
                prompts.get('CurrentConfirmationHelper') != '0x0' or
                choice.get('Description') != 'Choose an attack target.' or
                choice.get('Cancelable') is not True or choice.get('UseConfirmations') is not True or
                choice.get('Min') != 0 or choice.get('Max') != 0 or
                any(flags.get(k) is not False for k in ('Busy', 'SelectingCard', 'ShowChainPrompt')) or
                any(e.get('selected') is True for e in state.get('scene', {}).get('field_elements', []))):
            return None
        self.record_attack_baseline(state)
        own = next(f for f in state['board']['fields'] if f.get('local') is True)
        attackers = [e['card'] for e in own['visual_candidates']['MonsterCards']['candidates']
                     if e.get('card') and e['card'].get('address') == self.plan['attacker']]
        if (len(attackers) != 1 or attackers[0].get('AttackedThisTurn') is not False or
                attackers[0].get('CanAttack') is not True or
                not attack_available(state, own.get('player_index'), attackers[0].get('FieldSlot'))):
            return None
        # Visual helpers can still report a defender that cannot be selected.
        # Let the open attack chooser decide whether a direct attack is legal
        # before scanning card coordinates. Never infer an empty field from a
        # failed click, and keep the original plan if this probe is ignored.
        if (self.plan.get('intent') != 'banish_trade' and not self.direct_probe_used and
                type(attackers[0].get('ATK')) is int and
                attackers[0]['ATK'] > 0):
            self.direct_probe_used = True
            self.direct_probe_previous = (self.plan, self.attack_baseline)
            self.plan = dict(kind='direct', attacker=self.plan['attacker'],
                             attacker_name=self.plan['attacker_name'],
                             attacker_zone=self.plan['attacker_zone'],
                             expected_damage=attackers[0]['ATK'])
            self.record_attack_baseline(state)
            self.click(state, self.point('direct_attack'))
            self.stage, self.waited = 'await_direct_probe', 0
            return 'direct_attack_probe_clicked'
        result = 'target_retry_aimed'
        if self.target_attempts >= self.max_target_attempts:
            failed = self.failed_targets | {self.plan['target']}
            candidate = deepcopy(state)
            candidate['actions']['prompts']['CurrentChoiceHelper'] = '0x0'
            own = next(f for f in candidate['board']['fields'] if f.get('local') is True)
            others = {e['card']['address'] for e in own['visual_candidates']['MonsterCards']['candidates']
                      if e.get('card') and e['card']['address'] != self.plan['attacker']}
            try:
                alternate = battle_decision(candidate, others, excluded_targets=failed)
            except (KeyError, TypeError, ValueError):
                return None
            # Excluding a failed target from ranking never removes it from
            # the board or authorizes a direct attack.
            if alternate['kind'] != 'attack' or alternate['attacker'] != self.plan['attacker']:
                return None
            self.failed_targets, self.plan = failed, alternate
            self.target_attempts = 0
            self.target_probe_index = 0
            self.target_point = None
            self.record_attack_baseline(state)
            result = 'target_changed: ' + self.plan['target_name']
        self.aim_target(state, next_point=(result == 'target_retry_aimed' and
                                           self.target_native_pointer(state) is not None))
        return result

    def observe(self, state):
        if self.finished:
            return None
        if self.identity is not None and state.get('status') != 'duel':
            self.finished = True
            return 'duel_left'
        if self.identity is not None and state.get('my_turn') is False:
            self.finished = True
            return 'turn_passed'
        if state.get('status') != 'duel' or state.get('my_turn') is not True:
            return self.wait(100, 'No llegó un turno propio')
        self.begin(state)
        if self.stage == 'initial':
            if self.resume_target:
                actions = state.get('actions', {})
                prompts = actions.get('prompts', {})
                choice = prompts.get('CurrentChoiceHelper')
                if (state.get('phase') != 'BATTLE' or actions.get('status') != 'observed' or
                        choice in (None, '0x0') or
                        prompts.get('CurrentOptionHelper') != '0x0' or
                        prompts.get('CurrentConfirmationHelper') != '0x0'):
                    return self.wait(60, 'No apareció el objetivo enemigo pendiente')
                candidate = deepcopy(state)
                candidate['actions']['prompts']['CurrentChoiceHelper'] = '0x0'
                try:
                    plan = battle_decision(candidate)
                except (KeyError, TypeError, ValueError):
                    return self.wait(60, 'No se pudo validar el objetivo enemigo pendiente')
                if plan['kind'] != 'attack' or plan['target_name'] != self.resume_target:
                    raise RuntimeError('El aviso no coincide con el objetivo indicado')
                if self.plan is None:
                    self.plan = plan
                    self.target_choice = choice
                elif self.plan != plan or self.target_choice != choice:
                    raise RuntimeError('Cambió el objetivo mientras se esperaba confirmación')
                if self.selected_target(state):
                    self.record_attack_baseline(state)
                    return self.confirm_target(state)
                return self.wait(20, 'No se verificó el cursor sobre el objetivo')
            if self.end_turn_only:
                phase = state.get('phase')
                if phase not in ('MAIN 1', 'MAIN 2', 'BATTLE') or not ready(state, phase):
                    return self.wait(100, 'El turno propio no volvió a un estado libre')
                current = (phase, state['player'], state['manager'], state['own_id'])
                self.stable = self.stable + 1 if current == self.last else 1
                self.last = current
                if self.stable < 2:
                    return None
                return self.end(state)
            if self.resume_direct:
                actions = state.get('actions', {})
                prompts = actions.get('prompts', {})
                choice = prompts.get('CurrentChoiceHelper')
                if (state.get('phase') != 'BATTLE' or
                        actions.get('status') != 'observed' or
                        choice in (None, '0x0') or
                        prompts.get('CurrentOptionHelper') != '0x0' or
                        prompts.get('CurrentConfirmationHelper') != '0x0'):
                    return self.wait(60, 'No apareció el ataque directo pendiente')
                own = [f for f in state.get('board', {}).get('fields', []) if f.get('local') is True]
                if len(own) != 1:
                    return self.wait(60, 'No hay un campo propio único')
                occupied = [e['card'] for e in own[0].get('visual_candidates', {})
                            .get('MonsterCards', {}).get('candidates', []) if e.get('card')]
                candidate = deepcopy(state)
                candidate['actions']['prompts']['CurrentChoiceHelper'] = '0x0'
                try:
                    plan = battle_decision(candidate)
                except (KeyError, TypeError, ValueError):
                    return self.wait(60, 'No se pudo validar el ataque directo pendiente')
                if (plan['kind'] != 'direct' or plan['attacker_name'] != self.resume_direct or
                        len(occupied) != 1 or occupied[0]['address'] != plan['attacker']):
                    raise RuntimeError('El aviso no coincide con un único atacante directo')
                current = (choice, plan['attacker'], state['manager'])
                self.stable = self.stable + 1 if current == self.last else 1
                self.last = current
                if self.stable < 2:
                    return None
                self.plan = plan
                self.record_attack_baseline(state)
                self.click(state, self.point('direct_attack'))
                self.stage, self.waited = 'await_target', 0
                return 'target_clicked'
            phase = state.get('phase')
            if phase == 'BATTLE':
                self.stage, self.waited = 'battle', 0
                return 'battle_ready'
            if phase not in ('MAIN 1', 'MAIN 2') or not ready(state, phase):
                return self.wait(80, 'No apareció Main o Battle libre')
            current = (phase, state['player'], state['manager'], state['own_id'])
            self.stable = self.stable + 1 if current == self.last else 1
            self.last = current
            if self.stable < 2:
                return None
            require_recent(state)
            self.desktop.key('tab')
            self.click(state, self.point('phase_battle'))
            self.stage, self.waited = 'await_battle', 0
            return 'battle_clicked'
        if self.stage == 'await_battle':
            if state.get('phase') == 'BATTLE':
                self.stage, self.waited = 'battle', 0
                return 'battle_ready'
            if state.get('phase') not in ('MAIN 1', 'MAIN 2'):
                raise RuntimeError('La fase cambió inesperadamente tras pedir Battle')
            self.waited += 1
            if self.waited >= 18:
                # A disabled Battle button leaves the old phase picker open.
                if ready(state, state['phase']):
                    return self.end(state, menu_open=True)
                if self.waited >= 40:
                    raise RuntimeError('Battle no abrió y Main no volvió a estar libre')
            return None
        if self.stage == 'battle':
            if state.get('phase') != 'BATTLE':
                raise RuntimeError('Battle terminó antes de confirmar una acción')
            if not ready(state, 'BATTLE'):
                return self.wait(80, 'Battle no volvió a un estado libre')
            try:
                plan = battle_decision(state, self.used_attackers)
            except (KeyError, TypeError, ValueError):
                return self.wait(40, 'No se pudo formar una decisión de ataque verificable')
            self.waited = 0
            if plan == self.last:
                self.stable += 1
            else:
                self.last, self.stable = plan, 1
            if self.stable < 2:
                return None
            if plan['kind'] == 'end':
                return self.end(state)
            if self.attacks >= 12:
                raise RuntimeError('Se alcanzó el límite de ataques de este turno')
            self.plan = plan
            self.target_attempts = 0
            self.direct_probe_used = False
            self.direct_probe_previous = None
            self.failed_targets.clear()
            point = tuple(self.config['zones']['own_monsters'][plan['attacker_zone']])
            self.click(state, point)
            self.stage, self.waited = 'await_selection', 0
            return 'attacker_clicked'
        if self.stage == 'await_selection':
            matches = [e for e in state.get('scene', {}).get('field_elements', []) if
                       e.get('card_address') == self.plan['attacker'] and
                       e.get('visual_slot') == self.plan['attacker_zone'] and
                       e.get('selected') is True]
            if len(matches) == 1:
                point = tuple(self.config['zones']['own_monsters'][self.plan['attacker_zone']])
                offset = self.config['attack_sword_offset']
                self.click(state, (point[0] + offset[0], point[1] + offset[1]))
                self.stage, self.waited = 'await_target_prompt', 0
                self.target_choice = None
                self.target_choice_confirmations = 0
                return 'attack_option_clicked'
            return self.wait(16, 'No se verificó la selección del monstruo propio')
        if self.stage == 'await_target_prompt':
            actions = state.get('actions', {})
            prompts, flags = actions.get('prompts', {}), actions.get('flags', {})
            choice = prompts.get('CurrentChoiceHelper')
            if (state.get('phase') == 'BATTLE' and state.get('my_turn') is True and
                    actions.get('status') == 'observed' and
                    choice not in (None, '0x0') and
                    prompts.get('CurrentOptionHelper') == '0x0' and
                    prompts.get('CurrentConfirmationHelper') == '0x0' and
                    all(flags.get(k) is False for k in
                        ('Busy', 'SelectingCard', 'ShowChainPrompt'))):
                # The captured attack-target prompt keeps GameStateOpen=true.
                # Require the same ChoiceHelper twice before clicking a target.
                self.target_choice_confirmations = (
                    self.target_choice_confirmations + 1 if choice == self.target_choice else 1)
                self.target_choice = choice
                if self.target_choice_confirmations < 2:
                    return None
                if self.plan['kind'] == 'direct':
                    point = self.point('direct_attack')
                else:
                    point = self.enemy_point()
                self.record_attack_baseline(state)
                if self.plan['kind'] == 'attack':
                    self.target_probe_index = 0
                    needs_hover = (self.plan.get('uncertain_target') is True and
                                   self.target_game_object(state) is not None)
                    if self.calibrated_target() and not needs_hover:
                        self.target_point = point
                        self.click(state, point)
                        self.target_attempts = 1
                        self.stage, self.waited = 'await_target', 0
                        return 'target_clicked_calibrated'
                    self.aim_target(state)
                    return 'target_aimed'
                self.click(state, point)
                self.stage, self.waited = 'await_target', 0
                self.target_hover = None
                self.target_hover_confirmations = 0
                return 'target_clicked'
            self.target_choice = None
            self.target_choice_confirmations = 0
            return self.wait(16, 'No se verificó la elección de objetivo')
        if self.stage == 'aim_target':
            point = self.target_point or self.enemy_point()
            cursor = self.desktop.position()
            if abs(cursor[0] - point[0]) > 15 or abs(cursor[1] - point[1]) > 15:
                self.target_drift_attempts += 1
                if self.target_drift_attempts > self.max_target_attempts:
                    raise RuntimeError('No se pudo estabilizar el cursor sobre el objetivo enemigo')
                self.aim_target(state)
                return (f'target_reaimed: {self.target_drift_attempts}/'
                        f'{self.max_target_attempts}')
            self.target_drift_attempts = 0
            if self.confirmed_target_hover(state):
                # Revalidate the exact target after Unity has processed mouse
                # motion; an immediate move+click can miss a facedown card.
                self.record_attack_baseline(state)
                self.click_aimed_target(state)
                self.target_attempts += 1
                self.stage, self.waited = 'await_target', 0
                return ('target_clicked' if self.target_attempts == 1 else
                        f'target_retry_clicked: {self.target_attempts}/{self.max_target_attempts}')
            if (self.target_native_pointer(state) is not None and self.waited >= 2 and
                    self.target_hover_confirmations == 0):
                self.aim_target(state, next_point=True)
                return 'target_probe_aimed'
            return self.wait(20, 'No se verificó el cursor sobre el objetivo enemigo')
        if self.stage == 'await_target':
            if self.attack_resolved(state):
                self.attacks += 1
                self.used_attackers.add(self.plan['attacker'])
                self.stage, self.waited, self.stable, self.last = 'battle', 0, 0, None
                return 'attack_verified'
            if self.plan['kind'] == 'attack' and self.selected_target(state):
                return self.confirm_target(state)
            if self.waited >= 8:
                retry = self.retry_target(state)
                if retry is not None:
                    return retry
            return self.wait(40, 'No se verificó la confirmación ni el ataque')
        if self.stage == 'await_direct_probe':
            # A disappearing action alone is insufficient for this fallback:
            # require the expected direct damage as well as normal resolution.
            if self.attack_resolved(state, strict_direct=True):
                self.attacks += 1
                self.used_attackers.add(self.plan['attacker'])
                self.direct_probe_previous = None
                self.stage, self.waited, self.stable, self.last = 'battle', 0, 0, None
                return 'direct_attack_probe_verified'
            prompts = state.get('actions', {}).get('prompts', {})
            if self.waited >= 8 and prompts.get('CurrentChoiceHelper') == self.target_choice:
                self.plan, self.attack_baseline = self.direct_probe_previous
                self.direct_probe_previous = None
                self.stage, self.waited = 'await_target', 8
                return 'direct_attack_probe_ignored'
            return self.wait(40, 'No se verificó el resultado del intento de ataque directo')
        if self.stage == 'await_attack':
            if self.attack_resolved(state):
                self.attacks += 1
                self.used_attackers.add(self.plan['attacker'])
                self.stage, self.waited, self.stable, self.last = 'battle', 0, 0, None
                return 'attack_verified'
            return self.wait(32, 'No se confirmó el resultado del ataque')
        if self.stage == 'await_end':
            # A fast rival can finish its turn between monitor samples. In
            # that case END and the rival turn are never sampled, but a fresh
            # own DRAW after our END click proves the previous turn ended.
            if state.get('phase') == 'DRAW' and self.end_origin_phase not in (None, 'DRAW'):
                self.finished = True
                return 'turn_passed'
            if state.get('phase') == 'END':
                self.stage, self.waited = 'await_opponent', 0
                return 'end_phase_verified'
            return self.wait(24, 'No se confirmó el paso a END')
        if self.stage == 'await_opponent':
            if state.get('phase') == 'DRAW' and self.end_origin_phase not in (None, 'DRAW'):
                self.finished = True
                return 'turn_passed'
            return self.wait(80, 'END apareció, pero no se confirmó el turno rival')
        raise RuntimeError(f'Estado de control desconocido: {self.stage}')

    def attack_resolved(self, state, *, strict_direct=False):
        if self.plan['kind'] == 'direct' and self.attack_baseline:
            manager, own_field, rival_field, own_lp, rival_lp, _ = self.attack_baseline
            fields = state.get('board', {}).get('fields', [])
            own = [f for f in fields if f.get('local') is True]
            rival = [f for f in fields if f.get('local') is False]
            if (state.get('status') == 'duel' and state.get('manager') == manager and
                    state.get('my_turn') is True and state.get('phase') == 'BATTLE' and
                    len(own) == 1 and len(rival) == 1 and
                    own[0].get('address') == own_field and
                    rival[0].get('address') == rival_field and
                    own[0].get('life_points_candidate') == own_lp and
                    rival[0].get('life_points_candidate') ==
                    rival_lp - self.plan['expected_damage'] and
                    state.get('actions', {}).get('prompts', {}).get('CurrentChoiceHelper') == '0x0'):
                return True
        if strict_direct:
            return False
        if self.plan['kind'] == 'attack' and self.attack_baseline:
            manager, own_field, rival_field, own_lp, rival_lp, target_before = self.attack_baseline
            fields = state.get('board', {}).get('fields', [])
            own = [f for f in fields if f.get('local') is True]
            rival = [f for f in fields if f.get('local') is False]
            if (state.get('status') == 'duel' and state.get('manager') == manager and
                    state.get('my_turn') is True and state.get('phase') == 'BATTLE' and
                    len(own) == 1 and len(rival) == 1 and
                    own[0].get('address') == own_field and rival[0].get('address') == rival_field and
                    state.get('actions', {}).get('prompts', {}).get('CurrentChoiceHelper') == '0x0'):
                entries = rival[0].get('visual_candidates', {}).get('MonsterCards', {}).get('candidates', [])
                if len(entries) == 5:
                    card = entries[self.plan['target_zone']].get('card')
                    target_after = (card.get('address'), card.get('face_down'),
                                    card.get('face_position_raw'), card.get('name')) if card else None
                    if target_after != target_before:
                        return True
                own_entries = own[0].get('visual_candidates', {}).get('MonsterCards', {}).get('candidates', [])
                if (len(own_entries) == 5 and
                        not any(e.get('card') and e['card'].get('address') == self.plan['attacker']
                                for e in own_entries)):
                    return True
                if (type(own[0].get('life_points_candidate')) is int and
                        type(rival[0].get('life_points_candidate')) is int and
                        (own[0]['life_points_candidate'] != own_lp or
                         rival[0]['life_points_candidate'] != rival_lp)):
                    return True
        if not ready(state, 'BATTLE'):
            return False
        own = [f for f in state.get('board', {}).get('fields', []) if f.get('local') is True]
        if len(own) != 1:
            return False
        entries = own[0].get('visual_candidates', {}).get('MonsterCards', {}).get('candidates', [])
        cards = [e['card'] for e in entries if e.get('card') and
                 e['card'].get('address') == self.plan['attacker']]
        if len(cards) != 1:
            return False
        card = cards[0]
        if card.get('AttackedThisTurn') is True:
            return True
        if attack_available(state, own[0].get('player_index'), card.get('FieldSlot')):
            return False
        if self.plan['kind'] == 'direct':
            return True
        other = [f for f in state.get('board', {}).get('fields', []) if f.get('local') is False]
        if len(other) != 1:
            return False
        targets = other[0].get('visual_candidates', {}).get('MonsterCards', {}).get('candidates', [])
        return all(not e.get('card') or e['card'].get('address') != self.plan['target']
                   for e in targets)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'config.json')
    parser.add_argument('--resume-direct', metavar='CARD',
                        help='Retomar un aviso de ataque directo ya abierto para esta carta')
    parser.add_argument('--resume-target', metavar='CARD',
                        help='Confirmar un objetivo enemigo ya elegido y bajo el cursor')
    parser.add_argument('--end-turn', action='store_true',
                        help='Esperar Main/Battle libre y terminar el turno sin atacar')
    args = parser.parse_args()
    if sum((bool(args.resume_direct), bool(args.resume_target), args.end_turn)) > 1:
        parser.error('--resume-direct, --resume-target y --end-turn son modos separados')
    controller = TurnController(Desktop(), json.loads(args.config.read_text()),
                                resume_direct=args.resume_direct,
                                resume_target=args.resume_target, end_turn=args.end_turn)
    for line in sys.stdin:
        try:
            state = json.loads(line)
            result = controller.observe(state)
            if result:
                if result == 'attacker_clicked':
                    plan = controller.plan
                    target = plan.get('target_name', 'ataque directo')
                    print(f'Plan: {plan["attacker_name"]} → {target} '
                          f'(daño numérico estimado: {plan["expected_damage"]})', flush=True)
                print(result, flush=True)
            if controller.finished:
                return 0
        except (KeyError, TypeError, ValueError, OSError, RuntimeError,
                subprocess.CalledProcessError) as error:
            print(f'PARADA SEGURA: {error}', file=sys.stderr)
            return 2
    print('PARADA SEGURA: el monitor terminó antes de confirmar el turno', file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
