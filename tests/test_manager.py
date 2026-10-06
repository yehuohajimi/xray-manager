"""Exercise menu input, real process locks, and unchanged CLI accounting."""

import fcntl
from contextlib import closing
import importlib.util
import json
import os
import pathlib
import pty
import select
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


SOURCE = pathlib.Path(__file__).resolve().parents[1] / 'manager.py'


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        spec = importlib.util.spec_from_file_location('tested_manager', SOURCE)
        self.manager = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.manager)
        self.manager.ROOT = self.root
        self.manager.CONFIG = self.root / 'config.json'
        self.manager.XRAY = str(self.root / 'xray')
        self.manager.CONFIG.write_text(json.dumps({'inbounds': [{
            'tag': 'vless-in', 'port': 443,
            'settings': {'clients': [{'email': 'phone', 'id': 'original-uuid'}]},
            'streamSettings': {'realitySettings': {
                'serverNames': ['example.com'], 'shortIds': ['1234']}},
        }]}))
        (self.root / 'connection.json').write_text(json.dumps({
            'server': '192.0.2.1', 'public_key': 'public-key'}))
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(self.manager.os, 'geteuid', return_value=0).start()
        mock.patch.object(self.manager.glob, 'glob', return_value=[]).start()
        mock.patch.object(self.manager, 'run', side_effect=self.fake_run).start()
        self.commands = []

    def fake_run(self, *args):
        self.commands.append(args)
        if args[:2] == ('systemctl', 'show'):
            return 'epoch-1\n'
        if args[0] == self.manager.XRAY and args[1] == 'api':
            return json.dumps({'stat': [{
                'name': 'user>>>phone>>>traffic>>>uplink', 'value': 10}]})
        return ''

    def assert_unlocked(self):
        with (self.root / 'manager.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock, fcntl.LOCK_UN)
        self.assertIsNone(self.manager.db)

    def totals(self):
        with closing(sqlite3.connect(self.root / 'stats.sqlite3')) as db:
            return db.execute('SELECT last,total FROM counters').fetchall()

    def test_import_and_help_do_not_open_state(self):
        self.assertFalse((self.root / 'manager.lock').exists())
        self.manager.ROOT = self.root / 'missing'
        with mock.patch('builtins.print'):
            self.assertEqual(self.manager.main(['help']), 0)
            with mock.patch.object(sys.stdin, 'isatty', return_value=False):
                self.assertEqual(self.manager.main([]), 0)
        self.assertFalse(self.manager.ROOT.exists())

    def test_non_terminal_menu_fails_without_reading_input(self):
        with mock.patch.object(sys.stdin, 'isatty', return_value=False), \
                mock.patch('builtins.input') as prompt, mock.patch('builtins.print'):
            self.assertEqual(self.manager.main(['menu']), 1)
            prompt.assert_not_called()
        self.assertFalse((self.root / 'manager.lock').exists())

    def test_menu_defaults_for_terminal_and_eof_exits(self):
        with mock.patch.object(sys.stdin, 'isatty', return_value=True), \
                mock.patch.object(sys.stdout, 'isatty', return_value=True), \
                mock.patch('builtins.input', side_effect=EOFError), \
                mock.patch('builtins.print'):
            self.assertEqual(self.manager.main([]), 0)
        self.assert_unlocked()

    def test_menu_remains_usable_after_operation_failure(self):
        with mock.patch('builtins.input', side_effect=['13', '', '2', '', '0']), \
                mock.patch('builtins.print'), \
                mock.patch.object(self.manager, 'collect', side_effect=RuntimeError('API unavailable')):
            self.manager.interactive_menu()
        self.assert_unlocked()

    def test_every_prompt_releases_state_and_allows_collection(self):
        answers = iter(['4', '1', 'n', '', '5', '1', '', '0'])

        def answer(prompt):
            self.assert_unlocked()
            self.manager.execute(['collect'])
            return next(answers)

        with mock.patch('builtins.input', side_effect=answer), mock.patch('builtins.print'):
            self.manager.interactive_menu()
        self.assertEqual(self.manager.device_names(), ['phone'])
        self.assertEqual(self.totals(), [(10, 10)])

    def test_cancelled_changes_do_not_restart_or_stop(self):
        for choice, answers in [('3', ['tablet', '']), ('4', ['1', 'n']),
                                ('10', ['no']), ('11', ['']), ('5', [''])]:
            with self.subTest(choice=choice), \
                    mock.patch('builtins.input', side_effect=answers), mock.patch('builtins.print'):
                self.manager.menu_action(choice)
                self.assert_unlocked()
        self.assertEqual(self.commands, [])
        self.assertEqual(self.manager.device_names(), ['phone'])

    def test_confirmed_menu_actions_and_share_all(self):
        with mock.patch('builtins.print') as output:
            with mock.patch('builtins.input', side_effect=['tablet', 'y']):
                self.manager.menu_action('3')
            with mock.patch('builtins.input', return_value='a'):
                self.manager.menu_action('5')
            with mock.patch('builtins.input', side_effect=['2', 'yes']):
                self.manager.menu_action('4')
            with mock.patch('builtins.input', return_value='7'):
                self.manager.menu_action('6')
        self.assertEqual(self.manager.device_names(), ['phone'])
        self.assertTrue(any('tablet:' in str(call) for call in output.call_args_list))
        self.assertTrue(any('Last 7 days' in str(call) for call in output.call_args_list))
        self.assert_unlocked()

    def test_service_actions_collect_before_restart_and_stop(self):
        with mock.patch('builtins.print'), mock.patch('builtins.input', return_value='y'):
            self.manager.menu_action('9')
            self.assertEqual(self.commands, [('systemctl', 'start', 'xray')])
            for choice, command in [('10', 'restart'), ('11', 'stop')]:
                self.commands.clear()
                self.manager.menu_action(choice)
                self.assertEqual(self.commands[-1], ('systemctl', command, 'xray'))
                self.assertTrue(any(args[0] == self.manager.XRAY and args[1] == 'api'
                                    for args in self.commands[:-1]))
        self.assert_unlocked()

    def test_status_and_logs_work_without_state_or_lock(self):
        self.manager.ROOT = self.root / 'missing'
        with mock.patch.object(self.manager.subprocess, 'run'), mock.patch('builtins.print'):
            self.manager.execute(['status'])
            self.manager.execute(['logs'])
        self.assertFalse(self.manager.ROOT.exists())

    def test_failed_database_open_releases_lock(self):
        with mock.patch.object(self.manager.sqlite3, 'connect', side_effect=sqlite3.OperationalError('cannot open')):
            with self.assertRaises(sqlite3.OperationalError):
                self.manager.execute(['collect'])
        self.assert_unlocked()

    def test_cli_devices_and_stats_preserve_accounts_and_totals(self):
        with mock.patch('builtins.print') as output:
            self.manager.execute(['add-device', 'tablet'])
            self.manager.execute(['share', 'phone'])
            self.manager.execute(['stats'])
            self.manager.execute(['stats', '7'])
            self.manager.execute(['remove-device', 'tablet'])
        self.assertEqual(self.manager.device_names(), ['phone'])
        self.assertEqual(self.manager.inbound(self.manager.config())['settings']['clients'][0]['id'],
                         'original-uuid')
        self.assertEqual(self.totals(), [(10, 10)])
        self.assertTrue(any('vless://original-uuid@192.0.2.1' in str(call)
                            for call in output.call_args_list))
        self.assertEqual(self.commands.count(('systemctl', 'restart', 'xray')), 2)
        self.assert_unlocked()

    def test_mutation_holds_lock_through_collection_and_restart(self):
        original = self.fake_run

        def check_lock(*args):
            with (self.root / 'manager.lock').open('a') as other:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return original(*args)

        with mock.patch.object(self.manager, 'run', side_effect=check_lock), mock.patch('builtins.print'):
            self.manager.execute(['add-device', 'tablet'])
        self.assert_unlocked()

    def test_lock_and_database_are_released_after_failures(self):
        for failure in (RuntimeError('API unavailable'), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__), \
                    mock.patch.object(self.manager, 'collect', side_effect=failure):
                with self.assertRaises(type(failure)):
                    self.manager.execute(['collect'])
            self.assert_unlocked()
        self.manager.execute(['collect'])
        self.assertEqual(self.totals(), [(10, 10)])

    def test_invalid_arguments_do_not_collect_or_open_state(self):
        for args in (['stats', '0'], ['stats', 'bad'], ['stats', '91'],
                     ['add-device', 'bad name'], ['remove-device'], ['typo']):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    self.manager.execute(args)
        self.assertEqual(self.commands, [])
        self.assertFalse((self.root / 'manager.lock').exists())

    def test_stale_device_selection_is_rechecked_before_write(self):
        self.manager.execute(['collect'])
        with mock.patch('builtins.print'):
            self.manager.execute(['remove-device', 'phone'])
        restarts = self.commands.count(('systemctl', 'restart', 'xray'))
        with self.assertRaisesRegex(ValueError, 'Device not found'):
            self.manager.execute(['remove-device', 'phone'])
        self.assertEqual(self.commands.count(('systemctl', 'restart', 'xray')), restarts)
        self.assert_unlocked()

    def prepare_processes(self):
        # Import the real source, redirect state and service commands into the fixture.
        (self.root / 'runner.py').write_text(
            'import importlib.util, pathlib, sys\n'
            f'spec = importlib.util.spec_from_file_location("manager", {str(SOURCE)!r})\n'
            'm = importlib.util.module_from_spec(spec)\n'
            'spec.loader.exec_module(m)\n'
            f'm.ROOT = pathlib.Path({str(self.root)!r})\n'
            'm.CONFIG = m.ROOT / "config.json"\n'
            'm.XRAY = str(m.ROOT / "xray")\n'
            'm.os.geteuid = lambda: 0\n'
            'm.glob.glob = lambda pattern: []\n'
            'sys.exit(m.main())\n')
        (self.root / 'systemctl').write_text('#!/bin/sh\nif [ "$1" = show ]; then echo epoch-1; fi\n')
        (self.root / 'xray').write_text(
            '#!/bin/sh\nif [ "$1" = api ]; then\n'
            'echo \'{"stat":[{"name":"user>>>phone>>>traffic>>>uplink","value":10}]}\'\n'
            'fi\n')
        for command in ('systemctl', 'xray'):
            (self.root / command).chmod(0o755)
        env = os.environ.copy()
        env['PATH'] = str(self.root) + os.pathsep + env['PATH']
        return env

    def spawn(self, args, env, **kwargs):
        child = subprocess.Popen([sys.executable, str(self.root / 'runner.py'), *args],
                                 env=env, **kwargs)

        def cleanup():
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)

        self.addCleanup(cleanup)
        return child

    def test_concurrent_collectors_serialize_and_do_not_double_count(self):
        env = self.prepare_processes()
        with (self.root / 'manager.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            children = [self.spawn(['collect'], env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE) for _ in range(2)]
            # Neither collector can finish while an independent process owns the lock.
            for child in children:
                with self.assertRaises(subprocess.TimeoutExpired):
                    child.wait(timeout=0.1)
            fcntl.flock(lock, fcntl.LOCK_UN)
        for child in children:
            stdout, stderr = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, stderr.decode())
        self.assertEqual(self.totals(), [(10, 10)])

    def test_real_terminal_menu_does_not_block_background_collector(self):
        env = self.prepare_processes()
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        menu = self.spawn([], env, stdin=slave, stdout=slave, stderr=slave)

        def read_until(marker):
            data = b''
            deadline = time.monotonic() + 5
            while marker.encode() not in data:
                remaining = deadline - time.monotonic()
                self.assertGreater(remaining, 0, data.decode(errors='replace'))
                ready, _, _ = select.select([master], [], [], remaining)
                if ready:
                    data += os.read(master, 65536)
            return data.decode()

        def collect_while_waiting():
            self.assert_unlocked()
            child = self.spawn(['collect'], env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            _, stderr = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, stderr.decode())

        read_until('选择操作:')
        collect_while_waiting()
        os.write(master, b'4\n')
        read_until('选择设备编号')
        collect_while_waiting()
        os.write(master, b'1\n')
        read_until('[y/N]:')
        collect_while_waiting()
        os.write(master, b'n\n')
        read_until('按回车返回菜单')
        collect_while_waiting()
        os.write(master, b'\n')
        read_until('选择操作:')
        os.write(master, b'0\n')
        self.assertEqual(menu.wait(timeout=5), 0)
        self.assertEqual(self.totals(), [(10, 10)])
        self.assertEqual(self.manager.device_names(), ['phone'])


if __name__ == '__main__':
    unittest.main()
