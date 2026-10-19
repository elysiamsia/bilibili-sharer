# 完整变更记录

> 历史版本的全部条目。


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