"""Bounded, read-only traversal of the client fields linked to one duel."""
import struct

from inspect_dmo_memory import class_name, list_size, validate_generic_class

BOARD_TARGETS = ('DuelFieldScript', 'GameCard', 'VisualHelper', 'SyncList`1', 'CardFinder')
LOCATIONS = ('Any', 'Hand', 'Field', 'Deck', 'Graveyard', 'Banished', 'ExtraDeck', 'Overlayed')


def array_refs(mem, pointer, limit=256):
    if not pointer or not class_name(mem, pointer).endswith('[]'):
        raise ValueError('Se esperaba un array de referencias')
    if mem.u64(pointer + 16):
        raise ValueError('Array multidimensional no soportado')
    size = mem.u64(pointer + 24)
    if size > limit:
        raise ValueError('Array supera el límite de lectura')
    block = mem.read(pointer + 32, size * 8)
    if mem.u64(pointer + 24) != size:
        raise ValueError('Array cambió durante la lectura')
    return list(struct.unpack(f'<{size}Q', block))


def list_refs(mem, pointer, limit=256):
    before = mem.read(pointer + 16, 16) if pointer else b''
    count = list_size(mem, pointer)
    if count > limit:
        raise ValueError('Lista supera el límite de lectura')
    items = mem.u64(pointer + 16)
    # Do not read spare capacity, which can hold obsolete references.
    capacity = mem.u64(items + 24)
    if not class_name(mem, items).endswith('[]') or mem.u64(items + 16):
        raise ValueError('Almacenamiento de lista inválido')
    if count > capacity:
        raise ValueError('Lista fuera de capacidad')
    block = mem.read(items + 32, count * 8)
    if mem.read(pointer + 16, 16) != before:
        raise ValueError('Lista cambió durante la lectura')
    return list(struct.unpack(f'<{count}Q', block))


class BoardReader:
    def __init__(self, monitor):
        self.monitor = monitor
        self.mem = monitor.mem

    def sync_refs(self, pointer):
        """Resolve SyncList<T>.objects using its instantiated runtime layout."""
        m = self.monitor
        if not pointer or class_name(self.mem, pointer) != 'SyncList`1':
            raise ValueError('Se esperaba SyncList')
        klass = self.mem.u64(pointer)
        schema = validate_generic_class(self.mem, klass, m.definitions['SyncList`1'],
                                        m.definitions['VisualHelper'], m.metadata_base)
        # Generic definitions have zero offsets in the PE. Validate FieldInfo
        # against metadata, then use the instantiated offsets from the runtime.
        expected = m.layout['classes']['SyncList`1']['fields']
        for key, spec in expected.items():
            actual = schema['fields'][key]
            if (not 16 <= actual['offset'] < 4096 or
                    any(actual[p] != spec[p] for p in ('type_code', 'attributes'))):
                raise ValueError('Layout de SyncList inválido')
        field = schema['fields']['objects']
        if field['attributes'] & 0x10 or field['type_code'] != 21:
            raise ValueError('SyncList.objects no es una colección de instancia')
        address = pointer + field['offset']
        objects = self.mem.u64(address)
        refs = list_refs(self.mem, objects, limit=32)
        if self.mem.u64(pointer) != klass or self.mem.u64(address) != objects:
            raise ValueError('SyncList cambió durante la lectura')
        return refs

    def visual_candidates(self, obj, zone, local):
        """Diagnostic alternative; helper order is not a verified field slot."""
        m = self.monitor
        key = {'MonsterCards': 'MonsterVisualHelpers', 'MagicCards': 'MagicVisualHelpers'}[zone]
        pointer = m.pointer(obj, 'DuelFieldScript', key)
        refs = self.sync_refs(pointer)
        candidates = []
        for index, helper in enumerate(refs):
            entry = dict(helper_index=index, helper_address=hex(helper))
            if not helper:
                # A null entry is valid in these fixed-position client lists.
                # It does not by itself prove that the game field is empty.
                entry.update(helper_present=False, card=None)
                candidates.append(entry)
                continue
            try:
                m.check_object(helper, 'VisualHelper', unity=False)
                card = m.pointer(helper, 'VisualHelper', 'GameCard')
                entry['card'] = self.card_snapshot(card, key, index, reveal=local)
                if m.pointer(helper, 'VisualHelper', 'GameCard') != card:
                    raise ValueError('VisualHelper cambió de carta durante la lectura')
            except (OSError, ValueError) as error:
                entry.pop('card', None)
                entry['error'] = str(error)
            candidates.append(entry)
        if (m.pointer(obj, 'DuelFieldScript', key) != pointer or
                self.sync_refs(pointer) != refs):
            raise ValueError('La colección visual cambió durante la lectura')
        return dict(source=key, verified=False, candidates=candidates)

    def field_spell(self, obj, local):
        """Read the separate field spell slot, including its client visual copy."""
        m = self.monitor
        raw = m.pointer(obj, 'DuelFieldScript', 'FieldSpell')
        helper = m.pointer(obj, 'DuelFieldScript', 'FieldSpellVisualHelper')
        card_pointer = raw
        if helper:
            m.check_object(helper, 'VisualHelper', unity=False)
            card_pointer = m.pointer(helper, 'VisualHelper', 'GameCard')
            if not card_pointer:
                raise ValueError('La magia de campo visual no tiene carta vinculada')
        card = self.card_snapshot(card_pointer, 'FieldSpell', 0, reveal=local)
        if (m.pointer(obj, 'DuelFieldScript', 'FieldSpell') != raw or
                m.pointer(obj, 'DuelFieldScript', 'FieldSpellVisualHelper') != helper or
                helper and m.pointer(helper, 'VisualHelper', 'GameCard') != card_pointer):
            raise ValueError('La magia de campo cambió durante la lectura')
        if card and card.get('unknown'):
            diagnostic = card.get('diagnostic', {})
            # Same empty placeholder signature as the ordinary magic slots;
            # require the absence of a visual card as well.
            empty = (not helper and diagnostic.get('CurrentName') == '' and
                     diagnostic.get('CardLocation') == dict(enum_raw=0) and
                     all(isinstance(diagnostic.get(k), dict) and
                         diagnostic[k].get('CardID') == 0
                         for k in ('cardFinder', 'OverrideCardfinder')))
            if not empty:
                return dict(status='unknown', card=card)
            card = None
        if card and not card.get('hidden') and card.get('location') != 'Field':
            raise ValueError('La magia de campo vinculada no está en el campo')
        return dict(status='observed', card=card, helper_address=hex(helper),
                    source='FieldSpellVisualHelper' if helper else 'FieldSpell')

    def component(self, identity, name):
        m = self.monitor
        m.check_object(identity, 'NetworkIdentity')
        components = array_refs(self.mem, m.pointer(identity, 'NetworkIdentity',
                                '<NetworkBehaviours>k__BackingField'), limit=64)
        found = []
        for obj in components:
            if obj and class_name(self.mem, obj) == name:
                m.check_object(obj, name)
                found.append(obj)
        if len(found) != 1:
            raise ValueError(f'Se esperaba un componente {name}; encontrados: {len(found)}')
        return found[0]

    def card(self, pointer, zone, slot, reveal=True):
        if not pointer:
            return None
        m = self.monitor
        m.check_object(pointer, 'GameCard', unity=False)
        facedown = m.field(pointer, 'GameCard', 'FaceDown')
        # Opponent hand is not traversed. Face-down opponent field cards only
        # describe the occupied slot; their identity is not needed for this bot.
        if not reveal and facedown:
            return dict(address=hex(pointer), zone=zone, slot=slot, hidden=True, face_down=True)
        name = m.field(pointer, 'GameCard', 'CurrentName')
        if not name:
            raise ValueError('Carta vinculada sin nombre publicado')
        location = m.field(pointer, 'GameCard', 'CardLocation')['enum_raw']
        if not 0 <= location < len(LOCATIONS):
            raise ValueError('Ubicación de carta desconocida')
        result = dict(address=hex(pointer), name=name, zone=zone, slot=slot,
                      face_down=facedown, location=LOCATIONS[location])
        for key in ('CurrentOwnerID', 'CustomID', 'FieldSlot', 'Level', 'ATK', 'DEF',
                    'CanAttack', 'AttackedThisTurn', 'TimesAttackedThisTurn'):
            result[key] = m.field(pointer, 'GameCard', key)
        result['face_position_raw'] = m.field(pointer, 'GameCard', 'FacePosition')['enum_raw']
        result['attribute_raw'] = m.field(pointer, 'GameCard', 'Attribute')
        # CanAttack is a raw flag, not a proof that an action is legal now.
        return result

    def card_snapshot(self, pointer, zone, slot, reveal=True):
        """Keep one failed slot from discarding the other cards in its zone."""
        try:
            return self.card(pointer, zone, slot, reveal)
        except (OSError, ValueError) as error:
            result = dict(address=hex(pointer), zone=zone, slot=slot,
                          unknown=True, error=str(error), diagnostic={})
            m = self.monitor
            # Diagnose only validated GameCard objects. These raw values help
            # identify placeholders; none proves that a non-null slot is empty.
            try:
                m.check_object(pointer, 'GameCard', unity=False)
                facedown = m.field(pointer, 'GameCard', 'FaceDown')
                keys = ['CardLocation', 'FacePosition', 'FieldSlot', 'CurrentOwnerID']
                if reveal or facedown is False:
                    keys += ['OriginalName', 'CurrentName', 'CustomID']
                result['diagnostic']['FaceDown'] = facedown
                result['face_down'] = facedown
                for key in keys:
                    try:
                        result['diagnostic'][key] = m.field(pointer, 'GameCard', key)
                    except (OSError, ValueError):
                        pass
                # Only inspect identity clues for our own unreadable cards.
                # A CardFinder ID is diagnostic evidence, not a recovered name.
                if reveal:
                    for key in ('cardFinder', 'OverrideCardfinder'):
                        try:
                            finder = m.pointer(pointer, 'GameCard', key)
                            if not finder:
                                result['diagnostic'][key] = None
                                continue
                            m.check_object(finder, 'CardFinder', unity=False)
                            values = dict(address=hex(finder),
                                          CardType=m.field(finder, 'CardFinder', 'CardType'),
                                          CardID=m.field(finder, 'CardFinder', 'CardID'))
                            if m.pointer(pointer, 'GameCard', key) != finder:
                                raise ValueError('CardFinder cambió durante la lectura')
                            result['diagnostic'][key] = values
                        except (OSError, ValueError) as finder_error:
                            result.setdefault('diagnostic_errors', {})[key] = str(finder_error)
            except (OSError, ValueError):
                pass
            return result

    def field(self, obj, identity, manager_identity, player_identities, own_identity):
        m = self.monitor
        manager_link = m.pointer(obj, 'DuelFieldScript', 'DuelManagerIdentity')
        if manager_link != manager_identity:
            raise ValueError('El campo apunta a otro duelo o está en transición')
        owner = m.pointer(obj, 'DuelFieldScript', 'Player')
        if owner not in player_identities:
            raise ValueError('El dueño del campo no está en los jugadores de este duelo')
        m.check_object(owner, 'NetworkIdentity')
        local = owner == own_identity
        result = dict(address=hex(obj), identity=hex(identity), owner_identity=hex(owner),
                      local=local, player_index=m.field(obj, 'DuelFieldScript', 'PlayerIndex'),
                      side=m.field(obj, 'DuelFieldScript', 'SideIndex'), errors={})
        result['generation_id'] = m.field(obj, 'DuelFieldScript', 'GenerationID')
        result['life_points_candidate'] = m.field(obj, 'DuelFieldScript', 'AlternativeLifePoints')
        result['life_points_source'] = 'DuelFieldScript.AlternativeLifePoints'
        result['life_points_verified'] = False
        for key in ('DeckCount', 'ExtraDeckCount', 'HandCardCount'):
            value = m.field(obj, 'DuelFieldScript', key)
            if not 0 <= value <= 1000:
                raise ValueError(f'Conteo incoherente: {key}')
            result[key] = value
        for zone in ('MonsterCards', 'MagicCards', 'HandCards'):
            if zone == 'HandCards' and not local:
                continue
            try:
                pointer = m.pointer(obj, 'DuelFieldScript', zone)
                refs = (list_refs(self.mem, pointer) if zone == 'HandCards'
                        else array_refs(self.mem, pointer, limit=32))
                result[zone] = [self.card_snapshot(card, zone, slot, reveal=local)
                                for slot, card in enumerate(refs)]
                failures = [card for card in result[zone] if card and card.get('unknown')]
                if failures:
                    result['errors'][zone] = '; '.join(
                        f'posición {card["slot"] + 1}: {card["error"]}' for card in failures)
            except (OSError, ValueError) as error:
                result[zone] = None  # Unknown is different from an empty zone.
                result['errors'][zone] = str(error)
        try:
            result['field_spell'] = self.field_spell(obj, local)
        except (OSError, ValueError) as error:
            result['field_spell'] = dict(status='unknown', error=str(error))
        # Keep the original field unknown while investigating the client-side
        # visual references. An empty helper list is not proof of an empty field.
        for zone in ('MonsterCards', 'MagicCards'):
            if zone in result['errors']:
                diagnostics = result.setdefault('visual_candidates', {})
                try:
                    diagnostics[zone] = self.visual_candidates(obj, zone, local)
                except (OSError, ValueError) as error:
                    diagnostics[zone] = dict(verified=False, error=str(error))
        if (m.pointer(obj, 'DuelFieldScript', 'DuelManagerIdentity') != manager_link or
                m.pointer(obj, 'DuelFieldScript', 'Player') != owner):
            raise ValueError('El campo cambió de duelo o dueño durante la lectura')
        return result

    def read(self, manager):
        m = self.monitor
        m.check_object(manager, 'DuelManager')
        manager_identity = m.pointer(manager, 'NetworkBehaviour', '<netIdentity>k__BackingField')
        m.check_object(manager_identity, 'NetworkIdentity')
        own_identity = m.pointer(m.player, 'NetworkBehaviour', '<netIdentity>k__BackingField')
        players_pointer = m.pointer(manager, 'DuelManager', 'Players')
        fields_pointer = m.pointer(manager, 'DuelManager', 'DuelFields')
        players = list_refs(self.mem, players_pointer, limit=64)
        fields = list_refs(self.mem, fields_pointer, limit=64)
        if own_identity not in players or not fields:
            raise ValueError('El jugador local o los campos todavía no están vinculados')
        result = dict(status='linked', manager=hex(manager), fields=[], errors=[])
        for identity in fields:
            try:
                obj = self.component(identity, 'DuelFieldScript')
                field = self.field(obj, identity, manager_identity, players, own_identity)
                result['fields'].append(field)
                if field['errors']:
                    result['status'] = 'partial'
            except (OSError, ValueError) as error:
                result['errors'].append(dict(identity=hex(identity), error=str(error)))
                result['status'] = 'partial'
        if not any(f['local'] for f in result['fields']):
            result['status'] = 'partial'
            result['errors'].append(dict(error='No se leyó el campo local'))
        if (m.pointer(manager, 'DuelManager', 'Players') != players_pointer or
                m.pointer(manager, 'DuelManager', 'DuelFields') != fields_pointer or
                list_refs(self.mem, players_pointer, limit=64) != players or
                list_refs(self.mem, fields_pointer, limit=64) != fields):
            raise ValueError('Las referencias del duelo cambiaron durante la lectura')
        return result


def describe_card(card):
    if card.get('unknown'):
        if card.get('face_down') is True:
            return f'carta boca abajo; identidad sin lectura ({card["error"]})'
        return f'sin lectura ({card["error"]})'
    if card.get('hidden'):
        return 'carta boca abajo'
    text = f'{card["name"]} [ATK {card["ATK"]}/DEF {card["DEF"]}]'
    return text + (' (boca abajo)' if card.get('face_down') else '')


def render_visual_candidates(diagnostic, label):
    descriptions = []
    if 'error' in diagnostic:
        return f'  {label} (memoria del cliente): sin lectura ({diagnostic["error"]})'
    for entry in diagnostic.get('candidates', []):
        prefix = f'ref. {entry["helper_index"] + 1}: '
        if 'error' in entry:
            descriptions.append(prefix + f'sin lectura ({entry["error"]})')
        elif entry.get('card') is not None:
            descriptions.append(prefix + describe_card(entry['card']))
    return (f'  {label} (memoria del cliente, por validar): ' +
            ('; '.join(descriptions) or 'sin cartas publicadas; ocupación sin confirmar'))


def render_board(board):
    lines = []
    for field in board.get('fields', []):
        side = 'Tu campo' if field['local'] else f'Campo rival {field["player_index"]}'
        lines.append(f'{side}: vida por verificar={field["life_points_candidate"]}; '
                     f'mano={field["HandCardCount"]}; mazo={field["DeckCount"]}')
        for zone, label in (('HandCards', 'Mano'), ('MonsterCards', 'Monstruos'), ('MagicCards', 'Magias/trampas')):
            if zone not in field:
                continue
            cards = field[zone]
            visual = field.get('visual_candidates', {}).get(zone)
            if visual is not None:
                lines.append(render_visual_candidates(visual, label))
                # Raw unknown slots remain in JSON. Keep any independently
                # readable direct cards in text too, when the sources differ.
                if 'candidates' in visual and (
                        not cards or all(c is None or c.get('unknown') for c in cards)):
                    continue
            if cards is None:
                lines.append(f'  {label}: sin lectura ({field["errors"].get(zone, "")})')
                continue
            descriptions = []
            for card in cards:
                if card is None:
                    continue
                descriptions.append(f'{card["slot"] + 1}: {describe_card(card)}')
            lines.append(f'  {label}: ' + ('; '.join(descriptions) or 'vacío'))
    lines.extend(f'  Tablero pendiente: {error["error"]}' for error in board.get('errors', []))
    return '\n'.join(lines)
