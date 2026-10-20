# 工作原理与自研下载器

> 从 README 拆出的细节章节：双循环、批量过滤、点评三态、下载器与 ffmpeg 回退。

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

- **开（默认）**：**不再发布合成事件**（避免与 parser 双发）；链接消息送达后，自研下载该视频（DASH 双流 + ffmpeg 合流，全局并发上限 2）并 `send_file` 直发文件，产物发完即删。兜底超时 **900 秒**；**任何失败只记日志**——链接消息已发出这一事实不受影响，该视频也不会因此误入失败集合（结果是「只有链接、没有视频文件」）。
- **关**：完全维持现状——文字发出后发布合成消息事件（`message_id` 前缀 `bili_sharer_`），由 parser 识别链接并下载 / 发送视频；没装 parser 时就是「只有链接」。

### 发送

- `send_text` 发送到目标 stream，stream 由 `get_or_create_stream` 按 `target.platform` 自动创建
- stream 建不出来（平台名写错 / 适配器没接）→ 这一条**不标记已看**、记入该目标的失败集合，下个视频发送成功后失败集合整体清空，它还会再来一次；不会出现「没发出去却被永远当成已发」
- 发送失败 → 该视频记入本目标的失败集合（**仅内存**，重启后清空）；**下次成功发送前不再重试**——失败 bvid 被持续排除，该目标一旦发送成功，失败集合整体清空

### 状态持久化

- **Raw Pool** 与各目标状态（`last_send_time` / `next_send_time` / `sent_bvids`）经框架 JSON Storage API 落盘，重启后恢复
- 每个目标的已看历史最多保留 **1000** 条 bvid；超出时淘汰**最旧的**（按发送顺序排队，最近发过的不会被挤掉）

---

## 登录态从哪来（`services/material.py::load_credential` + `services/browser_cookies.py`）

字幕、高清、会员内容都要 B 站登录态。插件按这个顺序找，**谁先拿到 `SESSDATA` 就用谁**：

1. `[bili_credential]` 的五个字段（手填最优先，一般用不上）
2. `bililearn_cookie_file`（有 BiliLearn 的人可用；没有就留空）
3. **扫码登录记录** `data/bilibili_sharer/credentials.json`——私聊 Bot 说「登录B站」，`bili_login` 生成二维码发到会话里，手机 B 站扫码确认后写入；SESSDATA 一落盘，素材/点评/下载/发视频四条链路立刻用上，**不用重启**。扫码流程自己走（`generate → poll → crossDomain 换票`）：2026 年 B 站把发券拆成两步，轮询只给 `ticket`，要再访问一次换票地址、从 `Set-Cookie` 里收凭据
4. **本机浏览器 / 客户端 Cookie**（`auto_detect=true` 时）：Edge / Chrome / Brave / Vivaldi / Firefox 与 B 站桌面客户端，只读 `*.bilibili.com` 的几个字段
   - Chromium 系：`Local State` 里 DPAPI 保护的 AES 密钥 → `v10` 前缀的 AES-GCM 密文；`v20`（Chrome 127+ 的 App-Bound 加密）明确跳过
   - 浏览器运行时会**独占 Cookie 库**（实测 Edge 连只读都打不开）→ 记录「浏览器正在运行」并提示改用扫码
   - Firefox：`cookies.sqlite` 明文，设了主密码则跳过

安全口径：值只进 `Credential`，日志只写来源与字段个数；`login_private_only` 默认 true，禁止在群里扫码（那会把扫码人的账号绑给 Bot）。

---

## 自研下载器（`services/bili_downloader.py`）

B 站视频下载 / 合流（被动发视频与推送自发视频共用）：

- **链路**：链接识别（复用素材服务的 BV / av / 短链还原）→ 取分 P（`?p=` 自动解析）→ 时长检查 → 取 DASH 音视频双流 → **先下音频、再以「总预算 − 音频已占」下视频** → `ffmpeg -c copy` 合流（不重编码）→ 清理分片 → 交付产物与元数据。
- **体积预算**按「**视频流 + 音频流合并总大小**」计：`max_size_mb`（默认 200 MB），任一流越限即拒并删除半截文件，合流后再做一次总大小兜底校验；`max_size_mb <= 0` 视为**不限制**。
- **时长上限**独立检查：`max_minute`（默认 30 分钟），在取播放地址前判断；`max_minute <= 0` 视为不限制。
- **画质**：`video_quality`（默认 `"720P"`，非法值回退 720P）为请求上限；请求档拿不到时**就近降级**取 ≤ 请求档的最高可用流且不失败；**AVC 优先**（无 AVC 才回退其它编码），HDR / 杜比一律排除。
- **失败契约**：链接无法识别 / 分 P 越界 / 超时长 / 超预算 / 非 DASH 流 / 传输层或磁盘异常 / ffmpeg 合流失败——一律抛 `BiliDownloadError` 并清理半截文件；调用方（推送 / 被动）只记日志，链式降级为「只发链接 / 不发视频」。
- **没 ffmpeg 也能发**：优先走 DASH 双流 + 合流；找不到 ffmpeg 时自动改用 B 站的「单文件流」（`durl`，音视频合一、无需合流，**匿名**请求才拿得到，通常 360P/480P）。视频不提供单文件流时才抛错并提示装 ffmpeg（或装 parser 借它捆绑的那份）。
- **产物目录**：`data/bilibili_sharer/cache/`（本插件自建）；发送后删除（`delete_after=True`），只动自己的文件。
- **全局并发**：`asyncio.Semaphore(2)`，推送与被动两条链路共用。

### ffmpeg 解析顺序（四级回退）

1. `[video_send].ffmpeg_path` 配置值——支持 `%VAR%` 环境变量展开与去引号；写成裸命令名时走 PATH 查找；路径无效时记 warning 并**继续回退**下一级。
2. 系统 PATH——`shutil.which("ffmpeg")`。
3. 常见安装位置——`~/.local/bin`、`~/scoop/shims`、`%LOCALAPPDATA%\Microsoft\WinGet\Links`、`%LOCALAPPDATA%\ffmpeg\bin`、`%ProgramData%\chocolatey\bin`、`C:\ffmpeg\bin`（Bot 从计划任务 / 服务启动时 PATH 往往比终端短，装在这些目录也不至于找不到）。
4. parser 捆绑二进制——运行时 `plugin_api.get_plugin_path("parser")` + `bin/ffmpeg.exe`（只借二进制路径，不 import 其代码；parser 缺席只是少一级候选，不报错）。

四级都拿不到 → **不报错**，改用单文件流（见上）；只有连单文件流都没有时才抛 `BiliDownloadError`，表现为「只发链接、没有视频」。

---
