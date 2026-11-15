import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import dmo_run_bot as runner


class MatchRunnerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.session_logs = Path(folder.name) / 'sessions'
        patcher = patch.object(runner, 'SESSION_LOG_DIR', self.session_logs)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_shutdown_on_exit_after_cleanup_for_normal_error_and_interruptions(self):
        def signal_stop(signum):
            def stop(*args, **kwargs):
                runner.signal.getsignal(signum)(signum, None)
            return stop

        cases = [(None, 0, True), (RuntimeError('fallo'), 2, True),
                 (KeyboardInterrupt, 130, False),
                 (signal_stop(runner.signal.SIGINT), 130, False),
                 (signal_stop(runner.signal.SIGHUP), 130, True),
                 (signal_stop(runner.signal.SIGTERM), 130, True)]
        for failure, expected, should_shutdown in cases:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as folder:
                config, trace = Path(folder) / 'config.json', Path(folder) / 'run.jsonl'
                config.write_text('{"screen":{"width":1920,"height":1080}}')
                process = Mock(stdout=io.StringIO())
                process.poll.return_value = None
                handlers = {s: runner.signal.getsignal(s)
                            for s in (runner.signal.SIGINT, runner.signal.SIGHUP, runner.signal.SIGTERM)}
                def run(command, **kwargs):
                    if command[0] == '/usr/bin/shutdown':
                        reports = sorted(self.session_logs.glob('*.json'))
                        self.assertTrue(reports)  # Persisted before requesting shutdown.
                        self.assertEqual(json.loads(reports[-1].read_text())['exit_code'], expected)
                        self.assertTrue(process.stdout.closed)
                        process.terminate.assert_called_once_with()
                        process.wait.assert_called_once_with(timeout=5)
                with (patch.object(runner.os, 'geteuid', return_value=1000),
                      patch.object(runner.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name),
                      patch.object(runner, 'Bot'), patch.object(runner, 'Desktop'),
                      patch.object(runner, 'DuelLoop'),
                      patch.object(runner.subprocess, 'run', side_effect=run) as commands,
                      patch.object(runner, 'start_match'),
                      patch.object(runner.subprocess, 'Popen', return_value=process),
                      patch.object(runner, 'consume', side_effect=failure)):
                    result = runner.main(['--shutdown-on-exit', '--config', str(config),
                                          '--trace', str(trace)])
                self.assertEqual(result, expected)
                reports = sorted(self.session_logs.glob('*.json'))
                report = json.loads(reports[-1].read_text())
                self.assertEqual(report['exit_code'], expected)
                self.assertEqual(report['matches_started'], 1)
                self.assertEqual(report['unknown'], 1)
                self.assertEqual(report['shutdown_enabled'], should_shutdown)
                if isinstance(failure, RuntimeError):
                    self.assertEqual(report['error'], 'fallo')
                    self.assertEqual(report['reason'], 'error')
                elif not should_shutdown:
                    self.assertEqual(report['reason'], 'ctrl_c')
                self.assertTrue(process.stdout.closed)
                process.terminate.assert_called_once_with()
                if should_shutdown:
                    self.assertEqual(commands.call_count, 2)
                    commands.assert_called_with(['/usr/bin/shutdown', '-h', 'now'], check=True,
                                                stdin=subprocess.DEVNULL, timeout=30)
                else:
                    commands.assert_called_once_with(['sudo', '-v'], check=True)
                for signum, handler in handlers.items():
                    self.assertEqual(runner.signal.getsignal(signum), handler)

    def test_session_summary_preserves_results_and_does_not_overwrite(self):
        from datetime import datetime
        stats = runner.MatchStats()
        stats.started, stats.wins, stats.losses, stats.forfeits, stats.unknown = 10, 6, 2, 1, 1
        for _ in range(2):
            started = datetime.now().astimezone()
            runner.save_session_summary(self.session_logs, stats, started,
                name='JQN', reason='completed', exit_code=0, error=None,
                trace_path=Path('/tmp/test.jsonl'), shutdown_enabled=False)
        reports = list(self.session_logs.glob('*.json'))
        self.assertEqual(len(reports), 2)
        report = json.loads(reports[0].read_text())
        self.assertEqual((report['matches_started'], report['wins'], report['losses'],
                          report['forfeits'], report['unknown']), (10, 6, 2, 1, 1))
        self.assertGreaterEqual(report['duration_seconds'], 0)
        self.assertEqual(report['last_trace'], '/tmp/test.jsonl')
        self.assertIsNotNone(datetime.fromisoformat(report['ended_at']).tzinfo)

    def test_session_log_contains_time_and_observed_money_rate(self):
        from datetime import datetime
        from tests.test_dmo_session_metrics import currency_state
        now = [0]
        metrics = runner.SessionMetrics(clock=lambda: now[0])
        metrics.observe(currency_state(1000))
        now[0] = 120
        metrics.observe(currency_state(1300))
        path = runner.save_session_summary(self.session_logs, runner.MatchStats(),
            datetime.now().astimezone(), name='JQN', reason='ctrl_c', exit_code=130,
            error=None, trace_path=Path('/tmp/test.jsonl'), shutdown_enabled=False, metrics=metrics)
        report = json.loads(path.read_text())
        self.assertEqual(report['duration_hms'], '00:02:00')
        self.assertEqual(report['duration_seconds'], 120)
        self.assertEqual((report['initial_money'], report['final_money']), (1000, 1300))
        self.assertEqual(report['money_generated'], 300)
        self.assertEqual(report['money_per_minute'], 150)
        self.assertIn('Dinero/min: 150.00', report['metrics_summary'])

    def test_monitor_enables_currency_for_farming(self):
        self.assertIn('--currency', runner.monitor_command('JQN'))

    def test_monitor_preserves_installation_and_uses_current_interpreter(self):
        with patch.object(runner, 'game_directory', return_value=Path('/tmp/my game/Release')):
            command = runner.monitor_command('Player')
        self.assertEqual(command[:3], ['sudo', '-n', 'env'])
        self.assertIn('DMO_GAME_DIR=/tmp/my game/Release', command)
        self.assertIn(runner.sys.executable, command)

    def test_ctrl_c_session_report_retains_recent_balance_after_unstable_snapshot(self):
        from datetime import datetime
        from tests.test_dmo_session_metrics import currency_state
        now = [0]
        metrics = runner.SessionMetrics(clock=lambda: now[0])
        first = currency_state(63867)
        metrics.observe(first)
        now[0] = 31194.43
        metrics.observe(currency_state(77937))
        now[0] = 31196.33
        metrics.observe(dict(status='unstable', character=first['character'],
                             process_identity=first['process_identity']))
        path = runner.save_session_summary(self.session_logs, runner.MatchStats(),
            datetime.now().astimezone(), name='JQN', reason='ctrl_c', exit_code=130,
            error=None, trace_path=Path('/tmp/trace.jsonl'), shutdown_enabled=False, metrics=metrics)
        report = json.loads(path.read_text())
        self.assertEqual(report['reason'], 'ctrl_c')
        self.assertEqual(report['money_generated'], 14070)
        self.assertEqual(report['money_per_minute'], 27.06)
        self.assertEqual(report['money_metric_status'], 'observed_recent')
        self.assertIn('saldo de hace 1.9 s', report['metrics_summary'])

    def test_metrics_receive_fresh_sample_even_if_action_fails(self):
        from tests.test_dmo_session_metrics import currency_state
        metrics = runner.SessionMetrics()
        state = currency_state(1000)
        loop = Mock(finished=False)
        loop.observe.side_effect = RuntimeError('action failed')
        with self.assertRaisesRegex(RuntimeError, 'action failed'):
            runner.consume(io.StringIO(json.dumps(state) + '\n'), io.StringIO(), loop, metrics=metrics)
        self.assertEqual(metrics.initial_money, 1000)

    def test_buffered_old_balance_is_not_used_for_metrics(self):
        from tests.test_dmo_session_metrics import currency_state
        old = dict(currency_state(100000), timestamp='2000-01-01T00:00:00+00:00')
        new = currency_state(1000)
        metrics = runner.SessionMetrics()
        runner.consume(io.StringIO(json.dumps(old) + '\n' + json.dumps(new) + '\n'),
                       io.StringIO(), Mock(finished=True), metrics=metrics)
        self.assertEqual(metrics.initial_money, 1000)
        self.assertEqual(metrics.observations, 1)

    def test_session_log_failure_does_not_prevent_shutdown(self):
        with (patch.object(runner.os, 'geteuid', return_value=1000),
              patch.object(runner.shutil, 'which', return_value='/usr/bin/shutdown'),
              patch.object(runner.Path, 'read_text', side_effect=OSError('config ausente')),
              patch.object(runner, 'save_session_summary', side_effect=OSError('disco lleno')),
              patch.object(runner.subprocess, 'run') as command,
              patch.object(runner.sys, 'stderr', new_callable=io.StringIO) as errors):
            result = runner.main(['--shutdown-on-exit'])
        self.assertEqual(result, 2)
        command.assert_called_once_with(['/usr/bin/shutdown', '-h', 'now'], check=True,
                                       stdin=subprocess.DEVNULL, timeout=30)
        self.assertIn('No se pudo guardar el resumen de sesión: disco lleno', errors.getvalue())

    def test_missing_shutdown_rejects_option_without_starting_bot(self):
        with (patch.object(runner.shutil, 'which', return_value=None),
              patch.object(runner, 'Bot') as bot,
              patch.object(runner.subprocess, 'run') as command):
            with self.assertRaises(SystemExit) as error:
                runner.main(['--shutdown-on-exit'])
        self.assertEqual(error.exception.code, 2)
        bot.assert_not_called()
        command.assert_not_called()

    def test_shutdown_permission_failure_is_reported(self):
        with (patch.object(runner.os, 'geteuid', return_value=1000),
              patch.object(runner.shutil, 'which', return_value='/usr/bin/shutdown'),
              patch.object(runner.Path, 'read_text', side_effect=OSError('config ausente')),
              patch.object(runner.subprocess, 'run', side_effect=subprocess.CalledProcessError(
                  1, ['shutdown', '-h', 'now'])) as command,
              patch.object(runner.sys, 'stderr', new_callable=io.StringIO) as errors):
            result = runner.main(['--shutdown-on-exit'])
        self.assertEqual(result, 2)
        command.assert_called_once()
        self.assertIn('No se pudo apagar la computadora', errors.getvalue())

    def test_multiple_matches_reuse_monitor_and_verify_world(self):
        with tempfile.TemporaryDirectory() as folder:
            trace, config = Path(folder) / 'run.jsonl', Path(folder) / 'config.json'
            config.write_text('{"screen":{"width":1920,"height":1080}}')
            process = Mock(stdout=io.StringIO())
            process.poll.return_value = None
            with (patch.object(runner.os, 'geteuid', return_value=1000),
                  patch.object(runner.shutil, 'which', return_value='/usr/bin/tool'),
                  patch.object(runner, 'Bot') as bot,
                  patch.object(runner, 'Desktop'), patch.object(runner, 'DuelLoop'),
                  patch.object(runner.subprocess, 'run') as authenticate,
                  patch.object(runner, 'start_match') as start,
                  patch.object(runner.subprocess, 'Popen', return_value=process) as launch,
                  patch.object(runner, 'consume') as consume):
                result = runner.main(['--matches', '2', '--config', str(config), '--trace', str(trace)])
            self.assertEqual(result, 0)
            self.assertEqual(start.call_count, 2)
            self.assertEqual(consume.call_count, 2)
            launch.assert_called_once()
            authenticate.assert_called_once_with(['sudo', '-v'], check=True)
            bot.return_value.vision.wait_for.assert_called_once_with('world', timeout=30)
            process.terminate.assert_called_once_with()
            self.assertTrue(trace.with_name('run-match-002.jsonl').exists())

    def test_stale_buffered_samples_are_saved_without_actions(self):
        old = json.dumps(dict(timestamp='2000-01-01T00:00:00+00:00', status='duel')) + '\n'
        new = '{"status":"outside_duel"}\n'
        trace, loop = io.StringIO(), Mock(finished=True)
        runner.consume(io.StringIO(old + new), trace, loop)
        loop.observe.assert_called_once_with(dict(status='outside_duel'))
        self.assertEqual(trace.getvalue(), old + new)

    def test_camera_is_scrolled_once_before_first_duel_action(self):
        sample = json.dumps(dict(status='duel', phase='DRAW')) + '\n'
        stream = io.StringIO(sample * 2)
        loop = Mock(finished=False)
        def observe(state):
            loop.finished = True
        loop.observe.side_effect = observe
        camera = Mock()
        camera.move.return_value = (960, 540)
        camera.position.return_value = (960, 540)
        runner.consume(stream, io.StringIO(), loop, camera=camera,
                       camera_point=(960, 540), camera_ticks=12)
        camera.scroll_up.assert_called_once_with(12)
        loop.observe.assert_called_once_with(dict(status='duel', phase='DRAW'))

    def test_recovered_failures_are_saved_even_when_loop_continues(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'run.jsonl'
            loop = Mock(finished=False, recovery_error='No se verificó el robo',
                        recovery_origin='await_draw', recovery_attempts=1)
            def observe(state):
                if state['sample'] == 1:
                    return 'recovery_started: 1/2'
                loop.finished = True
                return 'turn_passed: 1'
            loop.observe.side_effect = observe
            with path.open('w') as trace:
                runner.consume(io.StringIO('{"sample":1}\n{"sample":2}\n'), trace, loop)
            report = json.loads(path.with_suffix('.recovery.jsonl').read_text())
            self.assertEqual(report['loop_stage'], 'await_draw')
            self.assertEqual(report['state'], dict(sample=1))
            self.assertFalse(path.with_suffix('.error.json').exists())

    def test_scene_cache_accumulates_links_and_replaces_other_process(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'cache.json'
            cache = runner.SceneCache(path)
            identity = dict(pid=123, process_start='start', binary_sha256='hash')
            def state(element):
                return dict(process_identity=identity, scene=dict(hand_elements=[dict(
                    element=element, card_address='0x2000', native_element='0x3000')]))
            cache.observe(state('0x1000'))
            cache.observe(state('0x1100'))
            report = json.loads(path.read_text())
            self.assertEqual(len(report['scene']['hand_elements']), 2)
            self.assertEqual(report['pid'], 123)
            identity = dict(identity, process_start='new-process')
            cache.observe(state('0x1200'))
            report = json.loads(path.read_text())
            self.assertEqual(len(report['scene']['hand_elements']), 1)
            self.assertEqual(report['process_start'], 'new-process')

    def test_scene_cache_does_not_save_unidentified_process(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'cache.json'
            runner.SceneCache(path).observe(dict(scene=dict(hand_elements=[dict(
                element='0x1000', card_address='0x2000', native_element='0x3000')])))
            self.assertFalse(path.exists())

    def test_start_requires_world_before_any_input(self):
        bot = Mock()
        bot.vision.is_state.return_value = False
        with self.assertRaisesRegex(runner.SafetyStop, 'No se reconoció el mundo'):
            runner.start_match(bot, 0)
        bot.start_duel.assert_not_called()

    def test_start_requires_templates_before_any_input(self):
        bot = Mock()
        bot.vision.calibrated.side_effect = lambda state: state != 'duel_lobby'
        with self.assertRaisesRegex(runner.SafetyStop, 'duel_lobby'):
            runner.start_match(bot, 0)
        bot.desktop.screenshot.assert_not_called()
        bot.start_duel.assert_not_called()

    def test_reuses_visual_start_after_world_verification(self):
        bot = Mock()
        runner.start_match(bot, 0)
        bot.vision.is_state.assert_called_once_with('world', bot.desktop.screenshot.return_value)
        bot.start_duel.assert_called_once_with()

    def test_memory_start_click_sends_motion_before_click(self):
        legacy = Mock(config=dict(timing=dict(click_delay=0.8)))
        desktop = runner.MemoryStartDesktop(legacy)
        desktop.input = Mock()
        desktop.input.move.return_value = (1618, 957)
        desktop.input.position.return_value = (1618, 957)
        with patch.object(runner.time, 'sleep') as sleep:
            desktop.click([1618, 957])
        desktop.input.move.assert_called_once_with((1618, 957))
        desktop.input.click.assert_called_once_with()
        sleep.assert_called_once_with(0.8)

    def test_start_retries_when_settings_remain_open(self):
        bot = Mock()
        bot.start_duel.side_effect = runner.SafetyStop("No apareció 'duel_lobby'")
        bot.vision.is_state.side_effect = [True, True]
        with patch.object(runner.time, 'sleep'):
            runner.start_match(bot, 0)
        self.assertEqual([call.args[0] for call in bot.click.call_args_list],
                         ['start', 'add_npc', 'bonz', 'ready'])
        bot.vision.wait_for.assert_called_once_with('duel_lobby')

    def test_start_does_not_retry_without_settings_screen(self):
        bot = Mock()
        bot.start_duel.side_effect = runner.SafetyStop('lobby failure')
        bot.vision.is_state.side_effect = [True, False]
        with patch.object(runner.time, 'sleep'):
            with self.assertRaisesRegex(runner.SafetyStop, 'lobby failure'):
                runner.start_match(bot, 0)
        bot.click.assert_not_called()

    def test_keeps_failing_sample_in_trace(self):
        line = json.dumps({'status': 'duel'}) + '\n'
        trace, loop = io.StringIO(), Mock()
        loop.observe.side_effect = RuntimeError('attack unconfirmed')
        with self.assertRaisesRegex(RuntimeError, 'attack unconfirmed'):
            runner.consume(io.StringIO(line), trace, loop)
        self.assertEqual(trace.getvalue(), line)

    def test_stops_on_finished_loop_without_consuming_more_states(self):
        trace, loop = io.StringIO(), Mock(finished=True)
        stream = io.StringIO('{"status":"world"}\ninvalid\n')
        runner.consume(stream, trace, loop)
        self.assertEqual(trace.getvalue(), '{"status":"world"}\n')
        self.assertEqual(stream.readline(), 'invalid\n')

    def test_authentication_precedes_start_and_monitor_cleanup_on_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            trace = Path(folder) / 'trace.jsonl'
            config = Path(folder) / 'config.json'
            config.write_text('{"screen":{"width":1920,"height":1080}}')
            process = Mock(stdout=io.StringIO('{"status":"duel"}\n'))
            process.poll.return_value = None
            events = []
            with (patch.object(runner.os, 'geteuid', return_value=1000),
                  patch.object(runner.shutil, 'which', return_value='/usr/bin/tool'),
                  patch.object(runner, 'Bot'), patch.object(runner, 'Desktop'),
                  patch.object(runner, 'DuelLoop') as loop_class,
                  patch.object(runner.subprocess, 'run', side_effect=lambda *a, **kw: events.append('auth')),
                  patch.object(runner, 'start_match', side_effect=lambda *a: events.append('start')),
                  patch.object(runner.subprocess, 'Popen', side_effect=lambda *a, **kw: (events.append('monitor') or process))):
                loop_class.return_value.observe.side_effect = RuntimeError('unconfirmed')
                result = runner.main(['--config', str(config), '--trace', str(trace)])
            self.assertEqual(result, 2)
            self.assertEqual(events, ['auth', 'start', 'monitor'])
            self.assertEqual(trace.read_text(), '{"status":"duel"}\n')
            report = json.loads(trace.with_suffix('.error.json').read_text())
            self.assertEqual(report['error'], 'unconfirmed')
            self.assertEqual(report['state'], dict(status='duel'))
            self.assertTrue(process.stdout.closed)
            process.terminate.assert_called_once_with()
            process.wait.assert_called_once_with(timeout=5)

    def test_cleanup_escalates_only_its_monitor_on_timeout(self):
        process = Mock(stdout=io.StringIO())
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired('monitor', 5), 0]
        runner.stop_monitor(process)
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()

    def test_summary_prints_on_ctrl_c_after_match_started(self):
        import contextlib
        with tempfile.TemporaryDirectory() as folder:
            trace, config = Path(folder) / 'run.jsonl', Path(folder) / 'config.json'
            config.write_text('{"screen":{"width":1920,"height":1080}}')
            output = io.StringIO()
            process = Mock(stdout=io.StringIO())
            process.poll.return_value = None
            with (patch.object(runner.os, 'geteuid', return_value=1000),
                  patch.object(runner.shutil, 'which', return_value='/usr/bin/tool'),
                  patch.object(runner, 'Bot'), patch.object(runner, 'Desktop'),
                  patch.object(runner, 'DuelLoop'),
                  patch.object(runner.subprocess, 'run'),
                  patch.object(runner, 'start_match'),
                  patch.object(runner.subprocess, 'Popen', return_value=process),
                  patch.object(runner, 'consume', side_effect=KeyboardInterrupt),
                  contextlib.redirect_stdout(output)):
                result = runner.main(['--config', str(config), '--trace', str(trace)])
            self.assertEqual(result, 130)
            self.assertIn('Partidas: 1 · Ganadas: 0 · Perdidas: 0 · Abandonadas: 0 · Sin resultado: 1',
                          output.getvalue())

    def test_summary_prints_after_automatic_error(self):
        import contextlib
        with tempfile.TemporaryDirectory() as folder:
            trace, config = Path(folder) / 'run.jsonl', Path(folder) / 'config.json'
            config.write_text('{"screen":{"width":1920,"height":1080}}')
            output = io.StringIO()
            process = Mock(stdout=io.StringIO())
            process.poll.return_value = None
            with (patch.object(runner.os, 'geteuid', return_value=1000),
                  patch.object(runner.shutil, 'which', return_value='/usr/bin/tool'),
                  patch.object(runner, 'Bot'), patch.object(runner, 'Desktop'),
                  patch.object(runner, 'DuelLoop'),
                  patch.object(runner.subprocess, 'run'),
                  patch.object(runner, 'start_match'),
                  patch.object(runner.subprocess, 'Popen', return_value=process),
                  patch.object(runner, 'consume', side_effect=RuntimeError('reinicio')),
                  contextlib.redirect_stdout(output)):
                result = runner.main(['--config', str(config), '--trace', str(trace)])
            self.assertEqual(result, 2)
            self.assertIn('Partidas: 1 · Ganadas: 0 · Perdidas: 0 · Abandonadas: 0 · Sin resultado: 1',
                          output.getvalue())


if __name__ == '__main__':
    unittest.main()
