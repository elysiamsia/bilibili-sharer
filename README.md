# bilibili_sharer

自动从 B 站抓热门视频，按 Bot 人设过滤后定时分享到**配置的目标**（群 / 私聊，`platform` 默认 `qq`）；同时向聊天 LLM 提供三个原生工具（读素材 / 搜视频 / **发视频**），并支持群友发链接时自动发视频与语音转写。

[市场页面](https://39.96.71.162/plugin/bilibili_sharer) · [GitHub 仓库](https://github.com/elysiamsia/bilibili-sharer) · [Releases](https://github.com/elysiamsia/bilibili-sharer/releases)

## 功能亮点

- **定时推送**：每个目标独立的人设、兴趣与定时器，互不干扰
- **三个原生工具**：`bili_video_material`（字幕/弹幕/热门评论素材）、`bili_search_videos`（搜索）、`bili_send_video`（把视频真正发出去）
- **被动链路**：群友发 B 站链接 → 自动下载发视频；其他平台链接借 parser（装了才借，没装就跳过）
- **语音转写**：视频没字幕时下载音频单轨转写，解决「只总结弹幕」的问题（默认关）
- **开箱可用**：人设默认复用 bot 全局人格、模型默认用 `utils` 任务，新装通常不用改任何东西
- **不依赖 parser**：推送与被动都能自己下载发视频；装了 parser 会自动避让，避免双发

## 快速开始

1. **安装**：市场里点安装，或把 `bilibili_sharer-1.2.1.mfp` 放进 `plugins/` 目录
2. **配置目标**：编辑 `config/plugins/bilibili_sharer/config.toml`，在 `[[sender.targets]]` 里填 `type`（`group`/`private`）与 `id`（群号/QQ 号）
3. **重启 Bot**（插件与配置都不热加载）

> ⚠️ **模型**：`[filter].model_name` / `[builder].model_name` 默认 `utils`（框架任务名，一般不用改）。想指定具体模型就填本机 `config/model.toml` 里的模型名；取不到模型时过滤会 fail-open（跳过判断、一律放行）。
> 📄 **字幕**：B 站字幕需要登录凭据。想要字幕就往 `[bili_credential]` 里填 `sessdata` 或 `bililearn_cookie_file`；不填也能用，只是没字幕（会自动走弹幕/评论或语音转写）。


## 最小配置示例

```toml
[sender]
check_interval_seconds = 60

[[sender.targets]]
type = "group"                 # group 或 private
id = "123456789"               # 群号 / QQ 号
platform = "qq"                # 适配器签名对应的平台名
send_interval_hours = 4        # 多久推一次（最小 0.5）
persona = "喜欢科技和游戏的群友"
interests = ["游戏", "科技"]
# 其余项留空 = 用默认值；改完重启 Bot 生效
```
## 常用配置速查

| 配置项 | 默认 | 说明 |
|---|---|---|
| `[sender].check_interval_seconds` | `60` | 定时器检查间隔（秒） |
| `[[sender.targets]]` | — | 发送目标：`type` / `id` / `platform` / `persona` / `interests` / `send_interval_hours` |
| `[filter].model_name` | `utils` | 过滤模型（任务名或模型名） |
| `[filter].batch_size` | `10` | 每批判断几个视频（1-50） |
| `[builder].model_name` | `utils` | 推荐语/点评模型 |
| `[builder].max_tokens` | `4096` | 点评输出上限 |
| `[persona]` 七项 | 全空 | 留空＝复用 bot 全局人格（`config/core.toml` 的 `[personality]`），填了才覆盖 |
| `[chat_tools].enabled` | `true` | 三个聊天工具是否可用 |
| `[opinion].push_mode` | `simple` | `off` / `simple`（浅推荐语）/ `deep`（读素材做内容级点评） |
| `[opinion].reactive_auto` | `false` | 群友发链接时是否自动点评一句 |
| `[video_send].push_enabled` | `true` | 推送时自己发视频（关掉＝只发链接） |
| `[video_send].reactive_enabled` | `true` | 群友发链接时自己发视频 |
| `[video_send].max_size_mb` / `max_minute` | `200` / `30` | 视频体积/时长上限（`<=0` 不限制） |
| `[asr].enabled` | `false` | 语音转写开关；默认复用 bot 的 `model_tasks.voice` |

> 完整的 12 个配置节、逐字段说明与可直接粘贴的示例，见 [配置完整参考](docs/config-reference.md)。

## 三条链路

1. **推送**（后台自动）：定时抓取 → 硬性过滤 → 按人设批量判断 → 发链接 + 推荐语（可选：自己把视频也发出来）
2. **被动**（群友触发）：会话里出现 B 站链接 → 校验开关/防抖/避让 → 自动发视频（可选：自动点评一句）
3. **聊天拉取**（LLM 按需）：问「这视频讲了什么」→ 读素材；说「直接发我」→ 调 `bili_send_video` 把文件发出来

## 已知限制

1. LLM 过滤按「每轮最多 30 个未看视频」批量判断，池子更大时后面的可能等下一轮。
2. 避让 parser 后，parser 若因自身防抖/仲裁/超限跳过，该链接可能**无人发送**（只记日志）。
3. `[video_send].defer_to_parser = false` 会与 parser **双下载、双发送**，只建议做对照试验。
4. 聊天工具关闭时仍对 LLM 可见，调用只会拿到「未启用」提示（固定注册架构的代价）。
5. 长视频超过 `[asr].max_minute` 时不转写，素材会明确提示「依据不足」。
6. 非 `qq` 平台的端到端链路尚未验证（`platform` 必须与本机适配器签名一致）。
7. 无 ffmpeg 时「自己发视频」会失败（只发链接/点评）；ffmpeg 由系统或 parser 捆绑提供。

## 更多文档

| 文档 | 内容 |
|---|---|
| [工作原理与自研下载器](docs/how-it-works.md) | 双循环、批量过滤、点评三态、下载器与 ffmpeg 三级回退 |
| [被动链路详解](docs/reactive.md) | 处理顺序、五合一避让、防抖、自动点评 |
| [配置完整参考](docs/config-reference.md) | 全部配置节示例 + 逐字段说明 |
| [从 BiliLearn MCP 迁移](docs/migration-bililearn.md) | 删除旧 MCP 条目、能力对照 |
| [完整变更记录](docs/changelog-full.md) | 历史版本全部条目 |

## 变更记录

### v1.2.1（2026-10-19）

- 文档整理：README 精简到百来行，实现细节拆到 `docs/`（工作原理、被动链路、配置参考、迁移、完整变更）。无功能改动。

### v1.2.0（2026-10-19）

- 新增 `bili_send_video` 工具：LLM 可以把视频真正发出去，不再只会贴链接。
- 人设默认复用 bot 全局人格（`[persona]` 七项留空即复用），语音转写默认复用 bot 的 `voice` 模型。
- 默认模型改为 `utils`；聊天工具、推送自发视频、被动自发视频、标签搜索默认开启（开箱即用、不依赖 parser）。
- 文档重组：README 精简，细节拆到 `docs/`；配置说明与源码注释全面重写。

### v1.1.0（2026-10-18）

- 原 bili-learn MCP 的能力内置化：素材读取、B 站搜索改为插件原生工具。
- 新增被动链路（群友发链接自动发视频/点评）与推送自发视频。
- 过滤改为批量判断；新增语音转写（无字幕时转音频）。

> 更早版本见 [完整变更记录](docs/changelog-full.md)。

## 许可与致谢

- 本插件：MIT（见 [LICENSE](LICENSE)）
- 素材/搜索工具的触发语义与参数设计参考 [BiliLearn](https://github.com/xiaoyaya191/bilibili_learning_bot)（MIT）；视频下载与避让思路参考 [parser](https://github.com/astrbot-community/astrbot_plugin_parser)（GPL-3.0，仅参考思路，未复制代码）。