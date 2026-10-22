# Changelog

本插件版本记录。版本号遵循语义化版本；正式版使用 GitHub Releases，测试版以预发布标记。

## [Unreleased]

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
