#!/usr/bin/env python3
"""Experimental memory-guided duel loop; the visual bot remains separate.

Consumes the read-only monitor's JSONL. Tries one published normal summon per
own turn, then battle and END. Failed transitions get bounded phase recovery.
"""

import argparse
import copy
import json
from pathlib import Path
import subprocess
import sys

from dmo.bot.dmo_memory_bot import (Desktop, MonsterPlacement, ROOT, Selector, SummonOption,
                            hand_signature, monster_zone_choice, probe_points, require_recent, selection)
from dmo.bot.dmo_turn_bot import TurnController
from dmo.bot.dmo_spell_controller import SpellController
from dmo.bot.dmo_card_panel import CardPanelController
from dmo.bot.dmo_special_summon import SpecialSummonController
from dmo.bot.dmo_end_fallback import EndFallback


class DuelLoop:
    def __init__(self, desktop, config, *, max_turns=0, spell_probe=False, spells=False):
        self.desktop, self.config = desktop, config
        self.max_turns = max_turns
        self.spell_probe = spell_probe
        self.spells = spells or spell_probe
        self.spell_count = 0
        self.spell_plan = None
        self.selecting_spell = False
        self.stage = 'waiting'
        self.manager = None
        self.turns_completed = 0
        self.summoned = False
        self.actor = None
        self.card_name = None
        self.card_address = None
        self.waited = 0
        self.last_draw = None
        self.draw_confirmations = 0
        self.finished = False
        self.unstable_samples = 0
        self.recovery_attempts = 0
        self.max_recoveries = config.get('memory_recovery_attempts', 5)
        self.recovery_sample_limit = config.get('memory_recovery_samples', 80)
        self.max_action_attempts = config.get('memory_action_attempts', 3)
        self.draw_attempts = 0
        self.recovery_error = None
        self.recovery_origin = None
        self.recovery_last = None
        self.recovery_stable = 0
        self.recovery_samples = 0
        self.card_panel = None
        self.special_summon = None
        self.panel_origin = None
        self.pending_normal_summon = None

    def click(self, state, point):
        point = tuple(point)
        if (len(point) != 2 or not all(type(v) is int for v in point) or
                not 0 <= point[0] < self.config['screen']['width'] or
                not 0 <= point[1] < self.config['screen']['height']):
            raise RuntimeError('Punto calibrado fuera de pantalla')
        require_recent(state)
        actual = self.desktop.move(point)
        if self.desktop.position() != actual:
            raise RuntimeError('El cursor cambió antes del clic')
        self.desktop.click()

    def turn_actor(self):
        self.actor = TurnController(self.desktop, self.config)
        self.stage, self.waited = 'turn', 0
        return 'turn_actions_started'

    def observe(self, state):
        try:
            return self.observe_state(state)
        except RuntimeError as error:
            # Coordinate/identity errors still stop immediately. Only failed
            # game transitions can be retried from freshly observed state.
            recoverable = str(error).startswith((
                'No se verificó', 'No se confirmó', 'No se recuperó',
                'DRAW no quedó libre', 'El robo se confirmó', 'Main terminó',
                'El turno cambió antes', 'No se estabilizó Main',
                'No se confirmó la carta', 'La invocación no se confirmó',
                'Summon no abrió', 'Mágica ', 'No apareció una elección de zona',
                'No apareció una carta seleccionada'))
            fallback_error = recoverable or str(error).startswith((
                'Recuperación agotada', 'Invocación especial pendiente',
                'Battle no volvió', 'Panel de cartas', 'Equipo:', 'Objetivo de MST',
                'No se pudo', 'Se alcanzó el límite', 'El panel de cartas',
                'Cambió el contenido del panel', 'Índice seleccionado',
                'Selecciones duplicadas', 'La opción del panel',
                'No se localizó el objetivo enemigo',
                'No se pudo estabilizar el cursor sobre el objetivo enemigo'))
            if (fallback_error and self.config.get('memory_end_fallback', True) and
                    self.manager is not None and self.stage != 'end_fallback' and
                    (not recoverable or self.stage == 'recovery' or
                     self.recovery_attempts >= self.max_recoveries)):
                self.recovery_error, self.recovery_origin = str(error), self.stage
                self.actor = EndFallback(self.desktop, self.config, state)
                self.card_panel = self.special_summon = self.panel_origin = None
                self.stage = 'end_fallback'
                return f'recovery_started: último recurso END · {error}'
            if (not recoverable or self.manager is None or self.stage == 'recovery' or
                    self.recovery_attempts >= self.max_recoveries):
                raise
            self.recovery_error, self.recovery_origin = str(error), self.stage
            self.recovery_attempts += 1
            self.stage, self.waited = 'recovery', 0
            self.recovery_last, self.recovery_stable, self.recovery_samples = None, 0, 0
            return f'recovery_started: {self.recovery_attempts}/{self.max_recoveries} · {error}'

    def recover_phase(self, state):
        self.recovery_samples += 1
        if self.recovery_samples > self.recovery_sample_limit:
            raise RuntimeError('Recuperación agotada: la fase no se estabilizó. '
                               f'Fallo original: {self.recovery_error}')
        actions = state.get('actions', {})
        flags, prompts = actions.get('flags', {}), actions.get('prompts', {})
        calm = (state.get('local_player') is True and actions.get('status') == 'observed' and
                type(flags.get('GameStateOpen')) is bool and
                all(flags.get(k) is False for k in ('Busy', 'SelectingCard', 'ShowChainPrompt')) and
                all(prompts.get(k) == '0x0' for k in
                    ('CurrentChoiceHelper', 'CurrentOptionHelper', 'CurrentConfirmationHelper')))
        phase = state.get('phase')
        current = (state['manager'], state.get('player'), state.get('my_turn'), phase,
                   flags.get('DrewForTurn'), flags.get('GameStateOpen'), calm)
        self.recovery_stable = self.recovery_stable + 1 if current == self.recovery_last else 1
        self.recovery_last = current
        if self.recovery_stable < 2:
            return 'recovery_reading_phase'
        if state.get('my_turn') is False:
            # A late turn transition makes an additional action unnecessary.
            self.stage, self.actor = 'turn', TurnController(self.desktop, self.config)
            self.actor.identity = (state['player'], state['manager'], state['own_id'])
            return 'recovery_rival_turn'
        if calm and state.get('my_turn') is True:
            if (phase == 'DRAW' and self.recovery_origin == 'turn' and
                    isinstance(self.actor, TurnController) and
                    self.actor.stage in ('await_end', 'await_opponent') and
                    self.actor.end_origin_phase not in (None, 'DRAW')):
                # END and the entire rival turn may pass between snapshots.
                # This is a new own turn, not a failed END or a stalled draw.
                return self.complete_turn()
            if phase == 'DRAW' and self.recovery_attempts == 1:
                own = [f for f in state.get('board', {}).get('fields', []) if f.get('local') is True]
                if len(own) == 1 and self.last_draw is not None:
                    field = own[0]
                    unchanged = (field.get('address') == self.last_draw[1] and
                        field.get('HandCardCount') == self.last_draw[2] and
                        field.get('DeckCount') == self.last_draw[3] and
                        tuple(c.get('address') for c in field.get('HandCards', [])) == self.last_draw[4])
                    if not unchanged or flags.get('DrewForTurn') is True:
                        self.stage, self.waited = 'await_draw_exit', 0
                        return 'recovery_draw_already_changed'
                    if flags.get('DrewForTurn') is False:
                        self.stage, self.waited = 'draw', 0
                        self.last_draw, self.draw_confirmations = None, 0
                        return 'recovery_retry_draw'
                elif self.last_draw is None and flags.get('DrewForTurn') is False:
                    self.stage, self.waited = 'draw', 0
                    self.draw_confirmations = 0
                    return 'recovery_retry_draw'
            if (phase in ('MAIN 1', 'MAIN 2') and flags.get('GameStateOpen') is True and
                    self.recovery_attempts == 1 and
                    self.recovery_origin in ('waiting', 'draw', 'await_draw', 'await_draw_exit', 'main')):
                self.stage, self.waited, self.actor = 'main', 0, None
                return 'recovery_main_ready'
            if phase in ('DRAW', 'STANDBY', 'MAIN 1', 'MAIN 2', 'BATTLE', 'END'):
                # Even DRAW can try the phase menu after the one verified
                # retry. The controller must confirm END or the rival turn.
                self.actor = TurnController(self.desktop, self.config, end_turn=True)
                self.actor.begin(state)
                self.stage, self.waited = 'turn', 0
                if phase == 'END':
                    self.actor.stage = 'await_opponent'
                    return 'recovery_waiting_rival'
                return 'recovery_' + self.actor.end(state)
        if self.recovery_samples >= self.recovery_sample_limit:
            raise RuntimeError('Recuperación agotada: no hay una fase propia libre para actuar o terminar '
                               f'el turno. Fallo original: {self.recovery_error}')
        return 'recovery_waiting_free_phase'

    def observe_state(self, state):
        if self.finished:
            return None
        if state.get('status') != 'duel':
            if self.manager is not None and state.get('status') == 'outside_duel':
                self.finished = True
                return 'duel_finished'
            if self.manager is not None:
                if self.stage == 'recovery':
                    self.recovery_samples += 1
                    if self.recovery_samples >= self.recovery_sample_limit:
                        raise RuntimeError('Recuperación agotada: no se pudo volver a leer el duelo')
                    return 'recovery_reading_phase'
                self.unstable_samples += 1
                if self.unstable_samples >= 40:
                    raise RuntimeError('No se recuperó un estado de duelo ni se confirmó su salida')
                return None
            self.waited += 1
            if self.waited >= 120:
                raise RuntimeError('No apareció un duelo')
            return None
        self.unstable_samples = 0
        if self.manager is None:
            self.manager = state['manager']
        elif state['manager'] != self.manager:
            raise RuntimeError('Cambió el duelo; iniciar un nuevo monitor')
        if state.get('my_turn') is False or state.get('phase') == 'DRAW':
            self.pending_normal_summon = None
        if (self.pending_normal_summon is not None and not self.summoned and
                self.stage in ('recovery', 'turn', 'end_fallback')):
            name, address, signature = self.pending_normal_summon
            details = state.get('actions', {}).get('prompt_details', {}).get('CurrentChoiceHelper', {})
            expected = dict(Description='Normal Summon\n<color=#00FFFFFF>' + name,
                Cancelable=True, Min=0, Max=0, UseConfirmations=False,
                ChoiceType=dict(enum_raw=0), PurposeType=dict(enum_raw=0))
            if all(details.get(k) == v for k, v in expected.items()):
                placement = monster_zone_choice(state, name, card_address=address)
                if placement is not None and placement[3] == signature:
                    self.card_name, self.card_address = name, address
                    self.actor = MonsterPlacement(self.desktop, self.config, name, card_address=address)
                    self.stage, self.waited = 'place', 0
                    return 'recovery_summon_zone_ready'
        if self.stage == 'end_fallback':
            result = self.actor.observe(state)
            if self.actor.finished:
                if self.actor.resume:
                    if self.actor.resumed_from_draw:
                        self.summoned = False
                        self.spell_count = 0
                        self.card_name = self.card_address = None
                    self.stage, self.waited, self.actor = 'waiting', 0, None
                    return result
                return self.complete_turn()
            return result
        option = state.get('actions', {}).get('prompts', {}).get('CurrentOptionHelper')
        if self.card_panel is not None or option not in (None, '0x0'):
            if self.stage == 'spell' and self.actor is not None:
                # The phase controller is paused during panels; retain the
                # pre-discard hand before the panel controller selects cards.
                self.actor.capture_discard_hand(state)
            if self.card_panel is None:
                self.panel_origin = copy.deepcopy(state)
                plan = self.spell_plan if self.stage == 'spell' else None
                self.card_panel = CardPanelController(self.desktop, self.config,
                    preferred_name=plan.get('search_name') if isinstance(plan, dict) else None,
                    discard=isinstance(plan, dict) and plan.get('effect') == 'draw_discard')
            result = self.card_panel.observe(state)
            if self.card_panel.finished:
                panel = self.card_panel
                if self.stage == 'spell' and isinstance(self.actor, SpellController):
                    self.actor.record_search_selection(panel)
                if 'special summon' in panel.identity[-1].lower() and len(panel.desired) == 1:
                    name = panel.cards[next(iter(panel.desired))]['name']
                    self.special_summon = SpecialSummonController(
                        self.desktop, self.config, self.panel_origin or state, name)
                self.card_panel = None
                self.panel_origin = None
            # Pause phase/attack controllers while a local panel is pending,
            # including panels offered during the rival's turn.
            return result
        if self.special_summon is not None:
            result = self.special_summon.observe(state)
            if self.special_summon.finished:
                self.special_summon = None
            return result
        if self.stage == 'recovery':
            return self.recover_phase(state)
        if self.stage == 'waiting':
            if state.get('my_turn') is not True or state.get('local_player') is not True:
                return None
            phase = state.get('phase')
            if phase == 'DRAW':
                self.stage, self.waited = 'draw', 0
                self.last_draw, self.draw_confirmations = None, 0
                return 'draw_ready'
            if phase == 'STANDBY':
                return None
            if phase in ('MAIN 1', 'MAIN 2'):
                self.stage, self.waited = 'main', 0
                return 'main_ready'
            if phase == 'BATTLE':
                return self.turn_actor()
            if phase == 'END':
                # A running duel may be observed at the end of a prior turn.
                return None
            return None
        if self.stage == 'draw':
            if state.get('my_turn') is not True or state.get('local_player') is not True:
                self.stage = 'waiting'
                return None
            if state.get('phase') != 'DRAW':
                self.stage, self.waited = 'waiting', 0
                return 'draw_advanced'
            actions = state.get('actions', {})
            flags = actions.get('flags', {})
            prompts = actions.get('prompts', {})
            # The initial DRAW waits for the deck click with GameStateOpen=false.
            # That flag gates Main/Battle actions, but does not mean DRAW is busy.
            if (actions.get('status') != 'observed' or
                    type(flags.get('GameStateOpen')) is not bool or
                    flags.get('DrewForTurn') is not False or
                    any(prompts.get(k) != '0x0' for k in
                        ('CurrentChoiceHelper', 'CurrentOptionHelper', 'CurrentConfirmationHelper')) or
                    any(flags.get(k) is not False for k in
                        ('Busy', 'SelectingCard', 'ShowChainPrompt'))):
                self.last_draw, self.draw_confirmations = None, 0
                self.waited += 1
                if self.waited >= 40:
                    raise RuntimeError('DRAW no quedó libre para robar')
                return None
            own = [f for f in state.get('board', {}).get('fields', []) if f.get('local') is True]
            if len(own) != 1 or type(own[0].get('HandCardCount')) is not int:
                raise RuntimeError('No se pudo contar la mano antes de robar')
            field = own[0]
            hand = field.get('HandCards')
            if (not isinstance(hand, list) or len(hand) != field['HandCardCount'] or
                    any(not c or not c.get('address') or c['address'] == '0x0' for c in hand) or
                    len({c['address'] for c in hand}) != len(hand) or
                    type(field.get('DeckCount')) is not int or field['DeckCount'] <= 0):
                raise RuntimeError('No se pudo confirmar la mano y el mazo antes de robar')
            current = (state['manager'], field['address'], field['HandCardCount'],
                       field['DeckCount'], tuple(c['address'] for c in hand))
            self.draw_confirmations = self.draw_confirmations + 1 if current == self.last_draw else 1
            self.last_draw = current
            if self.draw_confirmations < 2:
                return None
            self.click(state, self.config['points']['deck'])
            self.draw_attempts = 1
            self.stage, self.waited = 'await_draw', 0
            return 'draw_clicked'
        if self.stage == 'await_draw':
            own = [f for f in state.get('board', {}).get('fields', []) if f.get('local') is True]
            if (len(own) == 1 and own[0].get('address') == self.last_draw[1] and
                    own[0].get('HandCardCount', 0) > self.last_draw[2]):
                self.stage, self.waited = 'await_draw_exit', 0
                return 'draw_verified'
            if state.get('phase') not in ('DRAW', 'STANDBY'):
                self.stage, self.waited = 'waiting', 0
                return 'draw_advanced'
            self.waited += 1
            actions = state.get('actions', {})
            flags, prompts = actions.get('flags', {}), actions.get('prompts', {})
            if (self.waited >= 12 and self.draw_attempts < self.max_action_attempts and
                    state.get('phase') == 'DRAW' and state.get('my_turn') is True and
                    state.get('local_player') is True and actions.get('status') == 'observed' and
                    flags.get('DrewForTurn') is False and
                    all(flags.get(k) is False for k in ('Busy', 'SelectingCard', 'ShowChainPrompt')) and
                    all(prompts.get(k) == '0x0' for k in
                        ('CurrentChoiceHelper', 'CurrentOptionHelper', 'CurrentConfirmationHelper')) and
                    len(own) == 1 and own[0].get('address') == self.last_draw[1] and
                    own[0].get('HandCardCount') == self.last_draw[2] and
                    own[0].get('DeckCount') == self.last_draw[3] and
                    tuple(c.get('address') for c in own[0].get('HandCards', [])) == self.last_draw[4]):
                self.click(state, self.config['points']['deck'])
                self.draw_attempts += 1
                self.waited = 0
                return f'draw_retry: {self.draw_attempts}/{self.max_action_attempts}'
            if self.waited >= 40:
                raise RuntimeError('No se verificó el robo ni avance de fase')
            return None
        if self.stage == 'await_draw_exit':
            # HandCount can increase before DRAW exits or DrewForTurn updates.
            # A successful draw never makes another deck click eligible.
            if state.get('my_turn') is not True or state.get('phase') != 'DRAW':
                self.stage, self.waited = 'waiting', 0
                return 'draw_advanced'
            self.waited += 1
            if self.waited >= 40:
                raise RuntimeError('El robo se confirmó, pero DRAW no avanzó')
            return None
        if self.stage == 'main':
            if state.get('my_turn') is not True:
                raise RuntimeError('El turno cambió antes de invocar')
            if state.get('phase') not in ('MAIN 1', 'MAIN 2'):
                if state.get('phase') == 'BATTLE':
                    return self.turn_actor()
                raise RuntimeError('Main terminó inesperadamente')
            if self.spells and state.get('advice', {}).get('status') == 'wait':
                self.waited += 1
                if self.waited >= 120:
                    raise RuntimeError('No se estabilizó Main para evaluar las mágicas')
                return None
            self.selecting_spell = self.spells and any(
                o.get('kind') == 'spell_candidate' for o in state.get('advice', {}).get('options', []))
            kind = 'spell_candidate' if self.selecting_spell else 'summon_candidate'
            candidate = selection(state, kind=kind) if self.spells else selection(state)
            if candidate is not None and (self.selecting_spell or not self.summoned):
                cards = candidate['signature'][-1]
                slot = candidate['card']['slot']
                points = probe_points(self.config['screen']['width'],
                                      self.config['screen']['height'],
                                      self.config['points']['hand_first'], slot, len(cards))
                self.card_name = candidate['card']['name']
                self.card_address = candidate['card']['address']
                if self.selecting_spell:
                    if self.spell_count >= 20:
                        raise RuntimeError('Se alcanzó el límite de mágicas de este turno')
                    self.spell_plan = next(o for o in state['advice']['options']
                                           if o.get('kind') == 'spell_candidate')
                else:
                    self.spell_plan = None
                self.actor = Selector(self.desktop, points, click=True, kind=kind)
                self.stage, self.waited = 'select', 0
                return f'{kind}: {self.card_name}'
            advice = state.get('advice', {})
            if advice.get('status') == 'insufficient_data':
                raise RuntimeError('No se pudo evaluar la mano: ' + advice.get('reason', ''))
            if not self.selecting_spell and (self.summoned or advice.get('status') == 'no_suggestion'):
                return self.turn_actor()
            self.waited += 1
            if self.waited >= 120:
                if self.selecting_spell:
                    raise RuntimeError(f'No se pudo vincular la mágica {self.card_name or kind} con su interfaz')
                # A failed scene discovery never justifies clicking an unknown card.
                return self.turn_actor()
            return None
        if self.stage == 'select':
            result = self.actor.observe(state)
            if self.actor.finished:
                if self.actor.outcome != 'selected':
                    raise RuntimeError('No se confirmó la carta seleccionada')
                if self.selecting_spell:
                    if self.spell_probe:
                        self.finished = True
                        return f'spell_menu_ready: {self.card_name} · {self.card_address}'
                    self.actor = SpellController(self.desktop, self.config, self.spell_plan)
                    self.stage = 'spell'
                    return result
                self.actor = SummonOption(self.desktop, self.config)
                self.stage = 'option'
            return result
        if self.stage == 'spell':
            result = self.actor.observe(state)
            if self.actor.finished:
                self.spell_count += 1
                self.actor = None
                self.selecting_spell = False
                self.stage, self.waited = 'main', 0
            return result
        if self.stage == 'option':
            result = self.actor.observe(state)
            if result == 'summon_option_clicked':
                self.pending_normal_summon = (self.card_name, self.card_address, hand_signature(state))
            if self.actor.clicked and state.get('actions', {}).get('prompts', {}).get(
                    'CurrentChoiceHelper') not in (None, '0x0'):
                self.actor = MonsterPlacement(self.desktop, self.config, self.card_name,
                                              card_address=self.card_address)
                self.stage = 'place'
                return 'summon_zone_ready'
            if self.actor.finished:
                raise RuntimeError('Summon no abrió la elección de zona')
            return result
        if self.stage == 'place':
            result = self.actor.observe(state)
            if self.actor.finished:
                if self.actor.outcome != 'summon_verified':
                    raise RuntimeError('La invocación no se confirmó')
                self.summoned = True
                self.pending_normal_summon = None
                if self.spells:
                    self.stage, self.waited = 'main', 0
                else:
                    self.turn_actor()
            return result
        if self.stage == 'turn':
            result = self.actor.observe(state)
            if self.actor.finished:
                if result == 'duel_left':
                    self.finished = True
                    return 'duel_finished'
                if result != 'turn_passed':
                    raise RuntimeError('El controlador terminó sin confirmar al rival')
                return self.complete_turn()
            return result
        raise RuntimeError(f'Estado de bucle desconocido: {self.stage}')

    def complete_turn(self):
        self.turns_completed += 1
        self.summoned = False
        self.pending_normal_summon = None
        self.spell_count = self.recovery_attempts = 0
        self.last_draw, self.draw_confirmations, self.draw_attempts = None, 0, 0
        self.card_name = self.card_address = self.actor = None
        self.stage, self.waited = 'waiting', 0
        if self.max_turns and self.turns_completed >= self.max_turns:
            self.finished = True
        return f'turn_passed: {self.turns_completed}'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'config.json')
    parser.add_argument('--turns', type=int, default=0, help='0: continuar hasta terminar el duelo')
    parser.add_argument('--spells', action='store_true', help='Probar mágicas durante Main y registrar prompts pendientes')
    args = parser.parse_args()
    if args.turns < 0:
        parser.error('--turns no puede ser negativo')
    loop = DuelLoop(Desktop(), json.loads(args.config.read_text()), max_turns=args.turns, spells=args.spells)
    for line in sys.stdin:
        try:
            result = loop.observe(json.loads(line))
            if result and result not in ('probing', 'settling', 'checking', 'observing_click',
                                         'observing_option', 'observing_placement'):
                print(result, flush=True)
            if loop.finished:
                return 0
        except (KeyError, TypeError, ValueError, OSError, RuntimeError,
                subprocess.CalledProcessError) as error:
            print(f'PARADA SEGURA: {error}', file=sys.stderr)
            return 2
    print('PARADA SEGURA: el monitor terminó antes de cerrar el duelo', file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
