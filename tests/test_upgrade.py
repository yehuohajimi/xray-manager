"""Exercise the in-place upgrade flow with isolated fake service commands."""

import os
import pathlib
import sqlite3
import subprocess
import tempfile
import unittest
import hashlib
import zipfile


SOURCE = pathlib.Path(__file__).resolve().parents[1] / "upgrade-xray.sh"


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        for directory in ("bin", "lib", "state", "mock"):
            (self.root / directory).mkdir()
        (self.root / "manager.py").write_text("# new manager\n")
        (self.root / "lib/manager.py").write_text("# old manager\n")
        (self.root / "config.json").write_text('{"clients":["device-1"]}\n')
        (self.root / "state/connection.json").write_text('{"public_key":"keep"}\n')
        with sqlite3.connect(self.root / "state/stats.sqlite3") as db:
            db.execute("CREATE TABLE retained (value TEXT)")
            db.execute("INSERT INTO retained VALUES ('traffic')")
        self.command("bin/xray", "exit 0")
        self.command(
            "bin/xray-manager",
            'calls="$TEST_ROOT/state/collect-calls"\n'
            'n=$(cat "$calls" 2>/dev/null || printf 0)\n'
            'n=$((n + 1))\n'
            'printf "%s" "$n" > "$calls"\n'
            'if [[ ${FAIL_SECOND_COLLECT:-0} == 1 && $n == 2 ]]; then exit 1; fi',
        )
        self.command("mock/runuser", "exit 0")
        self.command("mock/qrencode", "exit 0")
        self.command(
            "mock/systemctl",
            'printf "%s\\n" "$*" >> "$TEST_ROOT/service-calls"\n'
            'if [[ $1 == restart && ${FAIL_FIRST_RESTART:-0} == 1 ]]; then\n'
            '  count="$TEST_ROOT/restart-calls"\n'
            '  n=$(cat "$count" 2>/dev/null || printf 0)\n'
            '  n=$((n + 1))\n'
            '  printf "%s" "$n" > "$count"\n'
            '  if [[ $n == 1 ]]; then exit 1; fi\n'
            'fi\n'
            'case "$1" in is-active|stop|start|restart) exit 0;; esac\n'
            'exit 1',
        )
        self.command(
            "mock/curl",
            'if [[ $* == *".dgst"* ]]; then\n'
            '  cp "$TEST_ROOT/release.dgst" "${@: -1}"\n'
            'else\n'
            '  cp "$TEST_ROOT/release.zip" "${@: -1}"\n'
            'fi',
        )
        script = SOURCE.read_text()
        paths = {
            "/usr/local/bin/xray-manager": str(self.root / "bin/xray-manager"),
            "/usr/local/bin/xray": str(self.root / "bin/xray"),
            "/usr/local/lib/xray-manager/manager.py": str(self.root / "lib/manager.py"),
            "/usr/local/etc/xray/config.json": str(self.root / "config.json"),
            "/var/lib/xray-manager": str(self.root / "state"),
        }
        for old, new in paths.items():
            script = script.replace(old, new)
        script = script.replace("[[ $EUID == 0 ]]", "[[ 1 == 1 ]]")
        (self.root / "upgrade-xray.sh").write_text(script)

    def command(self, path, body):
        target = self.root / path
        target.write_text("#!/usr/bin/env bash\n" + body + "\n")
        target.chmod(0o755)

    def run_upgrade(self, fail=False, version=False, fail_restart=False):
        env = os.environ.copy()
        env["PATH"] = f"{self.root / 'mock'}:{env['PATH']}"
        env["TEST_ROOT"] = str(self.root)
        env["FAIL_SECOND_COLLECT"] = "1" if fail else "0"
        env["FAIL_FIRST_RESTART"] = "1" if fail_restart else "0"
        if version:
            env["VERSION"] = "v26.3.27"
        else:
            env.pop("VERSION", None)
        return subprocess.run(
            ["bash", str(self.root / "upgrade-xray.sh")],
            env=env,
            capture_output=True,
            text=True,
        )

    def assert_retained(self):
        self.assertEqual((self.root / "config.json").read_text(), '{"clients":["device-1"]}\n')
        self.assertEqual((self.root / "state/connection.json").read_text(), '{"public_key":"keep"}\n')
        with sqlite3.connect(self.root / "state/stats.sqlite3") as db:
            self.assertEqual(db.execute("SELECT value FROM retained").fetchone(), ("traffic",))

    def test_manager_upgrade_preserves_state(self):
        result = self.run_upgrade()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "lib/manager.py").read_text(), "# new manager\n")
        self.assert_retained()
        self.assertIn("stop xray-stats.timer", (self.root / "service-calls").read_text())
        self.assertIn("start xray-stats.timer", (self.root / "service-calls").read_text())
        self.assertEqual(len(list((self.root / "state").glob("upgrade-*/stats.sqlite3"))), 1)

    def test_upgrade_installs_missing_qr_dependency(self):
        (self.root / 'mock/qrencode').unlink()
        self.command('mock/apt-get', 'printf "%s\\n" "$*" >> "$TEST_ROOT/dependency-calls"\nexit 0')
        result = self.run_upgrade()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / 'dependency-calls').read_text(), 'update\ninstall -y qrencode\n')
        self.assert_retained()

    def test_qr_dependency_install_failure_leaves_existing_install_untouched(self):
        (self.root / 'mock/qrencode').unlink()
        self.command('mock/apt-get', 'exit 9')
        result = self.run_upgrade()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / 'lib/manager.py').read_text(), '# old manager\n')
        self.assertNotIn('stop xray-stats.timer', (self.root / 'service-calls').read_text())
        self.assertEqual(list((self.root / 'state').glob('upgrade-*')), [])
        self.assert_retained()

    def test_failed_collection_restores_manager_and_timer(self):
        result = self.run_upgrade(fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / "lib/manager.py").read_text(), "# old manager\n")
        self.assert_retained()
        self.assertIn("start xray-stats.timer", (self.root / "service-calls").read_text())

    def prepare_release(self):
        with zipfile.ZipFile(self.root / "release.zip", "w") as archive:
            archive.writestr("xray", "#!/bin/sh\n# new core\nexit 0\n")
        digest = hashlib.sha256((self.root / "release.zip").read_bytes()).hexdigest()
        (self.root / "release.dgst").write_text(digest + "\n")

    def test_core_upgrade_preserves_state(self):
        self.prepare_release()
        result = self.run_upgrade(version=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("new core", (self.root / "bin/xray").read_text())
        self.assert_retained()

    def test_core_restart_failure_restores_old_programs(self):
        self.prepare_release()
        result = self.run_upgrade(version=True, fail_restart=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("new core", (self.root / "bin/xray").read_text())
        self.assertEqual((self.root / "lib/manager.py").read_text(), "# old manager\n")
        self.assert_retained()
        self.assertEqual((self.root / "restart-calls").read_text(), "2")


if __name__ == "__main__":
    unittest.main()
