#!/usr/bin/env bash
# Update the installed manager and, when VERSION is set, Xray-core in place.
# The live config, connection details, and accounting database are never replaced.
set -Eeuo pipefail
umask 077

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source_manager=$script_dir/manager.py
core=/usr/local/bin/xray
manager=/usr/local/lib/xray-manager/manager.py
config=/usr/local/etc/xray/config.json
state=/var/lib/xray-manager
connection=$state/connection.json
version=${VERSION:-}

[[ $EUID == 0 ]] || { echo 'Run as root'; exit 1; }
[[ -f $source_manager && -f $config && -f $connection && -f $manager && -x $core && -d $state ]] || {
    echo 'Standard xray-manager installation or repository manager.py is missing'
    exit 1
}
[[ -z $version || $version =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
    echo 'VERSION must look like v26.3.27'
    exit 1
}
systemctl is-active --quiet xray.service || { echo 'xray.service must be running'; exit 1; }
python3 - "$source_manager" <<'PY'
import ast, pathlib, sys
ast.parse(pathlib.Path(sys.argv[1]).read_text(), filename=sys.argv[1])
PY
runuser -u xray -- "$core" run -test -config "$config"

# Existing installs gain terminal QR sharing without replacing proxy identities.
if ! command -v qrencode >/dev/null; then
    echo 'Installing qrencode for terminal client sharing'
    apt-get update
    apt-get install -y qrencode
fi

# Keep concurrent upgrade runs apart. manager.py has its own lock for collection.
exec 9>"$state/upgrade.lock"
flock -n 9 || { echo 'Another upgrade is running'; exit 1; }
work=$(mktemp -d)
backup=$(mktemp -d "$state/upgrade-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")
timer_was_active=0
manager_switched=0
core_switched=0
success=0

finish() {
    local status=$?
    trap - EXIT
    if (( ! success )); then
        echo "Upgrade failed; restoring installed programs from $backup" >&2
        if (( manager_switched )); then
            install -m 644 "$backup/manager.py" "$manager.rollback.$$"
            mv -f "$manager.rollback.$$" "$manager"
        fi
        if (( core_switched )); then
            install -m 755 "$backup/xray" "$core.rollback.$$"
            mv -f "$core.rollback.$$" "$core"
            systemctl restart xray.service || echo 'Rollback restart failed; inspect xray.service' >&2
        fi
    fi
    if (( timer_was_active )); then
        if ! systemctl start xray-stats.timer; then
            echo 'Could not restart xray-stats.timer' >&2
            status=1
        fi
    fi
    rm -rf -- "$work"
    if (( ! success )); then
        echo "Backup retained: $backup" >&2
    fi
    exit "$status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ -n $version ]]; then
    case $(uname -m) in
        x86_64) arch=64 ;;
        aarch64) arch=arm64-v8a ;;
        *) echo 'Unsupported architecture'; exit 1 ;;
    esac
    base="https://github.com/XTLS/Xray-core/releases/download/$version/Xray-linux-$arch.zip"
    curl --fail --location --retry 3 "$base" -o "$work/xray.zip"
    curl --fail --location --retry 3 "$base.dgst" -o "$work/xray.dgst"
    python3 - "$work" <<'PY'
import hashlib, pathlib, re, sys
p = pathlib.Path(sys.argv[1])
digest = hashlib.sha256((p/'xray.zip').read_bytes()).hexdigest()
assert digest in re.findall(r'\b[0-9a-f]{64}\b', (p/'xray.dgst').read_text().lower()), 'SHA256 mismatch'
print('Release archive SHA256 verified:', digest)
PY
    mkdir "$work/release"
    unzip -q "$work/xray.zip" -d "$work/release"
    install -m 755 "$work/release/xray" "$work/xray"
    # The service user must be able to read the existing configuration.
    chmod 755 "$work"
    runuser -u xray -- "$work/xray" run -test -config "$config"
fi

if systemctl is-active --quiet xray-stats.timer; then
    timer_was_active=1
    systemctl stop xray-stats.timer
fi
# Wait for any in-flight collector via manager.py's lock, then snapshot its DB.
/usr/local/bin/xray-manager collect
cp -a "$config" "$backup/config.json"
cp -a "$connection" "$backup/connection.json"
cp -a "$core" "$backup/xray"
cp -a "$manager" "$backup/manager.py"
python3 - "$state/stats.sqlite3" "$backup/stats.sqlite3" <<'PY'
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as source:
    with sqlite3.connect(sys.argv[2]) as target:
        source.backup(target)
PY

install -m 644 "$source_manager" "$manager.next.$$"
mv -f "$manager.next.$$" "$manager"
manager_switched=1
if [[ -n $version ]]; then
    install -m 755 "$work/xray" "$core.next.$$"
    mv -f "$core.next.$$" "$core"
    core_switched=1
    systemctl restart xray.service
    sleep 2
    systemctl is-active --quiet xray.service
fi
/usr/local/bin/xray-manager collect
cmp -s "$config" "$backup/config.json"
cmp -s "$connection" "$backup/connection.json"
success=1
echo "Upgrade complete. Existing config, accounts, connection details, and statistics retained."
echo "Pre-upgrade backup: $backup"
