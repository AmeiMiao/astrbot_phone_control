# 第三方组件与引用说明

本插件本体以 [MIT License](LICENSE) 开源。插件在运行或分发过程中使用/引用了以下第三方
软件与组件；各组件版权归其各自权利人所有，许可证以对应项目原文为准。本文档用于披露
引用关系与许可证归属，不改变任何第三方组件的许可证条款。

## 一、随插件分发的二进制

| 组件 | 版本/来源 | 许可证 | 分发形式与说明 |
| --- | --- | --- | --- |
| **adb**（Android platform-tools） | AOSP Android SDK Platform-Tools | **Apache-2.0** | 位于 `bin/linux-x86_64/adb`，原样分发未修改。版权归 Google LLC 及 Android 项目贡献者；许可证与 NOTICE 全文见 `bin/linux-x86_64/NOTICE.txt`（来源 sdk-repo-linux-platform-tools.zip） |
| **scrcpy-server** | [scrcpy](https://github.com/Genymobile/scrcpy) v5.0.1 | **Apache-2.0** | 位于 `bin/scrcpy-server-v5.0.1`，为 scrcpy 项目发布的 server 构建产物，未修改；通过 adb 推送到设备的 `/data/local/tmp` 以 `app_process` 运行。版权：Copyright (c) 2018 Genymobile、Copyright (c) 2018-2026 Romain Vimont 及 scrcpy 贡献者。详见 `bin/scrcpy-server-NOTICE.txt` |

Apache-2.0 许可证全文见 `bin/linux-x86_64/NOTICE.txt`，或
<https://www.apache.org/licenses/LICENSE-2.0>。

## 二、运行时依赖（不随插件分发）

以下组件由 AstrBot、宿主机或用户环境提供，插件不包含其源码/二进制：

| 组件 | 用途 | 许可证 | 说明 |
| --- | --- | --- | --- |
| [AstrBot](https://github.com/AstrBotDevs/AstrBot) | 插件宿主框架（4.27.1–4.29.x） | **AGPL-3.0** + 项目 EULA | 本插件仅通过 AstrBot 公开的插件 API（Star/Context/FunctionTool/hook 等）运行，不包含 AstrBot 源代码；AstrBot 及其依赖的许可证以 AstrBot 仓库为准 |
| [mitmproxy](https://github.com/mitmproxy/mitmproxy) | 手机流量抓包与解密（`traffic.enabled`） | **MIT** | 通过 `requirements.txt` 作为可选运行依赖（`mitmproxy>=12,<13`），需在容器内安装；插件调用其 `mitmdump` 与 addon API |
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
