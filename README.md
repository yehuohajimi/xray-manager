# Xray Manager

面向 Debian/Ubuntu VPS 的 Xray 安装与管理脚本，使用 VLESS + REALITY + Vision，提供设备账号、流量统计和来源 IP 历史查询，以及保留现有账号和配置的原地升级。

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

```bash
apt-get update
apt-get install -y git ca-certificates
git clone https://github.com/yehuohajimi/xray-manager.git /root/xray-manager
cd /root/xray-manager
```

仓库中已有代码时使用 `git pull --ff-only`。安装脚本需要同目录的 `manager.py`，请克隆完整仓库。

## 首次安装 / 显式重装

全新服务器执行：

```bash
cd /root/xray-manager
SERVER_IP='你的公网IPv4或域名' bash deploy-xray.sh
```

| 参数 | 默认值 | 用途 |
|---|---|---|
| `SERVER_IP` | 必填 | 客户端连接的公网 IPv4 或域名 |
| `PORT` | `443` | 代理 TCP 监听端口，范围 1..65535，不能使用统计 API 端口 10085 |
| `SNI` | `www.cloudflare.com` | REALITY 目标站点及 serverName |
| `VERSION` | `v26.3.27` | 首次安装的 Xray-core 正式版本标签 |
| `REINSTALL` | `0` | 设为 `1` 时明确重装已有安装 |

```bash
SERVER_IP='你的公网IPv4或域名' PORT=443 SNI=www.cloudflare.com VERSION=v26.3.27 bash deploy-xray.sh
```

已有本项目的标准安装时，使用下节的升级脚本即可保留设备账号。只有需要重新生成代理身份时才使用显式重装：

```bash
SERVER_IP='你的公网IPv4或域名' REINSTALL=1 bash deploy-xray.sh
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

登录 VPS：

```bash
ssh root@你的VPS地址
xray-manager share device-1
```

复制输出中的整条 `vless://` 链接，导入支持 REALITY/Vision 的客户端，如 Windows 的 v2rayN、Android 的 v2rayNG、iOS 的兼容版本 Shadowrocket。启用节点和系统代理；需要代理整个设备的应用时使用客户端 TUN 功能。

首次验证可临时使用全局代理，确认正常后改为规则分流。此服务端不向客户端自动下发分流或 DNS 规则。

链接包含代理访问凭据，请勿公开分享。

## 每台设备独立账号

```bash
# 创建账号并立即输出导入链接
xray-manager add-device iphone
xray-manager add-device windows

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
systemctl start xray

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
- `tests/`：隔离环境中的升级与失败回滚测试。
- `.github/workflows/check.yml`：push 和 pull request 时运行语法检查和测试。

在仓库目录验证源码：

```bash
bash -n deploy-xray.sh upgrade-xray.sh
python3 -m unittest discover -s tests -v
```

测试使用临时文件和模拟服务命令。真实客户端连接、目标版本兼容性和网络线路质量仍需在目标 VPS 与实际设备上验证。

## 公开仓库的文件范围

仓库用于存放通用脚本、文档和测试。服务器登录备注、本地部署记录、设备链接、配置私钥、数据库、日志及备份应留在服务器或本地，`.gitignore` 已排除常见的此类文件。安装生成的运行文件位于 `/usr/local`、`/etc` 和 `/var/lib/xray-manager`，不需要复制到仓库。

公开克隆与更新使用上述 HTTPS 地址即可；上传代码到 GitHub 时仍需在维护者的电脑上进行 GitHub 认证。
