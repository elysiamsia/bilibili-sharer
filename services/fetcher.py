"""B 站 API 的三只篮子——综合热门、分区排行榜、关键词搜索——把视频捞回来，
按 bvid 去重，再做一轮「硬规则」初筛：播放量不够、分区被屏蔽、撞了屏蔽词的，
统统挡在池子外面。这些判断不需要 LLM，规则说了算。
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from typing import Any

from bilibili_api.hot import get_hot_videos
from bilibili_api.rank import RankType, get_rank
from bilibili_api.search import search_by_type, SearchObjectType, OrderVideo

from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("bili_sharer_fetcher")

# 配置里的分区名 → 排行榜 API 的 RankType 对照表
RANK_CATEGORY_MAP: dict[str, RankType] = {
    "anime": RankType.Douga,
    "game": RankType.Game,
    "technology": RankType.Technology,
    "music": RankType.Music,
    "kichiku": RankType.Kichiku,
    "ent": RankType.Ent,
    "knowledge": RankType.Knowledge,
    "dance": RankType.Dance,
    "animal": RankType.Animal,
    "life": RankType.Life,
    "food": RankType.Food,
    "cinephile": RankType.Cinephile,
    "fashion": RankType.Fashion,
    "sports": RankType.Sports,
    "car": RankType.Car,
}

# 配置里的排序名 → 搜索 API 的 OrderVideo 对照表
_SEARCH_ORDER_MAP: dict[str, OrderVideo] = {
    "totalrank": OrderVideo.TOTALRANK,
    "click": OrderVideo.CLICK,
    "pubdate": OrderVideo.PUBDATE,
    "scores": OrderVideo.SCORES,
    "stow": OrderVideo.STOW,
}


def _extract_video_info(item: dict[str, Any]) -> dict[str, Any] | None:
    """从热门/排行条目里抽出插件内部通用的标准视频字典；遇到畸形条目返回 None，调用方会跳过。"""
    try:
        owner = item.get("owner", {})
        stat = item.get("stat", {})
        return {
            "bvid": item.get("bvid", ""),
            "title": item.get("title", ""),
            "description": item.get("desc", ""),
            "url": f"https://www.bilibili.com/video/{item.get('bvid', '')}",
            "category": item.get("tname", ""),
            "category_id": item.get("tid", 0),
            "play_count": stat.get("view", 0),
            "tags": [],
            "cover_url": item.get("pic", ""),
            "author": owner.get("name", ""),
            "fetched_at": int(time.time()),
        }
    except Exception:
        return None


def _strip_html(text: str) -> str:
    """去掉 HTML 标签——搜索结果的标题总裹着一层高亮标签，得素面朝天。"""
    return re.sub(r"<[^>]+>", "", text)


def _extract_search_result(item: dict[str, Any]) -> dict[str, Any] | None:
    """从搜索条目里抽出标准视频字典。

    搜索 API 的字段名和热门/排行不一样：title 带 HTML 高亮标签（得剥掉），
    play 是播放量，tag 是标签列表，pic 可能是缺协议的相对路径（要补 https:），
    另有 bvid、description、typename/typeid、author、duration 等。
    没有 bvid 或形状不对就返回 None，调用方会跳过。
    """
    try:
        bvid = item.get("bvid", "")
        if not bvid:
            return None
        title = _strip_html(item.get("title", ""))
        pic = item.get("pic", "")
        if pic and not pic.startswith("http"):
            pic = f"https:{pic}"
        tags_raw = item.get("tag", [])
        tags = [t.get("tag_name", "") for t in tags_raw if isinstance(t, dict)] if isinstance(tags_raw, list) else []
        return {
            "bvid": bvid,
            "title": title,
            "description": str(item.get("description") or ""),
            "url": f"https://www.bilibili.com/video/{bvid}",
            "category": item.get("typename", ""),
            "category_id": item.get("typeid", 0),
            "play_count": item.get("play", 0),
            "tags": tags,
            "cover_url": pic,
            "author": item.get("author", ""),
            "fetched_at": int(time.time()),
        }
    except Exception:
        return None


class BiliFetcher:
    """B 站视频抓取器：热门、排行榜、搜索三个来源都归它跑。"""

    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg

    async def _random_delay(self) -> None:
        """请求前小睡 1~3 秒（随机），别把 B 站的门敲得太急。"""
        await asyncio.sleep(random.uniform(1.0, 3.0))

    async def fetch_popular(self) -> list[dict[str, Any]]:
        """取一页综合热门（20 个）；失败就返回空列表，不影响整轮抓取。"""
        await self._random_delay()
        try:
            data = await get_hot_videos(pn=1, ps=20)
            # API 返回 {"list": [...], "no_more": false}；直接给裸列表也接得住
            items = data.get("list", []) if isinstance(data, dict) else (data or [])
            logger.info(f"综合热门抓取到 {len(items)} 个视频")
            return [v for item in items if (v := _extract_video_info(item))]
        except Exception as e:
            logger.error(f"抓取综合热门失败: {e}")
            return []

    async def fetch_ranking(self, category: str) -> list[dict[str, Any]]:
        """取指定分区的排行榜；分区名不在 RANK_CATEGORY_MAP 里就警告一声、返回空列表。"""
        rank_type = RANK_CATEGORY_MAP.get(category)
        if not rank_type:
            logger.warning(f"未知分区: {category}")
            return []
        await self._random_delay()
        try:
            data = await get_rank(type_=rank_type)
            # 排行榜 API 直接给 {"list": [...], "note": "..."}；裸列表也接得住
            items = []
            if isinstance(data, dict):
                items = data.get("list", [])
            elif isinstance(data, list):
                items = data
            logger.info(f"排行榜 {category} 抓取到 {len(items)} 个视频")
            return [v for item in items if (v := _extract_video_info(item))]
        except Exception as e:
            logger.error(f"抓取排行榜 {category} 失败: {e}")
            return []

    async def fetch_by_search(self) -> list[dict[str, Any]]:
        """按 [fetcher.search_source] 配置搜索：开关打开时随机抽 pick_count 个关键词，挨个搜一轮。"""
        search_cfg = self.cfg.fetcher.search_source
        if not search_cfg.enabled or not search_cfg.keywords:
            return []

        pick = random.sample(
            search_cfg.keywords,
            min(search_cfg.pick_count, len(search_cfg.keywords)),
        )
        order = _SEARCH_ORDER_MAP.get(search_cfg.order, OrderVideo.TOTALRANK)
        results: list[dict[str, Any]] = []

        for kw in pick:
            await self._random_delay()
            try:
                data = await search_by_type(
                    keyword=kw,
                    search_type=SearchObjectType.VIDEO,
                    order_type=order,
                    page_size=search_cfg.page_size,
                )
                items = data.get("result", []) if isinstance(data, dict) else []
                count = 0
                for item in items:
                    video = _extract_search_result(item)
                    if video:
                        results.append(video)
                        count += 1
                logger.info(f"搜索 '{kw}' 获取到 {count} 个视频")
            except Exception as e:
                logger.warning(f"搜索关键词 '{kw}' 失败: {e}")

        logger.info(f"标签搜索共获取 {len(results)} 个视频")
        return results

    async def fetch(self) -> list[dict[str, Any]]:
        """一次完整抓取：热门 → 排行榜（轮换抽几个分区）→ 关键词搜索，合并后按 bvid 去重。"""
        all_videos: list[dict[str, Any]] = []

        # 第一站：全站热门先兜一页
        popular = await self.fetch_popular()
        all_videos.extend(popular)

        # 第二站：从配置允许的分区里随机抽几个刷排行榜
        rank_cfg = self.cfg.fetcher.rank_sources
        if rank_cfg.enabled and rank_cfg.categories:
            available = [c for c in rank_cfg.categories if c in RANK_CATEGORY_MAP]
            if available:
                picked = random.sample(
                    available,
                    min(rank_cfg.pick_count, len(available)),
                )
                for cat in picked:
                    ranking = await self.fetch_ranking(cat)
                    all_videos.extend(ranking)

        # 第三站：按关键词搜索再补一批
        search_videos = await self.fetch_by_search()
        all_videos.extend(search_videos)

        # 收尾：同一支视频常在几个篮子里撞面，按 bvid 只留第一份
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for v in all_videos:
            if v["bvid"] and v["bvid"] not in seen:
                seen.add(v["bvid"])
                deduped.append(v)

        logger.info(f"本次抓取共 {len(deduped)} 个视频（去重后）")
        return deduped

    def apply_hard_filters(
        self,
        videos: list[dict[str, Any]],
        min_views: int,
        blocked_categories: list[str],
        blocked_keywords: list[str],
    ) -> list[dict[str, Any]]:
        """硬性规则过滤——三道不问 LLM 的闸门：播放量低于 min_views、分区被屏蔽、
        文字里撞了屏蔽词，命中任何一条都进不了池子。

        屏蔽词会扫标题、描述、分区名和全部 tags——标题没写、但描述或标签里
        露了馅的视频（比如某主播的"和平精英"内容），也照样拦得住。
        """
        blocked_set = {kw.strip() for kw in blocked_keywords if kw and kw.strip()}
        filtered = []
        for v in videos:
            if v["play_count"] < min_views:
                continue
            if v["category"] in blocked_categories:
                continue
            # 把文字全浇进一个草垛，屏蔽词在里面找
            tags = v.get("tags", [])
            haystack_parts = [v["title"], v["description"], v["category"]]
            if isinstance(tags, list):
                haystack_parts.extend(str(t) for t in tags)
            haystack = "\n".join(str(p) for p in haystack_parts if p)
            if any(kw in haystack for kw in blocked_set):
                continue
            filtered.append(v)
        logger.info(f"硬性过滤后剩余 {len(filtered)} 个视频")
        return filtered
