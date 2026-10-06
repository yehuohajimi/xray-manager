# Xray Manager

面向 Debian/Ubuntu VPS 的 Xray 安装与管理脚本，使用 VLESS + REALITY + Vision，提供设备账号、流量统计和来源 IP 历史查询，以及保留现有账号和配置的原地升级。

项目运行结构以及配置、日志、统计库和备份的具体位置，见 [结构与文件路径说明](xray-README.md)。

## 运行要求与默认配置

- 在目标 VPS 上以 root 执行，系统使用 systemd；支持 x86_64 和 aarch64。
- 首次安装固定使用 Xray-core `v26.3.27`，从官方 GitHub Release 下载并核对附件中的 SHA256；可通过 `VERSION` 指定其他正式版本。
- 默认入站：VLESS + REALITY + `xtls-rprx-vision`，TCP `443`。
- 默认 REALITY serverName / SNI：`www.cloudflare.com`。
- 客户端 fingerprint：`chrome`，传输类型：TCP，encryption：`none`。
- 默认设备账号：`device-1`。安装时随机生成 UUID、REALITY 密钥及 shortId。
- Xray 使用专用 `xray` 用户；统计 API 只监听 `127.0.0.1:10085`。

## 在 VPS 获取代码

公开仓库可以通过 HTTPS 克隆，无需给 VPS 配置 GitHub 登录凭据。

git clone https://github.com/yehuohajimi/xray-manager.git /root/xray-manager

仓库中已有代码时使用 `git pull --ff-only`。安装脚本需要同目录的 `manager.py`，请克隆完整仓库。

## 首次安装 / 显式重装

全新服务器执行：

```bash
cd /root/xray-manager
bash deploy-xray.sh
```

无需填写公网地址：脚本会通过两个 HTTPS 服务自动探测公网 IPv4，并用它生成客户端链接。探测优先使用 ipify，失败或返回无效地址时改用 Cloudflare；请求绕过环境变量中的 HTTP 代理，每个服务最多等待 5 秒。探测失败时，交互终端会提示输入 IPv4 或域名；非交互执行会退出并提示通过 `SERVER_IP` 指定。已有安装仍需使用升级脚本，普通安装不会覆盖账号。

如果想使用域名，或服务器的入口地址与出口公网 IP 不同，可手动覆盖：

```bash
SERVER_IP='你的公网IPv4或域名' bash deploy-xray.sh
```

`SERVER_IP` 仅用于客户端链接的连接地址，Xray 仍监听 `0.0.0.0`。当前安装采用 IPv4 入站，域名应能解析到可连接的 IPv4；只填主机名，不带 `https://`、端口或路径。

| 参数 | 默认值 | 用途 |
|---|---|---|
| `SERVER_IP` | 自动探测公网 IPv4 | 可手动指定客户端连接的 IPv4 或域名 |
| `PORT` | `443` | 代理 TCP 监听端口，范围 1..65535，不能使用统计 API 端口 10085 |
| `SNI` | `www.cloudflare.com` | REALITY 目标站点及 serverName |
| `VERSION` | `v26.3.27` | 首次安装的 Xray-core 正式版本标签 |
| `REINSTALL` | `0` | 设为 `1` 时明确重装已有安装 |

```bash
SERVER_IP='你的公网IPv4或域名' PORT=443 SNI=www.cloudflare.com VERSION=v26.3.27 bash deploy-xray.sh
```

已有本项目的标准安装时，使用下节的升级脚本即可保留设备账号。只有需要重新生成代理身份时才使用显式重装：

```bash
REINSTALL=1 bash deploy-xray.sh
```

**重装会备份旧配置、替换旧安装，并重新生成 UUID、REALITY 密钥和 shortId，旧客户端链接随之失效。** SQLite 历史统计保留；同名账号的累计值会继续累计。

脚本会安装依赖、生成配置和身份、安装 systemd 服务、管理命令、每分钟统计任务及日志轮转规则。若 ufw 已启用，会允许所选 TCP 端口；其他主机防火墙及云安全组需放行该端口。更换 SNI 后应测试目标站点与 REALITY 的兼容性。

部署及升级针对本项目的标准 systemd 安装；自定义 Docker、面板内嵌或其他名称的服务需要单独处理。端口被其他服务占用时安装会报错退出。

## 保留账号和配置的原地升级

在 VPS 上拉取仓库更新后，运行升级脚本。未设置 `VERSION` 时只更新仓库中的 `manager.py`；设置 `VERSION` 时还会下载、校验并切换指定版本的 Xray-core：

```bash
cd /root/xray-manager
git pull --ff-only
bash upgrade-xray.sh

# 需要同时更新 Xray-core 时指定目标版本标签（此处为参数示例）：
VERSION=v26.3.27 bash upgrade-xray.sh
```

升级脚本只适用于本项目的标准 systemd 安装，要求 `xray.service` 正在运行。它会先采集统计、备份旧程序及配置和 SQLite 数据库、用新 Xray 校验现有配置，然后更新程序；新服务启动或采集失败时自动恢复旧程序。备份保留在 `/var/lib/xray-manager/upgrade-*`，其中含账号 UUID、REALITY 私钥及统计数据，应限制访问。设置 `VERSION` 更新 Xray-core 时，重启会短暂中断连接；只更新管理程序无需重启 Xray。

升级沿用 `/usr/local/etc/xray/config.json`、`/var/lib/xray-manager/connection.json` 和现有统计库，保留设备 UUID、REALITY 密钥和 shortId。采集程序会继续向统计库写入增量。当前脚本更新管理程序及可选的 Xray-core，不更新 systemd 单元、日志轮转规则或服务端配置结构；这些变更需要单独迁移。

## 客户端连接


xray-manager share device-1

复制输出中的整条 `vless://` 链接，导入支持 REALITY/Vision 的客户端，如 Windows 的 v2rayN、Android 的 v2rayNG、iOS 的兼容版本 Shadowrocket。启用节点和系统代理；需要代理整个设备的应用时使用客户端 TUN 功能。

首次验证可临时使用全局代理，确认正常后改为规则分流。此服务端不向客户端自动下发分流或 DNS 规则。

链接包含代理访问凭据，请勿公开分享。

## CLI 交互菜单

SSH 登录 VPS 后，在终端中直接运行：

```bash
xray-manager
# 或显式打开菜单
xray-manager menu
```

输入数字并按回车选择操作。主菜单按用途分组：

| 入口 | 功能 |
|---|---|
| 服务状态 | 查看 Xray 和统计定时任务状态 |
| 设备管理 | 设备列表、新增设备、客户端分享链接、撤销设备 |
| 日志与统计 | 累计或最近 1–90 天流量、当前 TCP 连接、历史来源 IP、最近 100 条服务日志、手动采集 |
| 备份管理 | 查看备份列表、大小及时间，查看文件详情，删除选中的备份 |
| 服务控制 | 启动、重启、停止 Xray |

设备列表只显示账号名，分享链接可选择单个或全部设备。每条 `vless://` 链接下方显示对应的终端二维码，方便在移动客户端中选择扫码导入；二维码使用同一条完整链接，在本机生成。终端宽度不足时会提示所需列数，扩大窗口或缩小字体后重新分享即可。子菜单输入 `0` 或 `q` 返回主菜单；从子菜单返回后直接显示主菜单。

二维码使用 `qrencode`，安装和升级脚本会安装缺失的依赖。若仅手动复制管理程序，可执行 `apt-get update` 和 `apt-get install -y qrencode` 安装。依赖缺失或生成失败时仍输出链接；命令行子命令 `xray-manager share [NAME]` 保持纯文本输出，便于复制和脚本使用。

新增/撤销设备、重启和停服会提示连接影响并要求确认，默认取消。备份删除也需要确认，删除后无法使用该备份恢复。操作完成或失败后按回车返回当前菜单；主菜单输入 `0` 或 `q` 退出，Ctrl+C 或输入结束也可退出。

菜单等待选择、输入参数、确认和返回期间不持有 `manager.lock`，也不保持统计数据库连接，因此不会阻塞后台每分钟采集或其他管理命令。实际采集和配置修改仍使用独占锁，增删设备的读取、校验、保存及重启在同一次锁定内完成；失败或退出时释放锁与数据库连接。

原有子命令保持可用，脚本和定时任务继续使用 `xray-manager collect`、`xray-manager stats` 等命令。非交互环境中不带参数只输出帮助，显式 `menu` 会报错，不会等待输入。帮助无需安装运行数据即可查看：`python3 manager.py help`。

已有安装在更新仓库后执行 `bash upgrade-xray.sh`，即可更新菜单功能，无需重装或设置 `VERSION`，只更新管理程序不会重启 Xray。

## 每台设备独立账号

```bash
# 创建账号并立即输出导入链接
xray-manager add-device iphone
xray-manager add-device windows

# 只列出设备账号名
xray-manager list-devices

# 查看某个账号，或所有账号的导入链接
xray-manager share iphone
xray-manager share

# 撤销账号，保留其历史统计
xray-manager remove-device iphone
```

账号名允许 1–40 个英文字母、数字、下划线、连字符。新增/撤销设备会校验配置、采集统计、备份配置，然后重启 Xray，现有连接会短暂中断。

不要把同一个 UUID 给多台设备使用，否则统计会合并。不要复用已撤销设备的名称，否则同名历史统计也会合并。UUID 是账号身份，不是硬件绑定，复制链接仍可在其他设备使用。

## 查看出入站和设备流量

```bash
# 从安装以来持久化累计值；查询前立即采集一次
xray-manager stats

# 最近 24 小时、最近 7 天、最近 30 天
xray-manager stats 1
xray-manager stats 7
xray-manager stats 30

# 手动采集
xray-manager collect

# 每 5 秒刷新
watch -n 5 xray-manager stats
```

统计名称说明：

| 名称 | 含义 |
|---|---|
| `user>>>iphone>>>traffic>>>uplink` | iphone 账号上传流量 |
| `user>>>iphone>>>traffic>>>downlink` | iphone 账号下载流量 |
| `inbound>>>vless-in>>>traffic>>>uplink` | 代理入站收到的数据 |
| `inbound>>>vless-in>>>traffic>>>downlink` | 代理入站发回客户端的数据 |
| `outbound>>>direct>>>traffic>>>uplink` | 代理向目标服务发送的数据 |
| `outbound>>>direct>>>traffic>>>downlink` | 代理从目标服务接收的数据 |

这些是同一批数据的不同统计视角，**不要把 user、inbound、outbound 的数值相加**。协议开销使这些数字可能不完全相同，也不等于商家账单或整个网卡流量；SSH、系统更新、未经认证的 REALITY 转发等不属于每设备账号流量。

- 每约 60 秒把 Xray 内存计数器的增量写入 SQLite。
- 按 systemd 服务启动标识识别重启，避免重启后计数归零造成累计值丢失。
- 累计统计长期保留；分时流量记录和 IP 历史保留 90 天。
- `stats N` 为最近 N×24 小时的采样增量，不是自然日/月账单。长连接跨边界时按采样时间归属。
- 崩溃、强制终止、重启机器可能损失最近一次采样后的流量（正常采集时约一分钟）；采集失败期间误差可能更大。属于运维统计，不是严格计费系统。

## 当前连接 IP

```bash
xray-manager online
watch -n 2 xray-manager online
```

显示代理端口当前已建立的 TCP 连接，Peer Address 是对端公网 IP 和端口，附带 `ss` 提供的 TCP 计数等信息。

**TCP 已连接不等于已经通过 VLESS 认证**，其中可能包含扫描器或 REALITY 转发连接。一个设备可能建立多个连接，多个设备也可能共用一个 NAT 公网 IP；不能把连接数量直接当成设备数量。空闲且没有 TCP 连接的客户端不会显示。

## 历史连接 IP 与流量

```bash
xray-manager ips
xray-manager stats
```

`ips` 按设备账号和来源公网 IP 汇总：首次请求时间、最近请求时间、已接受请求次数；时间为 UTC。它只收集带已认证用户 email 的访问记录，不把 API 本机访问和普通扫描作为设备记录。

将 `ips` 的账号名与 `stats` 中的 `user>>>账号名` 对照，即可查看该账号曾用过哪些 IP，以及该账号的上传/下载总量。

**重要边界：本方案持久化的是“账号流量”和“账号—来源 IP 历史”，没有把每个字节精确分配到来源 IP。** 同一 UUID 在多个 IP 同时使用或切换网络时，无法得出各 IP 的精确历史流量。Xray 原生用户计数器按 UUID/email 而非来源 IP 汇总；若要求严格按 IP 计费，需要另行部署网络层连接记账。

请求次数不是登录次数，首次/最近请求也不是精确的上线/下线时间。

## 启停、日志与配置

```bash
xray-manager status

# 优先使用这两个命令，操作前先保存一次统计
xray-manager restart
xray-manager stop

# 启动已停止的服务
xray-manager start

# 最近 100 条服务日志
xray-manager logs

# 开机自启
systemctl enable xray xray-stats.timer

# 启动错误和服务状态
journalctl -u xray -n 100 --no-pager
journalctl -u xray -f

# Xray 运行时错误和访问记录（含目标地址）
tail -f /var/log/xray/error.log
tail -f /var/log/xray/access.log

# 统计任务状态和错误
systemctl status xray-stats.timer
journalctl -u xray-stats.service -n 50 --no-pager

# 修改配置后的检查
runuser -u xray -- xray run -test -config /usr/local/etc/xray/config.json
xray-manager restart
```

`xray-stats.service` 是一次性任务，正常运行完成后显示 `inactive (dead)`；应查看退出码是否为 `0/SUCCESS`，以及 timer 是否 `active (waiting)`。

| 文件 | 用途 |
|---|---|
| `/root/xray-manager/deploy-xray.sh` | 安装/显式重装脚本 |
| `/root/xray-manager/upgrade-xray.sh` | 保留现有账号和配置的升级脚本 |
| `/root/xray-manager/manager.py` | 管理程序的仓库源码 |
| `/root/xray-manager/README.md` | 本文档 |
| `/usr/local/bin/xray` | Xray 可执行文件 |
| `/usr/local/etc/xray/config.json` | 服务端配置，包含 REALITY 私钥和设备 UUID |
| `/usr/local/bin/xray-manager` | 管理命令 |
| `/usr/local/lib/xray-manager/manager.py` | 管理和统计程序 |
| `/var/lib/xray-manager/connection.json` | 公网地址和 REALITY 公钥 |
| `/var/lib/xray-manager/stats.sqlite3` | 持久化流量与 IP 历史 |
| `/var/lib/xray-manager/config-*.json` | 增删设备时保存的配置备份 |
| `/var/log/xray/access.log` | 访问日志 |
| `/var/log/xray/error.log` | 运行错误日志 |
| `/etc/logrotate.d/xray` | 日志轮转规则，保留 7 份压缩文件 |

日志每天检查轮转，使用 copytruncate；复制/截断窗口可能丢失少量日志。历史记录适合运维排查，不是无损审计记录。

## 备份

通过主菜单的“备份管理”查看和管理已有备份，或使用子命令：

```bash
xray-manager backups
xray-manager backup-info /var/lib/xray-manager/config-实际时间戳.json
# --yes 表示已确认永久删除这个备份
xray-manager delete-backup /var/lib/xray-manager/config-实际时间戳.json --yes
```

支持设备修改生成的 `config-*.json`、升级生成的 `upgrade-*` 目录、`/root/xray-backup-*` 重装备份目录，以及 `/root/xray-stats-backup*.sqlite3`、`/root/xray-config-backup*.tar.gz` 手动备份。列表按修改时间倒序显示类型、路径、UTC 时间、文件大小和合计大小；详情展示文件名和大小，最多显示 50 条，不输出配置凭据或解析归档内容。文件大小为逻辑大小，可能与实际磁盘占用不同。

只能删除列表中识别的备份，符号链接不会作为备份入口，也不会遍历其目标。菜单选择的备份在确认期间如果被替换，会要求重新选择。删除期间与管理操作互斥；升级脚本运行时拒绝删除备份，以保留失败回滚需要的文件。此菜单用于已有备份的查看和删除，创建备份继续使用下列方法。

使用 SQLite 在线备份接口，避免复制正在写入的数据库：

```bash
python3 - <<'PY'
import sqlite3
with sqlite3.connect('/var/lib/xray-manager/stats.sqlite3') as src:
    with sqlite3.connect('/root/xray-stats-backup.sqlite3') as dst:
        src.backup(dst)
PY
chmod 600 /root/xray-stats-backup.sqlite3
tar -czf /root/xray-config-backup.tar.gz /usr/local/etc/xray /var/lib/xray-manager/connection.json
chmod 600 /root/xray-config-backup.tar.gz
```

## 仓库文件与本地检查

- `deploy-xray.sh`：首次安装和显式重装。
- `upgrade-xray.sh`：更新管理程序，可选更新 Xray-core。
- `manager.py`：安装与升级共用的管理程序源码。
- `tests/`：隔离环境中的交互菜单、并发锁、流量累计、升级与失败回滚测试。
- `.github/workflows/check.yml`：push 和 pull request 时运行语法检查和测试。
