# bilibili_sharer

自动从 Bilibili 抓取热门视频，按 Bot 人设过滤后定时分享到**配置的目标**（群 / 私聊，`platform` 默认 `qq`）。

各目标（群 / 私聊）拥有独立的人设、兴趣、平台与定时器，互不干扰。

同时向聊天 LLM 提供三个原生工具：`bili_video_material`（读取字幕 / 弹幕 / 热门评论素材；视频**没有字幕**且启用 `[asr]` 时会下载音频单轨做**语音转写**）、`bili_search_videos`（按关键词搜索视频）与 `bili_send_video`（把视频**真正发出去**——LLM 想直接给你看时用，不再只会「贴个链接」）。原 BiliLearn MCP（bili-learn）的这两项能力已**内置化**，不再需要 MCP 子进程与 JSON-RPC 协议流。工具**默认开启**，想关就在 `[chat_tools]` 里置 `enabled = false`（开关在工具 `execute` 内判定，见「聊天工具」一节）。

插件还有第三条链路——**被动链路**：群友在会话里发链接时自发视频，可另开自动点评（发视频默认开，自动点评默认关）。

> **平台说明（v1.2.0）**：每个发送目标都有 `platform` 字段（适配器名，默认 `qq`），不再写死 QQ。目标 `platform` 必须与本机已安装适配器的签名一致；非 `qq` 平台的端到端链路尚未验证。

## 快速开始

1. 编辑 `config/plugins/bilibili_sharer/config.toml`：至少配置一个 `[[sender.targets]]`（`type` + `id`），按需调整 `[persona]` / `[filter]` / `[fetcher]`。

   > `[persona]` **一项不填也有完整人设**：七个字段（`nickname` / `alias_names` / `personality_core` / `personality_side` / `identity` / `background_story` / `reply_style`）默认留空＝直接复用 bot 全局人格（`config/core.toml` 的 `[personality]`），填了哪一项就只在这个插件里覆盖哪一项；原 `persona` 字段现在是**额外补充说明**（默认空；非空则追加到组装好的人设最后）。

   > ⚠️ **模型默认值**：`[filter].model_name` 与 `[builder].model_name` 出厂默认都是 `utils`——这是 bot 自带的「任务名」，一般装好就能用、不用改。想指定具体模型就填本机 `config/model.toml` 里有的模型名（插件先按任务名找，找不到再按模型名找）。取不到模型时过滤链路是 **fail-open**（跳过判断、一律放行，硬性规则与去重仍生效），`[builder]` 则回落模板文案。
2. 重启 Bot（插件与配置不热加载）——启动后 send_loop 等 30 秒，随后立即检查一次各目标定时器。
3. 可选：填 `[bili_credential]`（或 `bililearn_cookie_file`）以便拿到 AI 字幕；从 bili-learn MCP 迁移见「从 BiliLearn MCP 迁移」章节。
4. 开箱默认值：两个聊天工具 `[chat_tools].enabled = true` 可用；推送自发视频 `video_send.push_enabled = true`、被动自发视频 `video_send.reactive_enabled = true`（没装 parser 也能发）；想要自动点评再开 `opinion.reactive_auto = true`，想要 deep 点评改 `opinion.push_mode = "deep"`。`[reactive_filter]` 本身**没有使能开关**，只是被动链路的会话过滤与频控参数——默认 `whitelist` / `blacklist` 都为空时**所有会话都会响应**（仍受防自触发与开关约束），要限制范围须显式配置。

---

## 三条链路

插件有三条互不依赖的链路：**推送**（后台自动跑）、**被动**（群友发链接触发）与**聊天拉取**（聊天 LLM 按需调用）。

### 链路 1：推送（后台自动，无需聊天触发）

```
B站 API（综合热门 + 分区排行榜轮换 + 标签搜索）
    ↓  每 fetch_interval_minutes（默认 60 分钟）
[Fetcher] ──→ 硬性过滤（播放量 / 分区 / 屏蔽词）──→ [Raw Pool]（JSON 持久化，超容量淘汰最旧）
                                                            ↓
[Send Loop] ──→ 检查各目标定时器（每 check_interval_seconds，默认 60 秒）
                    ↓  某目标到期
                批量 LLM 人设判断：池内前 30 个未看视频，每批 ≤ batch_size（默认 10）→ 判完全部批次后取首个命中（调用次数 M 为上界）
                    ↓  命中
                [opinion] 三态生成评论（off 无点评 / simple 浅推荐语 / deep 素材级点评：[material] 素材 + 字幕缺失时 [asr] 转写）
                    ↓
                发送标题 + 链接（+ 点评行）到目标平台 stream
                    ↓
        video_send.push_enabled ？
          ├─ false（默认）→ 发布合成消息事件（ON_MESSAGE_RECEIVED，message_id 前缀 bili_sharer_）
          │                   └→ 触发 parser 解析该链接并下载 / 发送视频（现状行为）
          └─ true → 不发布合成事件（避免与 parser 双发）
                     └→ 自研下载（DASH 双流 + ffmpeg 合流）→ send_file 直发视频文件，发完即删
```

### 链路 2：被动链路（群友发链接触发，默认开）

```
群友在会话里发链接（ON_MESSAGE_RECEIVED，后台 task，不挡聊天事件流）
    ↓  [plugin].enabled；video_send.reactive_enabled 与 opinion.reactive_auto 全关 → 零动作返回
[reactive_filter] 会话过滤（白名单优先）→ 防自触发三保险 → 链接识别
    ├─ video_send.reactive_enabled 开（每消息最多处理前 3 个链接）：
    │     避让五合一成立 → 跳过发视频（parser 会发，记 INFO）
    │     不成立 → 逐链接（防抖 debounce_seconds，默认 300s；失败也登记）：
    │         B 站链接   → 自研下载 → send_file 发完即删
    │         其他平台   → 借 parser parse → 复用其缓存产物 → send_file（不删 parser 文件）
    └─ opinion.reactive_auto 开：
          B 站链接 → [material] 素材（字幕缺失且 [asr].enabled 时含语音转写）+ LLM 说 1-3 句看法（每消息最多发 1 条；会话级频控，默认 3 次/10 分钟）
          其他平台 → 默认不点评（仅当同条消息已因发视频 parse 过时，复用其摘要简评）
```

### 链路 3：聊天拉取（LLM 在聊天里按需调用）

```
聊天里出现 B 站链接 / 有人问「这个视频讲了什么」
    ↓  LLM 调用 bili_video_material(url, include_danmaku, include_comments, include_timestamps)
内置素材服务 services/material.py
    链接识别（BV / av / 完整 URL）→ b23.tv 短链还原 → view 元数据
        → 字幕 /（仅字幕缺失且 [asr] 启用时）语音转写（ASR）/ 弹幕 / 热门评论（各段独立降级）
        → max_output_chars 截断 → TTL 缓存（600s、50 条）
    ↓  Markdown 素材
LLM 结合人设与聊天上下文给出具体总结

需要找视频 / 找资料 / 找选题
    ↓  LLM 调用 bili_search_videos(query, limit)
search_by_type(VIDEO) → Markdown 列表（标题 / BV / UP 主 / 播放 / 时长 / 分区 / 简介 / 链接）
```

> `services/material.py` 的抓取与限幅（`[material]` 配置节）由三处共用：聊天工具、推送 deep 点评（`opinion.push_mode = "deep"`）与被动链路自动点评（`opinion.reactive_auto = true`），并共享同一份 TTL 缓存（多目标 / 多次命中同一视频不重复抓取）。
>
> 同一条素材服务也是 `[asr]` 的唯一入口：**三条链路（聊天工具 / 推送 deep 点评 / 被动自动点评）都会把 `[asr]` 段透传给素材服务**，因此「无字幕 → 下载音频单轨转写」在任何一条链路里行为一致，缓存 `ar` 开关位也一致（开 / 关各占一条，互不命中）。

---

## 工作原理

### 双循环分离

- **fetch_loop**：固定间隔抓取 B站 数据，全局只有一个
- **send_loop**：高频检查各目标定时器，每个目标独立倒计时

### 各目标独立

- 目标可写自己的 `persona` / `interests`，留空则继承 `[persona]` 的全局值。全局那份由 `services/persona.py` 组装（bot 全局人格 → 插件覆盖项 → 追加 `persona` 补充说明）；目标级 `persona` 非空时是**整段替换**，不与全局拼接
- 各目标的已看历史（`sent_bvids`）独立追踪，目标之间不会互相"看过"
- 各目标的发送间隔、定时器与平台（`platform`）独立计算

### 抓取与硬性过滤（fetch_loop）

1. **综合热门**（`get_hot_videos`，每次 20 条）
2. **分区排行榜轮换**（`fetcher.rank_sources.enabled=true` 时，每轮从 `categories` 里随机取 `pick_count` 个分区）
3. **标签搜索**（`fetcher.search_source.enabled=true` 时，每轮随机取 `pick_count` 个关键词，按 `order` 排序，各取 `page_size` 条）
4. **bvid 去重**
5. **硬性规则过滤**（先于 LLM 过滤）：
   - `min_views` — 播放量低于此值的视频被过滤
   - `blocked_categories` — 命中分区名的视频被过滤
   - `blocked_keywords` — 扫描标题、描述、分区名与全部标签，命中即过滤
6. **入库 Raw Pool**：去重合并后超过 `max_raw_pool_size`（默认 200）按 `fetched_at` 淘汰最旧视频，并立即落盘

### 发送与定时器（send_loop）

- 插件启动后 send_loop 先等 **30 秒**（给首次抓取留时间），之后每 `check_interval_seconds`（默认 60 秒）检查一遍所有目标
- 目标 `id` 为空 → 首次遇到该 `type` 时告警一次，之后静默跳过
- 首次运行 / 重启后（`last_send_time<=0` 或 `next_send_time<=0`）→ 本轮立即触发发送，不等间隔
- 到期 → 尝试发送 → **无论是否命中，本轮结束后都把定时器重置为 `now + send_interval_hours`**（未命中不会立刻重试，要等下一个发送周期）

### 批量人设过滤（每个目标每轮一次）

- 人设来源：`services/persona.py::compose_persona([persona])`——七个字段默认留空即复用 bot 全局人格（`config/core.toml` 的 `[personality]`），非空项覆盖，`persona` 追加为额外补充；目标自带 `persona` 非空时整段替换
- 取该目标**未看过**视频中**池内前 30 个**（固定上限，避免把整个池塞进 LLM；池序即优先级，先入池的先判断）
- 每批 ≤ `filter.batch_size`（默认 10，可配 1-50），每批一次 LLM 调用
- prompt 是编号列表（标题 / 描述前 120 字 / 分区 / 标签），要求模型只输出 JSON 数组 `[{"index": 1, "matches": true, "reason": "一句话原因"}]`
- **判完全部批次**后按池序取**第一个命中**的视频发送（命中**不提前终止**批次，余下批次仍会调用完）；全不命中则本轮跳过
- 单批 **60 秒**超时；调用失败或解析失败 → **该批整批回退关键词匹配**；连续 **5 次**失败后全局降级为关键词匹配（不再调用 LLM）
- 过滤模型加载失败（模型名写错 / `model.toml` 里没有）→ 跳过 LLM 过滤（一律放行），仍受硬性规则与去重约束
- 日志一行汇总：`批量判断 N 个（M 次调用），命中：bvid=... confidence=... reason=...`（M 是按批大小算出的**上界**；发生关键词降级时实际 LLM 调用更少）

> ⚠️ `[filter].system_prompt` 需要是「只输出 JSON 数组」的版本。若仍是旧版两行文本格式（`匹配: 是/否` + `理由: ...`），模型会按旧格式回复、整批解析失败并回退关键词匹配，连续 5 批后过滤降级——日志里会有对应提示。

### 推送点评三态（`[opinion]`，决策顺序固定）

1. `recommend_enabled = false` → **评论为空**，消息只发「标题 + 链接」两行，`push_mode` 被忽略（总开关语义）。
2. `push_mode = "off"` → 同样无点评（保留作单开关快速关闭）。
3. `push_mode = "simple"`（默认，即现状）→ LLM 生成一句浅推荐语（**30 秒**超时；调用失败 / 模型不可用 → 模板文案，不会漏发）。
4. `push_mode = "deep"` → 先经 `services/material.py` 抓字幕 /（字幕缺失且 `[asr].enabled = true` 时）语音转写 / 弹幕 / 评论素材（`[material]` 限幅 + `[bili_credential]` 凭据 + TTL 缓存；素材抓取兜底 **90 秒**，**开 ASR 时自适应放宽为 90 + `[asr].timeout_seconds` + 60 秒 = 默认 270 秒**），再让 LLM 基于素材写一两句内容级点评（**30 秒**超时——不随 ASR 变化；输出上限 600 字，素材拼入上限 12000 字）。回落规则：
   - 素材 / 模型不可用 → **回落 simple 浅推荐语**（记 warning）；
   - 素材就绪但 deep LLM 回包失败 / 返回空 → **直接模板文案**，不再二次调 LLM（记 warning）。
- `push_mode` 取值非法（非 off/simple/deep）→ 归一化回退 `simple` 并记一行 warning。

### 推送自发视频（`[video_send].push_enabled`，默认开）

- **关（默认）**：完全维持现状——文字发出后发布合成消息事件（`message_id` 前缀 `bili_sharer_`），由 parser 识别链接并下载 / 发送视频。
- **开**：**不再发布合成事件**（避免与 parser 双发）；链接消息送达后，自研下载该视频（DASH 双流 + ffmpeg 合流，全局并发上限 2）并 `send_file` 直发文件，产物发完即删。兜底超时 **900 秒**；**任何失败只记日志**——链接消息已发出这一事实不受影响，该视频也不会因此误入失败集合（结果是「只有链接、没有视频文件」）。

### 发送

- `send_text` 发送到目标 stream，stream 由 `get_or_create_stream` 按 `target.platform` 自动创建
- 发送失败 → 该视频记入本目标的失败集合（**仅内存**，重启后清空）；**下次成功发送前不再重试**——失败 bvid 被持续排除，该目标一旦发送成功，失败集合整体清空

### 状态持久化

- **Raw Pool** 与各目标状态（`last_send_time` / `next_send_time` / `sent_bvids`）经框架 JSON Storage API 落盘，重启后恢复
- 每个目标的已看历史最多保留 **1000** 条 bvid（超出时截取其中 1000 条，**非严格最旧**——`sent_bvids` 由集合转列表后取末尾 1000 条）

---

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

## 自研下载器（`services/bili_downloader.py`）

B 站视频下载 / 合流（被动发视频与推送自发视频共用）：

- **链路**：链接识别（复用素材服务的 BV / av / 短链还原）→ 取分 P（`?p=` 自动解析）→ 时长检查 → 取 DASH 音视频双流 → **先下音频、再以「总预算 − 音频已占」下视频** → `ffmpeg -c copy` 合流（不重编码）→ 清理分片 → 交付产物与元数据。
- **体积预算**按「**视频流 + 音频流合并总大小**」计：`max_size_mb`（默认 200 MB），任一流越限即拒并删除半截文件，合流后再做一次总大小兜底校验；`max_size_mb <= 0` 视为**不限制**。
- **时长上限**独立检查：`max_minute`（默认 30 分钟），在取播放地址前判断；`max_minute <= 0` 视为不限制。
- **画质**：`video_quality`（默认 `"720P"`，非法值回退 720P）为请求上限；请求档拿不到时**就近降级**取 ≤ 请求档的最高可用流且不失败；**AVC 优先**（无 AVC 才回退其它编码），HDR / 杜比一律排除。
- **失败契约**：链接无法识别 / 无 ffmpeg / 分 P 越界 / 超时长 / 超预算 / 非 DASH 流 / 传输层或磁盘异常 / ffmpeg 合流失败——一律抛 `BiliDownloadError` 并清理半截文件；调用方（推送 / 被动）只记日志，链式降级为「只发链接 / 不发视频」。
- **产物目录**：`data/bilibili_sharer/cache/`（本插件自建）；发送后删除（`delete_after=True`），只动自己的文件。
- **全局并发**：`asyncio.Semaphore(2)`，推送与被动两条链路共用。

### ffmpeg 解析顺序（三级回退）

1. `[video_send].ffmpeg_path` 配置值——支持 `%VAR%` 环境变量展开与去引号；写成裸命令名时走 PATH 查找；路径无效时记 warning 并**继续回退**下一级。
2. 系统 PATH——`shutil.which("ffmpeg")`。
3. parser 捆绑二进制——运行时 `plugin_api.get_plugin_path("parser")` + `bin/ffmpeg.exe`（只借二进制路径，不 import 其代码；parser 缺席只是少一级候选，不报错）。

三级都拿不到 → 下载抛 `BiliDownloadError`（提示补配 `ffmpeg_path` / 装 PATH / 启用 parser），表现为「只发链接、没有视频」。

---

## 配置

配置文件路径：`config/plugins/bilibili_sharer/config.toml`（首次启动由框架按默认值自动补全）。

> ⚠️ 插件与配置**不热加载**：改完配置需要重启 Bot 才生效。
>
> 默认值：`chat_tools.enabled` / `video_send.push_enabled` / `video_send.reactive_enabled` 默认**开启**（开箱即用，不依赖 parser）；`opinion.reactive_auto` 默认关闭（避免话痨）。`opinion.recommend_enabled = true` + `push_mode = "simple"`、`video_send.defer_to_parser = true` 本身就是现状语义的显式化。

### 完整示例

```toml
[plugin]
enabled = true

# ── 抓取器 ──
[fetcher]
fetch_interval_minutes = 60        # 抓取间隔（分钟，最小 10）
max_raw_pool_size = 200            # Raw Pool 最大容量（10-1000）
min_views = 1000                   # 最低播放量过滤
blocked_categories = []            # 屏蔽的分区名（如 ["生活", "搞笑"]）
blocked_keywords = ["广告", "推广"]  # 屏蔽词，扫描标题/描述/分区/标签

[fetcher.rank_sources]
enabled = true                     # 是否启用分区排行榜补充
pick_count = 3                     # 每轮随机取几个分区（1-10）
categories = ["anime", "game", "technology", "music", "kichiku", "ent"]

[fetcher.search_source]
enabled = true                     # 是否启用标签搜索（默认开）
keywords = ["游戏", "科技"]         # 每轮随机取 pick_count 个关键词
order = "totalrank"                # totalrank(综合) / click(播放量) / pubdate(最新) / scores(评分) / stow(收藏)
pick_count = 3
page_size = 20                     # 每页结果数（1-42）

# ── LLM 人设过滤 ──
[filter]
model_name = "utils"               # 任务名（一般不用改），也可填 model.toml 里的模型名
system_prompt = "你是一个精确的内容审核助手。必须严格按用户要求的 JSON 数组格式输出，不要输出任何多余内容。"
temperature = 0.1
max_tokens = 1024                  # 批量判断一次要输出多条 JSON，别太小（64-4096）
batch_size = 10                    # 每批视频数（1-50）
extra_params = { }                 # 透传给模型 API，如 { thinking = { type = "disabled" } }

# ── LLM 推荐语 / 点评 ──
[builder]
model_name = "utils"
temperature = 0.7
max_tokens = 4096
extra_params = { }

# ── 人设：七个字段默认全空 = 直接复用 bot 全局人格（config/core.toml 的 [personality]）──
[persona]
nickname = ""                      # 留空=用全局昵称；填了只在本插件覆盖
alias_names = []                   # 留空=用全局别名
personality_core = ""              # 核心性格
personality_side = ""              # 另一面 / 小反差
identity = ""                      # 身份（学生、助手、朋友…）
background_story = ""              # 背景故事（只当底子，模型不会主动念出来）
reply_style = ""                   # 说话风格（会进 prompt，够用就好）
persona = ""                       # 额外补充说明：非空则追加在组装结果最后
interests = ["游戏", "科技", "萌宠", "独立游戏", "二次元"]   # 兴趣由插件自己管（全局人格没有这项）

# ── 发送器 ──
[sender]
check_interval_seconds = 60        # 定时器检查间隔（秒，最小 10）

# ── 发送目标（可多个）──
[[sender.targets]]
type = "group"                     # group 或 private
id = "123456789"                   # 群号或 QQ 号（留空会被跳过）
platform = "qq"                    # 目标平台（适配器名），默认 qq
send_interval_hours = 6.0          # 发送间隔（小时，最小 0.5）
persona = ""                       # 留空继承 [persona] 组装结果；非空=整段替换
interests = []                     # 留空继承全局

[[sender.targets]]
type = "private"
id = "987654321"
platform = "qq"
send_interval_hours = 4.0
persona = "一个喜欢分享可爱事物的女孩子~"
interests = ["二次元", "萌宠", "画画", "可爱的东西"]

# ── B 站素材（聊天工具 + 推送 deep 点评 + 被动自动点评共用）──
[material]
request_timeout_seconds = 20       # 只作用于短链还原（b23.tv 解析）；覆盖面见「material 字段说明」
danmaku_limit = 200                # 弹幕抓取条数上限
comment_limit = 15                 # 热门评论抓取条数上限
max_output_chars = 10000           # 素材输出最大字符数（约 2.5-3K token，超出截断）
include_timestamps = false         # true = 始终带时间戳（同时作用于字幕与弹幕）；false = 由工具参数按需开启
search_limit = 8                   # 搜索类素材返回结果数上限（工具参数只能收紧）

# ── 语音转写 ASR（默认关闭；只在视频拿不到字幕时启用）──
[asr]
enabled = false                              # 总开关（默认关闭 = 行为与不接 ASR 时一致）
mode = "api"                                 # 一期只实现 api；其它值按未启用安全降级
provider_name = "SiliconFlow"                # 从 bot 的 config/model.toml api_providers 复用 base_url/api_key
model_toml_path = ""                         # 可选：model.toml 路径覆盖（空 = 按插件位置推算 <仓库根>/config/model.toml）
api_base = ""                                # 可选覆盖：非空时优先于 provider 的 base_url
api_key = ""                                 # 可选覆盖：非空时优先于 provider 的 api_key（注意泄露面）
model = "FunAudioLLM/SenseVoiceSmall"        # 转写模型（默认端点与模型名已实测可用）
max_minute = 10                              # 视频超过该时长（分钟）跳过 ASR；默认 10 = 把上传+转写压在超时内；<=0 = 不限制
timeout_seconds = 120                        # ASR 请求超时（秒）；素材内 ASR 段硬时限 = 本值 + 60s 宽限；
                                             # 推送 deep / 被动点评的 90 秒素材兜底也按「90 + 本值 + 60」自适应放宽
language = "zh"                              # 语言提示；留空则不传该字段

# ── B 站登录凭据（留空 = 匿名访问）──
[bili_credential]
sessdata = ""
bili_jct = ""
buvid3 = ""
ac_time_value = ""
bililearn_cookie_file = ""         # 可填 BiliLearn 面板扫码登录的 cookie 文件路径，支持 %VAR% 展开

# ── 聊天工具开关（默认开启）──
[chat_tools]
enabled = true                     # 在两工具 execute 内判定；关闭时调用返回固定提示（见「聊天工具」）

# ── 点评形态 ──
[opinion]
recommend_enabled = true           # 推荐语总开关；false 时推送只发标题+链接（push_mode 被忽略）
push_mode = "simple"               # off=无点评 / simple=现状浅推荐语 / deep=素材服务内容级点评
reactive_auto = false              # 被动链路（群友发链接）自动点评（默认关）

# ── 视频发送（默认开：自己下载发送；关掉可退回「只发链接 / 交给 parser」）──
[video_send]
push_enabled = true                # 推送时自发视频（开启后不发合成事件，避免与 parser 双发）
reactive_enabled = true            # 被动收链接时自发视频（默认开；没装 parser 也能发）
max_size_mb = 200                  # 体积上限（MB，按视频流+音频流合并总大小计；<=0 = 不限制）
max_minute = 30                    # 时长上限（分钟，独立检查；<=0 = 不限制）
video_quality = "720P"             # 清晰度上限（VideoQuality 名，非法值回退 720P，不可得就近降级）
ffmpeg_path = ""                   # 空 = 系统 PATH，找不到再回退 parser 捆绑（三级顺序见「自研下载器」）
defer_to_parser = true             # 被动发视频避让 parser；false = 可能与 parser 双下载、双发送

# ── 被动链路过滤与频控 ──
[reactive_filter]
whitelist = []                     # 会话白名单（stream_id；非空时仅白名单生效，优先于黑名单）
blacklist = []                     # 会话黑名单（stream_id；仅白名单为空时生效）
debounce_seconds = 300             # 同一会话同一资源的被动响应防抖窗口（秒；失败也登记）
opinion_rate_limit = 3             # reactive_auto 会话级频率限制：每窗口最多点评次数
opinion_window_minutes = 10        # 频率限制窗口长度（分钟），防刷屏
```

### 字段说明

**`[plugin]`**

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `enabled` | `true` | 插件总开关（`false` 时跳过初始化，不抓取也不发送；被动链路同样跳过） |

**`[fetcher]`**

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `fetch_interval_minutes` | `60` | 抓取间隔（分钟，最小 10） |
| `max_raw_pool_size` | `200` | Raw Pool 最大容量（10-1000，超容量淘汰最旧） |
| `min_views` | `1000` | 最低播放量 |
| `blocked_categories` | `[]` | 屏蔽分区名（与接口返回的分区名精确比较） |
| `blocked_keywords` | `["广告", "推广"]` | 屏蔽词（扫描标题 / 描述 / 分区 / 标签） |
| `fetcher.rank_sources.enabled` | `true` | 是否启用分区排行榜补充 |
| `fetcher.rank_sources.pick_count` | `3` | 每轮随机取几个分区（1-10） |
| `fetcher.rank_sources.categories` | 六个分区 | 轮换分区列表，取值见下方分区表 |
| `fetcher.search_source.enabled` | `true` | 是否启用标签搜索（默认开，没有它池子容易空） |
| `fetcher.search_source.keywords` | `["游戏", "科技"]` | 搜索关键词列表 |
| `fetcher.search_source.order` | `totalrank` | 排序方式 |
| `fetcher.search_source.pick_count` | `3` | 每轮随机取几个关键词（1-10） |
| `fetcher.search_source.page_size` | `20` | 每页结果数（1-42） |

**`[filter]`（批量人设过滤）**

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `model_name` | `utils` | 过滤模型：默认任务名 `utils`（一般不用改），也可填 `model.toml` 里的模型名 |
| `system_prompt` | JSON 数组版提示词 | 需要求模型只输出 JSON 数组（见上方警告） |
| `temperature` | `0.1` | 生成温度（0-2） |
| `max_tokens` | `1024` | 最大 token（64-4096，批量输出多条 JSON 需要更大） |
| `batch_size` | `10` | 每批视频数（1-50，一次 LLM 调用判断几个） |
| `extra_params` | `{}` | 透传模型 API 的额外参数 |

**`[builder]`（推荐语 / deep 点评 / 被动点评）**

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `model_name` | `utils` | 推荐语模型：默认任务名 `utils`，也可填具体模型名（simple / deep / 被动自动点评共用） |
| `temperature` | `0.7` | 生成温度（0-2） |
| `max_tokens` | `4096` | 最大 token（64-4096，默认取上限，避免点评被截断） |
| `extra_params` | `{}` | 透传模型 API 的额外参数 |

**`[persona]`**（人设：默认复用 bot 全局人格）

七个字段与 `config/core.toml` 的 `[personality]` 同名，由 `services/persona.py::compose_persona` 组装成一段多行人设文本（`昵称：…` / `别名：…` / `核心性格：…` / `另一面：…` / `身份：…` / `背景故事：…` / `说话风格：…`，**非空才拼**），最后追加 `persona`（额外补充说明）。三条链路（批量人设过滤、推荐语 simple / deep、被动自动点评）用的都是这份组装结果。

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `nickname` | `""` | 留空＝复用全局昵称；填了＝只在本插件覆盖 |
| `alias_names` | `[]` | 留空＝复用全局别名；填了＝整套替换，拼装时用「、」连接 |
| `personality_core` | `""` | 核心性格（同上口径） |
| `personality_side` | `""` | 另一面 / 小反差（同上口径） |
| `identity` | `""` | 身份，如学生 / 助手 / 朋友（同上口径） |
| `background_story` | `""` | 背景故事（同上口径；模型只当底子，不主动复述） |
| `reply_style` | `""` | 说话风格（同上口径；会进 prompt 占 token，够用就好） |
| `persona` | `""` | **额外补充说明**（原字段保留；非空则追加在组装人设最后） |
| `interests` | 五个兴趣 | 兴趣列表——全局人格没有这一项，一直由插件管 |

> **降级口径**：bot 核心配置尚未初始化、没有 `[personality]` 段或字段缺失时，`compose_persona` 只用插件自己的值（有 `persona` 补充就用它，全空则返回空串），**绝不抛异常**、不影响抓取与发送；被动点评拿到空人设时照旧兜底为「一个普通的群友」。

**`[sender]`**

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `check_interval_seconds` | `60` | 定时器检查间隔（秒，最小 10）——send_loop 每次检查所有目标是否到期 |

**`[[sender.targets]]`（每个目标一段）**

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `type` | 必填 | `group` 或 `private` |
| `id` | 必填 | 群号或 QQ 号（空 ID 目标会被跳过并告警一次） |
| `platform` | `qq` | 目标平台（适配器名）——**必须与本机已安装适配器签名一致**；非 `qq` 平台端到端未验证 |
| `send_interval_hours` | `6.0` | 发送间隔（小时，最小 0.5） |
| `persona` | `""` | 该目标人设，留空继承 `[persona]` 组装结果（非空为整段替换） |
| `interests` | `[]` | 该目标兴趣，留空继承全局 |

**`[material]`（B 站素材抓取与限幅；聊天工具 / 推送 deep 点评 / 被动自动点评共用）**

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `request_timeout_seconds` | `20` | **只作用于短链还原（b23.tv 解析）**；素材服务内部另有固定 20 秒超时，且只覆盖插件自己直连的两类请求（view 元数据、字幕正文）——弹幕 / 评论 / 播放地址走 `bilibili_api`，不吃该值；改这里不会放大整条素材链路的超时 |
| `danmaku_limit` | `200` | 弹幕抓取条数上限（**不是工具参数**，LLM 无法单次调大） |
| `comment_limit` | `15` | 热门评论条数上限（同上，**不是工具参数**） |
| `max_output_chars` | `10000` | 素材输出最大字符数（超出截断，截断说明行也计入上限；保护 chatter 上下文） |
| `include_timestamps` | `false` | 时间戳偏好：`true` = 始终带时间戳（**同时作用于字幕与弹幕**，两者都加 `[MM:SS]` 前缀；LLM 无法单次关闭）；`false` = 由工具参数按需开启 |
| `search_limit` | `8` | 搜索返回条数上限；工具参数只能收紧（`min(limit, search_limit)`），不能放宽。**仅当 `search_limit>0` 时收紧；`search_limit<=0` 时不生效（按工具参数原样使用）** |

> ℹ️ **素材超时的真实覆盖面**：`request_timeout_seconds` 只在聊天工具侧给短链还原（`resolve_b23_short`）设超时。素材服务内部另有一个固定 20 秒超时（`services/material.py` 的 `DEFAULT_TIMEOUT`），**只覆盖插件自己直连的两类请求**——view 元数据、字幕正文。**弹幕、热门评论、字幕轨列表、搜索结果与视频播放地址都经 `bilibili_api` 发出，不吃这两个值**（超时由该库自身的网络设置决定）。所以把 `request_timeout_seconds` 从 20 调到 60，并不会让整条素材链路或某个慢的弹幕 / 评论请求多等；真正管住总等待时长的是各链路自己的外层兜底（deep / 被动点评的素材兜底 90 秒、开 ASR 后自适应放宽；发视频兜底 900 秒）。

**`[asr]`（语音转写；仅字幕缺失时启用）**

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `enabled` | `false` | 总开关（默认关闭 = 行为与旧版一致） |
| `mode` | `"api"` | 一期只实现 api 路线；其它值 log warning 后按未启用降级 |
| `provider_name` | `"SiliconFlow"` | 按 name 从 bot `config/model.toml` 的 `api_providers` 复用 base_url / api_key；本机没有同名 provider 时改成你 `model.toml` 里已有的名字，或显式填下面两项 |
| `model_toml_path` | `""` | 可选覆盖：`model.toml` 的路径（支持 `%VAR%` 展开、去引号）。**空 = 按插件自身位置推算** `<仓库根>/config/model.toml`（`services/` → `bilibili_sharer/` → `plugins/` → 仓库根）；插件被搬到非标准目录深度、或要指向别的文件时用它显式指定。取不到该文件时 ASR 按未启用降级，见下「model.toml 路径与取不到时的降级」 |
| `api_base` / `api_key` | `""` | 可选覆盖：非空时优先于 provider 复用值（api_key 写在插件配置里会落盘，注意泄露面） |
| `model` | `"FunAudioLLM/SenseVoiceSmall"` | 转写模型名（默认端点与模型名已实测可用；换模型需该 provider 支持 OpenAI 兼容 `/audio/transcriptions`） |
| `max_minute` | `10` | **目标分 P** 时长超过该值跳过 ASR（写一行跳过说明，多 P 视频不受全片总时长误伤；取不到分 P 时长才按全片判并标注；`<=0` = 不限制）。默认 10 的理由：192K 音频 10 分钟 ≈ 14MB，可让「下载+上传+转写」在超时内完成 |
| `timeout_seconds` | `120` | ASR 请求超时（秒）；**素材内 ASR 段外层硬时限 = 该值 + 60s 宽限**（`asyncio.wait_for`，超时降级一行说明、不留孤儿文件）；**三条链路的「素材抓取总兜底」也按同一公式自适应放宽**（见下「时限关系」） |
| `language` | `"zh"` | 转写语言提示；留空不传该字段 |

- **触发条件（「无字幕才 ASR」）**：素材工具取字幕后，只有「字幕失败 / 无字幕轨 / 正文为空」且 `[asr].enabled = true` 且 provider/base/key 齐全、**目标分 P** 时长 ≤ `max_minute` 时，才 `download_bilibili_audio` 下载**音频单轨**（不下视频流、不合流；实测 92s 视频 ≈ 0.94MB / 3s）→ POST `{base_url}/audio/transcriptions` → 插入 `## 语音转写（ASR）` 段。字幕拿得到时**零额外流量**。
- **默认关闭零噪声**：`enabled = false`（出厂默认）时素材**完全不出现** ASR 段——三条链路都恒把 `[asr]` 段透传给素材服务，但门槛看的是 `enabled`；「语音转写未启用」提示只在 `enabled = true` 而 provider/key 取不到时作为诊断信息出现。已加载凭据（`[bili_credential]`）时 ASR 下载会带上它——会员 / 登录可见视频的音频才可能取到。
- **provider 复用**：`provider_name` 非空且没显式填 `api_base` / `api_key` 时，插件读 bot 的 `config/model.toml`，按 `provider_name` 在 `api_providers` 里取 `base_url` 与 `api_key`。**若 `model.toml` 已配同名 provider（如 `SiliconFlow`），`[asr]` 会自动复用它的 `base_url` / `api_key`，不必再配一份**；否则请把 `provider_name` 改成本机 `model.toml` 里已有的 provider 名，或在 `[asr]` 显式填 `api_base` + `api_key`。api_key 只进 `Authorization` 请求头，日志与素材文本一律脱敏、绝不打印值。
- **model.toml 路径与取不到时的降级**：路径按「`[asr].model_toml_path` 配置覆盖 > 按插件文件位置推算的 `<仓库根>/config/model.toml`」解析，配置留空就用推算值。文件不存在 / TOML 解析失败 / `api_providers` 里没有该名字 → 只记一行 warning（日志带完整路径与「来源：…」标注，便于看清用的是哪条路径），provider 复用结果为空；此时若 `api_base` / `api_key` 也没填齐，ASR 整体**按未启用降级**——字幕缺失时素材里只写一行「语音转写未启用：缺少 api_base/api_key 且 provider 复用未拿到」，不抛异常、不做联网探测。
- **失败语义**：下载 / 转写 / 结构异常一律只写一行降级说明（如「> （语音转写失败：…）」），不影响其余段落；转写文本自身限幅 12000 字符，整篇素材仍受 `max_output_chars` 截断。
- **作用面（三条链路）**：`bili_video_material`（聊天工具）、`opinion.push_mode = "deep"`（推送深评）、`opinion.reactive_auto = true`（被动自动点评）都会把本段透传给素材服务，三处行为一致；`push_mode = "simple" / "off"`、`recommend_enabled = false` 与「其他平台默认跳过点评」的分支完全不碰本段（根本不抓素材，自然不做 ASR）。音频下载与发视频共用全局下载并发 Semaphore(2)。
- **时限关系（谁包谁）**：外层是各链路的「素材抓取兜底」，内层是素材服务里「ASR 段自己的硬时限」——外层必须大于内层，否则 ASR 还没跑完就被整条取消。

  | 调用方 | 素材抓取兜底（`[asr].enabled = false`） | 素材抓取兜底（`true`） | ASR 段内时限 |
  |--------|------------------------------|-------------------|--------------|
  | 聊天工具 `bili_video_material` | 无外层兜底（工具直接 await 素材服务） | 同左（不限时，只有内层 180s 级兜底） | `timeout_seconds + 60`（默认 180s） |
  | 推送 deep 点评（`builder._deep_material_timeout`） | **90 秒**（基础值） | **90 + `timeout_seconds` + 60 = 默认 270 秒** | 同上 |
  | 被动自动点评（`reactive_handler._material_timeout`） | **90 秒**（基础值） | **90 + `timeout_seconds` + 60 = 默认 270 秒** | 同上 |

  放宽判据只看 `enabled`，且与素材内部门槛是**同一个函数** `material._asr_enabled_flag`——「素材里会不会真跑 ASR」与「外层愿意等多久」永不分叉。`enabled = false`（或配置里没有 `[asr]` 段）时两条兜底取回基础值 90 秒，超时日志文本、素材入参与缓存 `ar` 位与「从未接入 ASR」时逐字一致——关掉开关就等于没这个功能。
- **LLM 环节不受影响**：deep 点评 / 被动点评的 LLM 超时仍是 30 秒——放宽的只是「抓素材（含 ASR）」这一段；被动链路的发视频兜底（900 秒）与其他平台超时（120 / 240 秒）也与 ASR 无关。
- **成本怎么估**：音频单轨 192K ≈ 1.9MB/分钟（实测 92 秒视频 0.94MB、下载 3s + 转写 28.7s）。把 `max_minute` 从 10 分钟抬到 30 分钟，一次 ASR 的量级约 57MB 上传 + 分钟级转写，外层与内层时限都要跟着抬（`timeout_seconds` 与两个 90 秒基础值都是**加法**关系，抬 `timeout_seconds` 会自动把三条链路的预算一起放宽，无需改代码）。

**`[bili_credential]`（B 站登录凭据）**

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `sessdata` / `bili_jct` / `buvid3` / `ac_time_value` | `""` | 手动填写的 Cookie 字段 |
| `bililearn_cookie_file` | `""` | 复用 BiliLearn 面板扫码登录的 cookie 文件（JSON），支持 `%VAR%` 环境变量展开 |

- 取值优先级：**配置字段 > cookie 文件**（逐字段兜底，两边都取不到 → 匿名访问）
- 需要登录才能拿到的内容：AI 字幕轨、部分评论接口；不配凭据时字幕段通常显示「未获取到字幕，可能无 CC 字幕或需要登录」
- 代码与日志**从不记录 cookie 值**：日志只写来源与字段个数。异常文本的脱敏覆盖面 = **聊天工具 / 推送 / 被动三条链路 + 素材构造点**：素材服务内部（view 元数据 / 字幕 / 弹幕 / 热门评论 / 搜索的失败文本）、两个聊天工具（链接回显与失败返回）、推送 deep 点评（凭据加载失败 + 素材获取失败 / 异常两处）、被动自动点评（同三处）、以及自研下载器的凭据加载点，写日志或抛异常前都过同一个 `material._redact`。脱敏值有两个来源：显式传入的凭据对象或 `[bili_credential]` 配置段字段，加上凭据加载成功时登记的「已知凭据值」——后者兜住拿不到凭据对象的构造点。ASR 的 `api_key` 另经自身脱敏（只进 `Authorization` 请求头）。

**`[chat_tools]`（聊天工具开关）**

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `enabled` | `true` | 两个聊天工具是否对 LLM 生效；在 `execute` 内判定，关闭时返回「聊天视频工具未启用（chat_tools.enabled=false）」并记 warning。代价见「聊天工具 → 注册与开关」 |

**`[opinion]`（点评形态）**

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `recommend_enabled` | `true` | 推荐语总开关；`false` 时推送只发标题 + 链接（`push_mode` 被忽略） |
| `push_mode` | `"simple"` | `off` = 无点评 / `simple` = 现状浅推荐语 / `deep` = 素材服务内容级点评（回落规则见「推送点评三态」）；非法值归一化回退 `simple` 并记 warning |
| `reactive_auto` | `false` | 被动链路（群友发链接）自动点评；不受链接防抖约束，由 `[reactive_filter]` 频控兜底 |

**`[video_send]`（视频发送）**

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `push_enabled` | `true` | 推送时自发视频（开启后**不发合成事件**，替换旧的 parser 触发，避免双发；失败只记日志不影响链接已送达；没装 parser 也能发） |
| `reactive_enabled` | `true` | 被动收链接时自发视频（B 站自研下载；其他平台有 parser 就借它的缓存，没有则跳过） |
| `max_size_mb` | `200` | 视频体积上限（MB，**按视频流 + 音频流合并总大小计**，超限即弃并清理半截文件；`<=0` = 不限制） |
| `max_minute` | `30` | 视频时长上限（分钟，取播放地址前独立检查；`<=0` = 不限制） |
| `video_quality` | `"720P"` | 清晰度上限（VideoQuality 名，非法值回退 720P；请求档不可得时就近降级、AVC 优先，不失败） |
| `ffmpeg_path` | `""` | ffmpeg 路径（空 = 系统 PATH，找不到再回退 parser 捆绑 `bin/ffmpeg.exe`；三级顺序见「自研下载器」） |
| `defer_to_parser` | `true` | 被动发视频避让 parser（避让五合一成立才跳过，见「被动发视频」）；`false` = **可能与 parser 双下载、双发送**（仅建议用于对照试验） |

**`[reactive_filter]`（被动链路过滤）**

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `whitelist` | `[]` | 会话白名单（stream_id）——**非空时仅白名单生效，优先于黑名单**（与 parser 的黑名单优先语义互不影响） |
| `blacklist` | `[]` | 会话黑名单（stream_id，仅白名单为空时参与判断） |
| `debounce_seconds` | `300` | 同一会话 + 同一资源 ID 的被动响应防抖窗口（秒）；**失败也登记防抖** |
| `opinion_rate_limit` | `3` | `reactive_auto` 会话级频率限制：每窗口最多点评次数（非法值归一到 ≥1） |
| `opinion_window_minutes` | `10` | 频率限制窗口长度（分钟），防刷屏（`<=0` 归一到 1 分钟） |

### 排行榜分区参考

| 配置名 | 说明 | rid |
|--------|------|-----|
| `anime` | 动画 | 1005 |
| `game` | 游戏 | 1008 |
| `technology` | 科技数码 | 1012 |
| `music` | 音乐 | 1003 |
| `kichiku` | 鬼畜 | 1007 |
| `ent` | 娱乐 | 1002 |
| `knowledge` | 知识 | 1010 |
| `dance` | 舞蹈 | 1004 |
| `animal` | 动物圈 | 1024 |
| `life` | 生活 | 160 |
| `food` | 美食 | 1020 |
| `cinephile` | 影视 | 1001 |
| `fashion` | 时尚 | 1014 |
| `sports` | 运动 | 1018 |
| `car` | 汽车 | 1013 |

---

## 聊天工具（LLM 可见说明）

### 注册与开关

- 两个工具与被动处理器共**三个组件始终注册**（架构约束：`manifest.json` 的 `include` 与 `get_components()` 必须完全一致，不做条件注册）；所有开关判定都在组件内部完成。
- `[chat_tools].enabled`（默认 `true`）在两个工具的 `execute` 入口判定：**关闭时调用即短路返回「聊天视频工具未启用（chat_tools.enabled=false）」**并记 warning，不碰凭据、不发网络请求。
- **已知代价（始终注册设计的权衡）**：关闭时 LLM 仍能在工具列表里看到两个工具的描述，可能照常发起调用并**浪费一次工具调用**（拿到的是固定未启用提示文本）；要彻底不占用 prompt 位需框架支持条件注册（未采用）。

### `bili_video_material`

读取一个 B 站视频的字幕 / 弹幕 / 热门评论（字幕缺失且 `[asr]` 启用时补语音转写），返回 Markdown 素材，供 LLM 结合人设与聊天上下文总结。

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `url` | str | 必填 | B站视频链接 / BV号 / av号 / b23.tv 短链（夹在分享文案里也可以） |
| `include_danmaku` | bool | `true` | 是否包含「## 弹幕精选」段（条数取配置 `danmaku_limit`） |
| `include_comments` | bool | `true` | 是否包含「## 热门评论」段（条数取配置 `comment_limit`） |
| `include_timestamps` | bool | `false` | 是否给字幕 / 弹幕加 `[MM:SS]` 时间戳 |

输出结构：头部说明行（「内容依据请以字幕 / 语音转写为准；弹幕与评论仅为观众反应」）→ 元数据块（标题 / UP 主 / 链接 / 时长 / 播放 / 点赞 / 投币 / 收藏 / 弹幕 / 分区 / 发布时间 / 分 P 数 + 简介）→ `## 字幕全文` → 仅字幕缺失且 `[asr].enabled` 为真时：`## 语音转写（ASR）` → 字幕与转写双缺失时一行专用提示（「本视频无字幕与语音转写：以下弹幕/评论不代表视频内容，请如实说明依据不足」）→ `## 弹幕精选（观众反应，非视频内容）` → `## 热门评论（观众反应，非视频内容）`，最后按 `max_output_chars` 截断。

语义要点：

1. **弹幕 / 评论条数不是工具参数**：`danmaku_limit` / `comment_limit` 只在 `[material]` 里配置，LLM 无法单次调大（防乱填条数撑爆 chatter 上下文）。这是与旧 MCP 版 schema 的主要差异。
2. **`include_timestamps` = 工具参数 OR 配置值**：
   - 配置 `true` → 始终带时间戳，LLM **无法**单次关闭；
   - 配置 `false`（默认）→ 由 LLM 按需传 `true` 开启。
3. **分段降级**：字幕 / 语音转写 / 弹幕 / 评论任意一段失败只写一行降级说明（如「> （弹幕已关闭）」「> （语音转写失败：…）」），不影响其余段落；只有链接识别失败或 view 元数据失败才返回 `(False, 失败说明)`。
4. **TTL 缓存**：`(bvid, 入参变体)` → Markdown，TTL 600 秒、上限 50 条；相同入参重复调用直接命中缓存；不同开关 / 条数 / 截断 / 分 P 页码 / 有无凭据 / asr 开关位各占一条、互不失效。
5. **语音转写（ASR）**：视频没有字幕时下载**音频单轨**，经所配 provider（默认 `SiliconFlow`）的 OpenAI 兼容 `/audio/transcriptions` 端点（默认模型 `FunAudioLLM/SenseVoiceSmall`）转写视频里说的话——解决「无字幕时素材只剩观众反应」。触发条件、provider 复用与失败语义见「配置 → `[asr]`」；临时音频用完即删，默认关闭。聊天工具、推送 deep 点评与被动自动点评**三条链路都把该段透传给素材服务**，因此三处的 ASR 行为与缓存 `ar` 位完全一致；两条自带 90 秒素材兜底的链路（deep / 被动）在 `[asr].enabled = true` 时按 `90 + timeout_seconds + 60` 自适应放宽，关闭时仍是 90 秒。
6. **观众反应标注**：弹幕 / 评论段标题自带「（观众反应，非视频内容）」，素材头部也有一行内容依据说明，防止 LLM 把「评论区在聊什么」当成「视频讲了什么」。
7. **链接处理**：原串能识别 BV / av 号时零网络开销直接使用；识别不了才按短链还原一次。素材里的展示链接会收敛成规范链接（丢掉分享者 `buvid` / `mid` 等参数，保留 `?p=` 分 P）。
8. 失败时返回 `(False, "无法从链接识别 BV 号：...")` 或 `(False, "素材获取失败：...")`，文案直接给 LLM 看（文本与日志均已脱敏凭据值）。

### `bili_search_videos`

按关键词搜索 B 站视频，返回结构化列表。

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `query` | str | 必填 | 搜索关键词 |
| `limit` | int | `8` | 最多返回条数 |

- 实际请求条数 = `min(limit, material.search_limit)`（默认 8）——工具参数**只能收紧、不能放宽**配置上限；**仅当 `search_limit>0` 时收紧，`search_limit<=0` 时该上限不生效**（按工具参数原样使用）；`search_limit` 对 LLM 不可见。
- 输出 `# B站搜索结果` 列表：标题 / BV / UP 主 / 播放 / 时长 / 分区 / 简介（截前 120 字）/ 链接。
- 空关键词、`limit<=0` 直接返回提示，不发请求；接口异常返回 `(False, "搜索失败：...")`。

---

## 行为说明

| 场景 | 行为 |
|------|------|
| 池中有视频且命中目标口味 | ✅ 发送 + 标记已看 |
| 池中前 30 个未看视频都不命中 | ❌ 本轮跳过（不扩大范围），等下一个发送周期 |
| 池中无未看视频 | ❌ 跳过本次，等下一个发送周期 |
| 硬性过滤后无剩余视频 | 不入池，等下次抓取 |
| B站 API 超时 / 报错 | ❌ 跳过本次抓取，等下一轮 |
| LLM 批量判断失败 / 解析失败 | 该批整批回退关键词匹配；连续 5 批失败 → 全局降级关键词匹配 |
| 过滤模型加载失败 | 跳过 LLM 过滤（一律放行），仍受硬性规则与去重约束 |
| 推荐语 LLM 失败 / 超时（30s，simple） | 使用模板文案发送，不会漏发 |
| `recommend_enabled=false` 或 `push_mode=off` | 推送消息只有「标题 + 链接」两行 |
| `push_mode=deep` 素材/模型不可用 | 回落 simple 浅推荐语（记 warning） |
| `push_mode=deep` LLM 回包失败 | 直接模板文案，不再二次调 LLM（记 warning） |
| `push_enabled=true` 自发视频失败 | 只记日志；链接消息已送达，该视频不记入失败集合，本轮照常标记已看 |
| 目标 `id` 为空 | 首次遇到该 `type` 时告警一次，之后跳过该目标 |
| 目标 stream 不存在 | 自动调用 `get_or_create_stream` 创建 |
| 发送失败（链接消息） | 该视频记入本目标失败集合（仅内存）；**下次成功发送前不再重试**（失败 bvid 持续排除，发送成功后整体清空）；本轮定时器照常重置 |
| 未命中 / 无视频可发 | 定时器照常重置到下一周期，不会立即重试 |
| 群友发链接 + `reactive_enabled=true`，避让五合一成立 | 本插件跳过发视频（记 INFO），视频由 parser 负责；parser 若也按规则跳过则无人发送（已知限制） |
| 群友发链接 + 关键词命中但 `parser_map` 无该条目（现版 parser 实际不可达） | **不避让**（五合一不成立）：本插件接管发送——防御性分支，见「避让五合一」第 ④ 条 |
| 群友发链接 + `defer_to_parser=false` | 不避让，本插件照发 → 可能与 parser 双下载、双发送 |
| 群友重发同一链接（`debounce_seconds` 窗口内） | 静默跳过（仅 INFO）；**上一次失败也计入** |
| 群友发非 B 站链接且 parser 缺席 / 未启用 | 跳过（被动非 B 站能力完全依赖 parser） |
| 群友发链接 + `reactive_auto=true` | 会话频控有额度时，对第一个 B 站链接发 1 条自动点评（内容 1-3 句，每消息最多 1 条） |
| 自动点评超频控（默认 3 次/10 分钟） | 跳过点评（记 INFO）；素材/LLM 失败会归还额度 |
| LLM 调用聊天工具但 `chat_tools.enabled=false` | 返回「聊天视频工具未启用（chat_tools.enabled=false）」，记 warning |
| 重启 Bot | ✅ Raw Pool、目标定时器、已看历史从 JSON 恢复；失败集合与被动防抖 / 频控记录清空 |
| 插件刚启动 | send_loop 先等 30 秒（等首次抓取），随后立即检查各目标定时器 |
| 修改配置 / 升级插件 | ⚠️ 不热加载，需重启 Bot |

---

## 消息格式

推送消息（`push_mode=off` 或 `recommend_enabled=false` 时无第三行）：

```
📺 【艾尔登法环】DLC全收集攻略
🔗 https://www.bilibili.com/video/BV1xx411c7mD
哇！老头环的DLC攻略终于来了！一起当艾尔登之王吧！
```

- 首行 emoji（📺 / 🎬 / ▶️ / 🎮 / 🎵 / 📹 随机） + 标题
- 第二行是视频链接
- 第三行点评由 LLM 生成：simple = 一句浅推荐语；deep = 基于素材的内容级点评；LLM 不可用时回落为模板文案（「分享一个视频《…》，UP主：…~」）

---

## 从 BiliLearn MCP（bili-learn）迁移

> v1.1.0 起，原 bili-learn MCP 提供的 `bili_video_material` / `bili_search_videos` 已由本插件内置提供（进程内直调，无 MCP 子进程与 JSON-RPC 协议流）。装好本插件后可以删除该 MCP 条目。

迁移步骤：

1. **删除 MCP 条目**：编辑 `config/mcp.toml`，在 `[mcp].stdio_servers` 的 inline table 中精确删除 `bili-learn = { ... }` 一段（注意前后逗号，其余条目一字不动）。
2. **聊天工具开关**：`config/plugins/bilibili_sharer/config.toml` 中 `[chat_tools].enabled`——内置工具**默认开启**；若你手动关掉过，调用只会得到「未启用」提示。
3. **配置 B 站凭据（可选）**：在插件配置 `[bili_credential]` 填 `sessdata` / `bili_jct` / `buvid3` / `ac_time_value`；或直接填旧面板的 cookie 文件路径，例如
   `bililearn_cookie_file = "C:\\Users\\<你的用户名>\\AppData\\Local\\BiliLearn\\Data\\bilibili_cookies.json"`（TOML 字符串里反斜杠要写成 `\\`，也可以直接用正斜杠 `/`，或写成单引号字面量字符串）。
   （需要登录的主要是 AI 字幕轨与部分评论接口；不配也能用，只是拿不到 AI 字幕。）
4. **重启 Bot**（插件 / 配置不热加载）。
5. **验证**：在群里发一条 B 站链接或问「这个视频讲了什么」，日志应出现 `bili_video_material` 的调用与素材抓取记录。

### 与 MCP 版 schema 的差异

| 项 | 旧 MCP 版（bili-learn） | 本插件内置版 |
|----|-------------------------|--------------|
| 弹幕条数 | `execute` 参数 | **不是参数**，取 `[material].danmaku_limit`（默认 200） |
| 评论条数 | `execute` 参数 | **不是参数**，取 `[material].comment_limit`（默认 15） |
| 输出字符上限 | `execute` 参数 | **不是参数**，取 `[material].max_output_chars`（默认 10000） |
| 搜索条数上限 | 无统一上限 | 参数只能收紧，上限取 `[material].search_limit`（默认 8） |
| 时间戳 | 参数直接决定 | 参数 OR `[material].include_timestamps` |
| 启用开关 | MCP 注册即生效 | `[chat_tools].enabled`（默认开）在 execute 内判定 |
| 传输方式 | MCP 子进程 + JSON-RPC | 进程内直调（无子进程 / 协议流 / 额外进程开销） |
| 触发说明 | MCP instructions | 工具 `description`（触发语义一致） |

---

## 文件结构

```
plugins/bilibili_sharer/
├── manifest.json              # 插件元数据（api_version 七项 + include 三组件：2 tool + 1 event_handler）
├── plugin.py                  # BasePlugin 入口 + 生命周期（启动 / 停止双循环）
├── config.py                  # BaseConfig 配置定义（13 个顶层配置节，含 [asr]）
├── builder.py                 # LLM 推荐语（opinion 三态）+ 消息格式化
├── __init__.py                # 包文档串
├── README.md                    # 本文档（发布说明与配置手册）
├── LICENSE                      # MIT 许可证（© 2026 bilibili_sharer contributors）
├── components/
│   ├── __init__.py
│   └── reactive_handler.py    # 被动链路事件处理器（开关 / 避让 / 防自触发 / 防抖 / 频控全在内部）
├── tools/
│   ├── __init__.py
│   ├── material_tool.py       # bili_video_material 工具（chat_tools.enabled 在 execute 内判定）
│   └── search_tool.py         # bili_search_videos 工具（同上）
└── services/
    ├── __init__.py
    ├── material.py            # 素材抓取原语 + 组装（含 ASR 集成）+ TTL 缓存 + 搜索输出（凭据加载）
    ├── asr.py                 # 语音转写：[asr] 解析 + provider 复用 + /audio/transcriptions（失败不抛、脱敏）
    ├── bili_downloader.py     # 自研下载器：DASH 双流 + ffmpeg 合流 + 音频单轨下载 + 预算 + BiliDownloadError 契约
    ├── state_manager.py       # Raw Pool + 目标定时器 + 已看历史（JSON 持久化）
    ├── fetcher.py             # B站 API 抓取 + 硬性规则过滤
    ├── filter.py              # 批量 LLM 人设过滤（每批 ≤ batch_size）
    ├── persona.py             # 人设组装：默认复用 bot 全局人格（config/core.toml [personality]）+ 插件覆盖 + 额外补充
    ├── llm_utils.py           # filter / builder / 被动点评共用的模型加载与 extra_params 合并
    └── scheduler.py           # 双循环调度器（fetch_loop + send_loop，推送的发送、点评三态与自发视频都在 send_loop）
```

---

## 依赖

- `bilibili-api-python>=17.4.1` — B站 API 封装（manifest 中唯一声明的 `python_dependencies`）
- `httpx` — 短链还原 / view 接口 / 字幕正文直连 / 流式下载 / ASR multipart 上传（Bot 运行环境已具备，本插件未新增依赖声明）
- **ffmpeg** — 自研下载合流必需（系统 PATH / `[video_send].ffmpeg_path` / parser 捆绑三级回退，见「自研下载器」）。**ffmpeg 由系统自备或复用 parser 插件捆绑的二进制，本插件不在 manifest 中声明它**；三级都找不到时「推送自发视频」与「被动发视频」会失败，表现为**只发链接 / 只发点评**（文字链路不受影响；ASR 只下音频单轨、不需要合流，也不受 ffmpeg 影响）
- Neo-MoFox 内置 **LLM API** — 批量人设过滤、推荐语 / deep 点评与被动自动点评
- Neo-MoFox **Send / Stream / Event / Storage / Plugin API** — 消息与文件发送、会话解析、合成事件、JSON 状态持久化、运行时查询 parser 插件（避让 / 借道 / 捆绑 ffmpeg）
- Neo-MoFox **Config API**（`config_api.get_core_config()`）— 读取 bot 全局人格 `core.personality`，供 `services/persona.py` 组装人设；核心配置未初始化时安全降级为只用插件自己的配置

> **与 parser 共存**：parser 在 import 时会全局设置 bilibili_api 的传输层（curl_cffi `select_client`，impersonate chrome131），本插件与其同进程共享该全局态；自研链路按共存口径实测，不假设默认传输层。对 parser 的引用全部是运行时公开方法调用，零代码复制（parser 为 GPL-3.0）。

> MIT 归属：`services/material.py` 与 `tools/*` 中的字幕取轨 / 正文拼接、链接解析思路、元数据块与工具描述语义移植自 xiaoyaya191/bilibili_learning_bot（© 2025 XingYe contributors, MIT），各文件头保留归属说明；`services/bili_downloader.py` 与 `components/reactive_handler.py` 为自研实现，链路形态参考 BiliLearn 与 bilibili_api 公开文档。
>
> **许可证**：本插件以 MIT 许可发布，完整条款见同目录的 `LICENSE` 文件（© 2026 bilibili_sharer contributors）。

---

## 已知限制

1. **素材超时覆盖面有限**：`material.request_timeout_seconds` 只作用于短链还原（b23.tv 解析）；素材服务内部的固定 20 秒只覆盖插件自己直连的 view 元数据与字幕正文两类请求，**弹幕 / 评论 / 字幕轨列表 / 搜索 / 播放地址都经 `bilibili_api` 发出、不吃该值**（由其自身网络设置决定）。
2. `platform` 必须与本机已安装适配器的签名一致（默认 `qq`）；**非 qq 平台端到端未验证**，填错会让该目标的发送失败并打日志。
3. 每轮最多判断池内前 30 个未看视频，全不命中则跳过本轮；未命中的定时器照常重置，所以「这轮没发出去」要等下一个 `send_interval_hours` 才可能再发。
4. `chat_tools.enabled = false` 时两个工具**仍然对 LLM 可见**（组件始终注册）：LLM 可能照常发起调用并浪费一次工具调用，拿到固定「未启用」提示——这是「不做条件注册」架构的已知权衡。
5. **parser 缺席 / 未启用时的边界**：被动链路其他平台链接跳过、非 B 站不点评；推送链路 `push_enabled = false`（关掉时）合成事件无人消费——照常发链接与点评文字，但**不会自动发视频**；默认开着由本插件自发。B 站功能（工具 / deep 点评 / 自研发视频）不受影响。
6. `video_send.defer_to_parser = false` = **可能与 parser 双下载、双发送**（parser 的处理器无法被温和阻止），该取值仅建议用于对照试验。
7. **避让后无人发送**：被动发视频避让成立后，parser 仍可能「按自己的规则跳过」（其自身防抖 300s / 资源 ID 防抖 / 多 Bot 仲裁 / `@` 他人 / 超限 / parse 失败）→ 该链接**没有任何一方发送**；只记日志，不做延迟接管。五合一第 ④ 条的认领检查为**防御性检查**（现版 parser 命中必认领、该位不翻转判定；未来若允许「命中但平台禁用」才自动接管），**不消除上述无人发送限制**。
8. **失败也登记防抖**：B 站视频下载 / 发送失败后，同一链接 `debounce_seconds`（默认 300s）内**静默跳过**（仅 INFO 日志）——窗口内群友重发也不会重试。
9. 一条消息含**多个外站链接**时，只处理 parser 命中的第一个（每次解析都拿整条消息文本匹配，其余链接落到同一资源 ID 被第二层防抖吞掉）；**「每条消息最多处理前 3 个链接」的上限只作用于发视频分支**，自动点评分支在全部链接中取第一个 B 站链接、不受该上限影响。
10. 本插件 `[reactive_filter]` 是**白名单优先**，parser 的 `is_session_allowed` 是**黑名单优先**——两套语义互不影响（各判各的）；同一 stream 同时在本插件白名单与 parser 黑名单内时，本插件会响应而 parser 沉默。
11. 被动链路的防抖与频控记录**仅存内存**，重启 Bot 后清零（重启后同链接可再次响应）。
12. 自研下载三级都找不到 ffmpeg 时下载必失败（`BiliDownloadError`），表现为「只发链接 / 不发视频」；非 DASH 提供的视频（理论罕见）也不支持。
13. 插件与配置不热加载：改配置、升级插件都需要重启 Bot。
14. **语音转写（`[asr]`，默认关）的成本与边界**：时长在**下载前**检查（按**目标分 P**，默认 `max_minute = 10` 分钟）——超限**零下载流量**，只多查一次分 P 信息；转写耗时随音频增长（实测 92s 音频：下载 3.0s + 转写 28.7s）；整段有 `timeout_seconds + 60s` 的内层硬时限（`asyncio.wait_for`），超时只写一行说明并清理临时音频，不会把聊天工具拖成数分钟；**推送 deep 点评与被动自动点评的「素材抓取兜底」也按同一公式自适应放宽（90 → 默认 270 秒）**——深评/被动点评遇到无字幕长视频时最多等这么久而不会中途被取消，`enabled = false` 时两者仍是 90 秒；ASR 服务不可用 / 失败 / 超时都退化为「无字幕」旧行为，并给 LLM 一行「本视频无字幕与语音转写：以下弹幕/评论不代表视频内容，请如实说明依据不足」的专用提示；音频体积走 200MB 兜底预算（正常语音视频远小于此）；转写文本 12000 字符硬截断；一期只有 api 路线（无本地 whisper）。

---

## CHANGELOG

### 未发布（开发中；manifest 版本仍为 1.1.0）

- **`[persona]` 默认复用 bot 全局人格**：新增七个与 `config/core.toml` `[personality]` 同名的字段（`nickname` / `alias_names` / `personality_core` / `personality_side` / `identity` / `background_story` / `reply_style`，默认全空＝复用），由新文件 `services/persona.py::compose_persona` 组装成人设文本；原 `persona` 字段改为**额外补充说明**（默认空，非空追加在组装结果最后）。推送链路的批量人设过滤与推荐语（simple / deep）、被动链路的自动点评都改用这份组装结果；核心配置不可用时只用插件自己的值，不抛异常。
- **全部配置字段说明改写为小白友好版**（`config.py` 的 Field description 与节说明）：逐条说清「这个开关干什么」「改了会怎样」「默认为什么是这样」；字段名 / 类型 / 默认值 / 校验一律未动。

### v1.2.0（2026-10-19）

- **人设默认复用全局人格**：`[persona]` 七项（昵称 / 别名 / 核心性格 / 另一面 / 身份 / 背景故事 / 说话风格）默认读 bot 的 `config/core.toml` 的 `[personality]`，填了才覆盖。
- **语音转写默认复用 bot 的 voice 模型**：`[asr].source` 默认 `voice`（沿用 `model_tasks.voice`，开箱可用），拿不到才退回 `provider_name`；`[asr].model` 留空＝跟随来源；新增 `[asr].endpoint_path` 适配非标准接口。
- **默认模型改为 `utils`**：`[filter]` / `[builder]` 的 `model_name` 默认填框架任务名，新装无需先改配置（解析顺序：先任务名、再模型名）。
- **文档与注释重写**：配置说明与源码注释改为更易懂的口吻，说清每项做什么；打包内附 `LICENSE`。

### v1.1.0（2026-10-18）

- **新增两个原生聊天工具**：`bili_video_material`（字幕 / 弹幕 / 热门评论，分段降级 + 字符截断 + 600s TTL 缓存）与 `bili_search_videos`，内置化取代原 BiliLearn MCP（bili-learn，进程内直调、无子进程与 JSON-RPC）。
- **新增 `[material]` 与 `[bili_credential]` 配置节**：素材条数 / 截断 / 时间戳 / 搜索上限，以及 B 站 Cookie 凭据（配置字段或复用 BiliLearn cookie 文件；日志与返回都不落凭据值）。
- **新增 `[chat_tools]` 开关**（默认 `false`）：在两工具 `execute` 内判定，关闭时短路返回「聊天视频工具未启用（chat_tools.enabled=false）」；组件始终注册，关闭时工具描述仍对 LLM 可见（已知权衡）。
- **新增 `[opinion]` 点评三态**：`recommend_enabled` 总开关（false = 只发标题 + 链接）、`push_mode = off / simple / deep`（deep = 素材服务内容级点评，素材/模型不可用回落 simple、LLM 回包失败用模板文案；非法值归一化回退 simple）、`reactive_auto` 被动自动点评（默认关）。
- **新增 `[video_send]` 视频发送**：`push_enabled` 推送自发视频（开启后不发合成事件、避免与 parser 双发；失败只记日志）、`reactive_enabled` 被动收链接自发视频、`max_size_mb`（按视频流 + 音频流合并总大小计）与 `max_minute` 预算（`<=0` = 不限制）、`video_quality`（就近降级、AVC 优先）、`ffmpeg_path`（三级解析：配置 → 系统 PATH → parser 捆绑）、`defer_to_parser`（默认避让；false = 可能与 parser 双下载、双发送）。
- **新增被动链路**（`components/reactive_handler.py`，`[reactive_filter]` 配置节）：订阅 ON_MESSAGE_RECEIVED、后台 task 不挡聊天事件流；会话过滤（白名单优先）、防自触发三保险（合成前缀 `bili_sharer_` / sender==self_id / 首段 @ 其他 bot）、链接识别（含 json 卡片）、被动发视频每消息最多处理前 3 个链接（点评不受此限）、stream+资源 ID 防抖（默认 300s，失败也登记）、避让五合一（避让开关 + parser 在场且实例可用 + 关键词命中 + **parser 认领该平台** + 其会话允许）、其他平台借道 parser 缓存（只发不删）、自动点评会话级频控（默认 3 次/10 分钟，不受链接防抖约束）。
- **新增自研下载器**（`services/bili_downloader.py`）：DASH 双流下载 + `ffmpeg -c copy` 合流，体积 / 时长预算独立检查，超限与任何失败一律 `BiliDownloadError` 并清理半截文件；全局下载并发 Semaphore(2) 两链路共用；插件启动时清扫缓存目录内超过 6 小时的孤儿文件。
- **新增 `[asr]` 语音转写**（默认 `enabled = false`）：视频**没有字幕**时才下载**音频单轨**（不下视频流、不合流）并经 OpenAI 兼容的 `/audio/transcriptions` 端点转写（默认 provider `SiliconFlow`、默认模型 `FunAudioLLM/SenseVoiceSmall`），在素材里插入 `## 语音转写（ASR）` 段——解决「无字幕视频的素材只剩弹幕与评论」；字幕拿得到时零额外流量，时长上限按**目标分 P**判定（默认 10 分钟，超限零下载流量、只多查一次分 P 信息）。
- **ASR 在三条链路生效 + 时限自适应**：聊天工具、推送 deep 点评、被动自动点评都把 `[asr]` 段透传给同一个素材服务，三处行为与缓存的 ASR 开关位一致；素材内 ASR 段硬时限 = `timeout_seconds + 60` 秒，deep / 被动点评的 90 秒素材兜底在 `[asr].enabled = true` 时按 `90 + timeout_seconds + 60` 放宽（默认 270 秒），关闭时仍是 90 秒；点评 LLM 的 30 秒与发视频的 900 秒兜底不变。
- **ASR 凭据复用与路径可配**：默认按 `provider_name` 从 bot `config/model.toml` 的 `api_providers` 取 base_url / api_key，`api_base` / `api_key` 显式覆盖优先；新增 `[asr].model_toml_path`（默认空 = 按插件位置推算）覆盖该文件路径，插件被移到非标准目录深度时不再推算错路。缺 key / 非 api 模式 / 超时长都只写一行降级说明，不抛异常、不做联网探测。
- **素材「观众反应」标注**：弹幕 / 评论段标题自带「（观众反应，非视频内容）」，素材头部加一行「内容依据请以字幕 / 语音转写为准」，字幕与语音转写双缺失时给 LLM 一行专用提示（如实说明依据不足）；工具 description 同步提醒不要把观众反应当视频内容。
- **凭据脱敏收口**：凭据值从不进日志；聊天工具 / 推送 / 被动三条链路的异常文本，以及素材服务与自研下载器内部的异常文本构造点，统一过同一个脱敏入口，并在凭据加载成功时登记其值作为兜底；ASR 的 api_key 只进 `Authorization` 请求头，日志与文本另经自身脱敏。
- **LLM 人设过滤改为批量判断**：每批 ≤ `filter.batch_size`（默认 10，新增字段），每轮最多判断池内前 30 个未看视频，判完全部批次后按池序取首个命中，日志一行汇总；解析失败整批回退关键词，连续 5 批全局降级；模型名取不到时一律放行（见「快速开始」的警告）。
- **发送目标新增 `platform` 字段**（默认 `qq`）：`get_or_create_stream` 与合成消息不再写死 `qq`。
- **新增 `LICENSE`**：MIT 许可证文件（© 2026 bilibili_sharer contributors）。
- **manifest**：`version` → 1.1.0；`api_version` 七项（llm/log/send/stream/event/storage/plugin）；`include` 三组件（`bili_video_material` / `bili_search_videos` 两个 tool + `bili_sharer_reactive` 一个 event_handler），与 `get_components()` 完全一致。
- **默认行为不变**：以上新开关与新段落默认全部关闭或维持现状（`chat_tools.enabled` / `video_send.reactive_enabled` / `video_send.push_enabled` / `opinion.reactive_auto` / `[asr].enabled` 都是 `false`；`defer_to_parser=true`、`recommend_enabled=true`、`push_mode=simple` 即现状显式化），默认基线与 v1.0.0 一致。

### v1.0.0

- 首个版本：fetch_loop + send_loop、硬性规则过滤、LLM 人设过滤（逐个视频）、LLM 推荐语、合成事件触发 parser 发视频。