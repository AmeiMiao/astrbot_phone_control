---
name: phone-media
description: 单篇图文与音视频任务，先理解正文或试看，再独立判断续看、看评论和回复；任务统一管理素材。
---

# 图文与视频阅读

以下精读流程适用于默认模式；自由浏览开启时按文末自由浏览规则操作。

- 图文：打开详情前 `phone_content(action="begin", kind="post", content_id="帖子ID")`，再打开匹配详情并 `read`。抓包按 ID 关联正文和原图；抓不到时只理解可见页面，不宣称读完全部。
- 点播视频：先打开并确认播放起点，再 `begin(kind="video", content_id="视频ID")`；首次 `read` 默认 8 秒。仅根据素材理解内容，不能用标题、弹幕和评论代替正文。
- 直播：按兴趣选择直播入口，`phone_watch_media(stream_kind="live")` 从当前时刻分段理解，不需要0秒起点、完整文件或总时长。直播按游标连续观看，processing 时轮询同段，离开时 action=finish。
- 感兴趣再 `decide(decision="continue", reason="原因")`，随后 `read` 顺序续看默认 20 秒。模型判断期间仍缓存素材，已读时间范围由工具返回，不重复读同一段。
- 无兴趣用 `decide(decision="skip", reason="原因")`；读完 `finish`，切换内容前结束当前任务。用户明确要求看完时遵循指令并受任务预算约束。
- 理解正文后，只有对讨论感兴趣才 `decide(read_comments)`，通过 `phone_screen` 或 `phone_read_screen` 读取评论；想回复具体已读评论时再 `decide(reply_comment, target="评论原文片段")`，另用 `phone_interaction_gate` 申请名额。看评论不等于回复。
- 点赞 like、收藏 favorite 和分享均独立决定，可不做；兴趣决定不执行动作，后续操作遵守权限、额度和冷却。
- 读取音频输入说明：原音频、转写、仅画面需如实区分。转写缺少完整音乐和语气信息，不能编造没听到的声音。
- `phone_watch_media` 接入同一视频任务；`/phone watch` 同样管理单次内容任务，返回后结束。
- 素材在正常运行中保护；完成后按截图清理开关和时限清理，异常立即回收。用 `phone_learn` 保存真正理解的摘要，App 操作经验由任务结束自动总结，也可手动 `phone_app_remember`。


当运行时提示 `自由浏览已开启` 时，可以直接打开贴吧、小黑盒、抖音或快手，
用 `phone_screen` 看屏幕、点击或滑动，安静观看直播。需要声音和动态内容时
直接调用 `phone_watch_media`；重复调用从已理解游标继续分段观看。
`phone_content` 的 begin/read/decide 精读流程和 `phone_learn` 学习记录均按需使用，
无需为休闲编造兴趣、学习成果或分享理由。只有实际看到或听到的内容才可描述。
各项弹性能力以运行时提示为准；每日次数按当前配置，互动授权、总额度、分享冷却与设备停止规则继续生效。

## 独立录制与文件传输

- 用户需要保存手机媒体时，App模式可用`phone_record_media(action="start",mode="both",seconds=60)`开始手机内封装；status查看，stop下载成品，cancel清理。最多900秒/128 MiB，录制成品本身不代表已理解；理解内容使用观看工具。
- `phone_files`的download下载手机共享文件，upload上传当前消息附件或已有file_id，list查看缓存，delete清理指定缓存。手机路径包含完整文件名，单文件128 MiB。
- 文件和录制默认保存在插件传输目录；用户需要接收时显式send_to_chat=true发到当前聊天。独立录音与媒体理解的连续音频分别使用资源，先结束当前音频采集再启动另一种。
