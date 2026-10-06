#!/usr/bin/env python3
import ast
import datetime as dt
from contextlib import contextmanager
import fcntl
import glob
import ipaddress
import json
import os
import pathlib
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import uuid

ROOT = pathlib.Path('/var/lib/xray-manager')
CONFIG = pathlib.Path('/usr/local/etc/xray/config.json')
XRAY = '/usr/local/bin/xray'
BACKUP_HOME = pathlib.Path('/root')
UPDATE_REPO = 'yehuohajimi/xray-manager'
UPDATE_BRANCH = 'main'
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

def share_links(name=None):
    c = inbound(config())
    info = json.loads((ROOT/'connection.json').read_text())
    reality = c['streamSettings']['realitySettings']
    links = []
    for user in c['settings']['clients']:
        if name and user['email'] != name:
            continue
        params = urllib.parse.urlencode({'encryption':'none','security':'reality','sni':reality['serverNames'][0],
            'fp':'chrome','pbk':info['public_key'],'sid':reality['shortIds'][0],'type':'tcp','flow':'xtls-rprx-vision','spx':'/'})
        link = f"vless://{user['id']}@{info['server']}:{c['port']}?{params}#{urllib.parse.quote(user['email'])}"
        links.append((user['email'], link))
    if name and not links:
        raise ValueError('Device not found')
    return links


def print_qr(link):
    try:
        # Pass credentials through stdin, never command-line arguments or files.
        code = subprocess.check_output(
            ['qrencode', '-t', 'ANSIUTF8', '-l', 'M', '-m', '4', '-o', '-'],
            input=link, encoding='utf-8', stderr=subprocess.DEVNULL, timeout=10)
    except FileNotFoundError:
        print('二维码不可用：请安装 qrencode（apt-get install -y qrencode），或复制上方链接导入。')
        return
    except (OSError, subprocess.SubprocessError, UnicodeError):
        print('二维码生成失败，可复制上方链接导入。')
        return
    plain = re.sub(r'\x1b\[[0-9;]*m', '', code)
    width = max((len(line) for line in plain.splitlines()), default=0)
    if not width:
        print('二维码生成失败，可复制上方链接导入。')
        return
    if shutil.get_terminal_size().columns < width:
        print(f'二维码需要至少 {width} 列宽度，请扩大终端窗口或缩小字体后重新分享。')
        return
    print('分享二维码（在手机客户端中选择扫码导入）:')
    try:
        print(code, end='' if code.endswith('\n') else '\n')
    except UnicodeError:
        print('当前终端不支持二维码字符，请使用 UTF-8 终端或复制上方链接导入。')


def print_shares(links, qr=False):
    for name, link in links:
        print(f'{name}:\n{link}')
        if qr:
            print_qr(link)
            print()


def share(name=None):
    print_shares(share_links(name))

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
xray-manager backups                 List saved backups and sizes
xray-manager backup-info PATH        Show backup metadata and file listing
xray-manager delete-backup PATH --yes  Permanently delete a listed backup
xray-manager update                  Update manager and latest stable Xray from GitHub
xray-manager update --manager-only   Update manager without restarting Xray
uplink=user upload; downlink=user download. User/inbound/outbound are overlapping
views: do not add them together. Historical bytes are per UUID, not per source IP.
Abrupt crashes/reboots can lose bytes since the last sample (~1 minute).
IP history records accepted requests, not exact device online/offline times.''')


def device_names():
    return [user['email'] for user in inbound(config())['settings']['clients']]


def fetch_update(url, target=None):
    args = ['curl', '--fail', '--location', '--silent', '--show-error',
            '--connect-timeout', '10', '--max-time', '60', '--retry', '2',
            '--max-filesize', '2097152', '-H', 'Accept: application/vnd.github+json']
    if target is not None:
        args.extend(['-o', str(target)])
    args.append(url)
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=210)


def update_json(url):
    try:
        value = json.loads(fetch_update(url))
    except json.JSONDecodeError:
        raise ValueError('GitHub 返回了无效的更新信息，请稍后重试。') from None
    if not isinstance(value, dict):
        raise ValueError('GitHub 返回了无效的更新信息。')
    return value


def online_update(manager_only=False, ask_confirmation=False):
    """Stage one repository commit, then let its upgrade script back up and switch."""
    print(f'正在获取 GitHub 更新: {UPDATE_REPO} ({UPDATE_BRANCH})', flush=True)
    commit = update_json(f'https://api.github.com/repos/{UPDATE_REPO}/commits/{UPDATE_BRANCH}')
    sha = commit.get('sha', '')
    if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise ValueError('GitHub 提交标识无效，已取消更新。')
    version = None
    current = None
    if not manager_only:
        release = update_json('https://api.github.com/repos/XTLS/Xray-core/releases/latest')
        version = release.get('tag_name', '')
        if (release.get('draft') is not False or release.get('prerelease') is not False
                or not isinstance(version, str) or not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', version)):
            raise ValueError('未取得有效的 Xray 正式稳定版本，已取消更新。')
        description = run(XRAY, 'version')
        match = re.search(r'^Xray\s+([0-9]+\.[0-9]+\.[0-9]+)(?=\s|$)', description)
        current = 'v' + match[1] if match else None
    print(f'Manager 来源: https://github.com/{UPDATE_REPO}/commit/{sha}')
    if version:
        print(f'Xray: {current or "未知版本"} → {version}')
    else:
        print('仅更新 manager，Xray 内核保持现有版本。')
    switch_core = version is not None and version != current
    if version and not switch_core:
        print('Xray 已是最新稳定版，仅更新 manager，无需重启 Xray。')
    if ask_confirmation:
        impact = '更新内核将重启 Xray，连接会短暂中断。' if switch_core else '本次无需重启 Xray。'
        if not confirm(impact + '保留设备账号、密钥和统计数据。继续更新？'):
            print('已取消更新。')
            return False
    with tempfile.TemporaryDirectory(prefix='xray-manager-update-') as staging:
        staging = pathlib.Path(staging)
        for filename in ('manager.py', 'upgrade-xray.sh'):
            print(f'正在下载 {filename}…', flush=True)
            fetch_update(f'https://raw.githubusercontent.com/{UPDATE_REPO}/{sha}/{filename}',
                         staging / filename)
        try:
            ast.parse((staging / 'manager.py').read_text(), filename='downloaded manager.py')
        except (SyntaxError, UnicodeError):
            raise ValueError('下载的 manager.py 无法通过语法检查，已取消更新。') from None
        subprocess.run(['bash', '-n', str(staging / 'upgrade-xray.sh')], check=True)
        env = os.environ.copy()
        # Inherited VERSION must not override the version selected from GitHub.
        env.pop('VERSION', None)
        if switch_core:
            env['VERSION'] = version
        print('代码检查通过，开始备份和更新…', flush=True)
        subprocess.run(['bash', str(staging / 'upgrade-xray.sh')], env=env, check=True)
    return True


def backup_candidates():
    """Only recognize project backups; never follow a top-level symlink."""
    patterns = ((ROOT, 'config-*.json', False, '配置'),
                (ROOT, 'upgrade-*', True, '升级'),
                (BACKUP_HOME, 'xray-backup-*', True, '重装'),
                (BACKUP_HOME, 'xray-stats-backup*.sqlite3', False, '统计库'),
                (BACKUP_HOME, 'xray-config-backup*.tar.gz', False, '配置归档'))
    result = []
    for parent, pattern, directory, kind in patterns:
        for path in parent.glob(pattern):
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
                result.append((path, kind, info))
    return sorted(result, key=lambda item: (item[2].st_mtime_ns, str(item[0])), reverse=True)


def find_backup(path):
    target = pathlib.Path(path)
    for item in backup_candidates():
        if item[0] == target:
            return item
    raise ValueError('备份不存在或不属于支持的备份路径。请先运行 xray-manager backups。')


def backup_files(path):
    if not path.is_dir():
        yield path, path.lstat()
        return
    def walk_error(error):
        raise error
    for parent, directories, files in os.walk(path, followlinks=False, onerror=walk_error):
        # Include symlink entries as metadata, but never traverse their targets.
        for name in sorted(directories + files):
            entry = pathlib.Path(parent) / name
            info = entry.lstat()
            if not stat.S_ISDIR(info.st_mode):
                yield entry, info


def backup_summary(item):
    path, kind, info = item
    size, count = 0, 0
    for _, entry in backup_files(path):
        size += entry.st_size
        count += 1
    return size, count


def compact_size(size):
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if size < 1024 or unit == 'TiB':
            return f'{size:.1f} {unit}'
        size /= 1024


def backup_time(info):
    return dt.datetime.fromtimestamp(info.st_mtime, dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


def list_backups():
    items = backup_candidates()
    if not items:
        print('暂无备份文件。')
    total = 0
    for index, item in enumerate(items, 1):
        size, _ = backup_summary(item)
        total += size
        path, kind, info = item
        print(f'  {index}. [{kind}] {path}\n     {backup_time(info)} | {compact_size(size)}')
    print(f'共 {len(items)} 个备份，文件大小合计 {compact_size(total)}。')
    return items


def show_backup(path):
    item = find_backup(path)
    target, kind, info = item
    size, count = backup_summary(item)
    print(f'路径: {target}\n类型: {kind}\n修改时间: {backup_time(info)}\n'
          f'文件大小: {compact_size(size)}\n文件条目: {count}')
    print('文件列表（最多显示 50 条）:')
    for index, (entry, entry_info) in enumerate(backup_files(target)):
        if index == 50:
            print(f'  …其余 {count - 50} 条未显示。')
            break
        name = entry.relative_to(target) if target.is_dir() else entry.name
        label = ' [符号链接]' if stat.S_ISLNK(entry_info.st_mode) else ''
        print(f'  {name}{label} | {compact_size(entry_info.st_size)}')


def delete_backup(path, expected_identity=None):
    # Same lock order as upgrade-xray.sh: upgrade first, then manager.
    # A running upgrade needs its backup for rollback; refuse to delete during it.
    with (ROOT / 'upgrade.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('升级正在进行，请完成后再删除备份。') from None
        with manager_state(database=False):
            target, _, info = find_backup(path)
            identity = (info.st_dev, info.st_ino)
            if expected_identity is not None and identity != expected_identity:
                raise ValueError('所选备份已发生变化，请重新选择。')
            if stat.S_ISDIR(info.st_mode):
                shutil.rmtree(target)
            else:
                target.unlink()
    print(f'已删除备份: {target}')


def execute(args, backup_identity=None, qr=False):
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
        'backups': (0, 0), 'backup-info': (1, 1), 'delete-backup': (2, 2),
        'update': (0, 1),
    }
    if cmd not in arity:
        raise ValueError(f'Unknown command: {cmd}. Use xray-manager help')
    low, high = arity[cmd]
    if not low <= len(params) <= high:
        raise ValueError(f'Invalid arguments for {cmd}. Use xray-manager help')
    if cmd == 'delete-backup' and params[1] != '--yes':
        raise ValueError('Usage: xray-manager delete-backup PATH --yes')
    if cmd == 'update' and params and params[0] != '--manager-only':
        raise ValueError('Usage: xray-manager update [--manager-only]')
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
    if cmd == 'update':
        online_update(manager_only=bool(params))
        return
    if cmd == 'backups':
        list_backups()
        return
    if cmd == 'backup-info':
        show_backup(params[0])
        return
    if cmd == 'delete-backup':
        delete_backup(params[0], expected_identity=backup_identity)
        return
    # These queries neither access manager state nor interfere with collection.
    if cmd == 'status':
        subprocess.run(['systemctl', 'status', 'xray', 'xray-stats.timer', '--no-pager'], timeout=20)
        return
    if cmd == 'logs':
        print(run('journalctl', '-u', 'xray', '-n', '100', '--no-pager'))
        return
    if cmd == 'share':
        with manager_state(database=False):
            links = share_links(params[0] if params else None)
        # Generating/displaying QR codes must not block the background collector.
        print_shares(links, qr=qr)
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
=== Xray Manager ===
  1. 服务状态
  2. 设备管理
  3. 日志与统计
  4. 备份管理
  5. 服务控制
  6. 在线更新
  0. 退出
'''

DEVICE_MENU = '''
=== Xray Manager / 设备管理 ===
  1. 设备列表
  2. 新增设备
  3. 客户端分享链接
  4. 撤销设备
  0. 返回主菜单
'''

STATS_MENU = '''
=== Xray Manager / 日志与统计 ===
  1. 流量统计
  2. 当前 TCP 连接
  3. 历史来源 IP
  4. 服务日志
  5. 立即采集统计
  0. 返回主菜单
'''

BACKUP_MENU = '''
=== Xray Manager / 备份管理 ===
  1. 备份列表与大小
  2. 查看备份详情
  3. 删除备份
  0. 返回主菜单
'''

SERVICE_MENU = '''
=== Xray Manager / 服务控制 ===
  1. 启动服务
  2. 重启服务
  3. 停止服务
  0. 返回主菜单
'''

UPDATE_MENU = '''
=== Xray Manager / 在线更新 ===
  1. 更新 manager + Xray 最新稳定版
  2. 仅更新 manager
  0. 返回主菜单
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


def device_action(choice):
    if choice == '1':
        execute(['list-devices'])
    elif choice == '2':
        name = input('新设备名称（字母/数字/_/-，1–40 字符；回车取消）: ').strip()
        if not name:
            return
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', name):
            raise ValueError('设备名称仅允许 1–40 个英文字母、数字、下划线或连字符。')
        if confirm(f'新增设备 {name} 会重启 Xray，现有连接将短暂中断。继续？'):
            execute(['add-device', name])
    elif choice in ('3', '4'):
        selected = choose_device(allow_all=choice == '3')
        if selected is None:
            return
        if choice == '3':
            print('以下链接包含访问凭据，请妥善保存。')
            execute(['share', *selected], qr=True)
        elif confirm(f'撤销 {selected[0]} 会使其链接失效并重启 Xray，保留历史统计。继续？'):
            execute(['remove-device', *selected])
    else:
        raise ValueError('请输入菜单中的编号。')


def stats_action(choice):
    simple = {'2': 'online', '3': 'ips', '4': 'logs', '5': 'collect'}
    if choice in simple:
        execute([simple[choice]])
    elif choice == '1':
        days = input('最近多少天（1–90；回车查看安装以来累计；0 取消）: ').strip()
        if days != '0':
            execute(['stats', *([days] if days else [])])
    else:
        raise ValueError('请输入菜单中的编号。')


def service_action(choice):
    if choice == '1':
        execute(['start'])
    elif choice in ('2', '3'):
        cmd, label = ('restart', '重启') if choice == '2' else ('stop', '停止')
        if confirm(f'{label} Xray 将中断现有连接。继续？'):
            execute([cmd])
    else:
        raise ValueError('请输入菜单中的编号。')


def update_action(choice):
    if choice not in ('1', '2'):
        raise ValueError('请输入菜单中的编号。')
    if online_update(manager_only=choice == '2', ask_confirmation=True):
        print('更新完成，正在打开新版管理菜单…', flush=True)
        os.execv(sys.executable, [sys.executable, str(pathlib.Path(__file__).resolve()), 'menu'])


def backup_action(choice):
    if choice == '1':
        execute(['backups'])
        return
    if choice not in ('2', '3'):
        raise ValueError('请输入菜单中的编号。')
    items = list_backups()
    if not items:
        return
    selection = input('选择备份编号（回车取消）: ').strip()
    if not selection:
        return
    if not selection.isascii() or not selection.isdigit() or not 1 <= int(selection) <= len(items):
        raise ValueError('请输入列表中的备份编号。')
    path, _, info = items[int(selection) - 1]
    if choice == '2':
        execute(['backup-info', str(path)])
    else:
        size, _ = backup_summary(items[int(selection) - 1])
        if confirm(f'永久删除备份 {path}（{compact_size(size)}）？删除后无法用它恢复'):
            execute(['delete-backup', str(path), '--yes'], backup_identity=(info.st_dev, info.st_ino))


def menu_action(choice):
    submenus = {'2': (DEVICE_MENU, device_action), '3': (STATS_MENU, stats_action),
                '4': (BACKUP_MENU, backup_action), '5': (SERVICE_MENU, service_action),
                '6': (UPDATE_MENU, update_action)}
    if choice in submenus:
        interactive_menu(*submenus[choice])
        return True  # Returning from a submenu immediately redraws the main menu.
    if choice == '1':
        execute(['status'])
    else:
        raise ValueError('请输入菜单中的编号。')


def report_error(error):
    print(f'操作失败: {error}', file=sys.stderr)
    if isinstance(error, subprocess.CalledProcessError) and error.output:
        print(error.output, file=sys.stderr)


def interactive_menu(text=MENU, action=menu_action):
    while True:
        print(text)
        choice = input('选择操作: ').strip()
        if choice in ('0', 'q', 'quit', 'exit'):
            return
        if not choice:
            continue
        try:
            if action(choice):
                continue
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
