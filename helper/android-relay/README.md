# Phone ADB Relay

Android 8+ 手机组件，包名 `fun.acat.phonerelay`。提供 TCP 中转和本机 ADB 控制两种服务，各自启动与停止；同签名覆盖安装保留参数和 ADB 身份。

## 本机 ADB 控制

1. 在插件「ADB 连接」选择「使用 App 控制」，设置 App 接入端口，保存并重载。容器发布相同 TCP 端口，默认 `6190`。
2. 在 插件页面左侧的「Relay App」填写手机可访问的插件主机名或 IPv4，获取配对码。配对码包含连接凭据，只填入自己的手机 App。
3. 手机开启本机 TCP ADB，填写本机地址及连接端口；Android 11+ 也可开启无线调试，使用配对端口和六位配对码完成配对，再填写无线调试的连接端口。配对端口与连接端口分别设置。
4. 填入插件配对码并启动 App 控制。App 通过固定证书 TLS 主动连接插件，通信可经直连网络或现有 VPN。

同机无线配对时，将系统无线调试页面和 Relay App 以分屏同时显示，保持配对窗口打开并及时填写端口和验证码。完成后使用无线调试首页显示的连接端口。

App 内置 ADB 客户端连接本机 adbd，命令以设备实际提供的 shell 权限执行。支持设备状态、shell、应用操作、点击/长按/滑动、按键、中文输入、剪贴板、截图和 UI 节点。中文输入与剪贴板共用手机端 scrcpy 控制通道。节点使用单次文件采集后回传，完成时清理临时文件。

音视频、通用文件传输和 HTTP(S) 抓包由插件直连模式提供。切换前停止手机任务，在插件配置中选择连接方式并重载；隐藏的直连参数保留。App 断线、取消或请求超时后不重放操作。手机重启、关闭调试或切换无线调试端口后，需重新确认本机 ADB 连接。

同一插件配对凭据绑定一台 App 设备身份；更换手机或卸载重装后，在「Relay App」页面重置配对并重新填写配对码。重置需设备任务空闲。

## TCP 中转

1. 手机开启网络 ADB（可由已授权 USB ADB 执行 `adb tcpip 5555`）。
2. 官方 WireGuard、Tailscale 或 ZeroTier 客户端连接同一网络，并在应用分流中允许本 App。
3. 填写手机组网 IPv4 与唯一允许的插件组网 IPv4，启动中转。组件在 tun/wg 接口地址的 `15555` 端口监听，目标为本机 `127.0.0.1:5555`。
4. 插件选择「直连」，配置相应组网 ADB 目标端口为 `15555`。

中转保留 ADB 自身授权，最多16对连接。接口消失后服务停止；通知和界面停止按钮回收连接。启动采用 `START_NOT_STICKY`，由用户重新启动。

## App 更新

插件随包提供 `bin/android/phone-adb-relay.apk` 及版本清单。App 中点击「检查 App 更新」，确认有新版本后下载；包名、签名、递增版本码、大小和 SHA256 校验通过，再点击「确认系统安装」。Android 可能要求先允许本 App 安装应用；用户取消安装时保留当前版本。

APK 版本独立于插件 metadata 版本。首次安装及未提供更新按钮的版本，可在 Dashboard 下载 APK 后覆盖安装。更新会中断 App 进程；安装后重新打开 App 并启动控制，已有配置和 ADB 密钥保留。无需 GitHub Release 或单独更新服务。

## 构建

需要 JDK 17+、Python 3、Android SDK platform35 与 build-tools35.0.0。依赖固定版本、下载地址及 SHA256 见 `dependencies.json`。构建者可替换依赖并重新构建；第三方许可见项目根 `THIRD_PARTY_NOTICES.md` 和 `licenses/`。

```bash
export JAVA_HOME=<JDK目录>
export ANDROID_HOME=<SDK目录>
export OUT_DIR=<构建缓存中的绝对输出目录>
export SIGNING_KEYSTORE=<私有签名密钥路径>
# SIGNING_PASSWORD 由构建环境注入
bash build.sh
bash test.sh
```

输出 `phone-adb-relay.apk` 与 `phone-adb-relay.json`，均放入插件 `bin/android/`。签名密钥由维护者保管；构建目录使用工作区缓存。APK 经 zipalign 与 v2/v3 签名校验，内置 scrcpy5.0.1 控制组件及第三方许可。

宿主测试使用实际 LibADB 依赖，核对远端关闭后的缓冲尾部及无符号字节读取。
