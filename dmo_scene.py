"""Bounded discovery of interactive objects linked to our hand; no clicks."""
import time
from dmo_board import list_refs
from inspect_dmo_memory import class_name

SCENE_TARGETS = ('SelectableDuelElement', 'CardHover', 'ButtonHover', 'global::Misc')


class SceneReader:
    def __init__(self, monitor):
        self.monitor = monitor
        self.element_hints = ()
        self.recent_hand_elements = ()
        self.transform_reader = None
        self.native_object_reader = None
        self.native_object_reader_error = None

    def load_hints(self, report, pid, generation=None, binary_sha256=None):
        """Addresses are hints only; never reuse captured hover/name/position data."""
        self.element_hints = ()
        self.recent_hand_elements = ()
        try:
            if report['pid'] != pid:
                raise ValueError('El informe de escena pertenece a otro PID')
            for key, expected in (('process_start', generation), ('binary_sha256', binary_sha256)):
                if key in report and report[key] != expected:
                    raise ValueError(f'Informe de escena incompatible: {key}')
            elements = report['scene']['hand_elements']
            if not isinstance(elements, list) or len(elements) > 4096:
                raise ValueError('Cantidad de pistas de escena inválida')
            hints = []
            for item in elements:
                values = tuple(int(item[key], 16) for key in
                               ('element', 'card_address', 'native_element'))
                if any(not 0 < value < 2**64 or value % 8 for value in values):
                    raise ValueError('Dirección de escena inválida')
                hints.append(values)
            self.element_hints = tuple(dict.fromkeys(hints))
        except (KeyError, TypeError) as error:
            raise ValueError('Formato de informe de escena inválido') from error

    @staticmethod
    def hand_context(state):
        if state.get('status') != 'duel':
            raise ValueError('El escaneo de escena requiere un duelo estable')
        local = [f for f in state.get('board', {}).get('fields', []) if f.get('local') is True]
        if len(local) != 1:
            raise ValueError('Falta un único campo local')
        field = local[0]
        cards = field.get('HandCards')
        if (not cards or len(cards) != field['HandCardCount'] or
                any(not c or c.get('unknown') or not c.get('name') for c in cards)):
            raise ValueError('El escaneo requiere una mano conocida y no vacía')
        hand = {int(c['address'], 16): c for c in cards}
        if len(hand) != len(cards):
            raise ValueError('Referencias repetidas en la mano')
        context = (state['player'], state['manager'], field['address'],
                   field['owner_identity'], field.get('generation_id'), tuple(hand))
        return context, hand

    def discovery_anchors(self, state):
        """Nearby live UI objects are search hints, never card associations."""
        m = self.monitor
        local = next(f for f in state['board']['fields'] if f.get('local') is True)
        # Recent UI allocations are more useful than the oldest links in a
        # long session. Keep them ahead of world objects and historic hints.
        anchors = [int(e['element'], 16) for e in
                   state.get('scene', {}).get('hand_elements', [])]
        # A just-played card may no longer appear in the current hand. Keep
        # its recently validated UI allocation ahead of old cache entries.
        anchors.extend(reversed(self.recent_hand_elements))
        anchors.extend(obj for obj, _, _ in reversed(self.element_hints[-32:]))
        for owner, typename, key in (
                (int(local['address'], 16), 'DuelFieldScript', 'DuelElements'),
                (m.player, 'DuelistScript', 'TargetableElements')):
            try:
                refs = list_refs(m.mem, m.pointer(owner, typename, key), limit=128)
                for obj in refs:
                    try:
                        m.check_object(obj, 'SelectableDuelElement')
                        anchors.append(obj)
                    except (OSError, ValueError):
                        continue
            except (OSError, ValueError):
                continue
        anchors.extend(int(c['address'], 16) for c in local['HandCards'])
        return tuple(dict.fromkeys(anchors))[:64]

    def discover_hand_elements(self, state, budget, max_hits=4096, *, stop_when_linked=False,
                               target_card=None):
        """One bounded scan for gameCard pointers, outside snapshot polling.

        A matching pointer is not an object: validate the candidate class,
        native component and current hand again before returning links.
        """
        if budget <= 0 or max_hits <= 0:
            raise ValueError('El presupuesto y el límite deben ser positivos')
        m = self.monitor
        context, hand = self.hand_context(state)
        wanted = {int(target_card, 16)} if target_card is not None else set(hand)
        if not wanted <= set(hand):
            raise ValueError('La carta buscada no pertenece a la mano actual')
        field = m.layout['classes']['SelectableDuelElement']['fields']['gameCard']
        offset = field['offset']
        if field['attributes'] & 0x10 or field['type_code'] != 18 or offset < 16:
            raise ValueError('gameCard no es un campo de instancia válido')
        result = dict(status='investigating', executable=False, screen_coordinates_available=False,
                      hand_elements=[], pointer_hits=0, rejected_candidates=0, errors=[])
        candidates = set()
        limited = False
        linked = set()
        early_stop = False
        started = time.monotonic()
        search_cards = wanted if stop_when_linked and target_card is not None else set(hand)
        hits = m.mem.find_pointers({hex(card): card for card in search_cards}, budget,
                                  preferred_addresses=self.discovery_anchors(state))
        try:
            for _, hit in hits:
                result['pointer_hits'] += 1
                obj = hit - offset
                try:
                    item = self.element(obj, hand)
                    if item is not None:
                        candidates.add(obj)
                        if (item.get('world') is False and
                                item.get('hover', {}).get('hoverable') is True):
                            linked.add(int(item['card_address'], 16))
                    else:
                        result['rejected_candidates'] += 1
                except (OSError, ValueError):
                    result['rejected_candidates'] += 1
                if stop_when_linked and wanted <= linked:
                    early_stop = True
                    break
                if result['pointer_hits'] >= max_hits:
                    limited = True
                    break
        finally:
            hits.close()
        result['scan'] = dict(m.mem.scan_stats)
        result['scan']['elapsed_seconds'] = round(time.monotonic() - started, 3)
        result['scan']['complete'] = m.mem.scan_complete and not limited and not early_stop
        if early_stop:
            result['scan']['stop_reason'] = 'hand_linked'
        if limited:
            result['scan']['stop_reason'] = 'pointer_hit_limit'
        # The scan can take a minute: never retain links to a previous hand/duel.
        try:
            after_context, after_hand = self.hand_context(m.snapshot())
            if after_context != context:
                raise ValueError('La mano o el duelo cambió durante el escaneo')
            for obj in sorted(candidates):
                try:
                    item = self.element(obj, after_hand)
                    if item is not None:
                        item['source'] = 'memory_gameCard_reference'
                        result['hand_elements'].append(item)
                except (OSError, ValueError):
                    result['rejected_candidates'] += 1
            final_context, _ = self.hand_context(m.snapshot())
            if final_context != context:
                raise ValueError('La mano o el duelo cambió al validar los objetos')
        except (OSError, ValueError) as error:
            result['status'] = 'context_changed'
            result['hand_elements'] = []
            result['errors'].append(str(error))
            return result
        if result['hand_elements']:
            result['status'] = 'hand_objects_linked'
        return result

    def preview_references(self):
        """Player UI sources; distinct from Misc's world hover references."""
        m = self.monitor
        m.check_object(m.player, 'DuelistScript')
        keys = ('CurrentDuelElement', 'CurrentCardPreviewSource')
        refs = {key: m.pointer(m.player, 'DuelistScript', key) for key in keys}
        result = dict(card_link_verified=False, objects={})
        for key, obj in refs.items():
            item = dict(address=hex(obj), status='null')
            if obj:
                item.update(status='unvalidated_candidate', class_name=class_name(m.mem, obj),
                            native_pointer=hex(m.mem.u64(obj + 16)))
            result['objects'][key] = item
        if any(m.pointer(m.player, 'DuelistScript', key) != obj for key, obj in refs.items()):
            raise ValueError('Las referencias de vista previa cambiaron durante la lectura')
        return result

    def cursor_references(self):
        """Read only allowlisted UI references, never account fields on Misc.

        GameObject addresses are diagnostic candidates, not card associations.
        """
        m = self.monitor
        misc = m.pointer(m.player, 'DuelistScript', 'misc')
        m.check_object(misc, 'global::Misc')
        if m.pointer(misc, 'global::Misc', 'duelistScript') != m.player:
            raise ValueError('Misc no apunta al jugador local')
        keys = ('HighlightedObject', 'HoveredBattleObject', 'HoveredSlot')
        refs = {key: m.pointer(misc, 'global::Misc', key) for key in keys}
        result = dict(misc=hex(misc), card_link_verified=False, objects={})
        for key, obj in refs.items():
            item = dict(address=hex(obj), status='null')
            if obj:
                item.update(status='unvalidated_candidate', class_name=class_name(m.mem, obj),
                            native_pointer=hex(m.mem.u64(obj + 16)))
            result['objects'][key] = item
        if (m.pointer(m.player, 'DuelistScript', 'misc') != misc or
                m.pointer(misc, 'global::Misc', 'duelistScript') != m.player or
                any(m.pointer(misc, 'global::Misc', key) != obj for key, obj in refs.items())):
            raise ValueError('Las referencias bajo el cursor cambiaron durante la lectura')
        return result

    def element(self, obj, hand):
        m = self.monitor
        m.check_object(obj, 'SelectableDuelElement')
        card = m.pointer(obj, 'SelectableDuelElement', 'gameCard')
        if card not in hand:
            return None
        m.check_object(card, 'GameCard', unity=False)
        result = dict(element=hex(obj), native_element=hex(m.mem.u64(obj + 16)),
                      card_address=hex(card), name=hand[card].get('name'), hand_slot=hand[card]['slot'],
                      screen_position=None, position_status='not_resolved',
                      world=m.field(obj, 'SelectableDuelElement', 'World'),
                      selected=m.field(obj, 'SelectableDuelElement', 'Selected'),
                      element_type=m.field(obj, 'SelectableDuelElement', 'ElementType'))
        hover = m.pointer(obj, 'SelectableDuelElement', 'cardHover')
        result['card_hover'] = hex(hover)
        if hover:
            try:
                m.check_object(hover, 'CardHover')
                parent = m.mem.u64(m.mem.u64(hover) + 88)
                m.accept_class('ButtonHover', parent)
                result['hover'] = dict(native_component=hex(m.mem.u64(hover + 16)),
                    hovered=m.field(hover, 'ButtonHover', 'Hovered'),
                    hoverable=m.field(hover, 'CardHover', 'Hoverable'),
                    target_image=hex(m.pointer(hover, 'ButtonHover', 'TargetImage')),
                    target_canvas_group=hex(m.pointer(hover, 'ButtonHover', 'TargetCanvasGroup')))
            except (OSError, ValueError) as error:
                result['hover_error'] = str(error)
        if (m.pointer(obj, 'SelectableDuelElement', 'gameCard') != card or
                m.pointer(obj, 'SelectableDuelElement', 'cardHover') != hover):
            raise ValueError('El elemento cambió durante la lectura')
        if result['world'] is True and self.native_object_reader is not None:
            try:
                native = int(result['native_element'], 16)
                link = self.native_object_reader.component_game_object(native)
                if (m.mem.u64(obj + 16) != native or
                        m.pointer(obj, 'SelectableDuelElement', 'gameCard') != card):
                    raise ValueError('El elemento cambió al leer su GameObject')
                result['game_object_link'] = link
            except (OSError, ValueError) as error:
                result['game_object_link_error'] = str(error)
        elif result['world'] is True and self.native_object_reader_error:
            result['game_object_link_error'] = self.native_object_reader_error
        if self.transform_reader is not None:
            try:
                native = int(result['native_element'], 16)
                result['transform'] = self.transform_reader.read(native)
                if (m.mem.u64(obj + 16) != native or
                        m.pointer(obj, 'SelectableDuelElement', 'gameCard') != card):
                    raise ValueError('La carta o su componente cambió al leer Transform')
            except (OSError, ValueError) as error:
                result.pop('transform', None)
                result['transform_error'] = str(error)
        return result

    def read(self, board):
        m = self.monitor
        result = dict(status='investigating', executable=False, screen_coordinates_available=False,
                      hand_elements=[], field_elements=[], collections=[], errors=[])
        try:
            result['preview_references'] = self.preview_references()
        except (OSError, ValueError) as error:
            result['errors'].append(f'Vista previa: {error}')
        try:
            result['cursor_references'] = self.cursor_references()
        except (OSError, ValueError) as error:
            result['errors'].append(f'Cursor: {error}')
        local = [f for f in board.get('fields', []) if f.get('local') is True]
        if len(local) != 1:
            raise ValueError('Falta un único campo local')
        field = local[0]
        cards = field.get('HandCards')
        if cards is None or len(cards) != field['HandCardCount'] or any(
                not c or c.get('unknown') or not c.get('name') for c in cards):
            raise ValueError('La mano no está lista para vincular objetos')
        hand = {int(c['address'], 16): c for c in cards}
        if len(hand) != len(cards):
            raise ValueError('Referencias repetidas en la mano')
        field_obj = int(field['address'], 16)
        m.check_object(field_obj, 'DuelFieldScript')
        sources = [(int(f['address'], 16), 'DuelFieldScript', 'DuelElements', f)
                   for f in board.get('fields', [])]
        sources.append((m.player, 'DuelistScript', 'TargetableElements', None))
        seen = set()
        for owner, typename, key, source_field in sources:
            source = f'{typename}.{key}'
            try:
                field_cards, visual_slots = {}, {}
                if source_field is not None:
                    m.check_object(owner, 'DuelFieldScript')
                    for zone in ('MonsterCards', 'MagicCards'):
                        entries = source_field.get('visual_candidates', {}).get(zone, {}).get('candidates', [])
                        for entry in entries:
                            card = entry.get('card')
                            if (not card or card.get('unknown') or
                                    not (card.get('name') or card.get('hidden'))):
                                continue
                            address = int(card['address'], 16)
                            if address in field_cards:
                                raise ValueError('Carta visual repetida entre zonas del campo')
                            field_cards[address] = card
                            visual_slots[address] = (zone, entry['helper_index'])
                pointer = m.pointer(owner, typename, key)
                refs = list_refs(m.mem, pointer, limit=128)
                matches = []
                field_matches = []
                errors = []
                for element in dict.fromkeys(refs):
                    if element == 0:
                        continue
                    try:
                        match = self.element(element, hand)
                        if match is not None:
                            match['source'] = source
                            matches.append(match)
                        if source == 'DuelFieldScript.DuelElements' and field_cards:
                            field_match = self.element(element, field_cards)
                            if field_match is not None:
                                address = int(field_match['card_address'], 16)
                                field_match['zone'], field_match['visual_slot'] = visual_slots[address]
                                field_match['local'] = source_field['local']
                                field_match['field_address'] = source_field['address']
                                field_match.pop('hand_slot', None)
                                field_match['source'] = source
                                field_matches.append(field_match)
                    except (OSError, ValueError) as error:
                        errors.append(f'{source} {hex(element)}: {error}')
                if m.pointer(owner, typename, key) != pointer or list_refs(m.mem, pointer, limit=128) != refs:
                    raise ValueError('La colección de elementos cambió durante la lectura')
                result['field_elements'].extend(field_matches)
                result['collections'].append(dict(source=source, count=len(refs), hand_matches=len(matches)))
                result['errors'].extend(errors)
                for match in matches:
                    if match['element'] not in seen:
                        result['hand_elements'].append(match)
                        seen.add(match['element'])
            except (OSError, ValueError) as error:
                result['errors'].append(f'{source}: {error}')
        if self.element_hints:
            accepted = 0
            for obj, expected_card, expected_native in self.element_hints:
                if expected_card not in hand:
                    continue
                try:
                    match = self.element(obj, hand)
                    if (match is None or int(match['card_address'], 16) != expected_card or
                            int(match['native_element'], 16) != expected_native):
                        continue
                    accepted += 1
                    if match['element'] not in seen:
                        match['source'] = 'revalidated_scene_report'
                        result['hand_elements'].append(match)
                        seen.add(match['element'])
                except (OSError, ValueError):
                    continue
            result['report_hints'] = dict(total=len(self.element_hints), accepted=accepted,
                                         skipped=len(self.element_hints) - accepted)
        # Do not associate UI elements with a hand that moved during traversal.
        current = list_refs(m.mem, m.pointer(field_obj, 'DuelFieldScript', 'HandCards'))
        if current != [int(c['address'], 16) for c in cards]:
            raise ValueError('La mano cambió durante el recorrido de elementos')
        recent = [int(e['element'], 16) for e in result['hand_elements'] if e.get('world') is False]
        if recent:
            self.recent_hand_elements = tuple(
                obj for obj in self.recent_hand_elements if obj not in recent) + tuple(dict.fromkeys(recent))
            self.recent_hand_elements = self.recent_hand_elements[-32:]
        if result['hand_elements']:
            result['status'] = 'hand_objects_linked'
        return result


def render_scene(scene):
    lines = ['Escena · coordenadas de pantalla todavía sin resolver']
    for item in scene['hand_elements']:
        hovered = item.get('hover', {}).get('hovered')
        suffix = ' · cursor encima' if hovered is True else ''
        lines.append(f'  Mano {item["hand_slot"] + 1}: {item["name"]} → elemento {item["element"]}{suffix}')
        if 'transform' in item:
            lines.append(f'    Posición local candidata: {item["transform"]["local_position"]} (no píxeles)')
        if 'transform_error' in item:
            lines.append('    Transform: ' + item['transform_error'])
    if not scene['hand_elements']:
        lines.append('  No se encontraron elementos vinculados a la mano en las colecciones consultadas.')
    lines.extend('  Diagnóstico: ' + error for error in scene['errors'])
    return '\n'.join(lines)
