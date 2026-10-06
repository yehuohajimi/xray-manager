"""Exercise pinned GitHub updates and menu behavior without network or live services."""

import fcntl
import importlib.util
import json
import os
import pathlib
import shlex
import subprocess
import tempfile
import unittest
from unittest import mock


SOURCE = pathlib.Path(__file__).resolve().parents[1] / 'manager.py'


class OnlineUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        spec = importlib.util.spec_from_file_location('updated_manager', SOURCE)
        self.manager = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.manager)
        self.manager.ROOT = self.root
        self.sha = 'a' * 40
        self.release = {'tag_name': 'v26.9.30', 'draft': False, 'prerelease': False}
        self.source = '# new manager\n'
        self.script = ('#!/bin/bash\n'
                       'printf "%s\\n" "${VERSION:-manager-only}" > '
                       + shlex.quote(str(self.root / 'selected-version')) + '\n'
                       'exit 0\n')
        self.requests = []
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(self.manager.os, 'geteuid', return_value=0).start()
        mock.patch.object(self.manager, 'run', return_value='Xray 26.3.27 (Xray)\n').start()
        mock.patch.object(self.manager, 'fetch_update', side_effect=self.fetch).start()
        mock.patch('builtins.print').start()

    def unlocked(self):
        with (self.root / 'manager.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock, fcntl.LOCK_UN)
        self.assertIsNone(self.manager.db)

    def fetch(self, url, target=None):
        self.unlocked()
        self.requests.append(url)
        if url == f'https://api.github.com/repos/{self.manager.UPDATE_REPO}/commits/main':
            return json.dumps({'sha': self.sha})
        if url == 'https://api.github.com/repos/XTLS/Xray-core/releases/latest':
            return json.dumps(self.release)
        prefix = f'https://raw.githubusercontent.com/{self.manager.UPDATE_REPO}/{self.sha}/'
        self.assertTrue(url.startswith(prefix), url)
        target.write_text(self.source if url.endswith('/manager.py') else self.script)
        return ''

    def test_cli_update_uses_pinned_commit_and_latest_stable_version(self):
        self.manager.execute(['update'])
        self.assertEqual((self.root / 'selected-version').read_text(), 'v26.9.30\n')
        raw = [url for url in self.requests if 'raw.githubusercontent.com' in url]
        self.assertEqual(len(raw), 2)
        self.assertTrue(all('/' + self.sha + '/' in url for url in raw))
        self.unlocked()

    def test_manager_only_skips_release_lookup_and_inherited_version(self):
        with mock.patch.dict(os.environ, {'VERSION': 'v99.1.1'}):
            self.manager.execute(['update', '--manager-only'])
        self.assertEqual((self.root / 'selected-version').read_text(), 'manager-only\n')
        self.assertFalse(any('releases/latest' in url for url in self.requests))

    def test_already_current_core_is_not_restarted(self):
        with mock.patch.object(self.manager, 'run', return_value='Xray 26.9.30 (Xray)\n'):
            self.manager.execute(['update'])
        self.assertEqual((self.root / 'selected-version').read_text(), 'manager-only\n')

    def test_prerelease_current_version_is_not_treated_as_stable(self):
        with mock.patch.object(self.manager, 'run', return_value='Xray 26.9.30-rc1 (Xray)\n'):
            self.manager.execute(['update'])
        self.assertEqual((self.root / 'selected-version').read_text(), 'v26.9.30\n')

    def test_invalid_stable_metadata_is_rejected_before_script_download(self):
        for release in ({'tag_name': 'v26.9.30', 'draft': False, 'prerelease': True},
                        {'tag_name': 'v26.9.30', 'draft': True, 'prerelease': False},
                        {'tag_name': 'v26.9.30-beta', 'draft': False, 'prerelease': False},
                        {'tag_name': 'v26.9.30'}, {}, []):
            with self.subTest(release=release):
                self.release = release
                self.requests.clear()
                with self.assertRaises(ValueError):
                    self.manager.execute(['update'])
                self.assertFalse(any('raw.githubusercontent.com' in url for url in self.requests))
                self.assertFalse((self.root / 'selected-version').exists())

    def test_invalid_commit_is_rejected(self):
        for sha in ('main', '../other', 'a' * 39, None):
            with self.subTest(sha=sha):
                self.sha = sha
                with self.assertRaises(ValueError):
                    self.manager.execute(['update'])
                self.assertFalse((self.root / 'selected-version').exists())

    def test_menu_confirmation_occurs_without_lock_and_cancel_downloads_nothing(self):
        def cancel(message):
            self.unlocked()
            self.assertIn('短暂中断', message)
            return False

        with mock.patch.object(self.manager, 'confirm', side_effect=cancel), \
                mock.patch.object(self.manager.os, 'execv') as reload_menu:
            self.manager.update_action('1')
        reload_menu.assert_not_called()
        self.assertFalse(any('raw.githubusercontent.com' in url for url in self.requests))

    def test_successful_menu_update_reloads_new_manager(self):
        with mock.patch.object(self.manager, 'confirm', return_value=True), \
                mock.patch.object(self.manager.os, 'execv') as reload_menu:
            self.manager.update_action('2')
        self.assertEqual((self.root / 'selected-version').read_text(), 'manager-only\n')
        self.assertEqual(reload_menu.call_args.args[1][-1], 'menu')
        self.unlocked()

    def test_invalid_downloaded_source_or_script_is_not_executed(self):
        for source, script, error in [('def broken(', self.script, ValueError),
                                      (self.source, '#!/bin/bash\nif\n', subprocess.CalledProcessError)]:
            with self.subTest(error=error.__name__):
                self.source, self.script = source, script
                with self.assertRaises(error):
                    self.manager.execute(['update'])
                self.assertFalse((self.root / 'selected-version').exists())

    def test_failed_upgrade_keeps_menu_running_without_reload(self):
        self.script = '#!/bin/bash\nexit 1\n'
        with mock.patch.object(self.manager, 'confirm', return_value=True), \
                mock.patch.object(self.manager.os, 'execv') as reload_menu:
            with self.assertRaises(subprocess.CalledProcessError):
                self.manager.update_action('1')
        reload_menu.assert_not_called()
        self.unlocked()

    def test_network_failure_does_not_run_upgrade(self):
        with mock.patch.object(self.manager, 'fetch_update', side_effect=subprocess.CalledProcessError(22, 'curl')):
            self.assertEqual(self.manager.main(['update']), 1)
        self.assertFalse((self.root / 'selected-version').exists())
