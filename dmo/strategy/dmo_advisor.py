"""Experimental suggestions from observed state. No game input or legal-action API."""
import argparse
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path


def zone_cards(field, zone):
    visual = field.get('visual_candidates', {}).get(zone)
    if visual is None or 'error' in visual:
        raise ValueError('Falta lectura de una zona del campo')
    if not 1 <= len(visual['candidates']) <= 32:
        raise ValueError('Colección visual incompleta')
    cards = []
    for index, entry in enumerate(visual['candidates']):
        if 'error' in entry or entry.get('helper_index') != index:
            raise ValueError('Hay referencias del campo sin lectura')
        card = entry.get('card')
        if card is None:
            if entry.get('helper_address') != '0x0':
                raise ValueError('Hay una referencia sin carta')
            continue
        if card.get('unknown') and not card.get('face_down'):
            raise ValueError('Hay una carta cuyo estado no se conoce')
        cards.append(card)
    return cards


def visible(card):
    return (not card.get('unknown') and not card.get('hidden') and
            card.get('face_down') is False and bool(card.get('name')))


def published_hand_actions(actions, own, hand, *, option_name, option_value):
    """Conservative hand-index mapping observed in the client capture.

    Full coverage is required: stale lists can survive hand changes and turns.
    A match is a filter for human suggestions, not proof of action legality.
    """
    if actions['flags'].get('GameStateOpen') is not True:
        raise ValueError('No se confirmó un estado de juego abierto')
    helpers = [h for h in actions['helpers'] if h.get('CardLocation', {}).get('enum_raw') == 1
               and h.get('FieldID') == own['player_index']]
    by_index = {}
    for helper in helpers:
        index = helper['ID']
        if type(index) is not int or index in by_index:
            raise ValueError('Identificadores de acciones duplicados o inválidos')
        by_index[index] = helper
    if set(by_index) != set(range(len(hand))) or any(c.get('slot') != i for i, c in enumerate(hand)):
        raise ValueError('Las opciones publicadas no coinciden con toda la mano')
    return {card['address'] for i, card in enumerate(hand)
            if any(o.get('value') == option_value and o.get('name') == option_name
                   for o in by_index[i]['options'])}


def published_summons(actions, own, hand):
    return published_hand_actions(actions, own, hand, option_name='Summon', option_value=2)


def published_attackers(actions, own, cards):
    """Match field actions to raw FieldSlot, never to the visual helper index."""
    if actions['flags'].get('GameStateOpen') is not True:
        raise ValueError('No se confirmó un estado de juego abierto')
    helpers = {}
    for helper in actions['helpers']:
        if (helper.get('CardLocation', {}).get('enum_raw') != 2 or
                helper.get('FieldID') != own['player_index'] or
                helper.get('CardType', {}).get('enum_raw') != 1):
            continue
        slot = helper['ID']
        if type(slot) is not int or slot < 0 or slot in helpers:
            raise ValueError('Identificadores de ataque ambiguos')
        helpers[slot] = helper
    slots = [card.get('FieldSlot') for card in cards]
    if any(type(slot) is not int or slot < 0 for slot in slots) or len(set(slots)) != len(slots):
        raise ValueError('Posiciones de monstruos ambiguas')
    return {card['address'] for card in cards
            if any(o.get('value') == 5 and o.get('name') == 'Attack'
                   for o in helpers.get(card['FieldSlot'], {}).get('options', []))}


def summon_attack(card, enemy_count):
    """Estimate Panda's on-field attack without changing the observed hand."""
    attack = card['ATK']
    if card.get('name') == 'Gyaku-Gire Panda':
        attack += 500 * enemy_count
    return attack


def suggestions(state, *, spells=False):
    """Rank published actions, including Panda's known attack modifier."""
    actions = state.get('actions')
    if actions is not None:
        if actions.get('status') != 'observed':
            raise ValueError('El diagnóstico de acciones está incompleto')
        flags = actions['flags']
        if flags.get('GameStateOpen') is False or any(
                flags.get(key) is True for key in ('Busy', 'SelectingCard', 'ShowChainPrompt')):
            return dict(status='wait', executable=False, options=[],
                        reason='El cliente indica estado ocupado, selección, cadena o estado de juego cerrado.')
    board = state.get('board', {})
    fields = board.get('fields', [])
    local = [f for f in fields if f.get('local') is True]
    rival = [f for f in fields if f.get('local') is False]
    if len(local) != 1 or len(rival) != 1 or board.get('errors'):
        raise ValueError('Se requieren ambos campos del duelo')
    own, other = local[0], rival[0]
    ours = zone_cards(own, 'MonsterCards')
    theirs = zone_cards(other, 'MonsterCards')
    # Read both back rows even though we do not identify hidden opponents.
    zone_cards(own, 'MagicCards')
    backrow = zone_cards(other, 'MagicCards')
    options = []
    if state['phase'] in ('MAIN 1', 'MAIN 2'):
        if spells:
            from dmo.strategy.dmo_spells import spell_plan
            plan = spell_plan(state)
            if plan is not None:
                options.append(plan)
        hand = own.get('HandCards')
        if hand is None or len(hand) != own.get('HandCardCount') or any(
                not c or c.get('unknown') or not c.get('name') or c.get('location') != 'Hand' for c in hand):
            raise ValueError('La mano está incompleta')
        candidates = [c for c in hand if isinstance(c.get('Level'), int) and
                      1 <= c['Level'] <= 4 and isinstance(c.get('ATK'), int)]
        if actions is not None:
            allowed = published_summons(actions, own, hand)
            candidates = [c for c in candidates if c['address'] in allowed]
            if not candidates and not options:
                return dict(status='no_suggestion', executable=False, options=[],
                            reason='El cliente no publica Summon para los candidatos de nivel 1–4; no sugiero otra invocación.')
        if candidates:
            from dmo.strategy.dmo_farm_strategy import FarmCombat
            combat = FarmCombat(state)
            trade = combat.trade()
            assailant = trade['attacker'] if trade and trade['attacker'] in candidates else None
            card = assailant or max(candidates, key=lambda c: combat.attack(c, hand=True))
            estimated = combat.attack(card, hand=True)
            options.append(dict(kind='summon_candidate', card=card['name'], address=card['address'],
                estimated_attack=estimated,
                intent='banish_trade' if assailant else 'battle',
                explanation=(f'Invocar D.D. Assailant para retirar a {trade["target"]["name"]} por batalla.'
                             if assailant else
                             f'Considerar {card["name"]}: mayor ATK estimado al invocar entre los candidatos de nivel 1–4 ({estimated}).') +
                            (f' Panda suma 500 por cada uno de los {len(theirs)} monstruos rivales.'
                             if card['name'] == 'Gyaku-Gire Panda' else '') +
                            (' Se incluye el bono de Gaia Power para TIERRA.'
                             if combat.gaia_active or combat.gaia_pending else '') +
                            (' El cliente publica Summon para esta posición de la mano.' if actions is not None else ''),
                requires=['Confirmar invocación normal disponible, espacio y condiciones particulares de la carta.']))
    elif state['phase'] == 'BATTLE':
        attackers = [c for c in ours if visible(c) and c.get('location') == 'Field' and
                     c.get('CanAttack') is True and c.get('AttackedThisTurn') is False and
                     isinstance(c.get('ATK'), int)]
        if actions is not None:
            published = published_attackers(actions, own, attackers)
            attackers = [c for c in attackers if c['address'] in published]
            if not attackers:
                return dict(status='no_suggestion', executable=False, options=[],
                            reason='No hay candidatos con Attack publicado para su FieldSlot; no sugiero otro ataque.')
        defenders = [c for c in theirs if visible(c) and c.get('location') == 'Field' and
                     isinstance(c.get('ATK'), int) and isinstance(c.get('DEF'), int)]
        pairs = [(a['ATK'] - max(d['ATK'], d['DEF']), a, d)
                 for a in attackers for d in defenders if a['ATK'] > max(d['ATK'], d['DEF'])]
        if pairs:
            margin, attacker, defender = max(pairs, key=lambda item: item[0])
            requirements = ['Confirmar posición de ataque, objetivo permitido y efectos; no es una estimación de daño.']
            if backrow:
                requirements.append('El rival tiene referencias de magias/trampas; sus efectos pueden cambiar el resultado.')
            options.append(dict(kind='battle_comparison', card=attacker['name'], target=defender['name'],
                address=attacker['address'], target_address=defender['address'], numerical_margin=margin,
                explanation=f'Comparar ataque de {attacker["name"]} a {defender["name"]}: '
                            f'ATK {attacker["ATK"]} supera ATK {defender["ATK"]} y DEF {defender["DEF"]}.',
                requires=requirements))
    return dict(status='suggestions' if options else 'no_suggestion', executable=False, options=options,
                reason='' if options else 'No hay una comparación suficiente con las reglas actuales; no implica que debas terminar el turno.')


class Advisor:
    def __init__(self, *, spells=False):
        self.spells = spells
        self.previous = None
        self.previous_time = None

    def observe(self, state, now):
        if (state.get('status') != 'duel' or state.get('my_turn') is not True or
                state.get('local_player') is not True):
            self.previous = self.previous_time = None
            return dict(status='wait', executable=False, options=[], reason='Esperar un turno propio confirmado.')
        current = {key: deepcopy(state.get(key)) for key in
                   ('player', 'manager', 'phase', 'turn_id', 'own_id', 'board', 'actions')}
        stable = (current == self.previous and self.previous_time is not None and
                  0 < now - self.previous_time <= 5)
        # Full snapshots can take just over 3 seconds in a long game session.
        # This matching-snapshot window does not change require_recent's
        # separate 3-second limit on the current sample before an input.
        self.previous, self.previous_time = current, now
        if not stable:
            return dict(status='wait', executable=False, options=[], reason='Esperar otra muestra coincidente del estado.')
        try:
            return suggestions(state, spells=self.spells)
        except (KeyError, TypeError, ValueError) as error:
            return dict(status='insufficient_data', executable=False, options=[], reason=str(error))


def render_advice(advice):
    lines = []
    for option in advice.get('options', []):
        lines.append('Sugerencia experimental · ' + option['explanation'])
        lines.extend('  ' + requirement for requirement in option['requires'])
    return '\n'.join(lines) or 'Asesor · ' + advice['reason']


def main():
    parser = argparse.ArgumentParser(description='Evaluar sugerencias sobre una captura JSONL, sin acceder al juego.')
    parser.add_argument('report', type=Path)
    args = parser.parse_args()
    advisor, previous = Advisor(), None
    with args.report.open() as stream:
        for line in stream:
            state = json.loads(line)
            advice = advisor.observe(state, datetime.fromisoformat(state['timestamp']).timestamp())
            if advice != previous:
                print(state['timestamp'], render_advice(advice))
            previous = advice


if __name__ == '__main__':
    main()
