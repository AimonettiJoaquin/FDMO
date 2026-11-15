#!/usr/bin/env python3
"""First memory-guided action: locate and select one suggested hand card.

Run as the desktop user. Read JSONL from the privileged, read-only monitor on
stdin. Cursor probes use the client's own CardHover state as verification.
No image recognition, native position projection, or process writes are used.
"""
import argparse
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys

from dmo_advisor import published_summons, published_hand_actions


ROOT = Path(__file__).resolve().parent


def hand_signature(state):
    own = [f for f in state['board']['fields'] if f.get('local') is True]
    if len(own) != 1:
        raise ValueError('Falta un único campo local')
    field = own[0]
    cards = field['HandCards']
    if cards is None or len(cards) != field['HandCardCount'] or any(
            not c or not c.get('name') or c.get('unknown') for c in cards):
        raise ValueError('La mano no está completa')
    return (state['player'], state['manager'], field['address'],
            field['owner_identity'], field.get('generation_id'),
            tuple((c['address'], c['name']) for c in cards))


def _linked_hand_card(state, address, name, *, kind):
    """Validate a specific existing binding without treating it as new advice."""
    if (state.get('status') != 'duel' or state.get('local_player') is not True or
            state.get('my_turn') is not True or state.get('phase') not in ('MAIN 1', 'MAIN 2')):
        return None
    actions = state.get('actions', {})
    flags = actions.get('flags', {})
    if (actions.get('status') != 'observed' or
            flags.get('GameStateOpen') is not True or
            any(flags.get(k) is not False for k in ('Busy', 'SelectingCard', 'ShowChainPrompt'))):
        return None
    if any(pointer not in (None, '0x0') for pointer in actions.get('prompts', {}).values()):
        return None
    own = [f for f in state.get('board', {}).get('fields', []) if f.get('local') is True]
    if len(own) != 1:
        return None
    cards = own[0].get('HandCards') or []
    matches = [c for c in cards if c and c.get('address') == address and
               c.get('name') == name]
    try:
        allowed = (published_hand_actions(actions, own[0], cards,
                   option_name='ActivateEffect', option_value=0) if kind == 'spell_candidate'
                   else published_summons(actions, own[0], cards))
        if address not in allowed:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    scene = state.get('scene', {})
    links = [e for e in scene.get('hand_elements', []) if e.get('card_address') == address and
             e.get('name') == name and not e.get('hover_error')]
    if (len(matches) != 1 or len(links) != 1 or scene.get('status') != 'hand_objects_linked' or
            not links[0].get('hover', {}).get('hoverable')):
        return None
    return dict(card=matches[0], link=links[0], signature=hand_signature(state))


def selection(state, *, kind='summon_candidate'):
    """Require a stable Advisor suggestion backed by published hand options."""
    advice = state.get('advice', {})
    options = [o for o in advice.get('options', []) if o.get('kind') == kind]
    if advice.get('status') != 'suggestions' or len(options) != 1:
        return None
    return _linked_hand_card(state, options[0]['address'], options[0].get('card'), kind=kind)


def probe_points(width, height, first, slot, count, step=75):
    """Screen-wide hand-band search; guesses are never treated as clicks."""
    if not 1 <= count <= 20 or not 0 <= slot < count:
        raise ValueError('Posición o cantidad de cartas inválida')
    x0, y0 = first
    # The calibrated hand starts at x0 and cards are spaced about 110px at
    # 1920px. It does not stretch across the whole screen. Verify these guesses
    # by hover, then retain the bounded screen search if the layout differs.
    estimated_x = x0 + slot * 110 * width / 1920
    xs = list(range(step//2, width, step))
    xs.sort(key=lambda x: abs(x - estimated_x))
    ys = [int(y0), int(y0 - 65), int(y0 + 65)]
    points = []
    for y in ys:
        if 0 <= y < height and y >= int(height * 0.65):
            points.extend((x, y) for x in xs)
    near = [(round(estimated_x + dx * width / 1920), int(y0)) for dx in (0, -30, 30)]
    near = [(x, y) for x, y in near if 0 <= x < width and height * 0.65 <= y < height]
    return list(dict.fromkeys(near + points))


def require_recent(state):
    try:
        age = (datetime.now().astimezone() -
               datetime.fromisoformat(state['timestamp']).astimezone()).total_seconds()
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError('Muestra sin timestamp válido') from error
    if not 0 <= age <= 3:
        raise RuntimeError('La última muestra ya no es reciente')


class Desktop:
    def position(self):
        result = subprocess.run(['hyprctl', '-j', 'cursorpos'],
                                capture_output=True, text=True, check=True)
        position = json.loads(result.stdout)
        x, y = position['x'], position['y']
        if not all(type(value) in (int, float) for value in (x, y)):
            raise ValueError('Hyprland devolvió una posición de cursor inválida')
        return (round(x), round(y))

    def move(self, point):
        x, y = point
        # A compositor warp can draw the pointer without delivering mouse
        # motion to the game. End with a real relative input event.
        lead = min(15, x)
        for _ in range(3):
            subprocess.run(['hyprctl', 'dispatch',
                            f'hl.dsp.cursor.move({{ x = {x-lead}, y = {y} }})'],
                           capture_output=True, text=True, check=True)
            subprocess.run(['ydotool', 'mousemove', '-x', str(lead), '-y', '0'],
                           capture_output=True, text=True, check=True)
            actual = self.position()
            if abs(actual[0] - x) <= 15 and abs(actual[1] - y) <= 15:
                return actual
        raise RuntimeError(f'El cursor quedó en {actual}, lejos del punto solicitado {point}')

    def click(self):
        subprocess.run(['ydotool', 'click', '0xC0'], check=True)

    def scroll_up(self, ticks):
        if type(ticks) is not int or not 1 <= ticks <= 32:
            raise ValueError('Cantidad de pasos de rueda inválida')
        subprocess.run(['ydotool', 'mousemove', '--wheel', '-x', '0', '-y', str(ticks)],
                       capture_output=True, text=True, check=True)

    def key(self, name):
        codes = {'tab': 15, 'backspace': 14}
        subprocess.run(['ydotool', 'key', f'{codes[name]}:1', f'{codes[name]}:0'], check=True)


class Selector:
    def __init__(self, desktop, points, *, click=False, kind='summon_candidate'):
        self.desktop, self.points, self.click = desktop, iter(points), click
        self.kind = kind
        self.expected = self.current = None
        self.observations = self.confirmations = 0
        self.finished = False
        self.clicked = False
        self.click_observations = 0
        self.outcome = None
        self.probes = 0
        self.advice_wait_samples = 0
        self.advice_wait_total = 0

    def next_point(self):
        try:
            point = next(self.points)
        except StopIteration:
            raise RuntimeError('No se encontró la carta con hover dentro de la búsqueda acotada')
        self.current = self.desktop.move(point)
        self.observations = self.confirmations = 0
        self.probes += 1

    def observe(self, state):
        if self.finished:
            return None
        if self.clicked:
            self.click_observations += 1
            elements = state.get('scene', {}).get('hand_elements', [])
            if any(e.get('element') == self.expected['link']['element'] and
                   e.get('card_address') == self.expected['card']['address'] and
                   e.get('selected') is True for e in elements):
                self.finished = True
                self.outcome = 'selected'
                return self.outcome
            if self.click_observations >= 8:
                self.finished = True
                self.outcome = 'click_unconfirmed'
                return self.outcome
            return 'observing_click'
        candidate = selection(state, kind=self.kind)
        if self.expected is None:
            if candidate is None:
                return 'waiting'
            self.expected = deepcopy(candidate)
            self.next_point()
            return 'probing'
        waiting_advice = candidate is None and state.get('advice', {}).get('status') == 'wait'
        if waiting_advice:
            candidate = _linked_hand_card(state, self.expected['card']['address'],
                                          self.expected['card']['name'], kind=self.kind)
        if candidate is None or candidate['signature'] != self.expected['signature'] or (
                candidate['card']['address'] != self.expected['card']['address'] or
                candidate['link']['element'] != self.expected['link']['element'] or
                candidate['link']['native_element'] != self.expected['link']['native_element']):
            raise RuntimeError('El estado, la mano o el objeto seleccionado cambió; búsqueda detenida')
        actual = self.desktop.position()
        if abs(actual[0] - self.current[0]) > 2 or abs(actual[1] - self.current[1]) > 2:
            raise RuntimeError(f'El cursor se movió fuera del punto probado: {actual}; '
                               'búsqueda detenida')
        if waiting_advice:
            self.advice_wait_samples += 1
            self.advice_wait_total += 1
            self.confirmations = 0
            self.observations = min(self.observations, 2)
            if self.advice_wait_samples >= 8 or self.advice_wait_total >= 40:
                raise RuntimeError('No se estabilizó la recomendación al seleccionar la carta')
            return 'waiting_advice'
        self.advice_wait_samples = 0
        self.observations += 1
        # Ignore the first two snapshots after motion: the UI may still show
        # the hover produced at the previous cursor position.
        if self.observations <= 2:
            return 'settling'
        hovered = [e for e in state['scene']['hand_elements']
                   if e.get('hover', {}).get('hovered') is True]
        target = (len(hovered) == 1 and hovered[0]['element'] ==
                  self.expected['link']['element'])
        self.confirmations = self.confirmations + 1 if target else 0
        if self.confirmations >= 2:
            if self.click:
                require_recent(state)
                self.desktop.click()
                self.clicked = True
                return 'clicked'
            self.finished = True
            self.outcome = 'verified'
            return self.outcome
        if self.observations >= 5:
            self.next_point()
            return 'probing'
        return 'checking'


class SummonOption:
    """One calibrated menu click after a card is selected and under cursor."""
    def __init__(self, desktop, config, *, kind='summon_candidate'):
        self.desktop = desktop
        self.kind = kind
        self.label = 'ActivateEffect' if kind == 'spell_candidate' else 'Summon'
        first = config['points']['hand_first']
        option = config['points']['summon']
        self.offset = (option[0] - first[0], option[1] - first[1])
        self.width = config['screen']['width']
        self.height = config['screen']['height']
        if kind == 'spell_candidate':
            # Tenki menu captured at 1920x1080 on 2026-09-27: two
            # buttons, Activate Effect at (767, 825), Set at (904, 825),
            # with the card cursor near (835, 957). Summon's three-button
            # menu has a different horizontal spacing.
            self.offset = (round(-70 * self.width / 1920),
                           round(-132 * self.height / 1080))
        self.identity = None
        self.position = None
        self.observations = 0
        self.clicked = False
        self.after_click = 0
        self.finished = False
        self.wait_samples = 0

    def observe(self, state):
        if self.finished:
            return None
        if self.clicked:
            self.after_click += 1
            if self.after_click >= 16:
                self.finished = True
                return 'option_click_recorded'
            return 'observing_option'
        candidate = selection(state, kind=self.kind)
        if candidate is None or candidate['link'].get('selected') is not True or (
                candidate['link'].get('hover', {}).get('hovered') is not True):
            self.identity = self.position = None
            self.observations = 0
            self.wait_samples += 1
            if self.wait_samples >= 60:
                raise RuntimeError(f'No apareció una carta seleccionada con {self.label} y hover en 60 muestras')
            return 'waiting_selected_card'
        self.wait_samples = 0
        position = self.desktop.position()
        identity = (candidate['signature'], candidate['link']['element'])
        if identity != self.identity or position != self.position:
            self.identity, self.position = identity, position
            self.observations = 1
            return 'checking_selected_card'
        self.observations += 1
        if self.observations < 2:
            return 'checking_selected_card'
        if self.kind == 'spell_candidate':
            own = next(f for f in state['board']['fields'] if f.get('local') is True)
            helpers = [h for h in state['actions']['helpers']
                       if h.get('CardLocation', {}).get('enum_raw') == 1
                       and h.get('FieldID') == own['player_index']
                       and h.get('ID') == candidate['card']['slot']]
            expected = {(0, 'ActivateEffect'), (4, 'Set')}
            if len(helpers) != 1 or len(helpers[0]['options']) != 2 or {
                    (o.get('value'), o.get('name')) for o in helpers[0]['options']} != expected:
                raise RuntimeError('El menú de mágica no coincide con Activate Effect / Set calibrado')
        target = (position[0] + self.offset[0], position[1] + self.offset[1])
        if not 0 <= target[0] < self.width or not 0 <= target[1] < self.height:
            raise RuntimeError(f'La opción {self.label} calculada queda fuera de la pantalla')
        require_recent(state)
        actual = self.desktop.move(target)
        if self.desktop.position() != actual:
            raise RuntimeError(f'El cursor cambió antes del clic en {self.label}')
        self.desktop.click()
        self.clicked = True
        return 'activate_option_clicked' if self.kind == 'spell_candidate' else 'summon_option_clicked'


def monster_zone_choice(state, card_name, *, card_address=None):
    """Recognize the pending placement prompt while all own zones look vacant."""
    if (state.get('status') != 'duel' or state.get('local_player') is not True or
            state.get('my_turn') is not True or state.get('phase') not in ('MAIN 1', 'MAIN 2')):
        return None
    actions = state.get('actions', {})
    flags, prompts = actions.get('flags', {}), actions.get('prompts', {})
    choice = prompts.get('CurrentChoiceHelper')
    if (actions.get('status') != 'observed' or not choice or choice == '0x0' or
            prompts.get('CurrentOptionHelper') != '0x0' or
            prompts.get('CurrentConfirmationHelper') != '0x0' or
            flags.get('GameStateOpen') is not False or
            any(flags.get(k) is not False for k in ('Busy', 'SelectingCard', 'ShowChainPrompt'))):
        return None
    own = [f for f in state.get('board', {}).get('fields', []) if f.get('local') is True]
    if len(own) != 1:
        return None
    field = own[0]
    try:
        signature = hand_signature(state)
    except (KeyError, TypeError, ValueError):
        return None
    cards = field['HandCards']
    matches = [c for c in cards if c['name'] == card_name and
               (card_address is None or c['address'] == card_address)]
    if len(matches) != 1:
        return None
    slot = matches[0]['slot']
    helpers = [h for h in actions.get('helpers', []) if
               h.get('CardLocation', {}).get('enum_raw') == 1 and
               h.get('FieldID') == field.get('player_index') and h.get('ID') == slot]
    if (len(helpers) != 1 or not any(o.get('name') == 'Summon'
                                     for o in helpers[0].get('options', []))):
        return None
    zones = field.get('MonsterCards')
    visual = field.get('visual_candidates', {}).get('MonsterCards', {})
    candidates = visual.get('candidates', [])
    if (not isinstance(zones, list) or len(zones) != 5 or
            visual.get('source') != 'MonsterVisualHelpers' or len(candidates) != 5):
        return None
    # In this client even occupied slots retain unnamed GameCard placeholders.
    # The visual helpers distinguish actual cards from vacant positions.
    free = []
    for index, (zone, candidate) in enumerate(zip(zones, candidates)):
        if candidate.get('helper_index') != index:
            return None
        diagnostic = zone.get('diagnostic', {}) if zone else {}
        finders = (diagnostic.get('cardFinder'), diagnostic.get('OverrideCardfinder'))
        if (not zone or not zone.get('unknown') or zone.get('name') or
                diagnostic.get('CurrentName') != '' or
                diagnostic.get('CardLocation', {}).get('enum_raw') != 0 or
                any(not isinstance(f, dict) or f.get('CardID') != 0 for f in finders)):
            return None
        if candidate.get('helper_present') is False and candidate.get('card') is None:
            free.append(candidate.get('helper_index'))
        elif not (candidate.get('card') and candidate['card'].get('location') == 'Field'
                  and candidate['card'].get('name')):
            return None
    if not free or any(type(i) is not int or not 0 <= i < 5 for i in free):
        return None
    chosen = min(free, key=lambda i: (abs(i - 2), i))
    return (state['player'], state['manager'], field['address'],
            signature, matches[0]['address'], choice, chosen)


class MonsterPlacement:
    """Click the calibrated center zone once, then require a memory transition."""
    def __init__(self, desktop, config, card_name, *, card_address=None):
        self.desktop, self.card_name = desktop, card_name
        self.card_address = card_address
        self.center_target = tuple(config['points']['summon_slot'])
        self.zones = config['zones']['own_monsters']
        self.target = None
        width, height = config['screen']['width'], config['screen']['height']
        if not (len(self.center_target) == 2 and 0 <= self.center_target[0] < width and
                0 <= self.center_target[1] < height and len(self.zones) == 5):
            raise ValueError('Zona de invocación calibrada fuera de pantalla')
        self.width, self.height = width, height
        self.identity = None
        self.confirmations = self.after_click = 0
        self.clicked = self.finished = False
        self.outcome = None
        self.wait_samples = 0

    def observe(self, state):
        if self.finished:
            return None
        if self.clicked:
            self.after_click += 1
            own = [f for f in state.get('board', {}).get('fields', []) if f.get('local') is True]
            if (state.get('status') == 'duel' and state.get('manager') == self.identity[1] and
                    len(own) == 1 and own[0].get('address') == self.identity[2]):
                hand = own[0].get('HandCards') or []
                visuals = (own[0].get('visual_candidates', {})
                           .get('MonsterCards', {}).get('candidates', []))
                before = tuple(card for card in self.identity[3][-1]
                               if card[0] != self.identity[4])
                after = tuple((c.get('address'), c.get('name')) for c in hand if c)
                placed = [c['card'] for c in visuals if c.get('helper_index') == self.identity[6]
                          and c.get('card') and
                          c['card'].get('name') == self.card_name and
                          c['card'].get('location') == 'Field']
                # DMO creates a different GameCard for the field. Compare the
                # complete remaining hand and a newly populated visual slot.
                if (len(hand) == len(self.identity[3][-1]) - 1 and
                        after == before and len(placed) == 1):
                    self.finished, self.outcome = True, 'summon_verified'
                    return self.outcome
            if self.after_click >= 24:
                self.finished, self.outcome = True, 'placement_unconfirmed'
                return self.outcome
            return 'observing_placement'
        identity = monster_zone_choice(state, self.card_name, card_address=self.card_address)
        if identity is None:
            self.identity = None
            self.confirmations = 0
            self.wait_samples += 1
            if self.wait_samples >= 60:
                raise RuntimeError('No apareció una elección de zona válida en 60 muestras')
            return 'waiting_zone_choice'
        self.wait_samples = 0
        self.confirmations = self.confirmations + 1 if identity == self.identity else 1
        self.identity = identity
        if self.confirmations < 2:
            return 'checking_zone_choice'
        self.target = (self.center_target if identity[6] == 2 else
                       tuple(self.zones[identity[6]]))
        if (len(self.target) != 2 or not all(type(v) is int for v in self.target) or
                not 0 <= self.target[0] < self.width or
                not 0 <= self.target[1] < self.height):
            raise RuntimeError('La zona libre calculada queda fuera de pantalla')
        require_recent(state)
        actual = self.desktop.move(self.target)
        if self.desktop.position() != actual:
            raise RuntimeError('El cursor cambió antes del clic en la zona')
        self.desktop.click()
        self.clicked = True
        return 'zone_clicked'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--click', action='store_true',
                        help='Hacer un único clic tras confirmar dos muestras de hover')
    parser.add_argument('--summon-option', action='store_true',
                        help='Con una carta ya seleccionada, probar un clic en su opción Summon')
    parser.add_argument('--place-monster', metavar='CARD',
                        help='Con la elección de zona abierta, colocar una vez esta carta en el centro')
    parser.add_argument('--config', type=Path, default=ROOT / 'config.json')
    args = parser.parse_args()
    if sum((args.click, args.summon_option, bool(args.place_monster))) > 1:
        parser.error('--click, --summon-option y --place-monster son modos separados')
    config = json.loads(args.config.read_text())
    screen, first = config['screen'], config['points']['hand_first']
    desktop = Desktop()
    option = SummonOption(desktop, config) if args.summon_option else None
    placement = MonsterPlacement(desktop, config, args.place_monster) if args.place_monster else None
    selector = None
    previous = None
    for line in sys.stdin:
        try:
            state = json.loads(line)
            if placement is not None:
                result = placement.observe(state)
                if result != previous and result in ('zone_clicked', 'summon_verified',
                                                     'placement_unconfirmed'):
                    print(result, flush=True)
                previous = result
                if placement.finished:
                    return 0 if placement.outcome == 'summon_verified' else 2
                continue
            if option is not None:
                result = option.observe(state)
                if result != previous and result in ('summon_option_clicked', 'option_click_recorded'):
                    print(result, flush=True)
                previous = result
                if option.finished:
                    return 0
                continue
            if selector is None:
                candidate = selection(state)
                if candidate is None:
                    continue
                cards = candidate['signature'][-1]
                slot = candidate['card']['slot']
                points = probe_points(screen['width'], screen['height'], first, slot, len(cards))
                selector = Selector(desktop, points, click=args.click)
                print(f'Carta elegida: {candidate["card"]["name"]} · mano {slot+1}/{len(cards)}',
                      flush=True)
            result = selector.observe(state)
            if result != previous and result in (
                    'probing', 'verified', 'clicked', 'selected', 'click_unconfirmed'):
                print(f'{result}: {selector.current} · intentos {selector.probes}', flush=True)
            previous = result
            if selector.finished:
                return 0 if selector.outcome != 'click_unconfirmed' else 2
        except (KeyError, TypeError, ValueError, OSError, RuntimeError,
                subprocess.CalledProcessError) as error:
            print(f'PARADA SEGURA: {error}', file=sys.stderr)
            return 2
    print('PARADA SEGURA: el monitor terminó antes de verificar la acción', file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
