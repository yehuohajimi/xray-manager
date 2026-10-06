"""Exercise the real install preflight without installing or touching host state."""

import os
import pathlib
import pty
import select
import subprocess
import tempfile
import time
import unittest


SOURCE = pathlib.Path(__file__).resolve().parents[1] / 'deploy-xray.sh'


class InstallAddressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        (self.root / 'mock').mkdir()
        (self.root / 'systemd').mkdir()
        (self.root / 'manager.py').write_text('# fixture\n')
        # Execute all address-selection logic, then stop before any installation writes.
        script = SOURCE.read_text().split('# Resolve the client address before stopping')[0]
        script += '\nprintf "SELECTED_SERVER=%s\\n" "$SERVER_IP"\n'
        script = script.replace('[[ $EUID == 0 ]]', '[[ 1 == 1 ]]')
        paths = {
            '/run/systemd/system': str(self.root / 'systemd'),
            '/usr/local/etc/xray/config.json': str(self.root / 'existing-config.json'),
            '/usr/local/bin/xray': str(self.root / 'existing-xray'),
            '/usr/bin/xray': str(self.root / 'package-xray'),
        }
        for old, new in paths.items():
            script = script.replace(old, new)
        (self.root / 'deploy-xray.sh').write_text(script)
        self.command('apt-get', 'printf "%s\\n" "$*" >> "$TEST_ROOT/apt-calls"\nexit 0')
        self.command('systemctl', 'printf "%s\\n" "$*" >> "$TEST_ROOT/service-calls"\nexit 1')
        self.command('curl', '''
printf '%s\n' "$*" >> "$TEST_ROOT/curl-calls"
case "${!#}" in
  https://api.ipify.org)
    printf '%s' "$PRIMARY_BODY"
    exit "$PRIMARY_STATUS";;
  https://www.cloudflare.com/cdn-cgi/trace)
    printf '%s' "$FALLBACK_BODY"
    exit "$FALLBACK_STATUS";;
esac
exit 1
''')

    def command(self, name, body):
        target = self.root / 'mock' / name
        target.write_text('#!/usr/bin/env bash\n' + body + '\n')
        target.chmod(0o755)

    def environment(self, **settings):
        env = os.environ.copy()
        for key in ('SERVER_IP', 'PORT', 'SNI', 'VERSION', 'REINSTALL'):
            env.pop(key, None)
        env.update({
            'PATH': str(self.root / 'mock') + os.pathsep + env['PATH'],
            'TEST_ROOT': str(self.root),
            'PRIMARY_BODY': '8.8.8.8\n', 'PRIMARY_STATUS': '0',
            'FALLBACK_BODY': 'ip=1.1.1.1\ncolo=LAX\n', 'FALLBACK_STATUS': '0',
        })
        env.update(settings)
        return env

    def run_preflight(self, **settings):
        return subprocess.run(['bash', str(self.root / 'deploy-xray.sh')],
                              env=self.environment(**settings), input='',
                              capture_output=True, text=True, timeout=5)

    def test_no_parameters_detects_ipv4_and_sets_link_address(self):
        result = self.run_preflight()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('SELECTED_SERVER=8.8.8.8', result.stdout)
        calls = (self.root / 'curl-calls').read_text().splitlines()
        self.assertEqual(len(calls), 1)
        self.assertIn('--ipv4 --noproxy *', calls[0])
        self.assertIn('--connect-timeout 3 --max-time 5 --max-filesize 4096', calls[0])
        self.assertIn('qrencode', (self.root / 'apt-calls').read_text())

    def test_primary_network_failure_uses_fallback_trace(self):
        result = self.run_preflight(PRIMARY_STATUS='28', PRIMARY_BODY='')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('SELECTED_SERVER=1.1.1.1', result.stdout)
        self.assertEqual(len((self.root / 'curl-calls').read_text().splitlines()), 2)

    def test_non_public_or_invalid_primary_responses_use_fallback(self):
        for body in ('<html>error</html>', '192.168.1.1', '127.0.0.1', '100.64.0.1',
                     '224.0.0.1', '::1', '8.8.8.8\n1.1.1.1', ''):
            with self.subTest(body=body):
                result = self.run_preflight(PRIMARY_BODY=body)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('SELECTED_SERVER=1.1.1.1', result.stdout)

    def test_manual_ip_or_domain_skips_network_probes(self):
        for supplied, normalized in [('8.8.4.4', '8.8.4.4'),
                                     ('VPS.Example.COM.', 'vps.example.com'),
                                     (' vps.example.com ', 'vps.example.com')]:
            with self.subTest(supplied=supplied):
                result = self.run_preflight(SERVER_IP=supplied)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'SELECTED_SERVER={normalized}', result.stdout)
                self.assertFalse((self.root / 'curl-calls').exists())

    def test_invalid_manual_hosts_fail_without_network_probe(self):
        for supplied in ('https://vps.example.com', 'vps.example.com:443', 'example.com/path',
                         '999.999.999.999', 'localhost', '2001:4860:4860::8888',
                         'bad_name.example.com', '$(touch injected).example.com'):
            with self.subTest(supplied=supplied):
                result = self.run_preflight(SERVER_IP=supplied)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('IPv4', result.stderr)
                self.assertFalse((self.root / 'curl-calls').exists())
                self.assertFalse((self.root / 'injected').exists())

    def test_probe_failure_without_terminal_exits_with_manual_instructions(self):
        result = self.run_preflight(PRIMARY_STATUS='28', FALLBACK_STATUS='28')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('SERVER_IP=', result.stderr)
        self.assertNotIn('SELECTED_SERVER=', result.stdout)

    def test_existing_install_is_rejected_before_dependencies_or_probes(self):
        (self.root / 'existing-config.json').write_text('keep-account')
        result = self.run_preflight()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('upgrade-xray.sh', result.stdout)
        self.assertFalse((self.root / 'curl-calls').exists())
        self.assertFalse((self.root / 'apt-calls').exists())
        self.assertEqual((self.root / 'existing-config.json').read_text(), 'keep-account')

    def test_reinstall_probe_failure_preserves_existing_service_and_config(self):
        (self.root / 'existing-config.json').write_text('keep-account')
        result = self.run_preflight(REINSTALL='1', PRIMARY_STATUS='28', FALLBACK_STATUS='28')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / 'existing-config.json').read_text(), 'keep-account')
        self.assertFalse((self.root / 'service-calls').exists())

    def test_interactive_probe_failure_accepts_manual_domain_after_invalid_input(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        child = subprocess.Popen(['bash', str(self.root / 'deploy-xray.sh')],
                                 env=self.environment(PRIMARY_STATUS='28', FALLBACK_STATUS='28'),
                                 stdin=slave, stdout=slave, stderr=slave)

        def cleanup():
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)

        self.addCleanup(cleanup)

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

        read_until('回车退出')
        os.write(master, b'https://bad.example.com\n')
        read_until('回车退出')
        os.write(master, b'vps.example.com\n')
        read_until('SELECTED_SERVER=vps.example.com')
        self.assertEqual(child.wait(timeout=5), 0)


if __name__ == '__main__':
    unittest.main()
