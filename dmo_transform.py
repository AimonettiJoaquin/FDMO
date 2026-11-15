"""Experimental native Transform reader for one inspected UnityPlayer build.

Only reads memory; never calls Unity's getters (which may synchronize jobs or
update layout). Coordinates are in Unity space, never click destinations.
Static evidence and limitations are recorded in MEMORY_INVESTIGATION.md.
"""
import hashlib
import math
import struct

from inspect_dmo_memory import GAME

UNITY_PLAYER = GAME / 'UnityPlayer.dll'
UNITY_SHA256 = 'ee9a2892f56e51ed4ed4a86d0d7223bf13c98c352e1339fa3c9ff4bd88a8515a'


def verify_engine(path=UNITY_PLAYER):
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != UNITY_SHA256:
        raise ValueError('UnityPlayer.dll cambió: el layout nativo debe investigarse de nuevo')
    return data


def engine_sections(mem, data):
    """Wine maps the PE header by filename, but its sections anonymously.

    Match the pinned header, permitting only ImageBase to equal the mapped
    base (Wine's relocation adjustment). Every other byte must still match.
    Anonymous readable memory alone is never sufficient.
    """
    pe = struct.unpack_from('<I', data, 60)[0]
    if data[:2] != b'MZ' or data[pe:pe+4] != b'PE\0\0':
        raise ValueError('Cabecera PE de UnityPlayer inválida')
    count = struct.unpack_from('<H', data, pe + 6)[0]
    optional = struct.unpack_from('<H', data, pe + 20)[0]
    header_size = struct.unpack_from('<I', data, pe + 84)[0]
    if not 0 < count <= 96 or not 0 < header_size <= 65536:
        raise ValueError('Límites PE de UnityPlayer inválidos')
    if struct.unpack_from('<H', data, pe + 24)[0] != 0x20b:
        raise ValueError('Se requiere una cabecera PE de 64 bits')
    image_base_offset = pe + 48
    preferred_base = struct.unpack_from('<Q', data, image_base_offset)[0]
    candidates = []
    rejected = []
    for start, end, perms, path in mem.maps:
        if path == str(UNITY_PLAYER) and 'r' in perms and end - start >= header_size:
            header = bytearray(mem.read(start, header_size))
            loaded_base = struct.unpack_from('<Q', header, image_base_offset)[0]
            if loaded_base not in (preferred_base, start):
                rejected.append(f'{hex(start)}: ImageBase {hex(loaded_base)} no coincide con la base mapeada')
                continue
            struct.pack_into('<Q', header, image_base_offset, preferred_base)
            if header == data[:header_size]:
                candidates.append(start)
            else:
                differences = [i for i, (a, b) in enumerate(zip(header, data[:header_size])) if a != b]
                rejected.append(f'{hex(start)}: {len(differences)} bytes distintos fuera de ImageBase; '
                                'offsets ' + ', '.join(hex(i) for i in differences[:16]))
    if len(candidates) != 1:
        details = '; '.join(rejected[:4]) or f'{len(candidates)} cabeceras compatibles'
        raise ValueError('No hay una única cabecera mapeada de UnityPlayer compatible: ' + details)
    base = candidates[0]
    sections = []
    for i in range(count):
        offset = pe + 24 + optional + 40*i
        virtual_size, rva, raw_size = struct.unpack_from('<3I', data, offset + 8)
        flags = struct.unpack_from('<I', data, offset + 36)[0]
        if flags & 0x40000000:  # IMAGE_SCN_MEM_READ
            sections.append((base + rva, base + rva + max(virtual_size, raw_size),
                             bool(flags & 0x20000000)))
    return sections


def apply_trs(point, position, rotation, scale):
    """Parent translation + quaternion rotation of componentwise scaled point."""
    v = [point[i] * scale[i] for i in range(3)]
    x, y, z, w = rotation
    cross = [y*v[2] - z*v[1], z*v[0] - x*v[2], x*v[1] - y*v[0]]
    cross2 = [y*cross[2] - z*cross[1], z*cross[0] - x*cross[2], x*cross[1] - y*cross[0]]
    return [position[i] + v[i] + 2*(w*cross[i] + cross2[i]) for i in range(3)]


class TransformReader:
    def __init__(self, mem):
        data = verify_engine()
        self.mem = mem
        self.engine_sections = engine_sections(mem, data)

    def component_game_object(self, component):
        """Read Component.gameObject without relying on allocation distances."""
        reads = {}

        def pointer(address):
            block = self.mem.read(address, 8)
            if reads.setdefault(address, block) != block:
                raise ValueError('El vínculo del objeto cambió durante la lectura')
            value = struct.unpack('<Q', block)[0]
            if not value or value % 8:
                raise ValueError('Puntero de objeto nulo o desalineado')
            return value

        def engine_address(address, executable=False):
            if (not any(a <= address < b and (not executable or code)
                        for a, b, code in self.engine_sections) or
                    not any(a <= address < b and 'r' in perms and
                            (not executable or 'x' in perms)
                            for a, b, perms, _ in self.mem.maps)):
                raise ValueError('Tipo nativo fuera del UnityPlayer inspeccionado')

        def native_object(obj):
            vtable = pointer(obj)
            engine_address(vtable)
            engine_address(pointer(vtable), executable=True)

        native_object(component)
        game_object = pointer(component + 0x20)
        native_object(game_object)
        for address, expected in reads.items():
            if self.mem.read(address, 8) != expected:
                raise ValueError('El vínculo del objeto cambió durante la lectura')
        return dict(verified=True, component=hex(component), game_object=hex(game_object),
                    source='UnityPlayer.Component.gameObject', unity_player_sha256=UNITY_SHA256)

    def read(self, component):
        mem = self.mem
        reads = {}

        def read(address, size):
            value = mem.read(address, size)
            previous = reads.setdefault((address, size), value)
            if value != previous:
                raise ValueError('Transform cambió durante la lectura')
            return value

        def pointer(address):
            value = struct.unpack('<Q', read(address, 8))[0]
            if not value or value % 8:
                raise ValueError('Puntero nativo nulo o desalineado')
            return value

        def engine_address(address, executable=False):
            in_image = any(a <= address < b and (not executable or code)
                           for a, b, code in self.engine_sections)
            readable = any(a <= address < b and 'r' in perms and
                           (not executable or 'x' in perms)
                           for a, b, perms, _ in mem.maps)
            if not in_image or not readable:
                raise ValueError('Tipo nativo fuera del UnityPlayer inspeccionado')

        def native_object(obj):
            vtable = pointer(obj)
            engine_address(vtable)
            engine_address(pointer(vtable), executable=True)

        native_object(component)
        game_object = pointer(component + 0x20)
        native_object(game_object)
        components = pointer(game_object + 0x20)
        transform = pointer(components + 8)
        native_object(transform)
        if pointer(transform + 0x20) != game_object:
            raise ValueError('Transform no pertenece al GameObject del componente')
        data = pointer(transform + 0x28)
        # Unity's getter waits for this job handle. We do not execute or wait on it.
        if struct.unpack('<Q', read(data, 8))[0]:
            raise ValueError('Transform tiene un trabajo pendiente; no se sincroniza el motor')
        index = struct.unpack('<i', read(transform + 0x30, 4))[0]
        transforms = pointer(data + 0x18)
        parents = pointer(data + 0x20)
        chain, seen = [], set()
        current = index
        while current != -1:
            if not 0 <= current < 1000000 or current in seen or len(chain) >= 64:
                raise ValueError('Jerarquía Transform fuera de límites o cíclica')
            seen.add(current)
            values = struct.unpack('<12f', read(transforms + current * 48, 48))
            position, rotation, scale = values[:3], values[4:8], values[8:11]
            if not all(math.isfinite(x) and abs(x) <= 1e9 for x in (*position, *rotation, *scale)):
                raise ValueError('Transform contiene valores no finitos o fuera de límites')
            if abs(sum(x*x for x in rotation) - 1) > 0.05:
                raise ValueError('Rotación Transform no normalizada')
            parent = struct.unpack('<i', read(parents + current * 4, 4))[0]
            chain.append(dict(index=current, parent=parent, local_position=list(position),
                              local_rotation=list(rotation), local_scale=list(scale)))
            current = parent
        if not chain:
            raise ValueError('Transform sin índice válido')
        world = chain[0]['local_position']
        for node in chain[1:]:
            world = apply_trs(world, node['local_position'], node['local_rotation'], node['local_scale'])
        if not all(math.isfinite(x) and abs(x) <= 1e12 for x in world):
            raise ValueError('Posición compuesta fuera de límites')
        for (address, size), expected in reads.items():
            if mem.read(address, size) != expected:
                raise ValueError('Transform cambió durante la lectura')
        return dict(status='native_transform_candidate', live_validated=False,
                    executable=False, screen_position=None,
                    unity_player_sha256=UNITY_SHA256,
                    native_component=hex(component), game_object=hex(game_object),
                    transform=hex(transform), transform_data=hex(data),
                    local_position=chain[0]['local_position'],
                    world_position_candidate=world, hierarchy=chain)
