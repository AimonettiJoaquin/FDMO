#!/usr/bin/env python3
"""Monitor de turno y fase de DMO. Solo lectura, sin entradas al juego."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

from dmo_board import BOARD_TARGETS, BoardReader, render_board
from dmo_card_memory import OwnCardMemory, render_card_memory
from dmo_advisor import Advisor, render_advice
from dmo_actions import ACTION_TARGETS, SPELL_PROMPT_TARGETS, ActionReader, render_actions
from dmo_scene import SCENE_TARGETS, SceneReader, render_scene

from inspect_dmo_memory import (
    METADATA, Memory, list_size, metadata_types, native_layout, read_value,
    validate_class,
)
from dmo_currency import read_wallet

TARGETS = ('DuelistScript', 'DuelManager', 'NetworkBehaviour', 'NetworkIdentity')
# Verified from this build's metadata defaults; binary and runtime layouts are
# checked at startup. Unknown enum values are rejected rather than guessed.
PHASES = ('DRAW', 'STANDBY', 'MAIN 1', 'BATTLE', 'MAIN 2', 'END')


def resolve_identity(result):
    """A verified local network player may have two unpublished display names."""
    names = result['names']
    names_empty = (all(key in names for key in ('Name', 'PlayerName')) and
                   all(value is None or not value.strip() for value in names.values()))
    local = result.get('is_local_player') is True
    result['accepted'] = bool(not result.get('npc') and local and
                              (result['name_matches'] or names_empty))
    result['name_verified'] = bool(result['accepted'] and result['name_matches'])
    result['reason'] = ('matched' if result['name_verified'] else
                        'local_player_without_name' if result['accepted'] else
                        'name_mismatch' if not result['name_matches'] and not names_empty else
                        'not_local_player' if 'is_local_player' in result else 'invalid_identity')
    return result


class DuelMonitor:
    def __init__(self, mem, name, layout, definitions, budget, board=False, actions=False, scene=False,
                 currency=False):
        self.mem = mem
        self.name = name
        self.layout = layout
        self.definitions = definitions
        self.budget = budget
        self.metadata_base = next(a for a, _, _, path in mem.maps if path == str(METADATA))
        self.classes = {}
        self.player = None
        self.observations = {}
        self.discovery_stats = None
        self.board_reader = BoardReader(self) if board else None
        self.action_reader = ActionReader(self) if actions else None
        self.scene_reader = SceneReader(self) if scene else None
        self.read_currency = currency

    def accept_class(self, name, address):
        result = validate_class(self.mem, address, self.definitions[name], self.metadata_base)
        if not result:
            raise ValueError(f'Clase {name} no validada')
        expected = self.layout['classes'][name]['fields']
        if any(any(result['fields'][key][p] != field[p]
                   for p in ('offset', 'type_code', 'attributes'))
               for key, field in expected.items()):
            raise ValueError(f'Layout de {name} diferente al ejecutable')
        self.classes[name] = result

    def field(self, obj, typename, key):
        field = self.layout['classes'][typename]['fields'][key]
        if field['attributes'] & 0x10 or field['offset'] < 16:
            raise ValueError('El monitor solo admite campos de instancia')
        return read_value(self.mem, obj + field['offset'], field)

    def pointer(self, obj, typename, key):
        return int(self.field(obj, typename, key)['pointer'], 16)

    def check_object(self, obj, name, *, unity=True):
        if not obj:
            raise ValueError(f'Referencia nula a {name}')
        klass = self.mem.u64(obj)
        if name not in self.classes:
            self.accept_class(name, klass)
        if klass != self.classes[name]['address']:
            raise ValueError(f'La referencia ya no es {name}')
        if not unity:
            return
        native = self.mem.u64(obj + 16)
        if not native:
            raise ValueError(f'{name} destruido')
        self.mem.read(native, 8)

    def validate_inheritance(self):
        # netIdentity is inherited from Mirror.NetworkBehaviour.
        parent = self.mem.u64(self.classes['DuelistScript']['address'] + 88)
        for _ in range(12):
            if not parent:
                break
            try:
                self.accept_class('NetworkBehaviour', parent)
                return
            except ValueError:
                parent = self.mem.u64(parent + 88)
        raise ValueError('No se pudo validar NetworkBehaviour en la jerarquía')

    def identify(self, obj):
        """Keep failed identity evidence; never substitute ownership for locality."""
        result = dict(address=hex(obj), accepted=False, names={}, errors={})
        try:
            self.check_object(obj, 'DuelistScript')
            result['npc'] = self.field(obj, 'DuelistScript', 'NPC')
        except (OSError, ValueError) as error:
            result.update(reason='invalid_object', error=str(error))
            return result
        if result['npc']:
            result['reason'] = 'npc'
            return result
        # One unset/invalid name must not hide the other valid name.
        for key in ('Name', 'PlayerName'):
            try:
                result['names'][key] = self.field(obj, 'DuelistScript', key)
            except (OSError, ValueError) as error:
                result['errors'][key] = str(error)
        result['name_matches'] = any(n and n.strip().casefold() == self.name.strip().casefold()
                                     for n in result['names'].values())
        # Inspect the network identity even if the name differs. This tells us
        # which check rejects a local player whose display name lives elsewhere.
        try:
            identity = self.pointer(obj, 'NetworkBehaviour', '<netIdentity>k__BackingField')
            result['identity'] = hex(identity)
            self.check_object(identity, 'NetworkIdentity')
            result['is_local_player'] = self.field(identity, 'NetworkIdentity', '<isLocalPlayer>k__BackingField')
            for flag in ('<isOwned>k__BackingField', '<isClient>k__BackingField', '<isServer>k__BackingField'):
                if flag in self.layout['classes']['NetworkIdentity']['fields']:
                    result[flag] = self.field(identity, 'NetworkIdentity', flag)
        except (OSError, ValueError) as error:
            result['errors']['network_identity'] = str(error)
        return resolve_identity(result)

    def matches_player(self, obj):
        return self.identify(obj)['accepted']

    def inspect_candidate(self, obj):
        result = self.identify(obj)
        self.observations[obj] = result
        if result['accepted']:
            self.player = obj
        return result['accepted']

    def diagnostic_report(self):
        rows = list(self.observations.values())
        return dict(character=self.name, found=self.player is not None,
                    reasons=dict(Counter(row['reason'] for row in rows)),
                    candidates=rows, scan=self.discovery_stats)

    def load_hints(self, report, pid, inspect_all=False):
        """Reports supply addresses only; all identity checks use fresh memory."""
        if report.get('pid') != pid or report.get('binary_sha256') != self.layout['binary_sha256']:
            return
        for name in ('DuelistScript', 'DuelManager'):
            try:
                self.accept_class(name, int(report['classes'][name]['address'], 16))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        if 'DuelistScript' not in self.classes:
            return
        self.validate_inheritance()
        hints = [link['duelist'] for link in report.get('dueling_links', [])]
        hints += [obj['address'] for obj in report.get('candidates', {}).get('DuelistScript', [])]
        for address in dict.fromkeys(hints):
            try:
                obj = int(address, 16)
                if self.inspect_candidate(obj) and not inspect_all:
                    return
            except (OSError, ValueError):
                continue

    def discover(self):
        if 'DuelistScript' not in self.classes:
            needle = self.metadata_base + self.definitions['DuelistScript']['name_offset']
            for _, hit in self.mem.find_pointers({'DuelistScript': needle}, self.budget):
                try:
                    self.accept_class('DuelistScript', hit - 16)
                    break
                except (OSError, ValueError):
                    continue
        if 'DuelistScript' not in self.classes:
            raise ValueError('No se encontró la clase DuelistScript')
        self.validate_inheritance()
        print(f'Buscando a {self.name}; este escaneo inicial puede tardar un minuto…',
              file=sys.stderr, flush=True)
        klass = self.classes['DuelistScript']['address']
        for _, hit in self.mem.find_pointers({'DuelistScript': klass}, self.budget):
            try:
                if self.inspect_candidate(hit):
                    self.discovery_stats = self.mem.scan_stats.copy()
                    self.discovery_stats['stop_reason'] = 'player_found'
                    return True
            except (OSError, ValueError):
                continue
        self.discovery_stats = self.mem.scan_stats.copy()
        summary = self.diagnostic_report()
        print('Identificación sin coincidencias: ' + json.dumps(summary['reasons'], ensure_ascii=False),
              file=sys.stderr, flush=True)
        return False

    def read_state(self):
        player = self.player
        identity = self.identify(player) if player else None
        if not identity or not identity['accepted']:
            raise ValueError(f'{self.name} no está validado como jugador local')
        base = dict(character=self.name, name_verified=identity['name_verified'],
                    identity_source='NetworkIdentity.isLocalPlayer',
                    local_player=True, player=hex(player))
        if not self.field(player, 'DuelistScript', 'Dueling'):
            starting = self.field(player, 'DuelistScript', 'StartingDuel')
            return dict(base, status='starting' if starting else 'outside_duel')
        manager = self.pointer(player, 'DuelistScript', 'duelManager')
        if not manager:
            return dict(base, status='transition')
        self.check_object(manager, 'DuelManager')
        phase = self.field(manager, 'DuelManager', 'CurrentPhase')['enum_raw']
        if not 0 <= phase < len(PHASES):
            raise ValueError(f'Fase desconocida: {phase}')
        own_id = self.field(player, 'DuelistScript', 'DuelID')
        turn_id = self.field(manager, 'DuelManager', 'CurrentTurnPlayerID')
        count = list_size(self.mem, self.pointer(manager, 'DuelManager', 'Players'))
        if not 2 <= count <= 64 or own_id < 0:
            return dict(base, status='transition')
        board = None
        if self.board_reader:
            try:
                board = self.board_reader.read(manager)
            except (OSError, ValueError) as error:
                board = dict(status='partial', fields=[], errors=[dict(error=str(error))])
        actions = self.action_reader.read(manager) if self.action_reader else None
        scene = None
        if self.scene_reader:
            try:
                scene = self.scene_reader.read(board or {})
            except (OSError, ValueError) as error:
                scene = dict(status='unavailable', executable=False, screen_coordinates_available=False,
                             hand_elements=[], collections=[], errors=[str(error)])
        # Recheck the link to avoid reporting a manager that changed mid-read.
        if (not self.field(player, 'DuelistScript', 'Dueling') or
                self.pointer(player, 'DuelistScript', 'duelManager') != manager):
            raise ValueError('El duelo cambió durante la lectura')
        state = dict(base, status='duel', manager=hex(manager), phase=PHASES[phase],
                    phase_id=phase, own_id=own_id, turn_id=turn_id, players=count,
                    my_turn=None if turn_id < 0 else turn_id == own_id)
        if board is not None:
            state['board'] = board
        if actions is not None:
            state['actions'] = actions
        if scene is not None:
            state['scene'] = scene
        return state

    def snapshot(self):
        # Two matching reads reduce torn snapshots without stopping the game.
        for _ in range(3):
            try:
                first = self.read_state()
                second = self.read_state()
                if first == second:
                    if self.read_currency:
                        try:
                            wallet = read_wallet(self)
                            if wallet['player'] != first.get('player'):
                                raise ValueError('El jugador cambió al leer el saldo')
                            first['currency'] = wallet
                        except (OSError, ValueError) as error:
                            first['currency'] = dict(status='unavailable', error=str(error))
                    return first
            except (OSError, ValueError):
                continue
        return dict(character=self.name, status='unstable')


def render(state):
    name = state['character'] if state.get('name_verified', True) else 'Jugador local'
    status = state['status']
    if status == 'duel':
        turn = ('turno sin asignar' if state['my_turn'] is None else
                'tu turno' if state['my_turn'] else 'turno del rival')
        line = f'{name} · {turn} · {state["phase"]} · {state["players"]} jugadores'
        if 'board' in state:
            line += '\n' + render_board(state['board'])
        remembered = render_card_memory(state.get('own_card_memory', {}))
        if remembered:
            line += '\n' + remembered
        if 'advice' in state:
            line += '\n' + render_advice(state['advice'])
        if 'actions' in state:
            line += '\n' + render_actions(state['actions'])
        if 'scene' in state:
            line += '\n' + render_scene(state['scene'])
        return line
    return f'{name} · ' + {
        'outside_duel': 'fuera del duelo', 'starting': 'iniciando duelo',
        'transition': 'duelo en transición', 'unstable': 'lectura inestable; esperando',
        'not_found': 'jugador local todavía no localizado',
    }[status]


def find_pid():
    matches = []
    for path in Path('/proc').glob('[0-9]*/comm'):
        try:
            if path.read_text().strip().casefold() == 'dmo.exe':
                matches.append(int(path.parent.name))
        except OSError:
            continue
    if len(matches) != 1:
        raise ValueError('No hay un único DMO.exe; indicá --pid con el proceso actual')
    return matches[0]


def process_start(pid):
    return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int)
    parser.add_argument('--name', default=os.environ.get('DMO_CHARACTER', 'JQN'))
    parser.add_argument('--report', type=Path, default=Path('/tmp/dmo-memory-report-v3.json'))
    parser.add_argument('--interval', type=float, default=1)
    parser.add_argument('--samples', type=int, default=0, help='0: hasta Ctrl+C')
    parser.add_argument('--jsonl', action='store_true', help='Una muestra JSON por línea')
    parser.add_argument('--max-scan-mib', type=int, default=16384)
    parser.add_argument('--board', action='store_true', help='Leer mano y campos vinculados al duelo (experimental)')
    parser.add_argument('--suggest', action='store_true', help='Sugerencias experimentales sin ejecutar acciones; activa --board')
    parser.add_argument('--spells', action='store_true', help='Añadir las reglas del mazo de mágicas al asesor')
    parser.add_argument('--currency', action='store_true', help='Leer el saldo local para métricas de farmeo')
    parser.add_argument('--actions', action='store_true', help='Diagnóstico de opciones publicadas por el cliente')
    parser.add_argument('--scene', action='store_true', help='Buscar elementos interactivos vinculados a la mano; activa --board')
    parser.add_argument('--scene-discover', action='store_true',
                        help='Escanear referencias a cartas de la mano una vez, emitir JSON y terminar')
    parser.add_argument('--scene-report', type=Path,
                        help='Revalidar y seguir objetos de un informe de --scene-discover; activa --scene')
    parser.add_argument('--scene-cache', type=Path,
                        help='Pistas opcionales de escena; ignorar si pertenecen a otro proceso')
    parser.add_argument('--scene-auto-discover', action='store_true',
                        help='Buscar enlaces de mano cuando falte la carta Summon sugerida; activa --scene y --suggest')
    parser.add_argument('--scene-transforms', action='store_true',
                        help='Diagnóstico nativo de Transform, sin coordenadas de pantalla; activa --scene')
    parser.add_argument('--diagnose', action='store_true', help='Emitir diagnóstico de identidad y terminar')
    parser.add_argument('--hints-only', action='store_true', help='Con --diagnose, leer solo direcciones del informe previo')
    args = parser.parse_args()
    if args.scene_report or args.scene_cache or args.scene_transforms or args.scene_auto_discover:
        args.scene = True
    if args.scene_auto_discover or args.spells:
        args.suggest = True
    if args.scene_discover:
        args.scene = True
        if args.diagnose:
            parser.error('--scene-discover no se combina con --diagnose')
    if args.suggest:
        args.board = True
        args.actions = True
    if args.scene:
        args.board = True
    if args.interval <= 0 or args.samples < 0 or args.max_scan_mib <= 0:
        parser.error('Intervalo y presupuesto deben ser positivos; samples no negativo')
    if args.hints_only and not args.diagnose:
        parser.error('--hints-only requiere --diagnose')
    mem = None
    try:
        pid = args.pid or find_pid()
        generation = process_start(pid)
        targets = TARGETS + BOARD_TARGETS if args.board else TARGETS
        if args.actions:
            targets += ACTION_TARGETS + SPELL_PROMPT_TARGETS
        if args.scene:
            targets += SCENE_TARGETS
        if args.currency and 'global::Misc' not in targets:
            targets += ('global::Misc',)
        layout = native_layout(targets)
        definitions = metadata_types(targets)
        mem = Memory(pid)
        monitor = DuelMonitor(mem, args.name, layout, definitions, args.max_scan_mib * 1024**2,
                              board=args.board, actions=args.actions, scene=args.scene,
                              currency=args.currency)
        if args.actions:
            monitor.action_reader.describe_prompts = True
        if args.scene:
            from dmo_transform import TransformReader
            try:
                reader = TransformReader(mem)
                monitor.scene_reader.native_object_reader = reader
                if args.scene_transforms:
                    monitor.scene_reader.transform_reader = reader
            except (OSError, ValueError) as error:
                if args.scene_transforms:
                    raise
                monitor.scene_reader.native_object_reader_error = str(error)
        if args.scene_report:
            if args.scene_report.stat().st_size > 1024**2:
                raise ValueError('El informe de escena supera 1 MiB')
            monitor.scene_reader.load_hints(json.loads(args.scene_report.read_text()), pid,
                                            generation, layout['binary_sha256'])
        elif args.scene_cache:
            try:
                if args.scene_cache.stat().st_size > 1024**2:
                    raise ValueError('La caché supera 1 MiB')
                report = json.loads(args.scene_cache.read_text())
                if any(key not in report for key in ('pid', 'process_start', 'binary_sha256')):
                    raise ValueError('Falta la identidad del proceso en la caché')
                monitor.scene_reader.load_hints(report, pid, generation, layout['binary_sha256'])
                print('Caché de escena cargada; se revalidarán los enlaces.', file=sys.stderr)
            except (OSError, ValueError, KeyError, TypeError) as error:
                print(f'Caché de escena omitida: {error}', file=sys.stderr)
        hint_error = None
        try:
            monitor.load_hints(json.loads(args.report.read_text()), pid, inspect_all=args.diagnose)
        except (OSError, ValueError, KeyError, TypeError) as error:
            hint_error = str(error)
            print(f'Informe previo no reutilizable: {error}', file=sys.stderr)
        if args.diagnose:
            if not args.hints_only and not monitor.player:
                monitor.discover()
            print(json.dumps(dict(pid=pid, hints_only=args.hints_only, hint_error=hint_error,
                                  **monitor.diagnostic_report()), ensure_ascii=False, indent=2), flush=True)
            return 0
        print(f'Monitor de solo lectura · PID {pid} · personaje {args.name} · Ctrl+C para salir',
              file=sys.stderr, flush=True)
        if args.jsonl:
            print('Modo JSON: los estados se escriben en la salida redirigida; '
                  'el progreso se mostrará aquí.', file=sys.stderr, flush=True)
        previous, attempt_at, samples = None, 0.0, 0
        auto_discovery_attempt = None
        card_memory = OwnCardMemory() if args.board else None
        advisor = Advisor(spells=True) if args.spells else Advisor() if args.suggest else None
        while args.samples == 0 or samples < args.samples:
            if process_start(pid) != generation:
                raise ValueError('El proceso fue reemplazado; reiniciá el monitor')
            mem.refresh_maps()
            try:
                valid = monitor.player and monitor.matches_player(monitor.player)
            except (OSError, ValueError):
                valid = False
            if not valid:
                monitor.player = None
                if time.monotonic() >= attempt_at:
                    if not monitor.discover():
                        print('No se repetirá el escaneo. Guardá un diagnóstico con --diagnose para revisar los candidatos.',
                              file=sys.stderr, flush=True)
                        return 3
                    attempt_at = time.monotonic() + 60
            state = (monitor.snapshot() if monitor.player else
                     dict(character=args.name, status='not_found'))
            if args.scene_discover:
                print('Buscando objetos que referencien la mano; mantené el duelo y la mano sin cambios…',
                      file=sys.stderr, flush=True)
                scene = monitor.scene_reader.discover_hand_elements(state, monitor.budget)
                if process_start(pid) != generation:
                    raise ValueError('El proceso fue reemplazado durante el escaneo')
                print(json.dumps(dict(pid=pid, process_start=generation,
                                      binary_sha256=layout.get('binary_sha256'),
                                      timestamp=datetime.now(timezone.utc).isoformat(),
                                      character=args.name, scene=scene), ensure_ascii=False), flush=True)
                print(f'Escaneo de escena terminado · {len(scene["hand_elements"])} elementos vinculados · '
                      f'estado: {scene["status"]}', file=sys.stderr, flush=True)
                return 0
            now = datetime.now(timezone.utc).isoformat()
            if card_memory is not None:
                # Stateful tracking runs only after the double-read snapshot.
                state = dict(state, own_card_memory=card_memory.observe(state, time.monotonic()))
            if advisor is not None:
                state = dict(state, advice=advisor.observe(state, time.monotonic()))
            if args.scene_auto_discover and state.get('status') == 'duel':
                options = [o for o in state.get('advice', {}).get('options', [])
                           if o.get('kind') in ('spell_candidate', 'summon_candidate')][:1]
                if (state.get('my_turn') is True and
                        state.get('phase') in ('MAIN 1', 'MAIN 2') and
                        state.get('advice', {}).get('status') == 'suggestions' and
                        len(options) == 1 and
                        options[0]['address'] not in {e.get('card_address') for e in
                                                       state.get('scene', {}).get('hand_elements', [])}):
                    own = [f for f in state.get('board', {}).get('fields', [])
                           if f.get('local') is True]
                    if len(own) == 1 and own[0].get('HandCards'):
                        signature = (state['manager'], tuple(c.get('address') for c in
                           own[0]['HandCards'] if c), options[0]['address'])
                        if signature != auto_discovery_attempt:
                            auto_discovery_attempt = signature
                            print('Buscando enlace de escena para ' + options[0]['card'] +
                                  '; el duelo debe permanecer estable…', file=sys.stderr, flush=True)
                            try:
                                found = monitor.scene_reader.discover_hand_elements(
                                    state, monitor.budget, stop_when_linked=True,
                                    target_card=options[0]['address'])
                                if found['status'] == 'hand_objects_linked':
                                    new_hints = tuple(
                                        (int(e['element'], 16), int(e['card_address'], 16),
                                         int(e['native_element'], 16))
                                        for e in found['hand_elements'])
                                    monitor.scene_reader.element_hints = tuple(dict.fromkeys(
                                        monitor.scene_reader.element_hints + new_hints))[-4096:]
                                    state = monitor.snapshot()
                                    now = datetime.now(timezone.utc).isoformat()
                                    state = dict(state, own_card_memory=card_memory.observe(
                                        state, time.monotonic()))
                                    state = dict(state, advice=advisor.observe(
                                        state, time.monotonic()))
                                    print(f'Enlaces de mano actualizados: '
                                          f'{len(found["hand_elements"])} · '
                                          f'{found.get("scan", {}).get("bytes_read", 0) / 1024**2:.1f} MiB leídos · '
                                          f'{found.get("scan", {}).get("elapsed_seconds", 0):.1f} s',
                                          file=sys.stderr, flush=True)
                                else:
                                    print('No se encontraron enlaces estables: ' +
                                          found['status'], file=sys.stderr, flush=True)
                            except (OSError, ValueError, KeyError, TypeError) as error:
                                print(f'Escaneo de mano omitido: {error}', file=sys.stderr, flush=True)
            if args.jsonl:
                print(json.dumps(dict(timestamp=now, process_identity=dict(
                    pid=pid, process_start=generation, binary_sha256=layout.get('binary_sha256')),
                    **state), ensure_ascii=False), flush=True)
            elif state != previous:
                print(f'[{datetime.now():%H:%M:%S}] {render(state)}', flush=True)
            previous = state
            samples += 1
            if args.jsonl and (samples == 1 or samples % 10 == 0 or samples == args.samples):
                total = f'/{args.samples}' if args.samples else ''
                print(f'Captura activa · muestras {samples}{total} · estado: {state["status"]}',
                      file=sys.stderr, flush=True)
            if args.samples == 0 or samples < args.samples:
                time.sleep(args.interval)
        if args.jsonl:
            print(f'Captura terminada · {samples} muestras escritas.', file=sys.stderr, flush=True)
        return 0
    except BrokenPipeError:
        # A one-action JSONL consumer closes stdin after verification.
        # Redirect fd 1 so Python's final stdout flush does not raise again.
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, sys.stdout.fileno())
        finally:
            os.close(devnull)
        return 0
    except PermissionError:
        print('Linux bloqueó la lectura. Ejecutá este monitor con sudo desde tu terminal.', file=sys.stderr)
        return 2
    except (OSError, ValueError, StopIteration) as error:
        print(f'Monitor detenido: {error}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    finally:
        if mem is not None:
            mem.close()


if __name__ == '__main__':
    raise SystemExit(main())
