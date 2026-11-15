#!/usr/bin/env python3
"""Start a Bonz match with the manually chosen difficulty, then run the bot."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

from dmo_bot import Bot, SafetyStop
from dmo_loop_bot import DuelLoop
from dmo_memory_bot import Desktop, ROOT
from dmo_window import WindowFrame, WindowDesktop
from dmo_match_stats import MatchStats
from dmo_session_metrics import SessionMetrics
from dmo_settings import game_directory

SESSION_LOG_DIR = ROOT / 'logs' / 'sessions'


def save_session_summary(directory, stats, started_at, *, name, reason, exit_code,
                         error, trace_path, shutdown_enabled, metrics=None):
    directory.mkdir(parents=True, exist_ok=True)
    ended_at = datetime.now().astimezone()
    path = directory / f'session-{started_at:%Y%m%d-%H%M%S-%f}-{os.getpid()}.json'
    report = dict(started_at=started_at.isoformat(), ended_at=ended_at.isoformat(),
                  duration_seconds=round((ended_at - started_at).total_seconds(), 2),
                  character=name, reason=reason, exit_code=exit_code, error=error,
                  matches_started=stats.started, wins=stats.wins, losses=stats.losses,
                  forfeits=stats.forfeits, unknown=stats.unknown,
                  summary=stats.summary(), last_trace=str(trace_path.resolve()),
                  shutdown_enabled=shutdown_enabled)
    if metrics is not None:
        report.update(metrics.report())
        report['metrics_summary'] = metrics.summary(report)
    with path.open('x') as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())
    return path.resolve()


class MemoryStartDesktop:
    """Deliver real mouse motion for the experimental runner's lobby clicks."""

    def __init__(self, legacy):
        self.legacy = legacy
        self.input = Desktop()

    def __getattr__(self, name):
        return getattr(self.legacy, name)

    def click(self, point, delay=None):
        actual = self.input.move(tuple(point))
        if self.input.position() != actual:
            raise RuntimeError('El cursor cambió antes del clic de inicio')
        self.input.click()
        time.sleep(self.legacy.config['timing']['click_delay'] if delay is None else delay)


def retry_start_from_setup(bot):
    """Retry START only while the same setup screen is still visible."""
    if not bot.vision.is_state('duel_setup', bot.desktop.screenshot()):
        return False
    for attempt in range(1, 4):
        print(f'START no avanzó; reintento {attempt}/3.', flush=True)
        bot.click('start')
        bot.settle()
        try:
            bot.vision.wait_for('duel_lobby')
        except SafetyStop:
            if bot.vision.is_state('duel_setup', bot.desktop.screenshot()):
                continue
            raise
        for name in ('add_npc', 'bonz', 'ready'):
            bot.click(name)
            bot.settle(1.5)
        return True
    return False


def start_match(bot, delay):
    for state in ('world', 'duel_setup', 'duel_lobby'):
        if not bot.vision.calibrated(state):
            raise SafetyStop(f"Falta la plantilla '{state}'; usar la calibración del bot visual")
    print(f'Volvé al juego: inicio en {delay} segundos. Dejá al personaje en el mundo, sin menús.',
          flush=True)
    for seconds in range(delay, 0, -1):
        print(f'  {seconds}…', flush=True)
        time.sleep(1)
    if isinstance(bot.desktop, WindowDesktop):
        bot.desktop.prepare()
    image = bot.desktop.screenshot()
    if not bot.vision.is_state('world', image):
        capture = bot.desktop.save_debug('memory-start-not-world', image)
        raise SafetyStop(f'No se reconoció el mundo antes de iniciar. Captura: {capture}')
    print('Iniciando duelo contra Bonz con la dificultad elegida manualmente…', flush=True)
    try:
        bot.start_duel()
    except SafetyStop:
        if not retry_start_from_setup(bot):
            raise


SCENE_CACHE = ROOT / 'debug' / 'dmo-scene-cache.json'


class SceneCache:
    """Save address hints as the desktop user; the privileged monitor only reads."""
    def __init__(self, path):
        self.path = path
        self.identity = None
        self.links = {}
        self.disabled = False

    def observe(self, state):
        if self.disabled:
            return
        identity = state.get('process_identity')
        if (not isinstance(identity, dict) or
                any(identity.get(k) is None for k in ('pid', 'process_start', 'binary_sha256'))):
            return
        if identity != self.identity:
            self.identity, self.links = identity.copy(), {}
        changed = False
        for element in state.get('scene', {}).get('hand_elements', []):
            if any(not element.get(k) for k in ('element', 'card_address', 'native_element')):
                continue
            link = {k: element[k] for k in ('element', 'card_address', 'native_element')}
            if self.links.get(link['element']) != link:
                self.links[link['element']] = link
                changed = True
        if changed:
            self.links = dict(list(self.links.items())[-4096:])
            report = dict(self.identity, scene=dict(hand_elements=list(self.links.values())))
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_suffix('.tmp')
                temporary.write_text(json.dumps(report))
                temporary.replace(self.path)
            except OSError as error:
                self.disabled = True
                print(f'No se pudo guardar la caché de escena: {error}', file=sys.stderr)


def monitor_command(name, *, spells=False):
    # Authentication happens before switching workspaces; input remains unprivileged.
    command = ['sudo', '-n', 'env', f'DMO_GAME_DIR={game_directory()}',
            sys.executable, str(ROOT / 'monitor_dmo.py'),
            '--name', name, '--suggest', '--scene-auto-discover', '--jsonl',
            '--currency', '--interval', '0.25', '--scene-cache', str(SCENE_CACHE)]
    return command + ['--spells'] if spells else command


def consume(stream, trace, loop, cache=None, stats=None, *, camera=None,
            camera_point=None, camera_ticks=12, metrics=None):
    last_event = None
    camera_scrolled = False
    for line in stream:
        trace.write(line)
        trace.flush()  # Keep the failing sample as well as successful transitions.
        state = json.loads(line)
        if cache is not None:
            cache.observe(state)
        # The monitor may block on its pipe while a new match is being opened.
        # Keep those samples in the trace, but never act on buffered old state.
        timestamp = state.get('timestamp')
        if isinstance(timestamp, str):
            age = (datetime.now().astimezone() - datetime.fromisoformat(timestamp).astimezone()).total_seconds()
            if age > 3:
                continue
        if metrics is not None:
            metrics.observe(state)
        try:
            if (camera is not None and not camera_scrolled and
                    state.get('status') == 'duel' and
                    state.get('phase') in ('DRAW', 'STANDBY', 'MAIN 1', 'BATTLE', 'MAIN 2', 'END')):
                if camera_point is None:
                    raise RuntimeError('Falta el punto de referencia para fijar la cámara')
                actual = camera.move(camera_point)
                if camera.position() != actual:
                    raise RuntimeError('El cursor cambió antes de fijar la cámara')
                camera.scroll_up(camera_ticks)
                camera_scrolled = True
                print(f'Cámara fijada: {camera_ticks} pasos de rueda hacia arriba.', flush=True)
                continue  # Read a fresh sample after the camera settles.
            result = loop.observe(state)
            if stats is not None:
                stats.observe(state, result)
            if (isinstance(result, str) and result.startswith('recovery_started:') and
                    isinstance(getattr(trace, 'name', None), str)):
                recovery_path = Path(trace.name).with_suffix('.recovery.jsonl')
                try:
                    with recovery_path.open('a') as report:
                        report.write(json.dumps(dict(error=loop.recovery_error,
                            loop_stage=loop.recovery_origin, attempt=loop.recovery_attempts,
                            state=state), ensure_ascii=False) + '\n')
                    print(f'Informe de recuperación: {recovery_path}', file=sys.stderr)
                except OSError as report_error:
                    print(f'No se pudo guardar la recuperación: {report_error}', file=sys.stderr)
        except (KeyError, TypeError, ValueError, OSError, RuntimeError,
                subprocess.SubprocessError) as error:
            if isinstance(getattr(trace, 'name', None), str):
                report_path = Path(trace.name).with_suffix('.error.json')
                try:
                    plan = getattr(loop, 'spell_plan', None)
                    report_path.write_text(json.dumps(dict(error=str(error),
                        loop_stage=str(getattr(loop, 'stage', 'unknown')),
                        actor_stage=str(getattr(getattr(loop, 'actor', None), 'stage', 'unknown')),
                        spell_plan=plan if isinstance(plan, dict) else None,
                        state=state), ensure_ascii=False, indent=2))
                    print(f'Informe del fallo: {report_path}', file=sys.stderr)
                except OSError as report_error:
                    print(f'No se pudo guardar el informe: {report_error}', file=sys.stderr)
            raise
        if result and result not in ('probing', 'settling', 'checking', 'observing_click',
                                     'observing_option', 'observing_placement') and result != last_event:
            print(result, flush=True)
            last_event = result
        if loop.finished:
            return
    raise SafetyStop('El monitor terminó antes de completar el bucle; revisar su salida y la captura JSONL')


def stop_monitor(process):
    if process.stdout is not None:
        process.stdout.close()
    if process.poll() is None:
        # sudo forwards SIGTERM to its monitored command, including during a scan.
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', default=os.environ.get('DMO_CHARACTER', 'JQN'))
    parser.add_argument('--window', action='store_true',
                        help='Modo experimental para ventana/borderless: coordenadas relativas a DMO')
    parser.add_argument('--spell-probe', action='store_true',
                        help='Elegir una mágica según las reglas y detenerse con su menú abierto')
    parser.add_argument('--monsters-only', action='store_true', help='Usar únicamente el bucle de monstruos')
    parser.add_argument('--matches', type=int, default=1, help='Cantidad de duelos; 0: repetir hasta detenerse')
    parser.add_argument('--shutdown-on-exit', action='store_true',
                        help='Apagar con shutdown -h now al salir, excepto si se detiene con Ctrl+C')
    parser.add_argument('--config', type=Path, default=ROOT / 'config.json')
    parser.add_argument('--turns', type=int, default=0,
                        help='0: continuar hasta terminar este duelo')
    parser.add_argument('--delay', type=int, default=5,
                        help='segundos para volver al juego después de autenticar sudo')
    parser.add_argument('--trace', type=Path,
                        default=Path('/tmp') / f'dmo-run-{datetime.now():%Y%m%d-%H%M%S}.jsonl')
    parser.add_argument('--session-log-dir', type=Path, default=SESSION_LOG_DIR,
                        help='Carpeta persistente para los resúmenes de cada sesión')
    args = parser.parse_args(argv)
    if args.turns < 0 or args.delay < 0 or args.matches < 0:
        parser.error('--turns, --delay y --matches no pueden ser negativos')
    if args.matches != 1 and (args.turns or args.spell_probe):
        parser.error('La repetición de partidas requiere jugar el duelo completo, sin --turns ni --spell-probe')
    if args.monsters_only and args.spell_probe:
        parser.error('--monsters-only no se combina con --spell-probe')
    shutdown = shutil.which('shutdown') if args.shutdown_on_exit else None
    if args.shutdown_on_exit and shutdown is None:
        parser.error('--shutdown-on-exit requiere el comando shutdown')
    previous_handlers = {}
    interruption_signal = None
    def interrupted(signum, frame):
        nonlocal shutdown, interruption_signal
        interruption_signal = signum
        if signum == signal.SIGINT:
            shutdown = None
        raise KeyboardInterrupt
    if args.shutdown_on_exit:
        print('Apagado automático activado; Ctrl+C detiene el bot sin apagar el equipo.', flush=True)
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous_handlers[signum] = signal.signal(signum, interrupted)
    process = None
    bot = None
    stats = MatchStats()
    trace_path = args.trace
    started_at = datetime.now().astimezone()
    metrics = SessionMetrics()
    reason, exit_code, session_error = 'unexpected_error', 1, None
    try:
        if os.geteuid() == 0:
            raise SafetyStop('Ejecutá este lanzador con python, sin sudo; solo el monitor necesita sudo')
        missing = [tool for tool in ('sudo', 'hyprctl', 'ydotool', 'grim') if not shutil.which(tool)]
        if missing:
            raise SafetyStop('Faltan herramientas: ' + ', '.join(missing))
        config = json.loads(args.config.read_text())
        bot = Bot(config)
        if args.window:
            bot.desktop = WindowDesktop(bot.desktop, WindowFrame.discover(config))
            bot.vision.desktop = bot.desktop
        else:
            bot.desktop = MemoryStartDesktop(bot.desktop)
            bot.vision.desktop = bot.desktop
        bot.desktop.require_tools()
        matches = 0
        cache = SceneCache(SCENE_CACHE)
        spells = not args.monsters_only
        while args.matches == 0 or matches < args.matches:
            trace_path = (args.trace if matches == 0 else args.trace.with_name(
                f'{args.trace.stem}-match-{matches + 1:03d}{args.trace.suffix}'))
            with trace_path.open('w') as trace:
                print(f'Partida {matches + 1} · captura: {trace_path}', flush=True)
                if matches == 0:
                    subprocess.run(['sudo', '-v'], check=True)
                if matches:
                    bot.vision.wait_for('world', timeout=30)
                start_match(bot, args.delay if matches == 0 else 0)
                stats.start()
                input_desktop = bot.desktop.input
                loop = DuelLoop(input_desktop, config, max_turns=args.turns,
                               spell_probe=args.spell_probe, spells=spells)
                if process is None:
                    process = subprocess.Popen(monitor_command(args.name, spells=spells),
                        stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True)
                consume(process.stdout, trace, loop, cache, stats,
                        metrics=metrics,
                        camera=input_desktop,
                        camera_point=(config['screen']['width'] // 2,
                                      config['screen']['height'] // 2),
                        camera_ticks=config.get('memory_camera_scroll_up', 12))
                if args.spell_probe:
                    print(f'Captura del menú: {bot.desktop.save_debug("spell-menu")}', flush=True)
            matches += 1
            stats.finish()
            print(stats.summary(), flush=True)
            print(metrics.summary(), flush=True)
        print('Bucle terminado.', flush=True)
        reason, exit_code = 'completed', 0
        return 0
    except KeyboardInterrupt:
        exit_code = 130
        if interruption_signal in (None, signal.SIGINT):
            reason = 'ctrl_c'
            shutdown = None
            print('Detenido con Ctrl+C. No se apagará la computadora.', file=sys.stderr)
        else:
            reason = signal.Signals(interruption_signal).name
            print('Ejecución interrumpida.', file=sys.stderr)
        return 130
    except (KeyError, TypeError, ValueError, OSError, RuntimeError,
            subprocess.SubprocessError) as error:
        reason, exit_code, session_error = 'error', 2, str(error)
        print(f'PARADA SEGURA: {error}', file=sys.stderr)
        if bot is not None:
            try:
                print(f'Captura de pantalla: {bot.desktop.save_debug("memory-run-stopped")}',
                      file=sys.stderr)
            except (OSError, RuntimeError, subprocess.SubprocessError):
                pass
        print(f'Estados guardados: {trace_path}', file=sys.stderr)
        return 2
    finally:
        stats.finish()
        try:
            if process is not None:
                stop_monitor(process)
        finally:
            try:
                try:
                    session_path = save_session_summary(args.session_log_dir, stats, started_at,
                        name=args.name, reason=reason, exit_code=exit_code, error=session_error,
                        trace_path=trace_path, shutdown_enabled=shutdown is not None, metrics=metrics)
                except OSError as error:
                    print(f'No se pudo guardar el resumen de sesión: {error}', file=sys.stderr)
                else:
                    print(f'Resumen de sesión: {session_path}', flush=True)
                print(stats.summary(), flush=True)
                print(metrics.summary(), flush=True)
            finally:
                # Terminal closure may make printing fail. Still request the
                # explicitly enabled shutdown after closing the trace/monitor.
                try:
                    if shutdown is not None:
                        subprocess.run([shutdown, '-h', 'now'], check=True,
                                       stdin=subprocess.DEVNULL, timeout=30)
                except (OSError, subprocess.SubprocessError) as error:
                    print(f'No se pudo apagar la computadora: {error}', file=sys.stderr)
                finally:
                    for signum, handler in previous_handlers.items():
                        signal.signal(signum, handler)


if __name__ == '__main__':
    raise SystemExit(main())
