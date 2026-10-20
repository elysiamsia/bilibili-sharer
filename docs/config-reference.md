# 配置完整参考

> 所有配置节的完整示例与逐字段说明。README 只保留常用项速查。


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

# ── B 站登录态（全空也能自动找：先看扫码记录，再看本机浏览器）──
[bili_credential]
sessdata = ""                      # 手填登录身份；一般不用填，私聊 Bot 说「登录B站」扫码更省事
bili_jct = ""
buvid3 = ""
ac_time_value = ""
bililearn_cookie_file = ""         # 可选：有 BiliLearn 才填（%VAR% 展开）；没有就留空
login_private_only = true          # 扫码登录只允许私聊（群里扫码会把扫码人的账号绑给 Bot）
auto_detect = true                 # 上面全空时自动找：扫码记录 → 本机浏览器/客户端 Cookie
browser_cookie_source = "auto"     # auto / edge / chrome / brave / firefox / none

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
| `sessdata` / `bili_jct` / `buvid3` / `ac_time_value` | `""` | 手动填写的 Cookie 字段（手填最优先，一般用不上） |
| `bililearn_cookie_file` | `""` | 可选：有 BiliLearn 才填的 cookie 文件（JSON），支持 `%VAR%` 环境变量展开 |
| `login_private_only` | `true` | 扫码登录是否只在私聊可用；群聊扫码会把**扫码那个人**的账号绑给 Bot，所以默认禁止 |
| `auto_detect` | `true` | 没填凭据时自动找本机登录态（扫码记录 → 浏览器/客户端 Cookie） |
| `browser_cookie_source` | `"auto"` | 自动探测范围：`auto` / `edge` / `chrome` / `brave` / `firefox` / `none` |

- 取值顺序（谁先拿到 `SESSDATA` 谁说了算）：**配置字段 → `bililearn_cookie_file` → 扫码登录记录 → 本机浏览器 Cookie**；全都没有 → 匿名访问
- **扫码登录**（推荐）：私聊 Bot 说「登录B站」→ 它发二维码 → 手机 B 站扫码确认 → 凭据写入 `data/bilibili_sharer/credentials.json`，立即生效、不用重启、不用改配置
- **自动探测**：读本机 Edge / Chrome / Brave / Vivaldi / Firefox 与 B 站桌面客户端的 `bilibili.com` Cookie（Chromium 用 DPAPI + AES-GCM 解密，v20 App-Bound 加密读不了；浏览器运行时会独占 Cookie 库，此时探测失败并提示改用扫码）
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
