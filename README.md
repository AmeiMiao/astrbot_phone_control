# astrbot_phone_control

[![GitHub](https://img.shields.io/badge/GitHub-acat--fun%2Fastrbot__phone__control-181717.svg)](https://github.com/acat-fun/astrbot_phone_control)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

> 把一台安卓模拟器变成 AstrBot 的「手机」：远程 ADB / scrcpy 控制，让主 Agent 以自身人格
> 浏览 App、理解内容、记录记忆，并按规则自主互动。

当前版本：`0.1.2` ｜ 适用 AstrBot：`>= 4.27.1, < 5` ｜ 主仓库：[GitHub](https://github.com/acat-fun/astrbot_phone_control) ｜ 许可证：[MIT](LICENSE)

---

## 简介

`astrbot_phone_control` 是 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的手机操控插件。
它通过远程 ADB（可选 scrcpy / minitouch 后端）操作一台安卓模拟器，把「看屏幕、点滑动、
输入、打开 App、理解内容、记录记忆」封装成 33 个 LLM 工具，并提供 `/phone` 命令族、
定时自主任务、只读监控面板与配置热应用。

插件不绑定具体 App：内置抖音 / B 站 / 小黑盒画像，其余任意已安装 App 用通用文本抽取兜底。
AI 可以自主浏览、看视频、查收件箱、按规则评论/回复/发帖；插件只负责授权、限额、冷却、
记账与安全边界，不写死任何 App 操作流程。

## 特性

- **连接常驻**：`control.scrcpy.keep_alive=true` 默认预热所选通道；首帧等待、断线恢复和实际回退原因可查，`/phone stop` 彻底停止。
- **内容任务**：`phone_content` 管理单篇图文或视频，原图正文关联、8 秒试看与 20 秒续看、评论阅读和回复分别决定。素材正常执行时受保护，结束按截图时限清理，异常立即回收。
- **经验自动归档**：有成功操作和后续页面证据时自动总结 App 使用经验，写入所选插件档案或知识库；待写重试与错误可在状态查看。
- **设备操控**：ADB / scrcpy 控制，UI 树解析、快速截图、点击/滑动/输入/按键/App 启停、后台清理。
- **自主玩手机**：定时自主浏览、随机玩手机、持续刷手机；用户任务优先抢占，轮次/预算/间隔可配。
- **内容理解**：信息流卡片抽取、浏览日记、学习档案、App 使用经验；经验可存入插件档案或 AstrBot 知识库，按选择查询与注入。
- **记忆插件集成**：可选同步 MemoryCompanion / LivingMemory（本地档案始终保留）。
- **人机验证**：两阶段视觉求解（滑块/顺序点击/识图/拼图/答题），有限自动尝试 → 暂停手机并通知人工接管。
- **互动能力**：主动评论 / 被动回复 / 发帖（默认关闭）由 AI 自主完成，插件只做许可、限额、冷却与记账。
- **抓包 + 屏幕识别协同**：`phone_screen` 自动附带当前前台 App 的抓包页面内容（按前台 App /
  最近操作 / 时效过滤），视觉信息用截图/OCR 兜底；`phone_traffic` 可查原始流量与分享链接。
- **流媒体内容识别**：录制设备播放声音 + 关键画面描述，看懂视频/直播/音乐（需要 STT/视觉 Provider）。
- **Token 对账**：按 AstrBot `provider_stats` 实际 usage 对账，滚动窗口额度可选（默认不限制）。
- **WireGuard 组网**：插件内置 userspace WG helper（容器无需 `NET_ADMIN`/`/dev/net/tun`）：
  `client` 连接已有 WG 服务器，或 `server` 让装了 WireGuard App 的手机接入；
  `/phone wg config` 输出服务器 Peer 片段 / 手机端完整配置，可选 `auto_adb` 把 ADB 目标切到
  WG 隧道。注意 ADB over WG 受 Android VpnService 按 UID 分流限制（手机官方 WG App 场景下
  adbd 回包不走隧道），详见使用手册「WireGuard 组网 → 已知限制」。
- **安全与运维**：管理员/主人白名单、统一设备执行入口（授权/占用/暂停/取消代次/期限）、
  只读 Dashboard、抓包日志打码与轮转；WG 私钥落盘 0600。

## 快速开始

### 环境要求

- AstrBot `>= 4.27.1, < 5`（已在 4.29.1 验证）；
- 一台开启 ADB 调试的安卓模拟器（或真机），AstrBot 容器可访问其 ADB 端口；
- 插件自带 `bin/linux-x86_64/adb` 与 `bin/scrcpy-server-v5.0.1`，无需额外安装 adb/scrcpy；
- 可选：`mitmproxy`（抓包识别，需独立 venv 安装，见使用手册）、PyAV 或宿主机 `ffmpeg`
  （scrcpy 视频镜像）、ADBKeyboard（中文输入）、minitouch（可选 touch 后端）。
- 可选：WireGuard 组网使用插件自带 userspace helper `bin/linux-x86_64/phone-wg`
  （无需 `NET_ADMIN`/`/dev/net/tun`；密钥与 helper 配置写入插件数据目录 `wireguard/`，文件 0600）。
  详见 [第三方引用说明](THIRD_PARTY_NOTICES.md)。

### 安装

1. 将本目录放到 AstrBot 的 `data/plugins/astrbot_phone_control/`（目录名需与
   `metadata.yaml` 的 `name` 一致），或在 AstrBot WebUI 上传插件包；
2. 在插件配置「ADB 连接」中填写必填项 `adb.host` 与 `adb.port`（模拟器 ADB 地址）；
3. 保存配置/重载插件，日志出现「已加载：ADB <host>:<port>」表示连接成功；
4. 对机器人发送 `/phone status` 验证，再试 `/phone browse douyin 3`。

未填写 `adb.host` / `adb.port` 时插件会加载失败并提示缺失项，这是预期行为。

### 示例

```text
用户：去小黑盒刷会儿，遇到 CS2 内容发我看看
AI：调用 phone_open_app / phone_browse / phone_share_to_user 等工具完成任务并汇报

用户：/phone task 去B站看三个科技视频并总结
AI：转入后台静默执行，完成后主动汇报

用户：/phone status
AI：返回设备连接、前台 App、电量与分辨率
```

## 配置

配置按卡片分组（详见 [使用手册 · 配置](docs/使用手册.md#配置) 与插件配置页）：

| 卡片 | 作用 |
| --- | --- |
| ADB 连接 | `host` / `port` 必填；`path` 留空用插件自带 adb；`serial`/超时可调 |
| 权限与安全 | 管理员/白名单/主人；浏览与任务结束清理；评论/回复/发帖开关与限额 |
| 屏幕感知 | UI 节点上限、dump/截图模式、实时屏幕、OCR/验证超时、感知来源 |
| 控制/感知后端 | auto/adb/scrcpy；scrcpy 视频镜像参数；可选 minitouch |
| WireGuard 组网 | enabled/mode(client/server)/listen_port/interface_address/server_endpoint(client)/public_endpoint(server)/server_public_key/PSK/phone_wg_ip/adb_local_port/auto_adb |
| 信息流浏览 | 默认 App、条数、滑动节奏、截图与日记开关 |
| 自主浏览 / 随机玩 / 持续刷 | 定时任务、随机间隔与概率、持续刷间隔/预算/偏好 App |
| 刷手机/学习模型 | 自主任务专用 Provider（用户对话不受影响） |
| Token 额度 | 滚动窗口额度与单轮预算（默认不限制） |
| 内容新鲜感 | 同类内容疲劳度阈值/半衰期/相似度（0=关闭） |
| 流媒体内容识别 | 录音源、时长、关键帧、ASR/视觉 Provider |
| 流量抓包识别 | 开关、端口、正文截断、保留窗口 |
| 验证与人工协助 | 失败次数/总时限/单次时限/等待时限/通知目标 |
| 记忆存储 / 提示词注入 / 调试 | 记忆去向、注入开关、动作日志级别 |

配置保存后 AstrBot 会自动重载插件；`/phone` 命令补丁会立即热应用。

## 常用命令

```text
/phone help                              查看帮助
/phone status                            设备状态
/phone browse [app] [条数]               浏览信息流并记入记忆
/phone task <instruction>                多步任务转后台静默执行
/phone tasks                             查看后台任务
/phone continuous on|off|status          持续刷手机
/phone play [tokens]                     立即静默玩手机一轮
/phone traffic [status|tail|on|off]      抓包状态/最近流量/开关
/phone wg [status|start|stop|restart|config]  WireGuard 组网状态/开关/导入用配置
/phone verify [status|resume|cancel <id>] 验证与人工协助
/phone quota [audit [小时]]              token 额度与对账
/phone novelty [status|reset]            内容新鲜感状态
/phone stop [job_id|all]                 停任务/抓包/随机与持续刷，回桌面强停应用
```

完整命令、33 个 LLM 工具说明与 Skill 手册见 [使用手册](docs/使用手册.md#llm-工具)。

## 安全与隐私

- 默认 `require_admin=true`：仅 AstrBot 管理员、白名单用户或插件定时任务可操作手机；
  `owner_ids` 非空时只认插件主人（管理员也不放行）。
- 插件不提供 shell / 任意命令执行工具；互动能力默认关闭，开启后仍受每日限额与冷却约束。
- 抓包仅在本机模拟器上通过 mitmproxy 进行，日志正文截断、URL 敏感参数打码、日志轮转；
  截图、日记、学习档案等数据均写入插件数据目录 `data/plugin_data/astrbot_phone_control/`。
- 验证码、短信/邮箱验证码、人脸与实名认证等账号安全操作始终交由用户处理。
- WireGuard 私钥与 helper 配置只写插件数据目录 `wireguard/`（keys.json / phone-wg.json，
  0600），不写入日志；server 模式需自行把 UDP `listen_port` 暴露到手机可达的网络，
  对端 `AllowedIPs` 按最小网段配置，公网部署建议配合防火墙与 PSK。

## 目录结构

```text
main.py                插件入口：入口类 PhoneControlPlugin + AstrBot 命令/钩子处理器
core/plugin/           插件运行逻辑 mixins（装配/生命周期、命令、任务、设备钩子、后端、监控、记忆、记账）
core/service.py        能力门面 PhoneService（状态/UI/截图/浏览/日记/分享/抓包页面内容）
core/verification_controller.py  验证会话控制器（尝试次数/总时限/人工接管/持久化）
core/wireguard.py      WireGuard 组网：X25519 密钥、helper 配置/状态文件、子进程生命周期、ADB over WG
core/tools/            33 个 LLM 工具实现（按域拆分）
core/*.py              设备层与纯逻辑模块（ADB/UI/动作/配置/抓包/记忆/新鲜感等）
skills/                AI 行为手册（自主浏览、互动、验证、媒体、整理）
views/dashboard/       只读监控面板与学习记录页
docs/使用手册.md        完整功能、配置、工具、命令与 FAQ
bin/                   自带 adb、scrcpy-server 与 phone-wg helper（含第三方 NOTICE）
```

各模块职责详见 [使用手册 · 目录结构](docs/使用手册.md#目录结构)。

## 开发与测试

```bash
# 纯逻辑单测（本机无 AstrBot 依赖时，test_main_* 等容器内用例会自动 skip）
python3 -m pytest -q

# 安装依赖（抓包可选）
pip install -r requirements.txt
```

- 插件要求 Python 3.10+（AstrBot 4.29 运行环境）；
- 完整测试与 UAT 验证需要在包含 AstrBot 依赖的容器内执行 `python3 -m pytest tests -q`；
- 修改代码前请阅读仓库内开发约定（若存在），提交保持中文信息与 LF 行尾。

## 常见问题

- **插件加载失败并提示缺少 ADB 配置**：`adb.host`/`adb.port` 是必填项，请在插件配置页填写后重载。
- **中文输入无效**：需在模拟器安装并启用 ADBKeyboard（`com.android.adbkeyboard/.AdbIME`）。
- **抓包没有内容**：确认模拟器可 `adb root`、容器已安装 mitmproxy；部分 App 证书固定/加密时只能回退截图识别。
- **用量与 AstrBot 面板不一致**：`/phone quota` 只统计手机相关 run，面板统计全部请求；
  实际值以 Agent run 结束后按 `provider_stats` 对账为准。

更多问题见 [使用手册 · 常见问题](docs/使用手册.md#常见问题)。

## 开源协议

本项目采用 **MIT License** 开源，详见 [LICENSE](LICENSE)。

> 版权声明：Copyright (c) 2026 ACAT。

## 第三方组件与引用

插件分发/依赖的第三方组件（scrcpy、Android platform-tools adb、minitouch、ADBKeyboard、
mitmproxy、PyAV、ffmpeg、AstrBot、MemoryCompanion / LivingMemory 等）的版权、许可证与
使用方式见 **[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)**。随插件分发的二进制
NOTICE 保留在 `bin/scrcpy-server-NOTICE.txt` 与 `bin/linux-x86_64/NOTICE.txt`。

## 声明

- 本项目与 Google、Genymobile、DeviceFarmer、AstrBot 及其维护者无隶属或背书关系；
  相关商标归各自权利人所有。
- 本插件仅用于操作使用者自己拥有或获授权使用的设备与账号；请遵守目标 App 的服务条款
  与所在地法律法规。因使用本插件产生的账号、数据或法律风险由使用者自行承担。

## 文档与仓库

- [完整使用手册](docs/使用手册.md)
- [第三方组件与引用说明](THIRD_PARTY_NOTICES.md)
- [许可证](LICENSE)

## 子进程与停止行为

`/phone stop` 会关闭抓包、自主浏览调度、随机与持续任务，停止实时屏幕和控制后端，并回桌面强停应用；桌面和当前输入法保持运行。后续可用 `/phone traffic on` 或 `/phone continuous on` 显式开启相应能力。

抓包、WireGuard 和解码管道由独立监护器管理，手机端 scrcpy、音频、minitouch 使用设备端看门狗。插件每 3 秒发送心跳，失联后最长约 18 秒回收。重载时按代码指纹识别前任租约，先释放资源再建立新会话。

App 经验在「提示词注入」卡片开启后，`app_memory_backend=plugin` 显示注入条数，`knowledge_base` 显示知识库名称或 ID。知识库需在 AstrBot 中创建并配置 Embedding Provider；记录、查询与注入均使用所选位置，存储错误会直接返回。
