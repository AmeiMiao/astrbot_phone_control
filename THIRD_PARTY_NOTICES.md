# 第三方组件与引用说明

本插件本体以 [MIT License](LICENSE) 开源。插件在运行或分发过程中使用/引用了以下第三方
软件与组件；各组件版权归其各自权利人所有，许可证以对应项目原文为准。本文档用于披露
引用关系与许可证归属，不改变任何第三方组件的许可证条款。

## 一、随插件分发的二进制

| 组件 | 版本/来源 | 许可证 | 分发形式与说明 |
| --- | --- | --- | --- |
| **adb**（Android platform-tools，Linux x86_64） | AOSP Android SDK Platform-Tools 37.0.1（官方） | **Apache-2.0** | 位于 `bin/linux-x86_64/adb`，原样分发未修改。版权归 Google LLC 及 Android 项目贡献者；许可证与 NOTICE 全文见 `bin/linux-x86_64/NOTICE.txt`（来源 sdk-repo-linux-platform-tools.zip） |
| **scrcpy-server** | [scrcpy](https://github.com/Genymobile/scrcpy) v5.0.1 | **Apache-2.0** | 位于 `bin/scrcpy-server-v5.0.1`，为 scrcpy 项目发布的 server 构建产物，未修改；通过 adb 推送到设备的 `/data/local/tmp` 以 `app_process` 运行。版权：Copyright (c) 2018 Genymobile、Copyright (c) 2018-2026 Romain Vimont 及 scrcpy 贡献者。详见 `bin/scrcpy-server-NOTICE.txt` |
| **phone-wg**（WireGuard userspace helper） | 本仓库 `helper/phone-wg` 源码构建；静态链接 [wireguard-go](https://git.zx2c4.com/wireguard-go/) `v0.0.0-20261006164505-2631ce99a06f` 与 [gVisor](https://gvisor.dev/) `v0.0.0-20250503011706-39ed1f5ac29c` | **MIT + Apache-2.0** | 随包分发 `bin/linux-x86_64/phone-wg`（linux/amd64，静态链接 ELF）；wireguard-go 为 **MIT**，gVisor netstack 为 **Apache-2.0**，编译时未修改上游代码。其他平台不随包分发，用户可按本仓库源码自行编译。静态链接组件清单与许可证声明见「四、phone-wg 静态链接的第三方组件」 |
| **Tailscale CLI / tailscaled** | [Tailscale](https://github.com/tailscale/tailscale) 1.104.2 官方 Linux amd64 归档 | **BSD-3-Clause** | `bin/linux-x86_64/tailscale`、`tailscaled`；独立用户态节点。许可证及官方归档 SHA256 见 `TAILSCALE-LICENSE.txt` 与 `NOTICE.txt` |
| **ZeroTier Sockets SDK** | [libzt](https://github.com/zerotier/libzt) `a707ea6ae0910efdc1125d04758c411e2e9ea4f9` | **BSL-1.1** 及组件许可证 | `bin/linux-x86_64/libzt.so`；按 `helper/mesh/build-zerotier.sh` 构建，来源与完整许可证见 `ZEROTIER-LICENSE.txt` 和 `zerotier-licenses/`；使用须遵守上游授权条款 |

Apache-2.0 许可证全文见 `bin/linux-x86_64/NOTICE.txt`，或 <https://www.apache.org/licenses/LICENSE-2.0>。

## 二、运行时依赖（不随插件分发）

以下组件由 AstrBot、宿主机或用户环境提供，插件不包含其源码/二进制：

| 组件 | 用途 | 许可证 | 说明 |
| --- | --- | --- | --- |
| [AstrBot](https://github.com/AstrBotDevs/AstrBot) | 插件宿主框架（4.27.1–4.29.x） | **AGPL-3.0** + 项目 EULA | 本插件仅通过 AstrBot 公开的插件 API（Star/Context/FunctionTool/hook 等）运行，不包含 AstrBot 源代码；AstrBot 及其依赖的许可证以 AstrBot 仓库为准 |
| [mitmproxy](https://github.com/mitmproxy/mitmproxy) | 手机流量抓包与解密（`traffic.enabled`） | **MIT** | 可选运行依赖（`requirements-traffic.txt`，`mitmproxy>=12,<13`）；因 AstrBot Core 固定 cryptography==50.0.1、mitmproxy 12.x 要求 cryptography<=48.1，需用独立 venv 安装并通过 `traffic.mitmdump_path` 指定，不随插件自动安装 |
| [PyAV](https://github.com/PyAV-Org/PyAV)（`av`） | scrcpy 视频镜像的实时解码（可选） | **BSD-3-Clause** | 不随插件分发，运行环境安装时生效；缺失时自动回退宿主机 `ffmpeg` 管道 |
| [FFmpeg](https://ffmpeg.org/) | 视频镜像回退解码（可选） | **LGPL-2.1-or-later / GPL-2.0-or-later**（取决于发行构建） | 由宿主机/容器提供，不随插件分发；具体许可证以所用构建为准 |
| [minitouch](https://github.com/DeviceFarmer/minitouch) | 可选 touch 注入后端 | **Apache-2.0**（Copyright © CyberAgent, Inc.） | 用户自备与模拟器架构匹配的 binary，插件仅在用户显式配置路径时推送到设备运行，不内置二进制 |
| [ADBKeyboard](https://github.com/senzhk/ADBKeyBoard) | 安卓端中文输入法（可选） | **GPL-2.0** | 用户自行安装到模拟器的独立 App；插件仅通过 ADB 广播 `ADB_INPUT_TEXT` 触发输入，不包含其任何代码 |
| astrbot_plugin_memory_companion（MemoryCompanion） | 可选记忆插件同步目标 | 以该插件发布页为准 | AstrBot 社区插件，运行时通过其公开桥接接口可选同步；不随本插件分发 |
| astrbot_plugin_livingmemory（LivingMemory） | 可选记忆插件同步目标 | 以该插件发布页为准 | AstrBot 社区插件，运行时通过插件实例写入；不随本插件分发 |

## 三、其他说明

- **Python 标准库**：本插件大量依赖 Python 标准库（PSF License），不单独分发。
- **AstrBot 运行时自带依赖**（`mcp`、`pydantic` 等）由 AstrBot 安装并管理，本插件不重复
  分发；其许可证以 AstrBot 及其依赖项目为准。
- 插件中出现的 App 名称、图标与商标（抖音、哔哩哔哩、小黑盒、微信等）仅用于识别模拟器
  上用户自行安装的应用，归各权利人所有；本插件与这些 App 的运营方无隶属或合作关系。
- 若您是某第三方组件的权利人，认为本项目的引用/分发方式需要调整，请通过仓库 Issue 联系。

## 四、phone-wg 静态链接的第三方组件

随包分发的 `bin/linux-x86_64/phone-wg`（linux/amd64）由本仓库 `helper/phone-wg`
的 Go 源码构建，静态链接以下组件
（Go 编译产物不保留动态依赖，许可证声明随二进制分发）：

| 组件 | 版本 | 许可证 | 版权/说明 |
| --- | --- | --- | --- |
| [wireguard-go](https://git.zx2c4.com/wireguard-go/) | `v0.0.0-20261006164505-2631ce99a06f` | **MIT** | Copyright (C) 2017-2025 WireGuard LLC. All Rights Reserved.；提供 WireGuard 协议实现、`tun/netstack` 与设备配置文本协议 |
| [gVisor](https://gvisor.dev/)（`gvisor.dev/gvisor` 的 netstack/tcpip） | `v0.0.0-20250503011706-39ed1f5ac29c` | **Apache-2.0** | Copyright 2018 The gVisor Authors；提供用户态 TCP/IP 协议栈 |
| [golang.org/x/crypto](https://pkg.go.dev/golang.org/x/crypto) | `v0.37.0` | **BSD-3-Clause** | Copyright 2009 The Go Authors；提供 Curve25519 密钥派生 |
| [golang.org/x/net](https://pkg.go.dev/golang.org/x/net) | `v0.39.0` | **BSD-3-Clause** | Copyright 2009 The Go Authors；提供 ICMP/IPv4 报文处理 |
| [golang.org/x/sys](https://pkg.go.dev/golang.org/x/sys) | `v0.32.0` | **BSD-3-Clause** | Copyright 2009 The Go Authors；提供系统调用封装 |
| [golang.org/x/time](https://pkg.go.dev/golang.org/x/time) | `v0.7.0` | **BSD-3-Clause** | Copyright 2009 The Go Authors |
| [github.com/google/btree](https://github.com/google/btree) | `v1.1.2` | **Apache-2.0** | Copyright 2014 Google LLC |
| Go 运行时与标准库 | Go 1.26 工具链 | **BSD-3-Clause** | Copyright 2009 The Go Authors |
| `golang.zx2c4.com/wintun` | `v0.0.0-20230126152724-0fa3db229ce2` | **MIT** | 仅 wireguard-go 的 Windows 构建使用；插件随包产物为 linux/amd64，不包含该模块代码（用户自行交叉编译 Windows 版时包含） |

Apache-2.0 许可证全文见 `bin/linux-x86_64/NOTICE.txt` 或
<https://www.apache.org/licenses/LICENSE-2.0>；BSD-3-Clause 许可证全文见
<https://go.dev/LICENSE>。

wireguard-go 的 MIT 许可证全文：

```text
Copyright (C) 2017-2025 WireGuard LLC. All Rights Reserved.

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
of the Software, and to permit persons to whom the Software is furnished to do
so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

gVisor 的声明：Copyright 2018 The gVisor Authors，许可证为 Apache-2.0（全文同上）。

## 五、Phone WG Relay

`bin/android/phone-wg-relay.apk` 由本项目 `helper/android-relay` 源码构建，使用项目 MIT 许可；依赖 Android 系统 API。WireGuard、Tailscale、ZeroTier 手机客户端由用户按其官方渠道安装；中转 App 在已连接的 VPN 接口上提供受来源地址限制的 ADB 转发。
