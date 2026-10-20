# 完整变更记录

> 历史版本的全部条目。


### v1.2.5（2026-10-20）

- **修复：扫码登录拿不到 SESSDATA**。B 站 2026 年改版后，`qrcode/poll` 成功返回的 `url` 只带跨域 `ticket`，不再直接带 Cookie；需要再请求 `crossDomain`（`passport.biligame.com/x/passport-login/web/crossDomain` 等三个地址按序尝试）并从 `Set-Cookie` 收 `SESSDATA` / `bili_jct` / `DedeUserID`。`bilibili_api.login_v2` 仍然只做 URL 拆分，所以旧实现「手机提示成功、`get_credential()` 却是空凭据」。
- 新增 `services/bili_login_flow.py`：自己实现 `generate → poll → 换票`，全程复用同一个 httpx 会话（buvid 等会话 cookie 才串得起来）；保留「老流程 URL 直接带 Cookie」「`cookie_info` 结构化兜底」两条快路径。
- `bili_login` 改为使用该模块；轮询状态按 86101/86090/86038/0 判定，失败文案区分「过期」「没换到 SESSDATA」「保存失败」。
- URL 查询串**不再做 URL 解码**：`SESSDATA` 里的 `%2C` 必须保留，否则 Cookie 头里的逗号会被当成分隔符。
- **真机验证（2026-10-20 17:21）**：扫码确认 → `换票成功（passport.biligame.com）` → 拿到 `sessdata / bili_jct / buvid3 / dedeuserid` → 凭据落盘、`get_self_info` 回显昵称，登录态立即生效。

### v1.2.4（2026-10-20）

- **新增 `bili_login` 扫码登录工具**：`QrCodeLogin(WEB)` 生成二维码 → `send_image` 发到当前会话 → 每 2 秒轮询 `check_state`（SCAN 提示已扫码 / CONF 提示确认中 / TIMEOUT 提示重来）→ 成功后把 sessdata / bili_jct / buvid3 / DedeUserID / ac_time_value 写入 `data/bilibili_sharer/credentials.json`（0600），并尝试用 `user.get_self_info` 回报昵称。凭据每次现读，**写完立即生效，不用重启**。
- **新增登录态自动探测**（`services/browser_cookies.py`）：Edge / Chrome / Brave / Vivaldi / Firefox / B 站桌面客户端；Chromium 走 `Local State` 的 DPAPI 密钥 + AES-GCM（`cryptography` 或 `pycryptodome` 任一即可，都没有就跳过该来源），Firefox 走明文 `cookies.sqlite`；`v20` App-Bound 加密明确跳过。只读、不打印值；Cookie 库被浏览器独占时给出「浏览器正在运行」的可读原因。
- **凭据取值链扩展**：配置字段 → BiliLearn 文件 → 扫码记录 → 本机浏览器 Cookie；全空仍匿名，行为向后兼容。
- **新增 `[bili_credential]` 字段**：`login_private_only`（默认 true，只允许私聊扫码）、`auto_detect`（默认 true）、`browser_cookie_source`（默认 auto）。
- **组件清单**：`include` 与 `get_components()` 增加到 5 个（4 个 tool + 1 个 event_handler）；manifest 版本 1.2.4。

### v1.2.3（2026-10-20）

- **修复（重要）：`plugin.py` 被截断，定时推送从未启动**：入口文件少了 builder / 调度器的创建与 `start()`，也少了 `on_plugin_unloaded`。插件加载成功、日志正常，但 `fetch_loop` / `send_loop` 根本没跑——只剩群友发链接的被动链路在工作。已按设计补回完整接线（记账本 → 抓取 → 筛选 → 排版 → 调度器 start），并补回卸载时的 `scheduler.stop()`。
- **修复：stream 不可用时视频被永久标记已发送**：`_try_send` 在 `_send_to_target` 返回 `None`（拿不到 stream）后仍调用 `mark_as_sent`，这条视频对那个目标从此沉默。现在改为记入该目标的失败名单、不标记已看；通道恢复后下一个视频发送成功即整体清空名单。
- **修复：`bili_send_video` 工具下载时忽略凭据**：工具把 `cfg.video_send` 而不是整份配置递给下载器，`[bili_credential]` 永远读不到，登录可见的画质被匿名降级。现在传整份配置。
- **改进：已发送历史确定性淘汰**：`sent_bvids` 改为有序列表追加，超过 1000 条淘汰最旧的一条（原来集合转列表后随机砍，可能丢掉刚发过的视频导致重复推送）。
- **修复：mfp / 市场安装时缓存落点错误**：插件被解包到 `%LOCALAPPDATA%\Temp\<guid>` 后，`_repo_root()` 按文件位置「往上数三层」得到 `%LOCALAPPDATA%`，下载/合流缓存因此写进 `C:\Users\<user>\AppData\Local\data\bilibili_sharer\cache`。现改为：文件夹安装照旧取 `plugins` 上一层；否则用框架 `src` 包（命名空间包，读 `__path__`）反推 Bot 仓库根；两条都不成立才退回原逻辑。
- 新增仓库内自检：`tests/` 静态契约（manifest `include` 与 `get_components` 一致、入口接线、配置节、版本一致性）+ 纯函数/状态单测；GitHub Actions 每次 push 跑一遍。**这类自检会在发布前抓住「文件截断」**。

### v1.2.2（2026-10-20）

- **没有 ffmpeg 也能发视频**：DASH 合流不可用时改用 B 站「单文件流」（`durl`，音视频合一；匿名请求才拿得到，通常 360P/480P），不再劝用户去装 ffmpeg。
- **ffmpeg 查找扩到常见安装位置**：`~/.local/bin`、scoop、WinGet Links、chocolatey、`C:\ffmpeg\bin`——Bot 从计划任务启动、PATH 比终端短时也能找到。
- **B 站风控友好提示**：接口返回 HTML/412 时提示「多为风控/限流」并自动重试一次，不再抛难懂的 `JSONDecodeError`。

### v1.2.1（2026-10-19）

- 文档整理：README 精简到百来行，细节拆到 `docs/`（工作原理、被动链路、配置参考、迁移、完整变更）；无功能改动。

### v1.2.0（2026-10-19）

- **人设默认复用全局人格**：`[persona]` 七项（昵称 / 别名 / 核心性格 / 另一面 / 身份 / 背景故事 / 说话风格）默认读 bot 的 `config/core.toml` `[personality]`，填了才覆盖；原 `persona` 字段改为额外补充说明（追加在组装结果最后）。由 `services/persona.py::compose_persona` 组装，核心配置不可用时只用插件自己的值、不抛异常。
- **新增 `bili_send_video` 工具**：把视频真正下载并发出去，聊天里不再只能贴链接。
- **语音转写默认复用 bot 的 voice 模型**：`[asr].source` 默认 `voice`（沿用 `model_tasks.voice`，开箱可用），拿不到才退回 `provider_name`；`[asr].model` 留空＝跟随来源；新增 `[asr].endpoint_path` 适配非标准接口。
- **默认模型改为 `utils`**：`[filter]` / `[builder]` 的 `model_name` 默认填框架任务名，新装无需先改配置（解析顺序：先任务名、再模型名）。
- **开箱默认值**：`chat_tools.enabled` / `video_send.push_enabled` / `video_send.reactive_enabled` 默认开；`opinion.reactive_auto` 默认关。
- **文档与注释重写**：配置字段说明改为小白友好版（逐条说清「干什么 / 改了怎样 / 为什么默认这样」，字段名 / 类型 / 默认值 / 校验未动）；打包内附 `LICENSE`。

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