"""Conservative battle decisions from client-published actions and board state."""

from dmo_advisor import published_attackers, visible, zone_cards
from dmo_farm_strategy import FarmCombat, damage_route


def battle_decision(state, excluded=(), *, excluded_targets=()):
    """Return an attack with calibrated zone indices, or a reason to end.

    Hidden stats remain unknown. Published actions and exact field slots are
    required, with a deck-specific Assailant trade and numeric lethal planning.
    """
    if (state.get('status') != 'duel' or state.get('local_player') is not True or
            state.get('my_turn') is not True or state.get('phase') != 'BATTLE'):
        raise ValueError('Se requiere Battle Phase propia confirmada')
    actions = state.get('actions', {})
    flags = actions.get('flags', {})
    if (actions.get('status') != 'observed' or flags.get('GameStateOpen') is not True or
            any(flags.get(k) is not False for k in ('Busy', 'SelectingCard', 'ShowChainPrompt')) or
            any(actions.get('prompts', {}).get(k) != '0x0' for k in
                ('CurrentChoiceHelper', 'CurrentOptionHelper', 'CurrentConfirmationHelper'))):
        raise ValueError('El cliente no está listo para elegir un ataque')
    board = state.get('board', {})
    if board.get('errors'):
        raise ValueError('La lectura del campo está incompleta')
    own = [f for f in board.get('fields', []) if f.get('local') is True]
    rival = [f for f in board.get('fields', []) if f.get('local') is False]
    if len(own) != 1 or len(rival) != 1:
        raise ValueError('Se requieren ambos campos del duelo')
    own, rival = own[0], rival[0]
    own_entries = own.get('visual_candidates', {}).get('MonsterCards', {}).get('candidates', [])
    rival_entries = rival.get('visual_candidates', {}).get('MonsterCards', {}).get('candidates', [])
    if len(own_entries) != 5 or len(rival_entries) != 5:
        raise ValueError('Faltan posiciones visuales de monstruos')
    attackers = zone_cards(own, 'MonsterCards')
    defenders = zone_cards(rival, 'MonsterCards')
    excluded_targets = set(excluded_targets)
    published = published_attackers(actions, own, attackers)
    excluded = set(excluded)
    ready = [c for c in attackers if c['address'] in published and c['address'] not in excluded and
             visible(c) and
             c.get('location') == 'Field' and c.get('face_position_raw') == 1 and
             c.get('CanAttack') is True and c.get('AttackedThisTurn') is False and
             type(c.get('ATK')) is int and c['ATK'] >= 0]
    if not ready:
        return dict(kind='end', reason='No hay un monstruo con Attack publicado y disponible.')
    slots = {}
    for entries, cards in ((own_entries, attackers), (rival_entries, defenders)):
        indexed = {e['card']['address']: e['helper_index'] for e in entries if e.get('card')}
        if len(indexed) != len(cards) or set(indexed) != {c['address'] for c in cards}:
            raise ValueError('No se pudo asociar cada carta a una zona visual única')
        slots.update(indexed)
    if any(type(slot) is not int or not 0 <= slot < 5 for slot in slots.values()):
        raise ValueError('Índice de zona visual inválido')
    if not defenders:
        attacker = max(ready, key=lambda c: c['ATK'])
        return dict(kind='direct', attacker=attacker['address'], attacker_name=attacker['name'],
                    attacker_zone=slots[attacker['address']], expected_damage=attacker['ATK'])
    # Finish a numeric lethal line before spending Assailant on a trade.
    damage, route = damage_route(ready, defenders,
        ox=any(visible(c) and c.get('name') == 'Enraged Battle Ox' for c in attackers),
        unattackable=excluded_targets)
    rival_lp = rival.get('life_points_candidate')
    if route and type(rival_lp) is int and 0 < rival_lp <= damage:
        attacker_id, target_id = route[0]
        attacker = next(c for c in ready if c['address'] == attacker_id)
        target = next(c for c in defenders if c['address'] == target_id)
        return dict(kind='attack', attacker=attacker_id, attacker_name=attacker['name'],
                    attacker_zone=slots[attacker_id], target=target_id,
                    target_name=target['name'], target_zone=slots[target_id],
                    expected_damage=damage, intent='lethal')
    trade = FarmCombat(state).trade(ready, excluded_targets=excluded_targets)
    if trade:
        attacker, target = trade['attacker'], trade['target']
        return dict(kind='attack', attacker=attacker['address'], attacker_name=attacker['name'],
                    attacker_zone=slots[attacker['address']], target=target['address'],
                    target_name=target['name'], target_zone=slots[target['address']],
                    expected_damage=0, expected_self_damage=trade['self_damage'], intent='banish_trade')
    # Hidden stats cannot establish a favorable numeric comparison.
    pairs = []
    for attacker in ready:
        for defender in defenders:
            if defender['address'] in excluded_targets:
                continue
            if not visible(defender):
                continue
            position = defender.get('face_position_raw')
            if position not in (1, 2):
                continue
            stat = defender.get('ATK') if position == 1 else defender.get('DEF')
            if type(stat) is not int or attacker['ATK'] <= stat:
                continue
            damage = attacker['ATK'] - stat if position == 1 else 0
            pairs.append((damage, stat, attacker, defender))
    if not pairs:
        hidden = [d for d in defenders if d['address'] not in excluded_targets and
                  (d.get('face_down') is True or d.get('hidden') is True)]
        if hidden:
            attacker = max(ready, key=lambda c: c['ATK'])
            target = min(hidden, key=lambda c: slots[c['address']])
            return dict(kind='attack', attacker=attacker['address'], attacker_name=attacker['name'],
                        attacker_zone=slots[attacker['address']], target=target['address'],
                        target_name='monstruo boca abajo', target_zone=slots[target['address']],
                        expected_damage=None, uncertain_target=True)
        return dict(kind='end', reason='No hay un objetivo visible con combate favorable.')
    damage, _, attacker, defender = max(pairs, key=lambda p: (p[0], p[1], p[2]['ATK']))
    return dict(kind='attack', attacker=attacker['address'], attacker_name=attacker['name'],
                attacker_zone=slots[attacker['address']], target=defender['address'],
                target_name=defender['name'], target_zone=slots[defender['address']],
                expected_damage=damage)
