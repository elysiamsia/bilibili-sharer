# 从 BiliLearn MCP 迁移

> 原先依赖 bili-learn MCP 的用户看这篇。


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
