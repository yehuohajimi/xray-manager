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
        self.manager.BACKUP_HOME = self.root / 'home'
        self.manager.BACKUP_HOME.mkdir()
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
        with mock.patch('builtins.input', side_effect=['3', '5', '', '0', '2', '1', '', '0', '0']), \
                mock.patch('builtins.print'), \
                mock.patch.object(self.manager, 'collect', side_effect=RuntimeError('API unavailable')):
            self.manager.interactive_menu()
        self.assert_unlocked()

    def test_every_prompt_releases_state_and_allows_collection(self):
        answers = iter(['2', '4', '1', 'n', '', '3', '1', '', '0', '0'])

        def answer(prompt):
            self.assert_unlocked()
            self.manager.execute(['collect'])
            return next(answers)

        with mock.patch('builtins.input', side_effect=answer), mock.patch('builtins.print'):
            self.manager.interactive_menu()
        self.assertEqual(self.manager.device_names(), ['phone'])
        self.assertEqual(self.totals(), [(10, 10)])

    def test_cancelled_changes_do_not_restart_or_stop(self):
        for action, choice, answers in [
                (self.manager.device_action, '2', ['tablet', '']),
                (self.manager.device_action, '4', ['1', 'n']),
                (self.manager.service_action, '2', ['no']),
                (self.manager.service_action, '3', ['']),
                (self.manager.device_action, '3', [''])]:
            with self.subTest(choice=choice), \
                    mock.patch('builtins.input', side_effect=answers), mock.patch('builtins.print'):
                action(choice)
                self.assert_unlocked()
        self.assertEqual(self.commands, [])
        self.assertEqual(self.manager.device_names(), ['phone'])

    def test_confirmed_menu_actions_and_share_all(self):
        with mock.patch('builtins.print') as output:
            with mock.patch('builtins.input', side_effect=['tablet', 'y']):
                self.manager.device_action('2')
            with mock.patch('builtins.input', return_value='a'):
                self.manager.device_action('3')
            with mock.patch('builtins.input', side_effect=['2', 'yes']):
                self.manager.device_action('4')
            with mock.patch('builtins.input', return_value='7'):
                self.manager.stats_action('1')
        self.assertEqual(self.manager.device_names(), ['phone'])
        self.assertTrue(any('tablet:' in str(call) for call in output.call_args_list))
        self.assertTrue(any('Last 7 days' in str(call) for call in output.call_args_list))
        self.assert_unlocked()

    def test_share_menu_prints_matching_qr_below_link_without_lock(self):
        code = '\x1b[37;40m██▀▄\x1b[0m\n'

        def encode(args, **kwargs):
            self.assert_unlocked()
            self.manager.execute(['collect'])
            self.assertEqual(args[0], 'qrencode')
            self.assertNotIn(kwargs['input'], args)
            self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
            self.assertEqual(kwargs['timeout'], 10)
            return code

        with mock.patch('builtins.input', return_value='1'), \
                mock.patch('builtins.print') as output, \
                mock.patch.object(self.manager.subprocess, 'check_output', side_effect=encode) as encoder, \
                mock.patch.object(self.manager.shutil, 'get_terminal_size', return_value=os.terminal_size((100, 40))):
            self.manager.device_action('3')
        printed = [call.args[0] for call in output.call_args_list if call.args]
        link_index = next(i for i, line in enumerate(printed) if 'vless://' in line)
        self.assertEqual(encoder.call_args.kwargs['input'], printed[link_index].split('\n')[1])
        self.assertIn('分享二维码', printed[link_index + 1])
        self.assertEqual(printed[link_index + 2], code)
        self.assertEqual(self.totals(), [(10, 10)])
        self.assert_unlocked()

    def test_share_all_encodes_each_devices_own_link(self):
        with mock.patch('builtins.print'):
            self.manager.execute(['add-device', 'tablet'])
        code = '\x1b[37;40m██\x1b[0m\n'
        with mock.patch('builtins.input', return_value='a'), \
                mock.patch('builtins.print') as output, \
                mock.patch.object(self.manager.subprocess, 'check_output', return_value=code) as encoder:
            self.manager.device_action('3')
        links = [call.args[0].split('\n')[1] for call in output.call_args_list
                 if call.args and '\nvless://' in call.args[0]]
        self.assertEqual(len(links), 2)
        self.assertNotEqual(links[0], links[1])
        self.assertEqual([call.kwargs['input'] for call in encoder.call_args_list], links)
        self.assertEqual(sum(call.args == (code,) for call in output.call_args_list), 2)

    def test_cli_share_stays_plain_text_without_qr_dependency(self):
        with mock.patch('builtins.print') as output, \
                mock.patch.object(self.manager.subprocess, 'check_output') as encoder:
            self.manager.execute(['share', 'phone'])
        encoder.assert_not_called()
        self.assertEqual(len(output.call_args_list), 1)
        self.assertIn('\nvless://', output.call_args.args[0])

    def test_qr_failure_retains_link_and_menu_can_continue(self):
        for error in (FileNotFoundError(), subprocess.CalledProcessError(1, 'qrencode'),
                      subprocess.TimeoutExpired('qrencode', 10), UnicodeError()):
            with self.subTest(error=type(error).__name__), \
                    mock.patch('builtins.print') as output, \
                    mock.patch.object(self.manager.subprocess, 'check_output', side_effect=error):
                self.manager.execute(['share', 'phone'], qr=True)
            printed = [call.args[0] for call in output.call_args_list if call.args]
            self.assertIn('\nvless://', printed[0])
            self.assertTrue(any('复制上方链接' in line for line in printed))
            self.assert_unlocked()

    def test_narrow_terminal_reports_required_width_without_wrapping_qr(self):
        code = '\x1b[37;40m' + '█' * 81 + '\x1b[0m\n'
        with mock.patch('builtins.print') as output, \
                mock.patch.object(self.manager.subprocess, 'check_output', return_value=code), \
                mock.patch.object(self.manager.shutil, 'get_terminal_size', return_value=os.terminal_size((80, 24))):
            self.manager.execute(['share', 'phone'], qr=True)
        printed = [call.args[0] for call in output.call_args_list if call.args]
        self.assertIn('\nvless://', printed[0])
        self.assertTrue(any('至少 81 列' in line for line in printed))
        self.assertNotIn(code, printed)

    def test_service_actions_collect_before_restart_and_stop(self):
        with mock.patch('builtins.print'), mock.patch('builtins.input', return_value='y'):
            self.manager.service_action('1')
            self.assertEqual(self.commands, [('systemctl', 'start', 'xray')])
            for choice, command in [('2', 'restart'), ('3', 'stop')]:
                self.commands.clear()
                self.manager.service_action(choice)
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

    def make_backups(self):
        config = self.root / 'config-20261006T120000.json'
        config.write_text('private-config')
        upgrade = self.root / 'upgrade-20261006T120000-abc'
        upgrade.mkdir()
        (upgrade / 'config.json').write_text('private-upgrade')
        (upgrade / 'stats.sqlite3').write_bytes(b'backup-db')
        reinstall = self.manager.BACKUP_HOME / 'xray-backup-20261006T120000'
        (reinstall / 'usr/local/etc/xray').mkdir(parents=True)
        (reinstall / 'usr/local/etc/xray/config.json').write_text('private-reinstall')
        archive = self.manager.BACKUP_HOME / 'xray-config-backup.tar.gz'
        archive.write_bytes(b'archive')
        database = self.manager.BACKUP_HOME / 'xray-stats-backup.sqlite3'
        database.write_bytes(b'database')
        return config, upgrade, reinstall, archive, database

    def test_backup_listing_recognizes_types_and_preserves_live_state(self):
        targets = self.make_backups()
        with mock.patch('builtins.print') as output:
            self.manager.execute(['backups'])
        self.assertEqual({item[0] for item in self.manager.backup_candidates()}, set(targets))
        text = '\n'.join(str(call) for call in output.call_args_list)
        self.assertIn('共 5 个备份', text)
        self.assertNotIn('private-config', text)
        self.assertFalse((self.root / 'manager.lock').exists())
        self.assertFalse((self.root / 'stats.sqlite3').exists())

    def test_backup_details_do_not_follow_links_or_print_credentials(self):
        _, upgrade, _, _, _ = self.make_backups()
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'secret.key').write_text('private-secret')
        (upgrade / 'linked-directory').symlink_to(outside, target_is_directory=True)
        (self.root / 'config-symlink.json').symlink_to(self.manager.CONFIG)
        with mock.patch('builtins.print') as output:
            self.manager.execute(['backup-info', str(upgrade)])
        text = '\n'.join(str(call) for call in output.call_args_list)
        self.assertIn('stats.sqlite3', text)
        self.assertIn('[符号链接]', text)
        self.assertNotIn('secret.key', text)
        self.assertNotIn('private-upgrade', text)
        self.assertNotIn(self.root / 'config-symlink.json',
                         {item[0] for item in self.manager.backup_candidates()})

    def test_backup_delete_requires_explicit_confirmation_and_supported_path(self):
        config, _, _, _, _ = self.make_backups()
        for args in (['delete-backup', str(config)],
                     ['delete-backup', str(config), '--no'],
                     ['delete-backup', str(self.manager.CONFIG), '--yes'],
                     ['delete-backup', str(self.root), '--yes'],
                     ['delete-backup', str(self.root / 'upgrade.lock'), '--yes']):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.manager.execute(args)
        self.assertTrue(config.exists())
        self.assertTrue(self.manager.CONFIG.exists())
        self.assertTrue((self.root / 'connection.json').exists())
        self.assert_unlocked()

    def test_backup_menu_cancel_and_waiting_input_allow_collection(self):
        targets = self.make_backups()
        answers = iter(['1', ''])

        def answer(prompt):
            self.assert_unlocked()
            self.manager.execute(['collect'])
            return next(answers)

        with mock.patch('builtins.input', side_effect=answer), mock.patch('builtins.print'):
            self.manager.backup_action('3')
        self.assertTrue(all(target.exists() for target in targets))
        self.assertEqual(self.totals(), [(10, 10)])

    def test_delete_backup_files_and_directory_leaves_link_targets_and_live_db(self):
        targets = self.make_backups()
        _, upgrade, _, _, _ = targets
        (upgrade / 'live-config-link').symlink_to(self.manager.CONFIG)
        self.manager.execute(['collect'])
        with mock.patch('builtins.print'):
            for target in targets:
                self.manager.execute(['delete-backup', str(target), '--yes'])
        self.assertEqual(self.manager.backup_candidates(), [])
        self.assertTrue(self.manager.CONFIG.exists())
        self.assertTrue((self.root / 'connection.json').exists())
        self.assertEqual(self.totals(), [(10, 10)])
        self.assert_unlocked()

    def test_deletion_during_upgrade_is_refused_and_locks_release(self):
        config, _, _, _, _ = self.make_backups()
        with (self.root / 'upgrade.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.assertRaisesRegex(RuntimeError, '升级正在进行'):
                self.manager.execute(['delete-backup', str(config), '--yes'])
            self.assertTrue(config.exists())
        with mock.patch('builtins.print'):
            self.manager.execute(['delete-backup', str(config), '--yes'])
        self.assertFalse(config.exists())
        self.assert_unlocked()

    def test_backup_selection_replacement_is_rejected(self):
        config, _, _, _, _ = self.make_backups()
        info = config.stat()
        config.rename(self.root / 'saved-original')
        config.write_text('replacement-backup')
        with self.assertRaisesRegex(ValueError, '已发生变化'):
            self.manager.execute(['delete-backup', str(config), '--yes'],
                                 backup_identity=(info.st_dev, info.st_ino))
        self.assertEqual(config.read_text(), 'replacement-backup')
        self.assert_unlocked()

    def test_backup_menu_confirmed_deletion_and_empty_listing(self):
        target = self.root / 'config-20261006.json'
        target.write_text('private')
        with mock.patch('builtins.print'), mock.patch('builtins.input', side_effect=['1', 'y']):
            self.manager.backup_action('3')
        self.assertFalse(target.exists())
        with mock.patch('builtins.print') as output, mock.patch('builtins.input') as prompt:
            self.manager.backup_action('2')
            prompt.assert_not_called()
        self.assertTrue(any('暂无备份' in str(call) for call in output.call_args_list))

    def prepare_processes(self):
        # Import the real source, redirect state and service commands into the fixture.
        (self.root / 'runner.py').write_text(
            'import importlib.util, pathlib, sys\n'
            f'spec = importlib.util.spec_from_file_location("manager", {str(SOURCE)!r})\n'
            'm = importlib.util.module_from_spec(spec)\n'
            'spec.loader.exec_module(m)\n'
            f'm.ROOT = pathlib.Path({str(self.root)!r})\n'
            'm.CONFIG = m.ROOT / "config.json"\n'
            'm.BACKUP_HOME = m.ROOT / "home"\n'
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
        os.write(master, b'2\n')
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
        read_until('选择操作:')
        for choice in (b'3\n', b'4\n', b'5\n', b'6\n'):
            os.write(master, choice)
            read_until('选择操作:')
            collect_while_waiting()
            os.write(master, b'0\n')
            read_until('选择操作:')
        os.write(master, b'0\n')
        self.assertEqual(menu.wait(timeout=5), 0)
        self.assertEqual(self.totals(), [(10, 10)])
        self.assertEqual(self.manager.device_names(), ['phone'])


if __name__ == '__main__':
    unittest.main()
