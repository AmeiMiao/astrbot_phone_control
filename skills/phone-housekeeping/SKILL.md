---
name: phone-housekeeping
description: 手机整理手册：何时清理后台、两种清理模式、注意事项与清理后的复核方式。
---

# 手机整理手册

## 什么时候清理

- 长时间连续刷视频/信息流之后；
- 频繁切换 App、感觉卡顿或发热时；
- 准备打开一个占内存的 App（游戏、视频、地图等）之前；
- 一次浏览任务结束、要把手机交还之前。

## 两种清理模式（工具 `phone_clean_background`）

- **留空 `package`**：按 `security.background_keep_packages` 逐包清理后台，保留当前前台、桌面与输入法；
- **填写 `package`**：对指定 App 执行 `am force-stop`，会停止该 App；名单内应用与当前前台始终保留。

## 自动清理

- 每次 `phone_browse` 浏览结束后，插件会自动按保留名单清理一次后台
  （配置 `security.auto_clean_after_browse` 默认开启，不影响前台），不需要你额外操作。
- 玩手机/控制类会话结束时，插件会自动按 HOME 回桌面并按保留名单逐包清理
  （配置 `security.auto_clean_after_task` 默认开启），也不要在汇报里提这件事。
- 中途需要更彻底地按包名清理时，再手动调用 `phone_clean_background`。

默认保留 WireGuard（`com.wireguard.android`）和 Tailscale（`com.tailscale.ipn`）；其他常驻应用可在保留名单追加完整包名。

## 注意事项

- 不要 `force-stop` 正在使用或马上要用的前台 App；不确定就先看 `phone_status`。
- 清理只影响后台进程，不会删数据、不影响登录态。
- 工具会返回清理前后 `MemFree` / `MemAvailable` 对比和当前前台包名。

## 清理之后

1. 用 `phone_status` 复核电量、前台 App 与设备状态；
2. 若仍然卡顿，可以用 `phone_wait` 等一会儿，或如实告诉用户设备负载较高；
3. 不要连续反复清理；一次任务清理一次即可。
