---
name: phone-interactions
description: 主动评论/被动回复/发帖的行为手册（只讲打法与边界，不写死执行序列；操作由你自己用低层工具完成）
---

# 手机互动手册（主动评论 / 被动回复 / 发帖）

这是行为参考手册，不是固定脚本。插件不提供「一键评论/发帖」工具，只提供
`phone_interaction_gate`（申请许可/名额）与 `phone_interaction_record`（记录结果）；
具体怎么点、输入框在哪、发送按钮长什么样，需要你自己用 `phone_screen`、`phone_tap`、
`phone_input_text`、`phone_key` 等低层工具观察与操作。

互动分三类，语义不要混：

| 类型 | kind | 什么时候用 | 名额口径 |
| --- | --- | --- | --- |
| 主动评论 | `comment` | 你刷帖/看视频时自己真的有感触，主动发评论 | `security.allow_comments`、`max_comments_per_day`、`interaction_cooldown_seconds`（与发帖共用冷却） |
| 被动回复 | `passive_reply` | 别人回复/评论了你，你读到后按心情与内容决定要不要回 | `security.allow_passive_replies`、`max_passive_replies_per_day`、`passive_reply_cooldown_seconds`（独立冷却） |
| 发帖 | `post` | 你想在论坛类 App 分享一条有价值的内容 | `security.allow_posts`、`max_posts_per_day`、`interaction_cooldown_seconds`（与主动评论共用冷却） |

## 通用原则

- 只有主人在插件配置里打开对应开关后才允许；没开启时 gate 会直接拒绝，不要绕过。
- **保证程度**：三个开关是流程约定与插件侧授权/配额/记账，不是底层动作拦截。实际发布由你
  用 `phone_tap`/`phone_input_text` 等低层工具完成，插件无法从技术上区分「发评论」与普通输入/
  点击；因此必须自觉先申请名额再发布，跳过 `phone_interaction_gate` 属于违规操作。
- **有真实感触才发**：内容确实触动你、或你有话想说时再发。不要为了完成任务硬发，
  更不要复制粘贴同一段内容刷屏；每轮最多 1 条。
- 心情、喜好参考：你自己的人格设定、上次浏览回忆（`phone_recall_diary`）、最近聊天
  （`phone_recent_chat`）、学习档案（`phone_learn` 记录过的东西）、长期记忆工具（若有）；
  不要暴露内部数值。
- 三类都是同一个流程：`gate` 申请名额 → 自己用低层工具完成 → `record` 记录结果。
  申请到名额但最终没发（放弃/失败）也要 `record(success=false)` 释放名额。
- 主动评论与发帖共用全局冷却（`interaction_cooldown_seconds`）；被动回复有**独立**的
  每日限额（`max_passive_replies_per_day`）与独立冷却（`passive_reply_cooldown_seconds`），
  不要拿主动评论名额冒充被动回复。
- 遇到验证码/滑块/顺序点击：用 `phone_bypass_verify`（详见 `phone-verification` 手册），
  不要反复 dump 或连续点击。

## 主动评论

1. `phone_interaction_gate(kind="comment", app="...")` 申请名额；返回「已预留 1 次名额」
   才继续，被拒绝就按原因停下。
2. `phone_screen` 认清当前 App 与页面（视频页/帖子详情/信息流），确认不是登录墙或弹窗。
3. 找评论入口：找文本或 content-desc 含「评论」「回复」「说点什么」的控件，或评论数图标
   （常见位置：视频页评论区在页面下方/右侧评论按钮；帖子详情底部有评论框）。入口不明时
   可以先小幅滑动、看 UI 树，不要盲点。
4. 点开评论输入区，再点一下输入框让键盘弹出；用 `phone_input_text` 输入你写好的短评论
   （一般 1–100 字，别刷长文/链接/营销内容）。
5. 输入后**先确认输入框里确实是你要发的内容**（`phone_screen` 或截图核对）；发送按钮
   通常是「发送」「发布」「评论」文本或纸飞机图标，点开前再核对一次。
6. **只点一次发送**。点完后用 `phone_screen`/等待文本校验是否成功（页面出现你的评论、
   评论数变化、出现「评论成功」提示等）；不确定就多观察一会儿，绝不重复点。
7. 成功后调用 `phone_interaction_record(kind="comment", success=true, app="...",
   summary="不超过 120 字的简述")`；失败/遇到登录墙/验证码/找不到入口 → 立即停止，
   调 `phone_interaction_record(kind="comment", success=false, summary="明确原因")`，
   并把原因告诉用户。

## 被动回复

被动回复的前提是**别人回复了你**，不是你主动去找评论发。流程：

1. **先看收件箱**：进入 App 后先 `phone_check_inbox(app="")`（留空=当前 App；也可先
   `phone_open_app` 再查）。它会尝试打开「回复我的 / 互动消息 / 私信 / 通知」页，回传
   消息列表的 UI 树与截图；看不到入口时按返回结果自己用 `phone_screen` 找消息 tab。
2. **读实际回复内容**：打开对应消息/评论，读清对方说了什么、上下文是什么，必要时用
   `phone_screen` 看原帖/视频画面。不要只看通知标题就回。
3. **按心情与内容判断**：结合你当前心情、人格和上下文，判断值不值得回。
   - 不想回、看不懂、只是系统通知/营销/无意义消息 → **直接跳过**，不申请名额、不占每日限额；
   - 内容有风险（引战、隐私、违法、广告、诈骗）→ 跳过并向用户简要说明；
   - 有真实表达欲、且回复合适 → 进入下一步。
4. **决定回复才申请名额**：`phone_interaction_gate(kind="passive_reply", app="...")`，
   被拒绝就按原因停下（可能是开关未开、今日限额用完或独立冷却未到）。
5. **自己回复**：用低层工具点开回复框、`phone_input_text` 输入短回复，核对内容后
   **只点一次发送**，再用 `phone_screen`/等待文本校验是否成功。
6. **记录结果**：`phone_interaction_record(kind="passive_reply", success=true/false,
   summary="回复了谁、说了什么主题，或失败原因")`。放弃回复也调用 `success=false`
   释放名额（如果已经 gate 过）。
7. 被动回复受独立每日限额与冷却约束；回完一条就收手，不刷屏、不追着对方连续回复。

## 发帖

1. `phone_interaction_gate(kind="post", app="xiaoheihe")` 申请名额。
2. 找发布入口（底部/右上角「+」「发布」「发帖」按钮），点进发帖页。
3. 填标题（≤50 字）与正文（≤500 字，短段落更好读）；如果页面要求选板块/话题，按内容
   选择最接近的再继续。
4. 发布前核对标题/正文确实在输入框中；**只点一次「发布」**。
5. 发布后用 `phone_screen` 校验：出现新帖子、页面跳转到帖子详情、或出现「发布成功」提示。
6. 成功/失败都要 `phone_interaction_record(kind="post", success=..., summary=...)`。

## 常见 App 入口提示（仅参考，按当前 UI 自己判断）

- B 站：视频页评论区一般在页面下方，可上滑展开；输入框常带「说点什么」提示；消息入口
  通常在「我的」页顶部或消息 tab。
- 抖音：视频右侧有评论图标（气泡/数字）；点开是评论面板，输入框在面板底部；消息在
  底部导航「消息」或顶部铃铛。
- 小黑盒：帖子详情底部有评论输入框；发帖入口通常在社区/圈子页的「+」或「发布」按钮；
  消息/回复在「消息」中心。
- 收件箱入口名称因 App 而异：回复我的、互动消息、@我的、私信、通知、未读都可能是入口，
  `phone_check_inbox` 会按这些关键词尝试识别，识别不到时自己看页面找。

## 安全与边界

- 失败就停：遇到登录墙、验证码、风控提示、网络异常、找不到入口，**不要反复重试点击**，
  更不要重复点发送（避免重复评论/发帖）；验证码场景用 `phone_bypass_verify` 按
  `phone-verification` 手册处理，失败就如实汇报。
- 不评论/发布违法、色情、暴力、政治敏感、广告导流、隐私信息（手机号/地址/身份证等）
  内容；不冒充他人，不搬运付费内容。
- summary 会写入插件记录供 `/phone interactions` 查看，写清楚做了什么/失败原因即可，
  不要复述敏感隐私全文。
