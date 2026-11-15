#!/usr/bin/env python3
"""Read-only IL2CPP v31 investigation for DMO; never attaches or writes memory.

Uses metadata tokens and runtime FieldInfo records to validate class layouts.
Object matches remain candidates: an uncollected object can be stale.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import struct
import sys


from dmo_settings import game_directory

GAME = game_directory()
METADATA = GAME / 'DMO_Data/il2cpp_data/Metadata/global-metadata.dat'
TARGETS = ('DuelManager', 'DuelistScript', 'DuelSideHelper', 'GameCard')
VALUES = {
    'DuelManager': ('CurrentTurnPlayerIndex', 'CurrentTurnPlayerID', 'CurrentPhase',
                    'GameStateOpen', 'FieldsLoaded', 'Players', 'DuelFields',
                    'AttacksDeclared', 'MonstersSummoned'),
    'DuelistScript': ('Dueling', 'StartingDuel', 'duelManager', 'DuelID', 'Side',
                      'NPC', 'DrewForTurn', 'CurrentDeck', 'ReadyToStartDuel'),
    'DuelSideHelper': ('LifePoints', 'Defeated', 'SideIndex', 'SideFields'),
    'GameCard': ('CurrentName', 'CurrentOwnerID', 'FieldSlot', 'CardLocation',
                 'FaceDown', 'CanAttack', 'AttackedThisTurn', 'TimesAttackedThisTurn',
                 'ATK', 'DEF', 'Level'),
}


def metadata_types(targets=TARGETS):
    data = METADATA.read_bytes()
    if struct.unpack_from('<II', data) != (0xFAB11BAF, 31):
        raise ValueError('Only the verified DMO metadata v31 layout is supported')
    def section(index):
        return struct.unpack_from('<II', data, 8 + index * 8)
    strings, strings_size = section(2)
    fields, fields_size = section(11)
    types, types_size = section(19)
    def name(index):
        if not 0 <= index < strings_size:
            raise ValueError('Invalid metadata string index')
        start = strings + index
        return data[start:data.index(b'\0', start)].decode('utf-8')
    result = {}
    for index in range(types_size // 88):
        offset = types + index * 88
        row = struct.unpack_from('<16i8H2I', data, offset)
        typename = name(row[0])
        namespace = name(row[1])
        matches = [target for target in targets if
                   (target == typename or ('::' in target and
                    target.split('::', 1)[1] == typename and
                    ('' if target.split('::', 1)[0] == 'global' else
                     target.split('::', 1)[0]) == namespace))]
        if not matches:
            continue
        members = []
        for j in range(row[18]):
            field_index = row[8] + j
            if not 0 <= field_index < fields_size // 12:
                raise ValueError('Invalid metadata field index')
            ni, ti, token = struct.unpack_from('<iiI', data, fields + field_index * 12)
            members.append(dict(name=name(ni), name_offset=strings + ni,
                                type_index=ti, token=token))
        for target in matches:
            if target in result:
                raise ValueError(f'Ambiguous metadata type: {target}; use namespace::name')
            result[target] = dict(index=index, name_offset=strings + row[0],
                                 definition_offset=offset, fields=members)
    if set(result) != set(targets):
        raise ValueError('Expected DMO classes were not found')
    return result


def native_layout(targets=TARGETS):
    """Locate IL2CPP registration in the PE file; no running process needed."""
    data = (GAME / 'GameAssembly.dll').read_bytes()
    pe = struct.unpack_from('<I', data, 60)[0]
    if data[:2] != b'MZ' or data[pe:pe + 4] != b'PE\0\0':
        raise ValueError('Not a PE file')
    count = struct.unpack_from('<H', data, pe + 6)[0]
    optional_size = struct.unpack_from('<H', data, pe + 20)[0]
    if struct.unpack_from('<H', data, pe + 24)[0] != 0x20B:
        raise ValueError('Expected a 64-bit PE')
    base = struct.unpack_from('<Q', data, pe + 48)[0]
    sections = [struct.unpack_from('<4I', data, pe + 24 + optional_size + i * 40 + 8)
                for i in range(count)]

    def file_offset(pointer):
        rva = pointer - base
        for _, va, raw_size, raw_pointer in sections:
            if va <= rva < va + raw_size:
                return raw_pointer + rva - va
        raise ValueError('PE pointer outside file-backed sections')

    metadata = METADATA.read_bytes()
    type_count = struct.unpack_from('<I', metadata, 8 + 19 * 8 + 4)[0] // 88
    needle = struct.pack('<Q', type_count)
    position = data.find(needle)
    definitions = metadata_types(targets)
    matches = []
    while position >= 0:
        if position >= 32 and data[position + 16:position + 24] == needle:
            try:
                offsets_table = file_offset(struct.unpack_from('<Q', data, position + 8)[0])
                types_table = file_offset(struct.unpack_from('<Q', data, position - 24)[0])
                classes = {}
                for name, definition in definitions.items():
                    pointer = struct.unpack_from('<Q', data, offsets_table + definition['index'] * 8)[0]
                    offsets = file_offset(pointer)
                    members = {}
                    for i, field in enumerate(definition['fields']):
                        offset = struct.unpack_from('<i', data, offsets + i * 4)[0]
                        if not -1 <= offset < 65536:
                            raise ValueError('Implausible field offset')
                        type_pointer = struct.unpack_from('<Q', data, types_table + field['type_index'] * 8)[0]
                        type_data, bits = struct.unpack_from('<QI', data, file_offset(type_pointer))
                        members[field['name']] = dict(offset=offset, type_code=(bits >> 16) & 255,
                                                     attributes=bits & 65535, type_data=type_data)
                    classes[name] = dict(type_index=definition['index'], fields=members)
                matches.append(dict(registration_file_offset=hex(position - 80), classes=classes))
            except (ValueError, struct.error):
                pass
        position = data.find(needle, position + 1)
    if len(matches) != 1:
        raise ValueError(f'Expected one validated registration, found {len(matches)}')
    return dict(binary_sha256=hashlib.sha256(data).hexdigest(),
                metadata_sha256=hashlib.sha256(metadata).hexdigest(),
                source='Static PE/metadata analysis; these are field offsets, not live object addresses.',
                **matches[0])


class Memory:
    def __init__(self, pid):
        self.pid = pid
        self.refresh_maps()
        # O_RDONLY only. No ptrace, process_vm_writev, or kernel setting changes.
        self.fd = os.open(f'/proc/{pid}/mem', os.O_RDONLY)
        try:
            self.pagemap_fd = os.open(f'/proc/{pid}/pagemap', os.O_RDONLY)
        except OSError:
            self.pagemap_fd = None
        self.page_size = os.sysconf('SC_PAGE_SIZE')

    def refresh_maps(self):
        self.maps = []
        for line in Path(f'/proc/{self.pid}/maps').read_text().splitlines():
            parts = line.split(maxsplit=5)
            start, end = (int(x, 16) for x in parts[0].split('-'))
            self.maps.append((start, end, parts[1], parts[5] if len(parts) == 6 else ''))

    def close(self):
        if self.pagemap_fd is not None:
            os.close(self.pagemap_fd)
        os.close(self.fd)

    def read(self, address, size):
        if not any(a <= address and address + size <= b and 'r' in p
                   for a, b, p, _ in self.maps):
            raise ValueError('Address outside readable mapped memory')
        result = os.pread(self.fd, size, address)
        if len(result) != size:
            raise ValueError('Short read')
        return result

    def u64(self, address):
        return struct.unpack('<Q', self.read(address, 8))[0]

    def regions(self):
        return [(a, b) for a, b, p, path in self.maps
                if p.startswith('rw') and (not path or path == '[heap]')]

    def allocated_ranges(self, start, size):
        """Skip unallocated pages in Wine's large anonymous reservations."""
        if self.pagemap_fd is None:
            yield start, size
            return
        pages = size // self.page_size
        try:
            entries = os.pread(self.pagemap_fd, pages * 8, start // self.page_size * 8)
            if len(entries) != pages * 8:
                raise ValueError('Short pagemap read')
        except (OSError, ValueError):
            yield start, size
            return
        run = None
        for index, (entry,) in enumerate(struct.iter_unpack('<Q', entries)):
            allocated = bool(entry & ((1 << 63) | (1 << 62)))  # present or swapped
            if allocated and run is None:
                run = index
            if not allocated and run is not None:
                yield start + run * self.page_size, (index - run) * self.page_size
                run = None
        if run is not None:
            yield start + run * self.page_size, (pages - run) * self.page_size

    def scan_blocks(self, preferred_addresses=()):
        """Visit blocks near live objects first, then the rest without repeats."""
        regions = self.regions()
        chunk = 4 * 1024 * 1024
        visited = set()
        for anchor in dict.fromkeys(preferred_addresses):
            for start, end in regions:
                if start <= anchor < end:
                    center = start + (anchor - start) // chunk * chunk
                    for address in (center, center - chunk, center + chunk):
                        if start <= address < end and address not in visited:
                            visited.add(address)
                            yield address, min(chunk, end - address)
                    break
        for start, end in regions:
            for address in range(start, end, chunk):
                if address not in visited:
                    yield address, min(chunk, end - address)

    def find_pointers(self, needles, max_bytes, *, preferred_addresses=()):
        scanned = 0
        self.scan_complete = True
        self.scan_stats = dict(bytes_read=0, skipped_unallocated_bytes=0, read_errors=0,
                               pagemap_available=self.pagemap_fd is not None,
                               eligible_mapped_bytes=sum(b - a for a, b in self.regions()),
                               budget_bytes=max_bytes, stop_reason='scanning')
        for address, size in self.scan_blocks(preferred_addresses):
            ranges = list(self.allocated_ranges(address, size))
            self.scan_stats['skipped_unallocated_bytes'] += size - sum(n for _, n in ranges)
            for block_address, block_size in ranges:
                if scanned + block_size > max_bytes:
                    self.scan_complete = False
                    self.scan_stats.update(stop_reason='budget', next_address=hex(block_address))
                    print('Scan budget reached; results are partial', file=sys.stderr)
                    return
                try:
                    block = self.read(block_address, block_size)
                except (OSError, ValueError):
                    self.scan_complete = False
                    self.scan_stats['read_errors'] += 1
                    continue
                scanned += block_size
                self.scan_stats['bytes_read'] = scanned
                for label, value in needles.items():
                    needle = struct.pack('<Q', value)
                    offset = block.find(needle)
                    while offset >= 0:
                        hit = block_address + offset
                        if hit % 8 == 0:
                            yield label, hit
                        offset = block.find(needle, offset + 1)
        self.scan_stats['stop_reason'] = 'completed' if self.scan_complete else 'read_errors'
        print(f'Scanned {scanned / 1024**2:.1f} MiB', file=sys.stderr)


def validate_class(mem, address, definition, metadata_base):
    # Stable prefix of Il2CppClass on 64-bit Unity IL2CPP.
    if mem.u64(address + 120) != address:
        return None
    if mem.u64(address + 104) != metadata_base + definition['definition_offset']:
        return None
    return _validate_class_fields(mem, address, definition, metadata_base)


def validate_generic_class(mem, address, definition, argument, metadata_base):
    """Validate an instantiated reference type with exactly one class argument.

    GenericClass::CreateClass leaves typeMetadataHandle null; the definition is
    reached through generic_class->type instead. No runtime functions are called.
    """
    def require(condition, reason):
        if not condition:
            raise ValueError(f'Clase genérica no validada: {reason}')

    def class_type(pointer, expected, label):
        handle, bits = struct.unpack('<QI', mem.read(pointer, 12))
        require((bits >> 16) & 255 == 18, f'{label}: no es tipo clase')
        target = metadata_base + expected['definition_offset']
        require(handle == target,
                f'{label}: metadatos {hex(handle)}, esperados {hex(target)}')

    require(mem.u64(address + 120) == address, 'autorreferencia')
    require(mem.u64(address + 16) == metadata_base + definition['name_offset'], 'nombre')
    generic = mem.u64(address + 96)
    require(bool(generic), 'falta generic_class')
    byval, bits = struct.unpack('<QI', mem.read(address + 32, 12))
    require(byval == generic and (bits >> 16) & 255 == 21, 'tipo GENERICINST')
    require(mem.u64(generic + 24) == address, 'cached_class')
    class_type(mem.u64(generic), definition, 'definición')
    instance = mem.u64(generic + 8)
    require(bool(instance), 'falta contexto de clase')
    count = struct.unpack('<I', mem.read(instance, 4))[0]
    require(count == 1, f'cantidad de argumentos {count}')
    require(mem.u64(generic + 16) == 0, 'contexto de método inesperado')
    arguments = mem.u64(instance + 8)
    class_type(mem.u64(arguments), argument, 'argumento')
    result = _validate_class_fields(mem, address, definition, metadata_base)
    require(result is not None, 'FieldInfo no coincide con nombres, tokens o clase')
    return result


def _validate_class_fields(mem, address, definition, metadata_base):
    fields_address = mem.u64(address + 128)
    if not fields_address:
        return None
    fields = {}
    for i, expected in enumerate(definition['fields']):
        raw = mem.read(fields_address + i * 32, 32)
        name_ptr, type_ptr, parent, offset, token = struct.unpack('<QQQiI', raw)
        if (name_ptr != metadata_base + expected['name_offset'] or
                parent != address or token != expected['token']):
            return None
        type_data, bits = struct.unpack('<QI', mem.read(type_ptr, 12))
        fields[expected['name']] = dict(offset=offset, type_code=(bits >> 16) & 255,
                                        attributes=bits & 65535, type_data=type_data)
    return dict(address=address, fields=fields)


def read_value(mem, address, field):
    code = field['type_code']
    if code == 2:
        value = mem.read(address, 1)[0]
        if value > 1:
            raise ValueError('Invalid boolean')
        return bool(value)
    if code in (8, 9):
        return struct.unpack('<i' if code == 8 else '<I', mem.read(address, 4))[0]
    if code == 17:  # enum: reported as raw integer until its definition is checked
        return {'enum_raw': struct.unpack('<i', mem.read(address, 4))[0]}
    if code == 14:  # Only explicitly selected name fields are decoded.
        ptr = mem.u64(address)
        if not ptr:
            return None
        length = struct.unpack('<i', mem.read(ptr + 16, 4))[0]
        if not 0 <= length <= 160:
            raise ValueError('Invalid name length')
        return mem.read(ptr + 20, length * 2).decode('utf-16-le')
    return {'pointer': hex(mem.u64(address))}


def class_name(mem, obj):
    klass = mem.u64(obj)
    pointer = mem.u64(klass + 16)
    return mem.read(pointer, 80).split(b'\0', 1)[0].decode('utf-8')


def list_size(mem, pointer):
    if not pointer or class_name(mem, pointer) != 'List`1':
        raise ValueError('Expected a managed List')
    size = struct.unpack('<i', mem.read(pointer + 24, 4))[0]
    items = mem.u64(pointer + 16)
    capacity = mem.u64(items + 24)
    if not 0 <= size <= capacity <= 65536:
        raise ValueError('Invalid managed List bounds')
    return size


def validate_candidate(mem, name, hit, values, classes):
    # Several pointers within class metadata can resemble an object header.
    if any(c['address'] <= hit < c['address'] + 512 for c in classes.values()):
        raise ValueError('Class metadata is not a game object')
    checks = {}
    if name in ('DuelManager', 'DuelistScript'):
        # UnityEngine.Object.m_CachedPtr: discarded components have a null pointer.
        native = mem.u64(hit + 16)
        if not native:
            raise ValueError('No native Unity object')
        mem.read(native, 8)
        checks['native_object'] = hex(native)
    if name == 'DuelManager':
        phase = values['CurrentPhase']['enum_raw']
        if not 0 <= phase <= 5 or not -1 <= values['CurrentTurnPlayerIndex'] <= 64:
            raise ValueError('Invalid duel phase or player index')
        for key in ('Players', 'DuelFields'):
            checks[key + '_count'] = list_size(mem, int(values[key]['pointer'], 16))
    elif name == 'DuelistScript':
        manager = int(values['duelManager']['pointer'], 16)
        if manager:
            if 'DuelManager' not in classes or mem.u64(manager) != classes['DuelManager']['address']:
                raise ValueError('Duelist manager reference has the wrong class')
            checks['manager_class_verified'] = True
        deck = int(values['CurrentDeck']['pointer'], 16)
        if deck and class_name(mem, deck) != 'DeckData':
            raise ValueError('Duelist deck reference has the wrong class')
    elif name == 'DuelSideHelper':
        checks['SideFields_count'] = list_size(mem, int(values['SideFields']['pointer'], 16))
    elif name == 'GameCard':
        if not values.get('CurrentName') or not 0 <= values['CardLocation']['enum_raw'] <= 7:
            raise ValueError('Invalid card name or location')
    return checks


def inspect(pid, budget):
    started = datetime.now(timezone.utc).isoformat()
    definitions = metadata_types()
    layout = native_layout()
    with_memory = Memory(pid)
    mem = with_memory
    try:
        metadata_base = next(a for a, _, _, path in mem.maps if path == str(METADATA))
        needles = {name: metadata_base + d['name_offset'] for name, d in definitions.items()}
        classes = {}
        for name, hit in mem.find_pointers(needles, budget):
            try:
                validated = validate_class(mem, hit - 16, definitions[name], metadata_base)
            except (ValueError, OSError):
                continue
            if validated:
                if validated['fields'] != layout['classes'][name]['fields']:
                    # Runtime type data can be relocated. Offsets, codes and
                    # attributes must still agree with the installed binary.
                    if any(any(validated['fields'][key][part] != expected[part]
                               for part in ('offset', 'type_code', 'attributes'))
                           for key, expected in layout['classes'][name]['fields'].items()):
                        continue
                classes[name] = validated
                print(f'Validated class {name}: {hit - 16:#x}', file=sys.stderr)
                if len(classes) == len(definitions):
                    mem.scan_stats['stop_reason'] = 'all_target_classes_found'
                    break
        class_scan_complete = len(classes) == len(definitions)
        class_scan_stats = mem.scan_stats.copy()
        objects = {name: [] for name in classes}
        rejected = {name: 0 for name in classes}
        rejection_reasons = {name: Counter() for name in classes}
        rejection_samples = {name: [] for name in classes}
        truncated = set()
        if classes:
            for name, hit in mem.find_pointers({n: c['address'] for n, c in classes.items()}, budget):
                if len(objects[name]) >= 100:
                    truncated.add(name)
                    continue
                try:
                    # The class structure has a self pointer; it is not an object.
                    if hit == classes[name]['address'] + 120:
                        continue
                    if mem.u64(hit + 8) != 0:  # conservative: skip monitored objects
                        continue
                    values = {}
                    for key in VALUES[name]:
                        f = classes[name]['fields'][key]
                        if f['offset'] < 16 or f['attributes'] & 0x10:
                            continue
                        values[key] = read_value(mem, hit + f['offset'], f)
                    checks = validate_candidate(mem, name, hit, values, classes)
                    objects[name].append(dict(address=hex(hit), values=values, checks=checks))
                except (OSError, ValueError, UnicodeError) as error:
                    rejected[name] += 1
                    reason = str(error)
                    rejection_reasons[name][reason] += 1
                    if len(rejection_samples[name]) < 8:
                        rejection_samples[name].append(dict(address=hex(hit), reason=reason))
                    continue
        linked = []
        for obj in objects.get('DuelistScript', []):
            values = obj['values']
            if values['Dueling'] and obj['checks'].get('manager_class_verified'):
                linked.append(dict(duelist=obj['address'], manager=values['duelManager']['pointer'],
                                   npc=values['NPC'], side=values['Side']))
        return dict(pid=pid, metadata_base=hex(metadata_base),
                    started_at=started, finished_at=datetime.now(timezone.utc).isoformat(),
                    binary_sha256=layout['binary_sha256'],
                    class_scan_complete=class_scan_complete,
                    object_scan_complete=mem.scan_complete,
                    class_scan_stats=class_scan_stats, object_scan_stats=mem.scan_stats,
                    rejected_candidates=rejected, dueling_links=linked,
                    rejection_reasons=rejection_reasons, rejection_samples=rejection_samples,
                    candidate_limits_reached=sorted(truncated),
                    warning='Object matches may be stale; not yet validated against the active duel.',
                    classes={n: dict(address=hex(c['address']), fields=c['fields']) for n, c in classes.items()},
                    candidates=objects)
    finally:
        if mem.pagemap_fd is not None:
            os.close(mem.pagemap_fd)
        os.close(mem.fd)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int)
    parser.add_argument('--metadata-only', action='store_true')
    parser.add_argument('--layout-only', action='store_true')
    parser.add_argument('--max-scan-mib', type=int, default=16384,
                        help='Read budget per pass in MiB (default: 16384).')
    args = parser.parse_args()
    if args.max_scan_mib <= 0:
        parser.error('--max-scan-mib must be positive')
    try:
        if args.layout_only:
            print(json.dumps(native_layout(), indent=2))
        elif args.metadata_only:
            print(json.dumps(metadata_types(), indent=2))
        elif args.pid:
            print(json.dumps(inspect(args.pid, args.max_scan_mib * 1024**2), indent=2))
        else:
            parser.error('--pid, --metadata-only or --layout-only is required')
    except (OSError, ValueError, StopIteration) as error:
        print(f'Inspection failed: {error}', file=sys.stderr)
        sys.exit(2)
