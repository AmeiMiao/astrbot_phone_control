# Changelog

本插件版本记录。版本号遵循语义化版本；正式版使用 GitHub Releases，测试版以预发布标记。

## [Unreleased]

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
