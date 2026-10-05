#!/usr/bin/env python3
import datetime as dt
import fcntl
import glob
import ipaddress
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import urllib.parse
import uuid

ROOT = pathlib.Path('/var/lib/xray-manager')
CONFIG = pathlib.Path('/usr/local/etc/xray/config.json')
XRAY = '/usr/local/bin/xray'
os.umask(0o077)
if os.geteuid() != 0:
    sys.exit('Run with sudo/root')
lock = (ROOT/'manager.lock').open('a')
fcntl.flock(lock, fcntl.LOCK_EX)
db = sqlite3.connect(ROOT/'stats.sqlite3')
db.executescript('''
CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, epoch TEXT, last INTEGER, total INTEGER);
CREATE TABLE IF NOT EXISTS traffic(ts TEXT, name TEXT, bytes INTEGER);
CREATE INDEX IF NOT EXISTS traffic_time ON traffic(ts);
CREATE TABLE IF NOT EXISTS sources(ts TEXT, ip TEXT, user TEXT);
CREATE INDEX IF NOT EXISTS sources_time ON sources(ts);
CREATE TABLE IF NOT EXISTS cursors(inode TEXT PRIMARY KEY, offset INTEGER);
''')

def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=20)

def stamp():
    return dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

def collect_logs():
    # copytruncate rotation collects before rotating; compressed archives are
    # retained for inspection, not ingested again.
    paths = glob.glob('/var/log/xray/access.log')
    for name in paths:
        if name.endswith('.gz'):
            continue
        with open(name, errors='replace') as f:
            st = os.fstat(f.fileno())
            inode = f'{st.st_dev}:{st.st_ino}'
            row = db.execute('SELECT offset FROM cursors WHERE inode=?', (inode,)).fetchone()
            offset = row[0] if row else 0
            f.seek(offset if offset <= st.st_size else 0)
            while True:
                pos = f.tell()
                line = f.readline()
                if not line or not line.endswith('\n'):
                    f.seek(pos)
                    break
                # Only authenticated VLESS accesses carry a configured email.
                m = re.search(r'from (?:tcp:|udp:)?(\[[^\]]+\]|[\d.]+):\d+ accepted .*?email:\s*(\S+)', line)
                if m:
                    ip = str(ipaddress.ip_address(m[1].strip('[]')))
                    # Xray logs UTC on this service; retain actual event time.
                    t = line[:19].replace('/','-').replace(' ','T')+'Z'
                    db.execute('INSERT INTO sources VALUES(?,?,?)', (t, ip, m[2]))
            db.execute('INSERT OR REPLACE INTO cursors VALUES(?,?)', (inode, f.tell()))

def collect():
    error = None
    try:
        epoch = run('systemctl','show','xray','-p','InvocationID','--value').strip()
        data = json.loads(run(XRAY,'api','statsquery','--server=127.0.0.1:10085'))
        if epoch != run('systemctl','show','xray','-p','InvocationID','--value').strip():
            raise RuntimeError('Xray restarted during collection; retry')
        now = stamp()
        for stat in data.get('stat', []):
            name, value = stat['name'], int(stat.get('value', 0))
            old = db.execute('SELECT epoch,last,total FROM counters WHERE name=?', (name,)).fetchone()
            delta = value if not old or old[0] != epoch or value < old[1] else value-old[1]
            total = (old[2] if old else 0) + delta
            db.execute('INSERT OR REPLACE INTO counters VALUES(?,?,?,?)', (name,epoch,value,total))
            if delta:
                db.execute('INSERT INTO traffic VALUES(?,?,?)', (now,name,delta))
    except Exception as e:
        error = e
    collect_logs()
    cutoff = (dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=90)).strftime('%Y-%m-%dT%H:%M:%SZ')
    db.execute('DELETE FROM traffic WHERE ts < ?', (cutoff,))
    db.execute('DELETE FROM sources WHERE ts < ?', (cutoff,))
    db.commit()
    if error:
        raise RuntimeError(f'Statistics API failed (access logs were still collected): {error}')

def config():
    return json.loads(CONFIG.read_text())

def inbound(c):
    return next(i for i in c['inbounds'] if i['tag']=='vless-in')

def share(name=None):
    c = inbound(config())
    info = json.loads((ROOT/'connection.json').read_text())
    reality = c['streamSettings']['realitySettings']
    for user in c['settings']['clients']:
        if name and user['email'] != name:
            continue
        params = urllib.parse.urlencode({'encryption':'none','security':'reality','sni':reality['serverNames'][0],
            'fp':'chrome','pbk':info['public_key'],'sid':reality['shortIds'][0],'type':'tcp','flow':'xtls-rprx-vision','spx':'/'})
        print(f"{user['email']}:\nvless://{user['id']}@{info['server']}:{c['port']}?{params}#{urllib.parse.quote(user['email'])}")

def save(c):
    candidate = CONFIG.with_suffix('.pending.json')
    candidate.write_text(json.dumps(c, indent=2)+'\n')
    try:
        run(XRAY,'run','-test','-config',str(candidate))
        collect()
        backup = ROOT/('config-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%f')+'.json')
        backup.write_bytes(CONFIG.read_bytes())
        os.chown(candidate, CONFIG.stat().st_uid, CONFIG.stat().st_gid)
        os.chmod(candidate, 0o640)
        candidate.replace(CONFIG)
        try:
            run('systemctl','restart','xray')
        except Exception:
            CONFIG.write_bytes(backup.read_bytes())
            run('systemctl','restart','xray')
            raise
    finally:
        candidate.unlink(missing_ok=True)

def human(n):
    return f'{n / 1024**3:.6f} GiB ({n:,} bytes)'

cmd = sys.argv[1] if len(sys.argv)>1 else 'help'
if cmd == 'reset-log-cursors':
    db.execute('DELETE FROM cursors')
    db.commit()
elif cmd == 'collect':
    collect()
elif cmd == 'stats':
    collect()
    days = int(sys.argv[2]) if len(sys.argv)>2 else None
    if days is not None:
        if not 1 <= days <= 90:
            sys.exit('days must be 1..90')
        cutoff = (dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')
        rows = db.execute('SELECT name,SUM(bytes) FROM traffic WHERE ts>=? GROUP BY name ORDER BY name',(cutoff,))
        print(f'Last {days} days (UTC, sampled):')
    else:
        rows = db.execute('SELECT name,total FROM counters ORDER BY name')
        print('Persisted totals since installation:')
    for name, value in rows:
        if not name.startswith('inbound>>>api-in'):
            print(f'{name:58s} {human(value)}')
elif cmd == 'ips':
    collect_logs()
    db.commit()
    print('USER                     IP                                       FIRST UTC             LAST UTC              REQUESTS')
    for user,ip,first,last,count in db.execute('SELECT user,ip,MIN(ts),MAX(ts),COUNT(*) FROM sources GROUP BY user,ip ORDER BY MAX(ts) DESC'):
        print(f'{user:24s} {ip:40s} {first}  {last}  {count}')
elif cmd == 'online':
    port = inbound(config())['port']
    print('Current TCP peers (includes unauthenticated/scanner connections):')
    print(run('ss','-Hntip','state','established',f'( sport = :{port} )'))
elif cmd == 'share':
    share(sys.argv[2] if len(sys.argv)>2 else None)
elif cmd == 'add-device':
    if len(sys.argv)!=3 or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',sys.argv[2]):
        sys.exit('Usage: xray-manager add-device NAME (letters/digits/_/-; 1..40 chars)')
    name = sys.argv[2]
    c = config()
    users = inbound(c)['settings']['clients']
    if any(u['email']==name for u in users):
        sys.exit('Device name already exists')
    users.append({'id':str(uuid.uuid4()),'email':name,'flow':'xtls-rprx-vision','level':0})
    save(c)
    share(name)
elif cmd == 'remove-device':
    if len(sys.argv)!=3:
        sys.exit('Usage: xray-manager remove-device NAME')
    c = config()
    users = inbound(c)['settings']['clients']
    new = [u for u in users if u['email']!=sys.argv[2]]
    if len(new)==len(users):
        sys.exit('Device not found')
    inbound(c)['settings']['clients'] = new
    save(c)
    print('Device revoked; historical accounting retained.')
elif cmd in ('restart','stop'):
    collect()
    print(run('systemctl',cmd,'xray'))
elif cmd == 'status':
    subprocess.run(['systemctl','status','xray','xray-stats.timer','--no-pager'])
else:
    print('''xray-manager status                  Service and sampler status
xray-manager share [NAME]            Show secret client URLs
xray-manager add-device NAME         New independent UUID; briefly restarts Xray
xray-manager remove-device NAME      Revoke UUID; briefly restarts Xray
xray-manager stats [DAYS]            Persistent bytes; optional last 1..90 days
xray-manager online                  Current inbound TCP IPs and socket counters
xray-manager ips                     Authenticated source IP history, last 90 days
xray-manager collect                 Sample now (also runs every minute)
xray-manager restart|stop            Sample before restarting/stopping
uplink=user upload; downlink=user download. User/inbound/outbound are overlapping
views: do not add them together. Historical bytes are per UUID, not per source IP.
Abrupt crashes/reboots can lose bytes since the last sample (~1 minute).
IP history records accepted requests, not exact device online/offline times.''')
db.close()
