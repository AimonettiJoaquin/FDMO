"""Read client-published action candidates, without invoking game methods."""
import struct

from dmo_board import list_refs
from inspect_dmo_memory import METADATA, metadata_types, class_name, list_size

ACTION_TARGETS = ('UsableHelper',)
SPELL_PROMPT_TARGETS = ('ChoiceHelper', 'ConfirmationHelper', 'OptionHelper', 'CardOptionHelper')


def option_names():
    """This build's OptionTypes defaults use one-byte IL2CPP signed integers."""
    data = METADATA.read_bytes()
    definition = metadata_types(('OptionTypes',))['OptionTypes']
    start = struct.unpack_from('<16i8H2I', data, definition['definition_offset'])[8]
    offset, size = struct.unpack_from('<II', data, 8 + 7 * 8)
    values, values_size = struct.unpack_from('<II', data, 8 + 8 * 8)
    defaults = {field: value for field, _, value in
                struct.iter_unpack('<iii', data[offset:offset + size])}
    result = {}
    for i, field in enumerate(definition['fields']):
        if field['name'] == 'value__':
            continue
        index = defaults[start + i]
        if not 0 <= index < values_size or data[values + index] >= 128:
            raise ValueError('Formato de enum OptionTypes no soportado')
        encoded = data[values + index]
        value = (encoded >> 1) ^ -(encoded & 1)
        if value in result:
            raise ValueError('OptionTypes tiene valores duplicados')
        result[value] = field['name']
    return result


def option_values(mem, pointer):
    before = mem.read(pointer + 16, 16) if pointer else b''
    count = list_size(mem, pointer)
    if count > 32:
        raise ValueError('Demasiadas opciones')
    items = mem.u64(pointer + 16)
    if class_name(mem, items) != 'OptionTypes[]' or mem.u64(items + 16):
        raise ValueError('Se esperaba OptionTypes[]')
    block = mem.read(items + 32, count * 4)
    if mem.read(pointer + 16, 16) != before:
        raise ValueError('Las opciones cambiaron durante la lectura')
    return list(struct.unpack(f'<{count}i', block))


def selected_options(mem, pointer):
    before = mem.read(pointer + 16, 16) if pointer else b''
    count = list_size(mem, pointer)
    if count > 64:
        raise ValueError('Demasiadas opciones seleccionadas')
    items = mem.u64(pointer + 16)
    if class_name(mem, items) != 'Int32[]' or mem.u64(items + 16):
        raise ValueError('Se esperaba Int32[] para ClientOptions')
    block = mem.read(items + 32, count * 4)
    if mem.read(pointer + 16, 16) != before:
        raise ValueError('Cambió ClientOptions durante la lectura')
    return list(struct.unpack(f'<{count}i', block))


class ActionReader:
    def __init__(self, monitor, names=None):
        self.monitor = monitor
        self.names = option_names() if names is None else names
        self.describe_prompts = False

    def read_card_options(self, pointer):
        m = self.monitor
        m.check_object(pointer, 'OptionHelper', unity=False)
        result = {k: m.field(pointer, 'OptionHelper', k) for k in (
            'Title', 'Description', 'MinimumRequiredOptions', 'MaximumRequiredOptions', 'OptionHelperType')}
        collection = m.pointer(pointer, 'OptionHelper', 'CardOptionHelpers')
        refs = list_refs(m.mem, collection, limit=64)
        cards = []
        for ordinal, helper in enumerate(refs):
            m.check_object(helper, 'CardOptionHelper', unity=False)
            card = m.pointer(helper, 'CardOptionHelper', 'gameCard')
            m.check_object(card, 'GameCard', unity=False)
            name = (m.field(card, 'GameCard', 'CurrentName') or
                    m.field(card, 'GameCard', 'OriginalName'))
            if not name:
                raise ValueError('Opción de carta sin nombre')
            entry = dict(ordinal=ordinal, address=hex(card), name=name,
                         helper=hex(helper), **{k: m.field(helper, 'CardOptionHelper', k)
                            for k in ('Index', 'PlayerID', 'CardLocation')})
            entry.update({k: m.field(card, 'GameCard', k) for k in ('ATK', 'DEF', 'Level')})
            if m.pointer(helper, 'CardOptionHelper', 'gameCard') != card:
                raise ValueError('Cambió la carta de opción')
            cards.append(entry)
        selected = m.pointer(m.player, 'DuelistScript', 'ClientOptions')
        result['selected_indices'] = selected_options(m.mem, selected)
        if (m.pointer(m.player, 'DuelistScript', 'ClientOptions') != selected or
                selected_options(m.mem, selected) != result['selected_indices'] or
                m.pointer(pointer, 'OptionHelper', 'CardOptionHelpers') != collection or
                list_refs(m.mem, collection, limit=64) != refs):
            raise ValueError('Cambió el panel de opciones durante la lectura')
        result['cards'] = cards
        return result

    def read(self, manager):
        m = self.monitor
        result = dict(status='observed', semantics_verified=False, executable=False,
                      flags={}, prompts={}, helpers=[], errors=[])
        for key in ('Busy', 'SelectingCard', 'ShowChainPrompt', 'CanReact', 'DrewForTurn'):
            try:
                result['flags'][key] = m.field(m.player, 'DuelistScript', key)
            except (OSError, ValueError) as error:
                result['errors'].append(f'{key}: {error}')
        try:
            result['flags']['GameStateOpen'] = m.field(manager, 'DuelManager', 'GameStateOpen')
        except (OSError, ValueError) as error:
            result['errors'].append(f'GameStateOpen: {error}')
        for key in ('CurrentChoiceHelper', 'CurrentOptionHelper', 'CurrentConfirmationHelper'):
            try:
                result['prompts'][key] = hex(m.pointer(m.player, 'DuelistScript', key))
            except (OSError, ValueError) as error:
                result['errors'].append(f'{key}: {error}')
        if self.describe_prompts:
            result['prompt_details'] = {}
            option_pointer = int(result['prompts'].get('CurrentOptionHelper', '0x0'), 16)
            if option_pointer:
                try:
                    details = self.read_card_options(option_pointer)
                    if m.pointer(m.player, 'DuelistScript', 'CurrentOptionHelper') != option_pointer:
                        raise ValueError('El prompt de opciones cambió')
                    result['prompt_details']['CurrentOptionHelper'] = details
                except (OSError, ValueError) as error:
                    result['prompt_details']['CurrentOptionHelper'] = dict(error=str(error))
            for key, typename, keys in (
                    ('CurrentChoiceHelper', 'ChoiceHelper',
                     ('Description', 'Cancelable', 'Min', 'Max', 'UseConfirmations', 'ChoiceType', 'PurposeType')),
                    ('CurrentConfirmationHelper', 'ConfirmationHelper',
                     ('Title', 'Timer', 'ConfirmationType'))):
                pointer = int(result['prompts'].get(key, '0x0'), 16)
                if not pointer:
                    continue
                try:
                    m.check_object(pointer, typename, unity=False)
                    details = {name: m.field(pointer, typename, name) for name in keys}
                    if m.pointer(m.player, 'DuelistScript', key) != pointer:
                        raise ValueError('El prompt cambió durante la lectura')
                    result['prompt_details'][key] = details
                except (OSError, ValueError) as error:
                    result['prompt_details'][key] = dict(error=str(error))
        try:
            pointer = m.pointer(m.player, 'DuelistScript', 'CurrentUsableHelpers')
            refs = list_refs(m.mem, pointer, limit=128)
            for helper in refs:
                try:
                    m.check_object(helper, 'UsableHelper', unity=False)
                    entry = dict(address=hex(helper))
                    for key in ('CardLocation', 'FieldID', 'ID', 'CardType'):
                        entry[key] = m.field(helper, 'UsableHelper', key)
                    options = m.pointer(helper, 'UsableHelper', 'Options')
                    raw = option_values(m.mem, options)
                    if m.pointer(helper, 'UsableHelper', 'Options') != options:
                        raise ValueError('Cambió el enlace a opciones')
                    entry['options'] = [dict(value=value, name=self.names.get(value, 'UNKNOWN')) for value in raw]
                    if any(value not in self.names for value in raw):
                        result['errors'].append(f'{hex(helper)}: opción desconocida')
                    result['helpers'].append(entry)
                except (OSError, ValueError) as error:
                    result['errors'].append(f'{hex(helper)}: {error}')
            if (m.pointer(m.player, 'DuelistScript', 'CurrentUsableHelpers') != pointer or
                    list_refs(m.mem, pointer, limit=128) != refs):
                raise ValueError('Cambió la colección de acciones')
        except (OSError, ValueError) as error:
            result['helpers'] = []
            result['errors'].append(str(error))
        if result['errors']:
            result['status'] = 'partial'
        return result


def render_actions(actions):
    flags = ', '.join(f'{key}={value}' for key, value in actions['flags'].items())
    lines = ['Diagnóstico de acciones · ' + flags]
    for helper in actions['helpers']:
        options = ', '.join(o['name'] for o in helper['options']) or 'sin opciones publicadas'
        lines.append(f'  Campo {helper["FieldID"]}, ID {helper["ID"]}, '
                     f'ubicación {helper["CardLocation"]}: {options}')
    if not actions['helpers']:
        lines.append('  Sin candidatos leídos; no implica que no haya acciones legales.')
    lines.extend('  Lectura pendiente: ' + error for error in actions['errors'])
    return '\n'.join(lines)
