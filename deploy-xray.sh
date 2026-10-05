#!/usr/bin/env bash
# Debian/Ubuntu + systemd. REINSTALL=1 explicitly replaces existing Xray.
# Run from the repository; manager.py must be next to this script.
set -Eeuo pipefail
umask 077
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[[ -f $script_dir/manager.py ]] || { echo 'manager.py is missing next to deploy-xray.sh'; exit 1; }
[[ $EUID == 0 ]] || { echo 'Run as root'; exit 1; }
SERVER_IP=${SERVER_IP:?Set SERVER_IP to the public IPv4 address or hostname}
PORT=${PORT:-443}
SNI=${SNI:-www.cloudflare.com}
VERSION=${VERSION:-v26.3.27}
REINSTALL=${REINSTALL:-0}
export SERVER_IP PORT SNI
[[ $PORT =~ ^[0-9]{1,5}$ ]] || { echo 'PORT must be an integer from 1 to 65535'; exit 1; }
PORT=$((10#$PORT))
(( PORT > 0 && PORT < 65536 && PORT != 10085 )) || { echo 'PORT must be 1..65535, excluding the statistics API port 10085'; exit 1; }
[[ $VERSION =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo 'VERSION must look like v26.3.27'; exit 1; }
[[ $REINSTALL == 0 || $REINSTALL == 1 ]] || { echo 'REINSTALL must be 0 or 1'; exit 1; }
command -v apt-get >/dev/null
[[ -d /run/systemd/system ]] || { echo 'systemd required'; exit 1; }
if [[ -e /usr/local/etc/xray/config.json || -x /usr/local/bin/xray || -x /usr/bin/xray ]] || systemctl cat xray.service &>/dev/null; then
  [[ $REINSTALL == 1 ]] || { echo 'Existing Xray found. Use upgrade-xray.sh to preserve accounts and config. REINSTALL=1 rebuilds the proxy identity.'; exit 1; }
  backup=/root/xray-backup-$(date -u +%Y%m%dT%H%M%SZ)
  mkdir -m 700 "$backup"
  for p in /usr/local/etc/xray /etc/xray /etc/systemd/system/xray.service /etc/systemd/system/xray.service.d /usr/local/bin/xray /var/lib/xray-manager; do
    [[ ! -e $p ]] || cp -a --parents "$p" "$backup/"
  done
  systemctl stop xray-stats.timer xray-stats.service 2>/dev/null || true
  if systemctl is-active --quiet xray && command -v xray-manager >/dev/null; then xray-manager collect; fi
  if systemctl cat xray.service &>/dev/null; then systemctl disable --now xray.service; fi
  if dpkg-query -W -f='${Status}' xray 2>/dev/null | grep -q 'install ok installed'; then apt-get remove -y xray; fi
  rm -f /usr/local/bin/xray
  rm -rf /etc/systemd/system/xray.service.d
  echo "Old installation backed up: $backup"
fi
if ss -H -lnt "sport = :$PORT" | grep -q .; then
  echo "TCP $PORT is occupied; stop the owning service or choose another PORT."
  exit 1
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl unzip python3 openssl iproute2 logrotate
case $(uname -m) in
  x86_64) arch=64 ;;
  aarch64) arch=arm64-v8a ;;
  *) echo 'Unsupported architecture'; exit 1 ;;
esac
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
base="https://github.com/XTLS/Xray-core/releases/download/$VERSION/Xray-linux-$arch.zip"
curl --fail --location --retry 3 "$base" -o "$tmp/xray.zip"
curl --fail --location --retry 3 "$base.dgst" -o "$tmp/xray.dgst"
python3 - "$tmp" <<'PY'
import hashlib, pathlib, re, sys
p = pathlib.Path(sys.argv[1])
digest = hashlib.sha256((p/'xray.zip').read_bytes()).hexdigest()
assert digest in re.findall(r'\b[0-9a-f]{64}\b', (p/'xray.dgst').read_text().lower()), 'SHA256 mismatch'
print('Release archive SHA256 verified:', digest)
PY
unzip -q "$tmp/xray.zip" -d "$tmp/release"
install -m 755 "$tmp/release/xray" /usr/local/bin/xray
getent passwd xray >/dev/null || useradd --system --home /var/lib/xray --shell /usr/sbin/nologin xray
install -d -m 750 -o root -g xray /usr/local/etc/xray
install -d -m 750 -o xray -g xray /var/log/xray
install -d -m 700 /var/lib/xray-manager
install -d -m 755 /usr/local/lib/xray-manager
# Keep the accounting DB across reinstalls, but rebuild the proxy identity.
python3 - <<'PY'
import json, os, pathlib, secrets, subprocess, uuid
keys = dict(line.split(':', 1) for line in subprocess.check_output(['/usr/local/bin/xray','x25519'], text=True).splitlines() if ':' in line)
keys = {k.strip(): v.strip() for k,v in keys.items()}
private = keys['PrivateKey']
public = keys.get('Password (PublicKey)', keys.get('Password', keys.get('PublicKey')))
assert public, 'Unknown x25519 output'
cfg = {
 'log': {'access':'/var/log/xray/access.log','error':'/var/log/xray/error.log','loglevel':'warning'},
 'api': {'tag':'api','services':['StatsService']},
 'stats': {},
 'policy': {'levels': {'0': {'statsUserUplink':True,'statsUserDownlink':True}},
            'system': {'statsInboundUplink':True,'statsInboundDownlink':True,'statsOutboundUplink':True,'statsOutboundDownlink':True}},
 'inbounds': [
  {'tag':'api-in','listen':'127.0.0.1','port':10085,'protocol':'dokodemo-door','settings':{'address':'127.0.0.1'}},
  {'tag':'vless-in','listen':'0.0.0.0','port':int(os.environ['PORT']),'protocol':'vless',
   'settings': {'clients':[{'id':str(uuid.uuid4()),'email':'device-1','flow':'xtls-rprx-vision','level':0}], 'decryption':'none'},
   'streamSettings': {'network':'tcp','security':'reality','realitySettings':{
     'show':False,'target':os.environ['SNI']+':443','xver':0,'serverNames':[os.environ['SNI']],
     'privateKey':private,'shortIds':[secrets.token_hex(8)]}}}],
 'outbounds': [{'tag':'direct','protocol':'freedom'},{'tag':'block','protocol':'blackhole'}],
 'routing': {'rules':[
   {'type':'field','inboundTag':['api-in'],'outboundTag':'api'},
   {'type':'field','ip':['10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','127.0.0.0/8','169.254.0.0/16','::1/128','fc00::/7','fe80::/10'],'outboundTag':'block'}]}
}
pathlib.Path('/usr/local/etc/xray/config.json').write_text(json.dumps(cfg, indent=2)+'\n')
pathlib.Path('/var/lib/xray-manager/connection.json').write_text(json.dumps({'server':os.environ['SERVER_IP'],'public_key':public})+'\n')
PY
chown root:xray /usr/local/etc/xray/config.json
chmod 640 /usr/local/etc/xray/config.json
cat > /etc/systemd/system/xray.service <<'UNIT'
[Unit]
Description=Xray VLESS REALITY Vision
After=network-online.target
Wants=network-online.target

[Service]
User=xray
Group=xray
ExecStart=/usr/local/bin/xray run -config /usr/local/etc/xray/config.json
Restart=on-failure
RestartSec=3
LimitNOFILE=1048576
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/log/xray
UMask=0027

[Install]
WantedBy=multi-user.target
UNIT
install -m 644 "$script_dir/manager.py" /usr/local/lib/xray-manager/manager.py
cat > /usr/local/bin/xray-manager <<'SH'
#!/bin/sh
exec /usr/bin/python3 /usr/local/lib/xray-manager/manager.py "$@"
SH
chmod 755 /usr/local/bin/xray-manager
chmod 644 /usr/local/lib/xray-manager/manager.py
cat > /etc/systemd/system/xray-stats.service <<'UNIT'
[Unit]
Description=Persist Xray traffic and authenticated source IPs
After=xray.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/xray-manager collect
UMask=0077
UNIT
cat > /etc/systemd/system/xray-stats.timer <<'UNIT'
[Unit]
Description=Collect Xray traffic every minute

[Timer]
OnBootSec=60s
OnUnitActiveSec=60s
AccuracySec=1s

[Install]
WantedBy=timers.target
UNIT
# Xray does not support USR1 log reopening on all releases. copytruncate is
# compatible, with a small possible loss window during copying/truncation.
cat > /etc/logrotate.d/xray <<'ROTATE'
/var/log/xray/*.log {
    daily
    maxsize 20M
    rotate 7
    compress
    missingok
    notifempty
    copytruncate
    su xray xray
    firstaction
        /usr/local/bin/xray-manager collect
    endscript
    lastaction
        /usr/local/bin/xray-manager reset-log-cursors
    endscript
}
ROTATE
# Explicit UTC makes access-log timestamps unambiguous.
mkdir -p /etc/systemd/system/xray.service.d
cat > /etc/systemd/system/xray.service.d/timezone.conf <<'UNIT'
[Service]
Environment=TZ=UTC
UNIT
if command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; then
    ufw allow "$PORT/tcp"
fi
touch /var/log/xray/access.log /var/log/xray/error.log
chown xray:xray /var/log/xray/access.log /var/log/xray/error.log
chmod 640 /var/log/xray/access.log /var/log/xray/error.log
runuser -u xray -- /usr/local/bin/xray run -test -config /usr/local/etc/xray/config.json
systemctl daemon-reload
systemctl enable --now xray.service
sleep 2
systemctl is-active --quiet xray.service
systemctl enable --now xray-stats.timer
/usr/local/bin/xray-manager collect
echo 'Installation complete. Client credentials (keep private):'
/usr/local/bin/xray-manager share
echo 'Use: xray-manager help'
echo 'If your provider has a firewall/security group, permit the chosen TCP port there.'
