# Changelog

本插件版本记录。版本号遵循语义化版本；正式版使用 GitHub Releases，测试版以预发布标记。

## [Unreleased]

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
