"""Deck-specific spell priorities from complete board and published hand actions."""

from dmo.strategy.dmo_advisor import published_hand_actions, visible, zone_cards
from dmo.strategy.dmo_farm_strategy import FarmCombat, ASSAILANT

SEARCH_SPELLS = ('Reinforcement of the Army', 'Fire Formation - Tenki', 'Terraforming')

def spell_plan(state):
    if (state.get('status') != 'duel' or state.get('local_player') is not True or
            state.get('my_turn') is not True or state.get('phase') not in ('MAIN 1', 'MAIN 2')):
        return None
    actions = state.get('actions', {})
    flags, prompts = actions.get('flags', {}), actions.get('prompts', {})
    if (actions.get('status') != 'observed' or flags.get('GameStateOpen') is not True or
            any(flags.get(k) is not False for k in ('Busy', 'SelectingCard', 'ShowChainPrompt')) or
            any(prompts.get(k) != '0x0' for k in
                ('CurrentChoiceHelper', 'CurrentOptionHelper', 'CurrentConfirmationHelper'))):
        return None
    board = state.get('board', {})
    fields = board.get('fields', [])
    own = [f for f in fields if f.get('local') is True]
    rival = [f for f in fields if f.get('local') is False]
    if len(own) != 1 or len(rival) != 1 or board.get('errors'):
        raise ValueError('Se requieren ambos campos para evaluar las mágicas')
    own, rival = own[0], rival[0]
    hand = own.get('HandCards')
    if (not isinstance(hand, list) or len(hand) != own.get('HandCardCount') or
            any(not c or c.get('unknown') or not c.get('name') or c.get('location') != 'Hand'
                for c in hand)):
        raise ValueError('La mano está incompleta para evaluar las mágicas')
    allowed = published_hand_actions(actions, own, hand, option_name='ActivateEffect', option_value=0)
    searches_pending = any(c['name'] in SEARCH_SPELLS and c['address'] in allowed for c in hand)
    ours = zone_cards(own, 'MonsterCards')
    theirs = zone_cards(rival, 'MonsterCards')
    backrow = zone_cards(rival, 'MagicCards')
    zone_cards(own, 'MagicCards')
    equip = [c for c in ours if visible(c) and type(c.get('ATK')) is int]
    field_spell = own.get('field_spell', {})
    active_field = field_spell.get('card')
    gaia_eligible = (field_spell.get('status') == 'observed' and
                     not (active_field and visible(active_field) and
                          active_field.get('name') == 'Gaia Power'))
    combat = FarmCombat(state)
    raigeki = combat.removal_reason('Raigeki') if any(
        c['name'] == 'Raigeki' and c['address'] in allowed for c in hand) else None
    smashing = combat.removal_reason('Smashing Ground') if any(
        c['name'] == 'Smashing Ground' and c['address'] in allowed for c in hand) else None
    hidden = [c for c in theirs if c.get('face_down') is True and
              c.get('face_position_raw') in (None, 0, 2)]
    nobleman_target = hidden[0] if hidden else None
    lp = rival.get('life_points_candidate')
    already_lethal = type(lp) is int and lp > 0 and combat.damage() >= lp
    # Do not heal the rival with Upstart or spend removal when battle suffices.
    # Pending Gaia must actually resolve before relying on its projected bonus.
    if already_lethal and not combat.gaia_pending:
        return None
    priorities = (
        ('Gaia Power', combat.gaia_pending and (already_lethal or
             raigeki == 'lethal' or smashing == 'lethal'), 'field', None, None,
         'Activar Gaia Power antes de ejecutar el daño letal estimado.'),
        ('Smashing Ground', smashing == 'lethal', 'destroy', None, None,
         'La remoción por mayor DEF abre daño letal estimado.'),
        ('Raigeki', raigeki == 'lethal', 'clear', None, None,
         'Limpiar el campo rival abre daño letal estimado.'),
        ('Reinforcement of the Army', True, 'search', None,
         ASSAILANT if combat.needs_assailant_search() else None,
         'Buscar el mejor guerrero entre las cartas que ofrece el cliente.'),
        ('Fire Formation - Tenki', True, 'search', None, 'Enraged Battle Ox', 'Buscar Enraged Battle Ox.'),
        ('Terraforming', True, 'search', None, 'Gaia Power', 'Buscar Gaia Power.'),
        ('Pot of Greed', True, 'draw', None, None, 'Robar dos cartas.'),
        ('Upstart Goblin', True, 'draw', None, None, 'Robar una carta.'),
        ('Graceful Charity', not searches_pending, 'draw_discard', None, None,
         'Robar tres y elegir dos descartes después de las búsquedas y otros robos.'),
        ('Gaia Power', gaia_eligible, 'field', None, None, 'Activar Gaia Power en la zona de campo.'),
        ('Nobleman of Crossout', bool(hidden), 'banish', nobleman_target, None,
         'Retirar un monstruo rival en defensa boca abajo.'),
        ('Smashing Ground', smashing == 'unbeatable', 'destroy', None, None,
         'El monstruo de mayor DEF es un obstáculo que no podemos superar en batalla.'),
        ('Raigeki', raigeki == 'unbeatable', 'clear', None, None,
         'Hay un monstruo más fuerte que nuestras opciones, incluso contando Gaia.'),
        ('Mystical Space Typhoon', bool(backrow), 'target',
         min(backrow, key=lambda c: (not visible(c), c['slot'])) if backrow else None,
         None, 'Destruir una magia/trampa rival.'),
        ('Big Bang Shot', bool(equip), 'equip',
         max(equip, key=lambda c: c['ATK']) if equip else None,
         None, 'Equipar al monstruo propio boca arriba de mayor ATK.'),
    )
    for name, eligible, effect, target, search_name, reason in priorities:
        cards = [c for c in hand if c['name'] == name and c['address'] in allowed]
        if eligible and cards:
            card = min(cards, key=lambda c: c['slot'])
            target_zone = target.get('slot') if target else None
            if target is not None and target_zone is None:
                zone = 'MonsterCards' if effect in ('banish', 'equip') else 'MagicCards'
                target_field = own if effect == 'equip' else rival
                target_zone = next(entry['helper_index'] for entry in
                    target_field['visual_candidates'][zone]['candidates']
                    if (entry.get('card') or {}).get('address') == target['address'])
            return dict(kind='spell_candidate', card=name, address=card['address'],
                        effect=effect, target_address=target['address'] if target else None,
                        target_zone=target_zone,
                        target_side=('own' if effect == 'equip' else 'rival') if target else None,
                        search_name=search_name, explanation=reason,
                        requires=['Verificar activación, zona y prompts propios de esta mágica.'])
    return None
