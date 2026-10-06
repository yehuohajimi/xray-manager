#!/usr/bin/env python3
import datetime as dt
from contextlib import contextmanager
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
db = None


@contextmanager
def manager_state(database=True):
    """Serialize one operation; never hold state while asking for input."""
    global db
    with (ROOT/'manager.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if database:
                db = sqlite3.connect(ROOT/'stats.sqlite3')
                db.executescript('''
CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, epoch TEXT, last INTEGER, total INTEGER);
CREATE TABLE IF NOT EXISTS traffic(ts TEXT, name TEXT, bytes INTEGER);
CREATE INDEX IF NOT EXISTS traffic_time ON traffic(ts);
CREATE TABLE IF NOT EXISTS sources(ts TEXT, ip TEXT, user TEXT);
CREATE INDEX IF NOT EXISTS sources_time ON sources(ts);
CREATE TABLE IF NOT EXISTS cursors(inode TEXT PRIMARY KEY, offset INTEGER);
''')
            yield
        finally:
            try:
                if db is not None:
                    db.close()
            finally:
                db = None
                fcntl.flock(lock, fcntl.LOCK_UN)

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

def help_text():
    print('''xray-manager status                  Service and sampler status
xray-manager [menu]                  Interactive menu (requires a terminal)
xray-manager list-devices            List account names without secret URLs
xray-manager share [NAME]            Show secret client URLs
xray-manager add-device NAME         New independent UUID; briefly restarts Xray
xray-manager remove-device NAME      Revoke UUID; briefly restarts Xray
xray-manager stats [DAYS]            Persistent bytes; optional last 1..90 days
xray-manager online                  Current inbound TCP IPs and socket counters
xray-manager ips                     Authenticated source IP history, last 90 days
xray-manager collect                 Sample now (also runs every minute)
xray-manager start                   Start Xray
xray-manager restart|stop            Sample before restarting/stopping
xray-manager logs                    Last 100 service log entries
uplink=user upload; downlink=user download. User/inbound/outbound are overlapping
views: do not add them together. Historical bytes are per UUID, not per source IP.
Abrupt crashes/reboots can lose bytes since the last sample (~1 minute).
IP history records accepted requests, not exact device online/offline times.''')


def device_names():
    return [user['email'] for user in inbound(config())['settings']['clients']]


def execute(args):
    """Run a CLI operation. Validation and menu prompts happen outside the lock."""
    cmd, *params = args
    if cmd in ('help', '-h', '--help'):
        help_text()
        return
    arity = {
        'reset-log-cursors': (0, 0), 'collect': (0, 0), 'stats': (0, 1),
        'ips': (0, 0), 'online': (0, 0), 'share': (0, 1),
        'add-device': (1, 1), 'remove-device': (1, 1), 'list-devices': (0, 0),
        'start': (0, 0), 'restart': (0, 0), 'stop': (0, 0),
        'status': (0, 0), 'logs': (0, 0),
    }
    if cmd not in arity:
        raise ValueError(f'Unknown command: {cmd}. Use xray-manager help')
    low, high = arity[cmd]
    if not low <= len(params) <= high:
        raise ValueError(f'Invalid arguments for {cmd}. Use xray-manager help')
    if cmd in ('add-device', 'remove-device', 'share') and params:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', params[0]):
            raise ValueError('Device name must contain letters/digits/_/-; 1..40 chars')
    days = None
    if cmd == 'stats' and params:
        try:
            days = int(params[0])
        except ValueError:
            raise ValueError('days must be 1..90') from None
        if not 1 <= days <= 90:
            raise ValueError('days must be 1..90')
    if os.geteuid() != 0:
        raise PermissionError('Run with sudo/root')
    os.umask(0o077)
    # These queries neither access manager state nor interfere with collection.
    if cmd == 'status':
        subprocess.run(['systemctl', 'status', 'xray', 'xray-stats.timer', '--no-pager'], timeout=20)
        return
    if cmd == 'logs':
        print(run('journalctl', '-u', 'xray', '-n', '100', '--no-pager'))
        return
    needs_db = cmd in ('reset-log-cursors', 'collect', 'stats', 'ips',
                       'add-device', 'remove-device', 'restart', 'stop')
    with manager_state(database=needs_db):
        if cmd == 'reset-log-cursors':
            db.execute('DELETE FROM cursors')
            db.commit()
        elif cmd == 'collect':
            collect()
        elif cmd == 'stats':
            collect()
            if days is not None:
                cutoff = (dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')
                rows = db.execute('SELECT name,SUM(bytes) FROM traffic WHERE ts>=? GROUP BY name ORDER BY name', (cutoff,))
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
            for user, ip, first, last, count in db.execute('SELECT user,ip,MIN(ts),MAX(ts),COUNT(*) FROM sources GROUP BY user,ip ORDER BY MAX(ts) DESC'):
                print(f'{user:24s} {ip:40s} {first}  {last}  {count}')
        elif cmd == 'online':
            port = inbound(config())['port']
            print('Current TCP peers (includes unauthenticated/scanner connections):')
            print(run('ss', '-Hntip', 'state', 'established', f'( sport = :{port} )'))
        elif cmd == 'list-devices':
            names = device_names()
            print('\n'.join(names) if names else 'No devices configured.')
        elif cmd == 'share':
            if params and params[0] not in device_names():
                raise ValueError('Device not found')
            share(params[0] if params else None)
        elif cmd == 'add-device':
            name = params[0]
            c = config()
            users = inbound(c)['settings']['clients']
            if any(u['email'] == name for u in users):
                raise ValueError('Device name already exists')
            users.append({'id': str(uuid.uuid4()), 'email': name, 'flow': 'xtls-rprx-vision', 'level': 0})
            save(c)
            share(name)
        elif cmd == 'remove-device':
            c = config()
            users = inbound(c)['settings']['clients']
            new = [u for u in users if u['email'] != params[0]]
            if len(new) == len(users):
                raise ValueError('Device not found')
            inbound(c)['settings']['clients'] = new
            save(c)
            print('Device revoked; historical accounting retained.')
        elif cmd in ('start', 'restart', 'stop'):
            if cmd != 'start':
                collect()
            print(run('systemctl', cmd, 'xray'))


MENU = '''
Xray Manager
  1. 服务状态                 2. 设备列表
  3. 新增设备                 4. 撤销设备
  5. 客户端分享链接           6. 流量统计
  7. 当前 TCP 连接            8. 历史来源 IP
  9. 启动服务                10. 重启服务
 11. 停止服务                12. 服务日志
 13. 立即采集统计             0. 退出
'''


def confirm(message):
    return input(message + ' [y/N]: ').strip().lower() in ('y', 'yes', '是')


def choose_device(allow_all=False):
    # Take a snapshot, then release the lock before waiting for a selection.
    with manager_state(database=False):
        names = device_names()
    if not names:
        print('尚未配置设备。')
        return None
    for index, name in enumerate(names, 1):
        print(f'  {index}. {name}')
    if allow_all:
        print('  a. 全部设备')
    choice = input('选择设备编号（回车取消）: ').strip()
    if not choice:
        return None
    if allow_all and choice.lower() == 'a':
        return []
    if not choice.isascii() or not choice.isdigit() or not 1 <= int(choice) <= len(names):
        raise ValueError('请输入列表中的设备编号。')
    return [names[int(choice)-1]]


def menu_action(choice):
    simple = {'1': 'status', '2': 'list-devices', '7': 'online',
              '8': 'ips', '9': 'start', '12': 'logs', '13': 'collect'}
    if choice in simple:
        execute([simple[choice]])
    elif choice == '3':
        name = input('新设备名称（字母/数字/_/-，1–40 字符；回车取消）: ').strip()
        if not name:
            return
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', name):
            raise ValueError('设备名称仅允许 1–40 个英文字母、数字、下划线或连字符。')
        if confirm(f'新增设备 {name} 会重启 Xray，现有连接将短暂中断。继续？'):
            execute(['add-device', name])
    elif choice in ('4', '5'):
        selected = choose_device(allow_all=choice == '5')
        if selected is None:
            return
        if choice == '5':
            print('以下链接包含访问凭据，请妥善保存。')
            execute(['share', *selected])
        elif confirm(f'撤销 {selected[0]} 会使其链接失效并重启 Xray，保留历史统计。继续？'):
            execute(['remove-device', *selected])
    elif choice == '6':
        days = input('最近多少天（1–90；回车查看安装以来累计；0 取消）: ').strip()
        if days != '0':
            execute(['stats', *([days] if days else [])])
    elif choice in ('10', '11'):
        cmd, label = ('restart', '重启') if choice == '10' else ('stop', '停止')
        if confirm(f'{label} Xray 将中断现有连接。继续？'):
            execute([cmd])
    else:
        raise ValueError('请输入菜单中的编号。')


def report_error(error):
    print(f'操作失败: {error}', file=sys.stderr)
    if isinstance(error, subprocess.CalledProcessError) and error.output:
        print(error.output, file=sys.stderr)


def interactive_menu():
    while True:
        print(MENU)
        choice = input('选择操作: ').strip()
        if choice in ('0', 'q', 'quit', 'exit'):
            return
        if not choice:
            continue
        try:
            menu_action(choice)
        except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as error:
            report_error(error)
        input('\n按回车返回菜单…')


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    terminal = sys.stdin.isatty() and sys.stdout.isatty()
    if not args:
        args = ['menu' if terminal else 'help']
    try:
        if args[0] == 'menu':
            if len(args) != 1:
                raise ValueError('Usage: xray-manager menu')
            if not terminal:
                raise ValueError('交互菜单需要终端，请使用 SSH 登录后运行；脚本请使用子命令。')
            if os.geteuid() != 0:
                raise PermissionError('Run with sudo/root')
            os.umask(0o077)
            interactive_menu()
        else:
            execute(args)
    except EOFError:
        print('\n已退出。')
    except KeyboardInterrupt:
        print('\n已取消并退出。')
        return 130
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as error:
        report_error(error)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
