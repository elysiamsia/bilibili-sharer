# 被动链路详解

> 群友发链接后插件的完整处理流程（避让 parser、防抖、自动点评）。

## 被动链路详解（`components/reactive_handler.py`）

事件处理器 `bili_sharer_reactive`（`event_handler`，weight=40）订阅 `ON_MESSAGE_RECEIVED`。`execute` 把整条链路丢进后台 task 后**立即返回 SUCCESS**——下载 / 合流 / LLM 动辄几十秒，不占用事件流超时；处理器**永不返回 STOP**，不会把消息挡在聊天流外（那等于打断 bot 聊天）。

### 处理顺序（固定，逐层短路）

1. `[plugin].enabled` 为 false → 跳过。
2. `video_send.reactive_enabled` 与 `opinion.reactive_auto` **全关 → 零动作**（默认基线：处理器注册但不做任何事）。
3. **会话过滤**：`[reactive_filter]` 白名单优先——`whitelist` 非空时**仅白名单内**会话生效（此时 `blacklist` 不参与判断）；白名单为空时才查黑名单。两者都为空（出厂默认）→ **所有会话都响应**（只要开关开启且过了防自触发），不想全群生效须显式配置。
4. **防自触发（三保险）**，命中任一即跳过：
   - 合成消息：`message_id` 前缀 `bili_sharer_`（推送链路自发的合成事件，防自反馈环）；
   - 自身消息：发送者 `sender_id == self_id`（self_id 取不到时**不**凭空认定是自己，判 False 放行）；
   - 首段 `@` 的是**别的 bot**：那是发给别人的话，不该接。
5. **链接识别**（纯本地判定，不发网络请求、不询问 parser）：URL、裸 BV / av 号、QQ json 卡片里的链接都能取出；按域名（bilibili.com / b23.tv / bili2233.cn / bilibili.cn）或 BV / av 号把资源标记为「B 站 / 非 B 站」，按出现顺序去重。非 B 站链接是否被认领由 parser 的关键词匹配在**发视频分支**判定（见下）；**发视频分支一条消息最多处理前 3 个链接**（防长文粘贴刷下载），自动点评分支不受此限（在全部链接中取第一个 B 站链接）。
6. 之后按开关走「发视频」与「自动点评」两个分支（下述）。

### 防抖（资源级，默认 300 秒）

- 防抖 key = `stream_id + 资源 ID`（B 站取 BV / av 号；带 `?p=` 的分 P 视为不同资源），窗口取 `[reactive_filter].debounce_seconds`（默认 300 秒，内存计时、定期清过期）。
- **其他平台链接实际过两层防抖**：第一层在调用 parser 解析**之前**，用消息里规范化后的链接 URL 作 key 预检（同链接重发不重复触发解析）；第二层在 `parse` 返回**之后**，再按 parser 给出的资源 ID（key 前缀 `parser:`）判一次——不同 URL 指向同一资源也能拦住重复发送。
- **命中防抖只记一行 INFO 日志**，静默跳过。
- **失败也会登记防抖**：下载 / 发送失败后，同一链接 `debounce_seconds`（默认 300s）内**静默跳过**（仅 INFO 日志）——不会因群友重发或消息重放反复重试失败资源。
- 自动点评分支**不经过**链接防抖（由会话级频率限制单独兜底，见下）。

### 被动发视频（`video_send.reactive_enabled`）

- **避让五合一**（**五条同时成立**才避让）：① `video_send.defer_to_parser = true`（开关本身，为 `false` 时不再往下判）；② parser 插件已加载且实例可用（`plugin_api.is_plugin_loaded("parser")` 取到实例，且实例具备 `search_key_pattern` / `is_session_allowed` 公开面）；③ `parser.search_key_pattern(消息文本)` 命中关键词；④ **parser 认领该关键词对应的平台**——命中的关键词在 parser 的 `parser_map` 里有对应条目、且该条目的 `parse` 可调用；⑤ `parser.is_session_allowed(stream_id)` 允许本会话。
  五条全成立 → 本插件**跳过发视频并记 INFO**（视频交给 parser 发）；任一不成立 → **不避让，本插件自己发**。第 ④ 条是**防御性检查**：现版 parser 的 `parser_map` 与关键词模式由同一 `enabled` 循环生成，**命中必认领**——平台被禁用时其关键词本就命中不了（走 ③「未命中」由本插件接管），该位在当下不会翻转任何判定结果；它是为「未来 parser 若允许模式命中但平台禁用」准备的自动接管位。取不到 `parser_map`、该关键词无条目、条目的 `parse` 不可调用、或检查过程抛异常，一律按「未认领」处理（不避让、本插件接管）。
- **已知限制（无人发送）**：避让成立后，parser 仍可能「按自己的规则跳过」——parser 自身的防抖（300s）/ 资源 ID 防抖 / 多 Bot 仲裁 / `@` 他人 / 大小超限 / parse 失败等——此时该链接**没有任何一方发送视频**。本插件只记日志，**不会延迟接管或事后补发**。上条第 ④ 条的认领检查是防御性位，**不消除本限制**。
- `defer_to_parser = false` = **可能与 parser 双下载、双发送**（parser 的事件处理器无法被温和阻止）；该取值只建议用于对照试验，日常不必开。
- 避让不成立时的两条发送分支：
  - **B 站链接**：走自研下载 `services/bili_downloader.py`（见「自研下载器」），产物发完即删；整体兜底超时 900 秒。
  - **其他平台链接**：运行时经 `plugin_api` 借 parser 的 `parse`（超时 120s）→ 取其 `video_contents[0].get_path()`（超时 240s）→ **复用 parser 的缓存文件**发送，**只发不删**（缓存归 parser 的清理器管，本插件不碰他人文件）。parser 缺席时非 B 站链接直接跳过。
- **一条消息含多个外站链接时，只处理 parser 命中的第一个**（每次 parse 都拿整条消息文本匹配，其余链接解析到同一资源 ID，被资源 ID 防抖吞掉）。
- 两条发视频分支都过**全局下载并发 Semaphore(2)**（与推送自发视频共用）。

### 被动自动点评（`opinion.reactive_auto`）

- **不避让 parser**（parser 不点评，无重复问题）。
- **不受「链接防抖」约束**，由 `[reactive_filter].opinion_rate_limit / opinion_window_minutes`（默认 **3 次/10 分钟**）的会话级频率限制兜底，防刷屏；素材或 LLM 失败会归还额度，不白占次数。
- B 站链接：走 `services/material.py` 素材（`[material]` 限幅 + 凭据 + TTL 缓存 + `[asr]` 段透传；兜底超时 **90 秒**，**开 ASR 时自适应放宽为 90 + `[asr].timeout_seconds` + 60 秒 = 默认 270 秒**）→ LLM 以组装后的人设（`services/persona.py`，默认复用 bot 全局人格）说 1-3 句有具体依据的看法（超时 30 秒，输出上限 600 字）→ `send_text` 发出（内容 1-3 句）。**一条消息最多发 1 条点评**，且点评分支在全部识别链接中取第一个 B 站链接（不受发视频分支的 3 条上限影响）。
- 其他平台链接：本机没有「不触发下载」的纯元信息通路，**默认跳过点评**；仅当同一条消息已因发视频分支 parse 过时，复用其 header / title / text / extra.info 摘要做简评。
- 模型不可用 / 素材失败 / 发送失败 → 静默跳过（记日志），不抛错。

### 与 parser 的关系（未装 / 未启用 parser 时的功能边界）

- **B 站功能全量可用**：聊天工具、推送链接、deep 点评、被动 B 站发视频与自动点评都走自研链路，不依赖 parser。
- **其他平台链接（被动发视频）跳过**：非 B 站借道完全依赖 parser，缺席时只记 debug 日志。
- **推送链路的合成事件无人消费**：`push_enabled = false`（关掉时）链接与推荐语照常发出，但不会有人自动下载发视频——默认 `push_enabled = true` 由本插件自研自发（同时不再发合成事件）；只有想交回 parser 发才关它。
- ffmpeg 第三级回退（parser 捆绑路径）依赖 parser 在场；无 parser 时需系统 PATH 或 `ffmpeg_path` 配置（见下）。
- 会话过滤语义差异：本插件 `[reactive_filter]` 是**白名单优先**，parser 的 `is_session_allowed` 是**黑名单优先**——两套语义互不影响（各判各的），但同一 stream 同时在本插件白名单与 parser 黑名单时，本插件会响应而 parser 沉默。

---
