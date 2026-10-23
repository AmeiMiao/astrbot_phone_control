# Changelog

本插件版本记录。版本号遵循语义化版本；正式版使用 GitHub Releases，测试版以预发布标记。

## [Unreleased]

## [0.1.6.14] - 2026-10-23

### 新增
- 模型识别已安装应用类型；金融类、未确认类型和识别失败拒绝启动，命令/工具/浏览共用校验；应用列表展示类型与启动结果。

## [0.1.6.13] - 2026-10-23

### 新增
- 插件说明、配置、帮助与 Dashboard 显示使用风险提示：使用者自行承担账号限流、封禁、设备级封锁及其他全部风险。

## [0.1.6.12] - 2026-10-23

### 新增
- 发帖与回复可按语义选择服务器或手机本地保存图片，支持来源筛选、互动预约校验、原图核验与相册准备；Agent 在 App 核对预览后完成发布。

## [0.1.6.11] - 2026-10-23

### 新增
- 记忆索引支持相似关系图谱：按同一向量空间的余弦相似度连线，支持阈值、节点数与来源筛选、缩放、拖动、节点详情和删除。

## [0.1.6.10] - 2026-10-23

### 新增
- Dashboard「记忆索引」支持单条索引删除，保留原始档案；删除结果跨重载、缓存与后台重建持续生效。

## [0.1.6.9] - 2026-10-23

### 新增
- Dashboard「浏览与学习」统一侧栏入口，内容顶部切换浏览日志和学习记录。
- 浏览日志支持确认删除，关联索引即时失效；学习记录删除同步维护索引，后台重建与缓存遵守删除结果。

## [0.1.6.8] - 2026-10-23

### 优化
- 状态与帮助图片使用纯文字卡片模板，原文、换行及参数占位符按文字展示，支持完整长图输出。

## [0.1.6.7] - 2026-10-23

### 修复
- 命令图片将尖括号作为文字展示，帮助中的 `<app>`、`<instruction>` 等参数占位符完整可见。

## [0.1.6.6] - 2026-10-23

### 优化
- 状态与帮助图片保留逐行排版，便于逐项阅读；向量页搜索框使用统一样式。
- LivingMemory 实例由当前插件注册信息获取。

## [0.1.6.5] - 2026-10-23

### 修复
- 命令输出配置卡片排在提示词配置之后，调试卡片保持在配置页末尾。

## [0.1.6.4] - 2026-10-23

### 修复
- 手机学习写入 LivingMemory 时复用其访问规则和作用域解析，支持用户跨会话共享、身份别名与隔离会话；后台用户任务按发起人解析归属。

## [0.1.6.3] - 2026-10-23

### 新增
- Dashboard 学习向量页：查看 App 操控经验、聊天学习与浏览索引的模型、维度、更新时间、正文和实际向量，支持来源筛选、关键词搜索与分页。

## [0.1.6.2] - 2026-10-23

### 新增
- `/phone status`、`/phone help` 默认以图片输出，命令输出卡片提供两个独立开关；渲染失败提示并发送文字。

## [0.1.6.1] - 2026-10-23

### 修复
- `/phone status` 与 `phone_status` 从图片保存配置读取存储位置，连接设备后可完整输出状态。

## [0.1.6] - 2026-10-22

### Added

- 记忆存储新增「保存到插件本地」开关 `memory.local_enabled`（默认开启）：关闭后学习档案、浏览日志与聊天记忆不再写入插件本地，依赖本地记忆的聊天语义找回同时停用并按条件隐藏配置。
- App 使用经验新增独立向量模型配置 `prompt.app_memory_embedding_provider_id`（开启 App 经验后显示，留空使用默认 Embedding Provider）与独立索引目录 `semantic_app/`；打开 App、读取屏幕或查看实时画面后，自动按当前 App、当前页面召回相关经验，`phone_app_memory` 改为语义优先、档案兜底。
- App 经验失效更新：操作报错时累计失败，连续 2 次标记「可能已失效」并提示重新探索，重新 `phone_app_remember` 覆盖成功后清除失效标记。
- 新增 `auto.share_cooldown_minutes`（同一目标会话主动分享冷却，默认 30 分钟，0=不限制），避免自刷任务反复向同一群/人分享刷屏；用户直接任务不受限制。
- 新增独立「图片保存」配置卡片：保存图片开关 `image.auto_save_images`、主要存储位置 `image.image_storage_location`、两地都存 `image.save_images_both`。

### Changed

- 为满足 AstrBot 插件市场包体积限制，随包只保留 Linux x86_64 的 `adb` 与 `phone-wg`（`bin/linux-x86_64/`）；Linux ARM64、Windows 等其他平台不再随包分发，`adb` 改用系统 adb（`adb.path` 或 PATH），`phone-wg` 可按 `helper/phone-wg` 源码自行编译后放到插件根目录 `bin/phone-wg`。
- App 使用经验移除 AstrBot 知识库后端与 `prompt.app_memory_backend` / `app_memory_kb_name` 配置，经验只保存在插件本地 `app_memory.json`，不再需要选择存储位置。
- 聊天语义找回索引只包含学习档案与浏览日志，不再索引 App 使用经验；移除 `recall.include_app_experience` 配置项。
- 自主玩手机流程改为由 AI 自己的兴趣驱动：先按自身兴趣决定打开/继续，中途不感兴趣直接退出；分享与评论/点赞等互动放到内容看完或玩完后独立判断，可都做、无先后；最近聊天仅用于结束后判断分享/互动对象，不再作为开始刷手机的动机。
- 信息流浏览默认 App `browse.default_app` 默认留空：`/phone browse` 未指定 App 时从已安装第三方 App 中随机选择，`phone_browse` 工具 `app` 留空同样随机。
- 图片保存配置从「信息流浏览」卡片迁移到独立卡片；旧版 `browse.*` 同名配置在插件加载时自动迁移并清理旧键。
- 使用指南、随机玩/持续刷提示词、分享工具描述、`phone_recent_chat` 返回说明与内置 `phone-autonomy` 行为手册同步更新。

### Fixed

- 修复 `/phone browse` 未指定 App 时随机选择逻辑缺少导入导致的指令失败。
- 补全工具层与浏览快照错误类型的缺失导入、验证控制器类型注解，避免异常分支运行时 `NameError`。
- App 经验召回更可靠：打开/进入 App 时使用工具参数解析出的目标包名，截屏类工具无 UI 快照时查询前台包名，语义召回超时或异常时回退本地档案。

## [0.1.4.6] - 2026-10-22

### Changed

- 配置页整体整理：按连接、权限、浏览、自主任务、记忆、提示词、感知、控制后端、组网、媒体、抓包、验证、调试分组排序，卡内按「先开关后参数」排列。
- 记忆存储由单一 `memory.backend` 下拉改为 `memory.companion_enabled` / `memory.livingmemory_enabled` 独立开关；未选中 MemoryCompanion 时不再显示绑定用户与「个人记忆-行动」配置项。旧版 `memory.backend` 在插件加载时自动迁移为对应开关并隐藏。
- WireGuard 未开启时只显示开关，开启后再按 client/server 角色显示其余配置项。
- 新增「聊天语义找回」卡片并入配置分组；普通聊天只读本地记忆，不触发手机连接或后台任务。

## [0.1.4.5] - 2026-10-22

### Added

- 聊天语义找回：把学习档案、浏览日志与 App 使用经验建成插件本地向量索引，普通聊天按当前消息语义检索相关记忆并注入本轮临时内容块（不写 system_prompt、不写会话历史）。
- 新增 `recall` 配置卡片：总开关、Embedding Provider ID（留空自动选默认，找不到时自动跳过）、条数/阈值/字符上限、索引上限与分块长度、查询超时、刷新周期、数据来源开关。
- 索引维护：插件启动空闲、学习/日志写入后防抖与周期检查时在后台增量构建（新增/修改/删除按文档 hash 识别），失败指数退避且只告警一次；索引损坏自动重建，保留上一代索引。
- 只读注入路径不持有 PhoneService：普通聊天只读本地索引与一次查询向量，不连接 ADB、不预热 scrcpy、不创建后台任务；ADB 未配置（假开启）时同样可用。默认只在授权用户私聊生效，任务/定时/群聊与命令不注入。

## [0.1.4.4] - 2026-10-22

### Changed

- 学习记录区分归属：自行唤醒（持续刷/随机玩/定时自主浏览）产生的学习记录不再写入 MemoryCompanion/LivingMemory 用户记忆；用户指定使用手机（会话工具、`/phone browse`、用户发起的后台任务/玩手机任务）产生的学习记录计入发起用户的用户信息。
- 学习记录本地档案与 Dashboard「学习记录」页显示归属（自行唤醒/用户指定）；后台任务记录创建者 `requested_by` 并优先作为记忆绑定用户。
- `memory.bot_action_enabled` 仅作用于自行唤醒学习：开启后按「个人记忆-行动」额外记入 MemoryCompanion 一份（不再写入用户记忆）。

## [0.1.4.3] - 2026-10-22

### Added

- 浏览日志自动清理：`browse.diary_max_entries` 控制上限（默认 500），达到上限自动清理最旧日志；`browse.diary_protect_rounds` 保护最近 N 轮任务（默认 3，0=不保护），受保护轮次的日志即使让总量超过上限也不会被清理。每个 Agent 任务 / 每条 `/phone browse` 命令算一轮。
- Dashboard 新增「浏览日志」页：统计、关键词搜索与分页查看，支持 ADB 未启用（假开启）时只读访问。

### Changed

- 「浏览日记」统一更名为「浏览日志」（命令说明、工具描述、提示词注入说明、Dashboard、README 与使用手册）。

## [0.1.4.2] - 2026-10-22

### Added

- 随包二进制支持更多宿主平台：Linux ARM64（`bin/linux-aarch64/`，Debian android-platform-tools 34.0.5
  自包含重建 + 依赖库）与 Windows x86_64（`bin/windows-x86_64/`，官方 platform-tools 37.0.1 + DLL、
  `phone-wg.exe`）；插件按宿主系统与架构自动选择对应 `bin/<平台>/adb|phone-wg`，并为 zip/市场安装
  自动补可执行位。
- 非 Linux 平台降级：不再依赖 `/proc`、`fcntl`、`killpg` 的进程监护；Windows 下核心 ADB 控制可用，
  Linux 专属的进程监护、scrcpy 视频解码、抓包与 WireGuard helper 降级或不可用并给出原因。

### Changed

- 其他平台/架构（如 macOS）不随包分发二进制，回退到 `adb.path` 或 PATH 中的系统 adb，缺失时提示平台信息。

## [0.1.4.1] - 2026-10-22

### Changed

- ADB 地址/端口默认留空：未配置时插件可正常安装与加载为「未启用」状态（假开启），面板、配置与资料迁移可用，不连接设备、不启动后台任务。
- 不再自动回填 `127.0.0.1:5555`；`/phone status`、命令与手机工具在未配置时返回可操作的未启用提示，填写地址并重载插件后启用。

## [0.1.4] - 2026-10-22

### Changed

- 元数据版本使用 `0.1.4`，插件显示名称为“手机操控”。

## [0.1.3.3] - 2026-10-22

### Added

- 独立持久化数据迁移支持预览和确认，合并日记、聊天记忆、学习档案、App 经验、兴趣状态、互动/用量/指标与内容任务历史，复制截图和图片原图并调整本地路径。
- 记录去重、冲突保留当前值、可选补充缺失设备密钥；导入前自动备份，文件变化检测、写入失败回滚及内存刷新保证迁移可重复执行。


## [0.1.3.2] - 2026-10-22

### Fixed

- 新安装默认使用 ADB `127.0.0.1:5555`，空白初始配置同步保存默认值，配置与迁移入口可直接使用；显式非法端口仍报告配置错误。
- 配置文件、数据目录、Web API、调度与后台任务按 metadata.name 隔离。

### Added

- 面板提供独立的原插件配置迁移：预览字段、确认导入、保留当前自定义值、目标备份及文件变化检测；源配置与原插件数据保持完整。
- 点赞与收藏支持独立兴趣决策、名额预约和结果记账，两者共用每次 Agent 任务的额度，失败释放名额。
- `phone_shell` 按配置授权在 Android 设备执行单次命令，返回退出码与有界标准输出/错误，支持超时、取消和设备门禁。

### Changed

- GitHub 仓库与插件更新地址统一为 `acat778/astrbot_phone_control`。
- 配置按连接、访问与互动授权、浏览与自主任务、记忆、感知与高级能力排列；权限卡内依次展示各类授权和后台保留策略，保留配置字段路径。

## [0.1.3.1] - 2026-10-22

### Added

- Dashboard「保存的图片」支持原图查看、搜索分页、语义/理由/标签/类型编辑与确认删除；学习记录支持按唯一编号删除，资料修改即时保存。

### Fixed

- 保存的标准 WireGuard + Tailscale 默认后台名单自动包含 Phone WG Relay，停止、任务收尾与抓包初始化使用统一名单；自定义或显式空名单保持配置值。

### Changed

- README 将子进程与停止行为归入运行说明，App 经验配置归入独立功能说明。

## [0.1.3] - 2026-10-21

### Added

- Phone WG Relay 手机组件通过普通 App UID 转发 WG 内 ADB，支持无 root Android 官方 WireGuard App；中转仅监听隧道地址并限制唯一控制端，通知栏可停止。
- `wireguard.adb_target_port` 选择手机入口；`socks_port` 提供环回 SOCKS5 TCP CONNECT，按 peer AllowedIPs 访问 IPv4/IPv6 目标。
- 图片库提供保存开关、两地都存和主要存储位置；默认存 AstrBot 服务端，两地都存开启时隐藏主要位置。`phone_save_image` 保存原图及描述、标签、来源、SHA256 和路径；`phone_saved_images` 支持后续查询和传到手机，手机哈希与相册索引均核验。

### Fixed

- 内容采集归属整次 Agent 任务，工具返回后继续采帧与录音；音频导出在线程执行，分析中的后续素材按游标顺序消费，停止时取消在途分析。
- LivingMemory 内容类型使用 `GENERAL`，来源类型独立保存在 `phone_memory_type`，会话作用域保持一致。
- 设备状态识别模拟器与软键盘；UI 区分可编辑、聚焦和可用的 hint 信息；文字输入后核对关键词再回车，操作手册明确各 App 的搜索按钮/回车提交方式。
- 手机工具轮的流式过程正文先缓冲再判断交付，只发送最终回复；当前会话分享成功后静默收尾。分享正文发送前总结到 100 字以内。
- 阿里云 OpenAI 兼容接口的原音频采用 data URI 编码，保持所选模型、同源接口与凭据。
- 内置手册读取上限为 16 KiB，完整提供操作规则。

## [0.1.2] - 2026-10-21

### Added

- scrcpy 常驻开关默认开启，连接预热、首帧等待和恢复统一管理；状态区分配置后端与最近实际来源，停止优先于常驻。
- 单篇内容任务 `phone_content`：图文正文和原图按内容 ID 关联；视频 8 秒试看、20 秒顺序续看，关键帧频率可配，原音频优先与 STT 降级明确区分。
- 内容理解、评论阅读、回复兴趣、点赞和收藏独立决定；任务内素材受保护，完成按截图清理策略回收，异常立即清理。
- App 操作经验根据成功操作和后续页面证据自动总结，按所选位置写入；知识库待写队列持久化、有界重试与错误状态可查。

### Fixed

- 首帧未解码时等待就绪，静态画面通过 RESET_VIDEO 请求新帧；非阻塞视频流等待可读数据，MJPEG 按可用数据写出，FFmpeg 使用低延迟单线程解码，减少静态画面导致的 ADB 回退。
- 设备心跳短暂超时在原 15 秒租约内有界重试，只刷新尚未过期的租约，避免单次超时中断长任务的控制和音频。
- 小米 Token Plan 原音频使用同源 OpenAI 兼容协议，避免 Anthropic 适配器将声音替换为文字占位。

### Fixed

- 后台清理统一按 `security.background_keep_packages` 逐包执行；停止、浏览/任务收尾、清理工具和抓包初始化均保留常驻名单，默认包含 WireGuard 与 Tailscale。
- 帖子分享在同一页面回到顶部、确认滚动边界稳定后重新截图；页面切换或无法确认顶部时返回错误，视频及其他当前画面可分别指定。
- `/phone stop` 强停最近任务和第三方应用，保留桌面与当前输入法，报告清理失败；停止抓包、实时屏幕、控制后端及自主浏览调度、随机/持续任务，并保存关闭状态。
- scrcpy 启动、停止与视频升级使用同一把锁；临时启动失败按 30 秒退避恢复，minitouch 随控制后端关闭，活动音频连接独立保留。
- 常驻子进程使用心跳租约保活；本机独立监护器回收整个子进程组，Android 看门狗在设备端独立结束控制/音频服务并恢复抓包代理。插件失联后最长约 18 秒回收；重载按代码指纹识别前任租约，完成退出后重建。

### Added

- `phone_clipboard` 按需通过 scrcpy 控制通道读取安卓手机/模拟器当前剪贴板，支持复制分享链接；分包读取、协议长度校验、超时与取消均处理流状态，控制/截图选择 ADB 时同样可用。
- App 使用经验支持 `plugin` 与 `knowledge_base` 两种独占存储；记录、查询、打开 App 和提示词注入统一使用所选位置。知识库支持按 App/页面覆盖更新，写入失败保留有效记录并返回错误。
- App 经验配置按开启状态与存储位置动态展示插件注入条数或 AstrBot 知识库名称。

## [0.1.1] - 2026-10-21

### Added

- WireGuard 组网（client/server）：插件内置 userspace Go helper
  （`bin/linux-x86_64/phone-wg`，容器无需 `NET_ADMIN`/`/dev/net/tun`），新增 `wireguard`
  配置卡（enabled/mode/listen_port/interface_address/server_endpoint/public_endpoint/
  server_public_key/preshared_key/phone_wg_ip/adb_local_port/auto_adb）。
- X25519 密钥生成与 WireGuard 私钥 clamp；client/server/phone 三套密钥持久化到
  `plugin_data/astrbot_phone_control/wireguard/keys.json`（0600，目录 0700）。
  client 模式 `/phone wg config` 输出服务器侧需添加的 Peer 片段；server 模式自动生成
  手机密钥并输出手机端完整 WireGuard 配置（可复制导入手机 WG App）。
- `/phone wg [status|start|stop|restart|config]` 命令与 LLM 工具 `phone_wireguard`
  （工具数 30 → 31）；`/phone status` 追加 WireGuard 摘要。
- ADB over WG：helper 转发 `127.0.0.1:<adb_local_port>` → `<phone_wg_ip>:5555`；
  `auto_adb=true` 时 helper 健康后自动把 ADB 目标切到隧道，停止/异常/配置变化时恢复。
- helper 生命周期跟随插件（初始化启动、配置热应用重启/停止、terminate 停止并恢复
  ADB 目标）；helper stderr 转发 AstrBot 日志；确定性失败（helper 缺失、配置/密钥非法、
  helper 退出码 2/3）只告警一次并停止自动重试，临时失败按 30 秒起指数退避、连续 4 次
  失败收敛为不可用。
- 新增 `tests/test_wireguard.py` 与 `tests/test_wireguard_lifecycle.py`：密钥/配置/状态
  解析/热应用分类单测，以及 fake helper 覆盖的启动/停止/失败分流/健康判定/ADB 切换。
- README 与使用手册新增「WireGuard 组网」章节（client/server 用法、手机端配置导入、
  ADB over WG、安全注意与故障排查）。

### Changed

- `wireguard` 配置卡按角色区分显示：`server` 模式仅显示监听端口/手机可达地址等 server 项，
  `client` 模式仅显示要连接的服务器地址/公钥等 client 项（schema `condition` 按 `mode` 显隐），
  通用项两种模式都显示；使用手册配置表增加「适用模式」列。

### 已知限制

- ADB over WG 受 Android `VpnService` 按 UID 分流限制：官方 WireGuard App 场景下 adbd
  （shell UID 2000）的响应不走 VPN 隧道，隧道侧只能发出 SYN、收不到回包（UAT MuMu
  Android 12 实测）。因此 0.1.0 的手机侧验证范围为「安装 App / 导入配置 / 建立隧道 /
  与服务器握手」；ADB over WG 端到端适用于 Linux/容器等标准 WG 对端，或手机以 root 级
  全局 WireGuard（非 VpnService）接入的场景。普通 App 自身发起的流量仍可经隧道互通。

## [0.0.6] - 2026-10-20

### Fixed

- 验证会话隔离：显式取消会先终止在途求解再换代次；求解结果绑定会话 ID、尝试 ID 与
  代次并严格匹配（0 同样参与校验），旧会话结果不会写入新会话。
- 验证观察质量：UI dump 成功不等于页面可观察；空树、纯系统节点、无可读文本、加载页
  判定为观察不足并进入人工协助，不再因「没有挑战关键词」自动通过；验证通过必须有
  肯定证据或页面确实变化。
- 验证状态读取故障：区分首次无文件、合法空状态、读取/解析/结构错误；已有状态无法
  可靠读取时保留原文件并阻断设备操作，需显式 `/phone verify cancel <任意编号>` 处置
  （坏文件改名保留为 `.corrupt-*`，不删除），`/phone stop` 不会静默恢复。
- 任务收尾清理改为持有设备租约的设备事务：HOME、等待与 `am kill-all` 全程受租约保护，
  新任务需要设备时可接管租约并让旧收尾在下一次检查处停止，避免旧收尾清掉新任务的后台。
- `phone_check_inbox` 按真实副作用归为写操作：验证观察/尝试期间查收件箱不再切换 App
  或点击，避免打断验证页面。
- 验证动作批次整体预校验：坐标越界、未知动作、非法 confirm、超量等非法批次在设备动作
  前整批拒绝，不再出现「第一笔已点击、第二步才报错」的部分执行。
- 最终确认复查单次尝试期限：期限在批次执行过程中到达时不再执行确认点击，转入停止/人工处理。
- 抓包生命周期与并发安全：修复插件重载后旧实例后台循环未停止导致多实例并发抢同一设备
  的问题（AstrBot 只识别插件类自身的 terminate）；抓包启动/停止/设备配置串行化并幂等，
  确定性不可用（无 root、系统证书挂载被拒、缺少 mitmdump 等）只告警一次并停止自动重试，
  临时失败按退避重试，失败原因不再出现 `None`。

## [0.0.5] - 2026-10-20

### Fixed

- 市场/zip 默认安装可能丢失插件自带 `bin/linux-x86_64/adb` 的可执行位，导致
  `Permission denied`、scrcpy 回退 ADB；插件加载时自动为自带二进制补齐 u/g/o 可执行位。
- 设备无 root 时不再反复尝试抓包：`adb root` 失败后标记本次运行跳过抓包，保活循环
  不再重试/刷告警；`/phone traffic` 状态可见 `root_unavailable`，其他功能不受影响。

## [0.0.4] - 2026-10-20

### Fixed

- 修复插件安装时依赖冲突：`requirements.txt` 不再声明 `mitmproxy`。AstrBot Core 固定
  `cryptography==50.0.1`，而 mitmproxy 12.x 要求 `cryptography<=48.1`，自动安装会被核心
  依赖保护拦截并导致插件安装失败。
- 抓包依赖改为可选项：新增 `requirements-traffic.txt` 与 `traffic.mitmdump_path` 配置；
  需要抓包时用独立 venv 安装 mitmproxy，并把该配置指向 `venv/bin/mitmdump`，不影响
  AstrBot 核心依赖。

## [0.0.3] - 2026-10-20

### Fixed

- 修复真实 AstrBot 加载时 `core/plugin/compat.py` 解析插件入口模块失败导致插件无法加载：
  AstrBot 的插件模块名是 `data.plugins.<目录名>`；改为从当前模块名逐级向上查找已加载的
  `<prefix>.main`，兼容真实包与测试临时包。
- 新增 `tests/test_compat_main_module.py` 覆盖 `data.plugins.<dir>.main` 深路径解析。

## [0.0.2] - 2026-10-20

### Fixed

- 日志统一使用 `astrbot.api.logger`：`core/config.py`、`core/diary.py`、`core/browse.py`、
  `core/usage_audit.py` 不再自建标准库 logger。
- 手机能力指南与浏览日记、学习档案、App 经验、新鲜感、验证状态等动态内容不再写入
  `req.system_prompt`，改为通过 `req.extra_user_content_parts` 的临时内容块
  （`TextPart(...).mark_as_temp()`）追加，避免改写系统提示词破坏模型前缀缓存。

### Changed

- 相应更新配置提示、使用手册与 token 估算口径（识别请求时同时扫描临时内容块）。

## [0.0.1] - 2026-10-20

### Added

- 首个开源基线：AstrBot 手机操控插件（远程 ADB/scrcpy，自主浏览、记忆、人机验证、
  互动、抓包与屏幕识别协同、流媒体识别、token 对账、只读监控面板）。
