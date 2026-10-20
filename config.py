"""Bilibili sharer 插件配置定义。"""

from __future__ import annotations

from typing import ClassVar

from src.app.plugin_system.base import BaseConfig, Field, SectionBase, config_section


_RANK_CATEGORIES = [
    "anime", "game", "technology", "music", "kichiku", "ent",
    "knowledge", "dance", "animal", "life", "food", "cinephile",
    "fashion", "sports", "car",
]


class TargetConfig(SectionBase):
    """一个发送目标的小本子：[[sender.targets]] 每一条就是它哦。"""

    type: str = Field(description="呀，这一行决定视频发到哪儿呢：写 group 就发到群里，写 private 就悄悄私聊某个人。必填哦，留空这个目标就立不起来啦。")
    id: str = Field(description="收件人的编号：群号或者 QQ 号（其它平台就填那边的账号 ID）。填错的话视频会发不出去，卡在池子里干等呢。")
    platform: str = Field(default="qq", description="走哪个聊天平台（填适配器的名字），默认 qq；接了别的平台就填它的名字，比如 telegram。写错会找不到发送通道哟。")
    send_interval_hours: float = Field(default=6.0, description="隔多少小时给这个目标发一条视频，默认 6（一天大约四次）。调小发得更勤、容易刷屏；最少只能设到 0.5 小时呢。", ge=0.5)
    persona: str = Field(default="", description="这一条目标专属的人设。留空（默认）就跟着 [persona] 组装出来的全局人设；整段填了就直接替换它——适合「给 A 群文艺、给 B 群沙雕」这种分群玩法～")
    interests: list[str] = Field(default_factory=list, description="这一条目标专属的兴趣清单，留空就用全局的。填了以后挑片只按这份口味来，写得越具体越对味哦。")


class BiliSharerConfig(BaseConfig):
    """Bilibili 分享插件的全部配置呀。"""

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "Bilibili 分享插件配置"

    @config_section("plugin")
    class PluginSection(SectionBase):
        """插件的开开关关就住在这里哦。"""

        enabled: bool = Field(default=True, description="插件总开关呀。打开（默认）才会抓视频、挑片、定时推送；关掉以后抓取、推送和被动点评全停（只有聊天工具还看 [chat_tools] 自己的开关），配置都留着呢。")

    @config_section("fetcher")
    class FetcherSection(SectionBase):
        """抓片这一段的节奏与门槛，都在这里啦。"""

        fetch_interval_minutes: int = Field(default=60, ge=10, description="每隔几分钟出门抓一批新视频，默认 60（一小时一轮）。调小素材更新快，可对 B 站的请求也更密哦；下限是 10 分钟。")
        max_raw_pool_size: int = Field(default=200, ge=10, le=1000, description="候选池最多囤多少个视频，默认 200（10-1000）。池子大挑得更全面，就是内存和筛选时间跟着涨；设很小就只剩最新那几片啦。")
        min_views: int = Field(default=1000, ge=0, description="播放量低于这个数的视频直接扔掉，默认 1000。调高只留热门；设成 0 就什么都不筛，连刚发布的小宝藏也收进来哟。")
        blocked_categories: list[str] = Field(default_factory=list, description="分区黑名单：要填 B 站返回的分区名（比如「生活」「搞笑」），一模一样才算命中哦；默认空着＝不屏蔽。想永久躲开某类内容就填这里嘛。")
        blocked_keywords: list[str] = Field(
            default_factory=lambda: ["广告", "推广"],
            description="屏蔽词：标题、简介、分区名或标签里出现这些词就当场淘汰，默认挡掉「广告」「推广」。这一关在 LLM 判断之前，加太多词小心把自己想看也误伤了哦～",
        )

        @config_section("rank_sources")
        class RankSourcesSection(SectionBase):
            """按分区排行榜轮流捞热门的设置呢。"""

            enabled: bool = Field(default=True, description="要不要从分区排行榜补素材呀？打开（默认）每轮会挑几个分区捞热门；关掉就只剩搜索来源，池子容易空哦。")
            pick_count: int = Field(default=3, ge=1, le=10, description="每轮从分区池里随机抓几个分区，默认 3（1-10）。数字大内容更杂、抓取也更慢一点；调成 1 就天天只看一个分区啦。")
            categories: list[str] = Field(
                default_factory=lambda: ["anime", "game", "technology", "music", "kichiku", "ent"],
                description="可以轮换的分区池，默认这六个。想加就从可选值里挑：" + ", ".join(_RANK_CATEGORIES) + "；写错名字的分区会被忽略，不报错哟。留空可就没得分区可轮换呢。",
            )

        rank_sources: RankSourcesSection = Field(default_factory=RankSourcesSection)

        @config_section("search_source", title="标签搜索来源", description="通过关键词搜索 B 站视频")
        class SearchSourceSection(SectionBase):
            """用关键词去 B 站搜片的设置呀。"""

            enabled: bool = Field(default=True, description="要不要用关键词搜索来补货呀？默认开着：每轮随机抽几个关键词去 B 站搜片，适合口味很明确的群。关掉就只靠分区排行榜捞片，池子会小一些哦。")
            keywords: list[str] = Field(
                default_factory=lambda: ["游戏", "科技"],
                description="搜索用的关键词清单，每轮随机抽 pick_count 个来搜，默认「游戏」「科技」。词写得越具体，搜回来的越对味；留空可就没得搜啦。",
            )
            order: str = Field(
                default="totalrank",
                description="搜索结果怎么排：totalrank 综合、click 播放量、pubdate 最新、scores 评分、stow 收藏，默认 totalrank。想要热榜感选 click，想要新鲜感选 pubdate 哟。",
            )
            pick_count: int = Field(default=3, ge=1, le=10, description="每轮抽几个关键词去搜，默认 3（1-10）。抽得多结果多，一次抓取也更耗时呢。")
            page_size: int = Field(default=20, ge=1, le=42, description="每个关键词取回多少条结果，默认 20，B 站一页最多 42。一次多囤点就调到 42，代价是抓取慢一点～")

        search_source: SearchSourceSection = Field(default_factory=SearchSourceSection)

    @config_section("filter", title="LLM 过滤设置", description="配置视频过滤的 LLM 模型和提示词")
    class FilterSection(SectionBase):
        """让模型照人设挑片的设置哦。"""

        model_name: str = Field(
            default="utils",
            description="让哪个模型来筛片呀，默认 utils——这是 bot 自带的「任务名」，一般不用改就能用；也可以填 config/model.toml 里的具体模型名。换更强的判断更准也更贵更慢；名字写错就取不到模型、过滤整个跳过（一律放行，屏蔽规则与去重还在），从此不按口味挑片咯。",
        )
        system_prompt: str = Field(
            default="你是一个精确的内容审核助手。必须严格按用户要求的 JSON 数组格式输出，不要输出任何多余内容。",
            description="筛片时对模型念的「工作守则」，默认要求它只回一个 JSON 数组。要是让它自由发挥多说几句，程序就解析不出来、整批退回关键词兜底啦——改文案请保留 JSON 要求呢。",
            input_type="textarea",
            rows=4,
        )
        temperature: float = Field(default=0.1, ge=0, le=2, description="模型的发挥空间，默认 0.1：筛片要的是稳定，越低越照着规矩来。调高它会更有想法，也更容易凭感觉放行哦。")
        max_tokens: int = Field(
            default=1024,
            ge=64,
            le=4096,
            description="一次回答最多写多少 token，默认 1024。因为一批要输出好几条 JSON，比单条判断更吃额度；调太小会被截断、整批解析失败哟。",
        )
        batch_size: int = Field(default=10, ge=1, le=50, description="一次 LLM 调用打包判断几个视频，默认 10（1-50）。批大省调用次数、可回答太长容易截断；批小判得细、但更费调用～")
        extra_params: dict = Field(
            default_factory=dict,
            description="高级玩具：原样透传给模型 API 的额外参数，默认空着。比如 { thinking = { type = \"disabled\" } } 能关掉思考模式省时间；认不准就别填呀。",
        )

    @config_section("builder", title="LLM 评论生成设置", description="配置推荐语生成的模型")
    class BuilderSection(SectionBase):
        """写推荐语那只笔的设置呢。"""

        model_name: str = Field(default="utils", description="写推荐语用哪个模型，默认 utils（bot 自带的任务名，通常不用改就能用），也可以填 model.toml 里的具体模型名。想话更有个性就换个更会写的；名字写错也不影响发送，只是每条改用模板文案顶上啦。")
        temperature: float = Field(default=0.7, ge=0, le=2, description="推荐语的活泼度，默认 0.7：调高更俏皮也更容易说怪话，调低更稳、却容易变成复读机。想稳定点就压到 0.5 以下哦。")
        max_tokens: int = Field(default=4096, ge=64, le=4096, description="推荐语的长度上限（token），默认 4096（该字段上限）——留足空间给它说完整，别设太小，不然话会被拦腰砍断啦。")
        extra_params: dict = Field(
            default_factory=dict,
            description="透传给模型 API 的额外参数，默认空；用法和 [filter].extra_params 一样（例如 { thinking = { type = \"disabled\" } } 关思考）。不确定就留空嘛。",
        )

    @config_section("persona")
    class PersonaSection(SectionBase):
        """Bot 用什么样的人格分享视频呀。

        默认七项全部留空＝直接沿用 bot 全局人格（config/core.toml 的 [personality]）；
        填了哪一项，就只在这个插件里把它换掉，其它照常复用呢。
        """

        nickname: str = Field(default="", description="昵称：Bot 在这群叫什么呀。留空（默认）就沿用全局人格里的昵称，填了就只在这个插件里改成它～")
        alias_names: list[str] = Field(default_factory=list, description="别名列表：别人还可以这么喊它。默认空＝用全局的别名；这里填了就整套替换哦。")
        personality_core: str = Field(default="", description="核心性格：它是个什么样的孩子。留空沿用全局，填了就把这段换掉——写「活泼、爱安利、说话带波浪号」这种好懂的就行呢。")
        personality_side: str = Field(default="", description="另一面：性格里的小反差，留空沿用全局。比如「看着大大咧咧，其实很在意朋友的心情」，一两句就够哟。")
        identity: str = Field(default="", description="身份：它的自我设定（学生、助手、朋友……），默认沿用全局那一份。想让它在这个插件里换个身份出现才填呀。")
        background_story: str = Field(default="", description="背景故事：它的世界观，留空沿用全局。这段只当底子，模型不会自己念出来，所以短一点就好啦。")
        reply_style: str = Field(default="", description="说话风格：怎么措辞、什么语气，留空沿用全局。想让它像姐姐一样说话就写这里；这段可能很长，够用就好——它也会进 prompt 占 token 呢。")
        persona: str = Field(default="", description="额外补充说明：默认空着。不想动上面七项、只想再加一句「特别偏爱独立游戏」这类叮嘱，就写这里——它会被拼在组装好人设的最后面哟。")
        interests: list[str] = Field(
            default_factory=lambda: ["游戏", "科技", "萌宠", "独立游戏", "二次元"],
            description="兴趣方向：它爱看什么。全局人格里没有这一项，所以由插件自己管，默认游戏/科技/萌宠/独立游戏/二次元。写得越具体，挑片越准～",
        )

    @config_section("sender")
    class SenderSection(SectionBase):
        """发给谁、多久看一眼，都在这儿～"""

        check_interval_seconds: int = Field(default=60, ge=10, description="定时器每隔多少秒醒来瞅一眼「有没有到点的目标」，默认 60。改小更准时，改大最多晚到一会儿，不用太操心哦。")

        targets: list[TargetConfig] = Field(default_factory=list, description="发送目标清单，一个 [[sender.targets]] 就是一条。空着＝谁都不发，插件就只是抓片玩；想让它真正开工至少填一条（type + id）呢。")

    @config_section("material", title="素材抓取设置", description="B 站视频素材（弹幕/评论/简介）抓取与输出限幅")
    class MaterialSection(SectionBase):
        """抓字幕/弹幕/评论并限幅的设置哦。"""

        request_timeout_seconds: int = Field(default=20, description="这一步只管 b23.tv 短链还原最多等几秒（默认 20，网慢加到 30）；素材服务内部另有固定 20 秒超时、不吃这个值，所以调它并不能放大整条素材链路哦。")
        danmaku_limit: int = Field(default=200, description="最多收多少条弹幕，默认 200。收得多更接近现场，可喂给模型的素材也更长（还要被下面的总长度限制截一刀呢）。")
        comment_limit: int = Field(default=15, description="最多收多少条热门评论，默认 15。想多听群友玩梗就加大，只是评论一般比弹幕更慢、更贵哟。")
        max_output_chars: int = Field(
            default=10000,
            description="素材打包后的字数上限，默认 10000（约合 2.5-3K token），超了就截断——它是来保护聊天上下文的。觉得点评变模糊，可以适度放大啦。",
        )
        include_timestamps: bool = Field(
            default=False,
            description="弹幕要不要带时间戳？打开后字幕也会一起带上 [MM:SS] 前缀，默认关着。开了方便说「第 2 分钟那里超好笑」，可惜素材更占字数呢。",
        )
        search_limit: int = Field(default=8, description="用搜索工具时最多返回几条结果，默认 8。想要更多选项就调大，回复也会跟着变长哦。")

    @config_section("asr", title="语音转写（ASR）", description="无字幕视频下载音频并用 API 转写语音（默认关闭）")
    class AsrSection(SectionBase):
        """没字幕时把音频转成文字的设置哦；默认直接沿用 bot 的 model_tasks.voice，也可以改用 model.toml 的服务商。"""

        enabled: bool = Field(default=False, description="要不要给无字幕的视频做语音转写呀？默认关。打开后没字幕的片子也能引台词；代价是慢一点、要花钱、还得配好凭据呢。")
        mode: str = Field(default="api", description="转写方式，一期只做了 api；其它值不会报错，只当作没启用安全降级——所以先保持 api 就好啦。")
        source: str = Field(
            default="voice",
            description="凭据从哪儿来：voice（默认）＝直接沿用 bot 的 model_tasks.voice 那套语音模型，开箱可用；provider＝先用下面 provider_name 指的服务商，拿不到再退回 voice。用 voice 就什么都不用配呢。",
        )
        endpoint_path: str = Field(
            default="",
            description="进阶选项：转写接口的路径，留空＝/audio/transcriptions（OpenAI 兼容的标准写法，绝大多数服务商都是它）。只有换到路径不一样的自家服务才需要改呀。",
        )
        provider_name: str = Field(
            default="SiliconFlow",
            description="用 bot 里哪个服务商（按 name 去 config/model.toml 的 api_providers 拿 base_url 和 api_key），默认 SiliconFlow。换名字就是换一家，别填没配过的哦。",
        )
        model_toml_path: str = Field(
            default="",
            description="进阶选项：手动指定 model.toml 的位置。留空（默认）会按插件自身位置推算到 bot 仓库的 config/model.toml，支持 %VAR% 展开——一般不用管它哦。",
        )
        api_base: str = Field(default="", description="进阶选项：非要绕过服务商配置时，在这里直填接口地址；空着＝用 provider 的。填了它优先呢。")
        api_key: str = Field(default="", description="同上，这里直填密钥；空着＝用 provider 的。写在配置文件里就有泄露面，能不填就不填呀。")
        model: str = Field(default="", description="用哪个转写模型。留空（默认）＝自动跟随来源：用 voice 时就是 voice 任务里的那个模型，用 provider 时退回 FunAudioLLM/SenseVoiceSmall；想指定就填服务商支持的模型名，名字要写对哟。")
        max_minute: int = Field(default=10, description="音频超过几分钟就干脆不转写了，默认 10——长片上传+转写容易顶到超时。填 0 或负数＝不限时长，很烧时间，慎用呢。")
        timeout_seconds: int = Field(default=120, description="一次转写最多等多久，默认 120 秒。网络慢就加大；这段等待会算进深评的总时限里，别设到天上哦。")
        language: str = Field(default="zh", description="告诉模型音频是什么语言，默认 zh。留空就不传这个字段，让它自己猜～")

    @config_section("bili_credential", title="B 站凭据设置", description="B 站登录态：手填 Cookie / 扫码登录 / 自动探测本机登录态")
    class BiliCredentialSection(SectionBase):
        """B 站登录 Cookie 放在这里哟（全空也能自动找：先看扫码记录，再看本机浏览器）。"""

        sessdata: str = Field(default="", description="B 站 Cookie 里的 SESSDATA（登录身份）。填了能取到更完整的字幕内容、更高清的画质；不填就匿名，能用但少一些东西。它是登录凭据，别外泄呀。")
        bili_jct: str = Field(default="", description="Cookie 里的 bili_jct（写操作令牌）。本插件只读内容，其实用不上它，填了也无害～")
        buvid3: str = Field(default="", description="Cookie 里的 buvid3（设备标识）。有时能降低被风控的概率，可填可不填哦。")
        ac_time_value: str = Field(default="", description="Cookie 里的 ac_time_value，只用来刷新 SESSDATA。想要长期免扫码就填它呀。")
        bililearn_cookie_file: str = Field(
            default="",
            description="可选：装了 BiliLearn 的人可以填它导出的 cookie 文件路径（%LOCALAPPDATA%\\BiliLearn\\Data\\bilibili_cookies.json）。没装 BiliLearn 就留空——扫码登录或自动探测本机浏览器就够啦。",
        )
        login_private_only: bool = Field(
            default=True,
            description="扫码登录是否只允许在私聊里进行，默认 True。群里扫码会把「扫码那个人」的账号绑给 Bot，所以默认不给群聊用；确实想在群里登录再关掉它（不推荐哟）。",
        )
        auto_detect: bool = Field(
            default=True,
            description="上面几项都空着时，要不要自动找本机已有的 B 站登录态？默认开：先看 Bot 自己的扫码登录记录，再看本机浏览器/客户端的 Cookie（Edge、Chrome、Brave、Firefox、B 站桌面客户端）。关掉就只认手填与扫码啦。",
        )
        browser_cookie_source: str = Field(
            default="auto",
            description="自动探测只看哪家：auto 全试（默认）/ edge / chrome / brave / firefox / none（不查浏览器）。浏览器正在运行时 Cookie 库被独占、读不到（尤其 Edge），这时关掉浏览器或直接用扫码登录就好啦。",
        )

    @config_section("chat_tools", title="聊天工具设置", description="LLM 聊天工具开关（默认关闭）")
    class ChatToolsSection(SectionBase):
        """开放给聊天 LLM 使用的 B 站工具开关呢。"""

        enabled: bool = Field(
            default=True,
            description="要不要让 bili_video_material / bili_search_videos 真的干活呀？默认开：Bot 可以直接查素材、搜 B 站视频。关掉的话两个工具还在列表里，但被调用只会回一句「未启用」哦。",
        )

    @config_section("opinion", title="点评设置", description="推荐语与点评行为配置")
    class OpinionSection(SectionBase):
        """推荐语与点评怎么说话的设置哦。"""

        recommend_enabled: bool = Field(
            default=True,
            description="推荐语总开关。关掉时推送只有「标题+链接」两行，下面 push_mode 直接被无视；打开（默认）才按 push_mode 生成点评。想临时静音就关它最省事哟。",
        )
        push_mode: str = Field(
            default="simple",
            description="点评长什么样：off 不点评、simple 一句浅推荐语（默认）、deep 抓素材做内容级点评。deep 更全但更慢更费钱，素材或模型不可用会自动退回 simple，放心试～",
        )
        reactive_auto: bool = Field(default=False, description="群友发链接时 Bot 要不要主动点评一句呀？默认关——开着容易被当成话痨。偶尔露一句再打开，配合下面的频率限制更稳哦。")

    @config_section("video_send", title="视频发送设置", description="视频下载与发送配置（默认关闭）")
    class VideoSendSection(SectionBase):
        """要不要把视频文件本身也发出去呀？"""

        push_enabled: bool = Field(default=True, description="主动推送时是否顺便把视频文件也发出去，默认开：自己下载并发送，同时不再触发 parser（免得两个一起发）。关掉＝只发链接，交给 parser 之类的插件处理——没装 parser 的话就没人发视频啦。")
        reactive_enabled: bool = Field(default=True, description="群友发 B 站链接时自动下载并发视频呀？默认开，就是「伸手党福音」模式，比较占带宽，先在小群试试哦。关掉就只做点评、不发文件。")
        max_size_mb: int = Field(default=200, description="单个视频最多允许多大（MB，按视频流+音频流合并后的总大小算），默认 200，超了直接放弃这条；填 0 或负数＝不限大小。想收更大的片就调高，下载也更慢啦。")
        max_minute: int = Field(default=30, description="单个视频最多允许多长（分钟），默认 30，跟体积上限各查一次、互不相干；填 0 或负数＝不限时长。想收长片就调大，发送也更耗时呢。")
        video_quality: str = Field(default="720P", description="想要的清晰度（写 VideoQuality 的名字），默认 720P。名字写错就退回 720P；账号拿不到这一档时不会报错，而是就近选一档更低的（匿名常见 480P 哟）。")
        ffmpeg_path: str = Field(default="", description="ffmpeg 在哪儿？留空＝先用系统 PATH 里的，找不到再回退 parser 捆绑的那份。只有你另有构建才需要填哦。")
        defer_to_parser: bool = Field(default=True, description="被动发视频时让不让着 parser？默认 True：它已经处理过的链接本插件就跳过，避免重复发。只有你把 parser 停了才建议关掉它～")

    @config_section("reactive_filter", title="被动过滤设置", description="被动链路的会话过滤与频率限制")
    class ReactiveFilterSection(SectionBase):
        """被动接链接时的会话过滤与防刷屏设置嘛。"""

        whitelist: list[str] = Field(
            default_factory=list,
            description="只在这些会话里响应（填 stream_id）。空着＝所有会话都听；一旦填了，就只认名单内的——白名单比黑名单优先呢。",
        )
        blacklist: list[str] = Field(default_factory=list, description="这些会话里完全不响应（填 stream_id）。白名单空着时它才起作用；两处都填的话以白名单为准哦。")
        debounce_seconds: int = Field(default=300, description="同一个会话里同一条视频（按 BV/资源算）多少秒内只处理一次，默认 300 秒：连着甩同一个链接也只发一条，换别的视频不受影响哦。只管发视频，自动点评走下面的频控；填 0 或负数＝不防抖。")
        opinion_rate_limit: int = Field(default=3, description="自动点评的频率上限：每个窗口里最多点评几句，默认 3。嫌话多就调到 1（填 0 或负数也按 1 句算），嫌冷清就加大～")
        opinion_window_minutes: int = Field(default=10, description="上面那个计数窗口的长度（分钟），默认 10——也就是「10 分钟最多 3 句」。窗口越长越克制；不足 1 分钟会按 1 分钟算哦。")

    plugin: PluginSection = Field(default_factory=PluginSection)
    fetcher: FetcherSection = Field(default_factory=FetcherSection)
    filter: FilterSection = Field(default_factory=FilterSection)
    builder: BuilderSection = Field(default_factory=BuilderSection)
    persona: PersonaSection = Field(default_factory=PersonaSection)
    sender: SenderSection = Field(default_factory=SenderSection)
    material: MaterialSection = Field(default_factory=MaterialSection)
    asr: AsrSection = Field(default_factory=AsrSection)
    bili_credential: BiliCredentialSection = Field(default_factory=BiliCredentialSection)
    chat_tools: ChatToolsSection = Field(default_factory=ChatToolsSection)
    opinion: OpinionSection = Field(default_factory=OpinionSection)
    video_send: VideoSendSection = Field(default_factory=VideoSendSection)
    reactive_filter: ReactiveFilterSection = Field(default_factory=ReactiveFilterSection)
