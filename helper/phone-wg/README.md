# phone-wg：WireGuard userspace helper

`phone-wg` 是 AstrBot 手机控制插件自带的 Go 辅助程序，用 [wireguard-go](https://git.zx2c4.com/wireguard-go/)
与 `tun/netstack` 在**用户态**实现 WireGuard 组网与 TCP 转发：

- 不需要 `NET_ADMIN`/`NET_RAW`、`/dev/net/tun`、内核 `wireguard` 模块，也不需要 `ip`/`wg` 工具，
  可在无特权容器内运行；
- 既可作 **WG client**（`endpoint` 指向远端 WG 服务端），也可作 **WG server**（监听 UDP 端口，
  接受手机等 WG client 接入，自动学习对端 endpoint 支持 roaming）；
- 通过本地 `127.0.0.1:<port>` 到隧道内 `ip:port` 的 TCP 转发实现 ADB over WireGuard；Android 官方 WG App 使用 [Phone WG Relay](../android-relay/README.md) 的普通 App UID 中转入口。
- 可配置 `socks_listen=127.0.0.1:<port>`，使用 SOCKS5 CONNECT 动态访问 peer AllowedIPs 内的 IPv4/IPv6 地址。域名由客户端解析后传入 IP，UDP/IP 转发由独立网络网关提供。

插件 Python 侧以子进程方式管理本 helper：写入配置 JSON、读取状态 JSON、按退出码与日志判断失败原因。

## 目录

| 文件 | 说明 |
| --- | --- |
| `main.go` | 命令行入口（`run`/`genkey`/`pubkey`/`version`）、信号处理 |
| `config.go` | 配置 JSON 解析与校验 |
| `runtime.go` | WireGuard 设备、TCP 转发、状态文件周期写入、优雅关闭 |
| `status.go` | 状态文件结构、IPC 状态解析、原子写入 |
| `logger.go` | 分级日志（输出到 stderr） |
| `config_test.go` | 配置校验、IPC 文本协议、状态 JSON 字段的单元测试 |
| `build.sh` | 构建脚本（本机 Go 或 docker 镜像） |

## 构建

要求 Go 1.23 及以上；或本机有 docker（脚本自动使用 `golang:1.26-alpine` 构建）。

```bash
cd helper/phone-wg
./build.sh                 # 默认构建 linux/amd64、linux/arm64、windows/amd64
TARGETS="linux/arm64" ./build.sh   # 只构建单个目标
```

构建固定使用 `CGO_ENABLED=0`，产物为静态链接二进制，按目标平台直接写入插件的
`bin/<平台>/`：

| 目标 | 产物 |
| --- | --- |
| `linux/amd64` | `bin/linux-x86_64/phone-wg` |
| `linux/arm64` | `bin/linux-aarch64/phone-wg` |
| `windows/amd64` | `bin/windows-x86_64/phone-wg.exe` |

构建后可核对：

```bash
file ../../bin/linux-aarch64/phone-wg
# ELF 64-bit LSB executable, ARM aarch64, statically linked
```

`build.sh` 会先后执行 `go mod tidy`、`go vet ./...`、`go test ./...` 再按目标矩阵编译；
`go mod tidy` 会在本目录生成 `go.sum`（本地构建产物，不随仓库提交），`bin/` 与 `out/`
同样是本地构建产物。Windows 产物运行时需要 WireGuard 官方 `wintun.dll`（不随插件分发）。

## 命令行

```text
phone-wg run --config <配置文件> [--status-file <状态文件>]
phone-wg genkey
phone-wg pubkey --private-key <base64>
phone-wg version
```

- `run`：前台运行；收到 `SIGTERM`/`SIGINT` 后关闭监听与连接、撤销设备并以退出码 0 退出。
- `genkey`：生成 32 字节 Curve25519 私钥（已完成 clamp），输出标准 base64。
- `pubkey`：由私钥推导公钥并以 base64 输出。
- `version`：输出版本号（同时写入状态文件的 `version` 字段）。

日志逐行写入 stderr（纯文本，带时间与级别），不向 stdout 写业务数据。

## 配置 JSON

插件写入、helper 读取。未知字段忽略；缺省值：`mtu=1420`、`log_level="info"`、
`listen_port=0`、`preshared_key=""`、`persistent_keepalive_seconds=0`。

```json
{
  "interface": {
    "private_key": "<base64 32B>",
    "addresses": ["10.66.0.2/24"],
    "listen_port": 0,
    "mtu": 1420
  },
  "peers": [
    {
      "public_key": "<base64 32B>",
      "preshared_key": "",
      "endpoint": "192.168.250.50:51820",
      "allowed_ips": ["10.66.0.0/24"],
      "persistent_keepalive_seconds": 25
    }
  ],
  "forwards": [
    {"listen": "127.0.0.1:15555", "target": "10.66.0.3:5555"}
  ],
  "log_level": "info"
}
```

| 字段 | 说明 |
| --- | --- |
| `interface.private_key` | 本端 WireGuard 私钥（base64）。helper 不生成密钥，由插件生成并持久化 |
| `interface.addresses` | netstack 接口地址（CIDR），client 如 `10.66.0.2/24`，server 如 `10.66.0.1/24` |
| `interface.listen_port` | UDP 监听端口；`0` 表示由系统分配（client 默认；server 填固定端口） |
| `interface.mtu` | 接口 MTU，缺省 1420 |
| `peers[].public_key` | 对端公钥（base64） |
| `peers[].preshared_key` | 可选预共享密钥（base64），空串表示未启用 |
| `peers[].endpoint` | 对端 `ip:port`；client 必填，server 对手机可留空，由对端握手报文学习并支持 roaming |
| `peers[].allowed_ips` | 必填非空，`0.0.0.0/0` 表示全隧道 |
| `peers[].persistent_keepalive_seconds` | 持久保活间隔，`0` 表示关闭；非 0 时设备就绪后立即触发一次握手 |
| `forwards[]` | 本地 TCP 转发：`listen` 必须为 `127.0.0.1:<port>`，`target` 必须落在某个 `allowed_ips` 内 |
| `socks_listen` | 可选环回 SOCKS5 TCP 监听地址；与固定转发端口不同，留空关闭 |
| `log_level` | `debug`/`info`/`warn`/`error`/`silent`，缺省 `info` |

配置校验失败（非法 JSON、非法 base64/CIDR、重复监听端口、非法 endpoint、forward 目标不在
任何 `allowed_ips` 内等）、或缺少必要参数时，helper 在 stderr 说明原因并以**退出码 2**退出。

## 状态 JSON

`--status-file` 指定的文件在首次就绪后立即写一次，之后每秒刷新；写入为原子操作
（先写 `<path>.tmp` 再 rename，插件不会读到半截 JSON）。

```json
{
  "running": true,
  "version": "0.1.0",
  "interface": {
    "listen_port": 51820,
    "addresses": ["10.66.0.1/24"],
    "public_key": "<base64>"
  },
  "peers": [
    {
      "public_key": "<base64>",
      "endpoint": "1.2.3.4:51820",
      "allowed_ips": ["10.66.0.0/24"],
      "last_handshake_unix": 1792510000,
      "rx_bytes": 1234,
      "tx_bytes": 5678
    }
  ],
  "forwards": [
    {"listen": "127.0.0.1:15555", "target": "10.66.0.3:5555", "active_connections": 1, "total_connections": 7, "last_error": ""}
  ],
  "updated_at_unix": 1792510001
}
```

- `interface.listen_port` 为设备实际绑定的端口（`listen_port=0` 时为系统分配的实际端口）；
- `peers[].endpoint` 取运行时学习到的 endpoint，未学习到时回退配置值；
- `last_handshake_unix=0` 表示尚未握手；`forwards[].last_error` 空串表示无错误；
- 插件可用 `updated_at_unix`/mtime 判断 helper 是否存活（超过 15 秒未更新视为异常）。

## 运行期失败语义

- 单条 forward 拨号失败**不退出**：记录 `forwards[].last_error`、关闭该连接，下一次 accept 继续尝试；
  成功拨号后清空 `last_error`。
- WireGuard 设备/监听端口/转发端口初始化失败（如端口被占用）→ stderr 说明原因，**退出码 3**；
  由插件决定是否退避重试。
- 状态文件首次写入失败同样以退出码 3 退出（否则插件无法判断 helper 健康状态）；运行期写入
  失败只记日志并持续重试。

## 测试

```bash
cd helper/phone-wg
go vet ./...
go test ./...
```

单元测试覆盖配置解析与校验、IPC 文本协议解析、状态 JSON 字段名、`genkey`/`pubkey` 密钥格式等。

## 许可证

`phone-wg` 编译并静态链接了以下第三方组件，随插件分发时其许可证声明见仓库根
`THIRD_PARTY_NOTICES.md`：

- [wireguard-go](https://git.zx2c4.com/wireguard-go/)（MIT）
- [gVisor](https://gvisor.dev/) netstack（Apache-2.0）
