# Phone WG Relay

Android 8+ 手机组件，以普通 App UID 在 WireGuard、Tailscale 或 ZeroTier 隧道中接收 ADB 连接并中转到手机 `127.0.0.1:5555`。支持官方客户端的应用级 VPN；手机 ADB 需先通过 USB 授权或设备网络调试开启。

## 安装与连接

1. 安装插件随包的 `bin/android/phone-wg-relay.apk`；同签名版本可覆盖更新。
2. 手机开启网络 ADB（可由已授权 USB ADB 执行 `adb tcpip 5555`），确认 adbd 监听 5555。
3. 官方 WireGuard/Tailscale/ZeroTier 客户端连接同一网络；应用分流中允许 `fun.acat.phonerelay`。
4. 打开 Phone WG Relay，填写手机组网 IPv4 地址和唯一允许的插件组网 IPv4 地址，点击启动。组件仅在 tun/wg 接口地址的 15555 端口监听，转发目标固定为手机 loopback adbd。
5. WireGuard 设置 `wireguard.adb_target_port=15555`；Tailscale/ZeroTier 设置 `mesh.adb_port=15555`，分别开启对应 `auto_adb`。后台保留名单包含当前 VPN 客户端与 `fun.acat.phonerelay`。

通知栏和界面的停止按钮立即关闭监听与活动连接。VPN 接口消失后服务停止；服务使用 `START_NOT_STICKY`，由用户重新启动。并发上限为 16 对连接，连接 ADB 的超时为 5 秒。Android 的 ADB 授权机制保持生效。

Linux/root 全局 WireGuard 对端可在插件中选择其实际 adbd 端口。插件侧转发端口为独立的 `wireguard.adb_local_port`，手机组件入口固定为 15555。

## 构建

源文件使用项目 MIT 许可。需要 JDK 17+、Android SDK platform 35 和 build-tools 35.0.0，签名密钥由维护者保存在构建环境的私有配置中。

```bash
export JAVA_HOME=<JDK目录>
export ANDROID_HOME=<SDK目录>
export OUT_DIR=<构建缓存中的绝对输出目录>
export SIGNING_KEYSTORE=<私有签名密钥路径>
# SIGNING_PASSWORD 通过构建环境秘密变量注入
bash build.sh
```

输出 `OUT_DIR/phone-wg-relay.apk`，经 zipalign 和 APK v2/v3 签名验证后复制到插件 `bin/android/`。发布资产包含同一 APK，可单独下载后侧载。自动化安装可先启动可见 Activity，再传 `address`、`peer` 和 `start` Intent 参数；服务本身只在应用内使用。
