"""小账本（用 JSON 存在硬盘上）：Raw Pool 里现在躺着哪些视频、每个目标下一趟
几点派送、它们各自已经看过哪些片子，全记在这里。Bot 开机时翻开、改动时合上，
重启也不会失忆。
"""

from __future__ import annotations

import time
from typing import Any

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.storage_api import load_json, save_json

logger = get_logger("bili_sharer_state")

_RAW_POOL_KEY = "raw_pool"
_TARGET_STATES_KEY = "target_states"
_STORE_NAME = "bilibili_sharer"
_MAX_SENT_BVIDS = 1000  # 每个目标最多记 1000 条「已发送」，账本不无限膨胀


def _make_target_key(type_: str, id_: str) -> str:
    """把目标的类型和 ID 拼成一把唯一钥匙，账本靠它认人。"""
    return f"{type_}_{id_}"


class StateManager:
    """Raw Pool 和所有目标状态的管理员；两本账都以 JSON 落盘，重启不失忆。"""

    def __init__(self) -> None:
        self._pool: list[dict[str, Any]] = []
        self._states: dict[str, dict[str, Any]] = {}

    async def load(self) -> None:
        """开机点名：从 JSON 把池子和目标状态两本账翻出来；文件读不出或格式不对就当空白。"""
        pool = await load_json(_STORE_NAME, _RAW_POOL_KEY)
        self._pool = list(pool) if isinstance(pool, list) else []

        states = await load_json(_STORE_NAME, _TARGET_STATES_KEY)
        self._states = dict(states) if isinstance(states, dict) else {}

        logger.info(f"状态加载完成：Raw Pool {len(self._pool)} 个视频，{len(self._states)} 个目标")

    async def save_pool(self) -> None:
        """把视频池这本账写回 JSON。"""
        await save_json(_STORE_NAME, _RAW_POOL_KEY, self._pool)

    async def save_states(self) -> None:
        """把目标状态这本账写回 JSON。"""
        await save_json(_STORE_NAME, _TARGET_STATES_KEY, self._states)

    # ── Raw Pool ──

    def get_all_videos(self) -> list[dict[str, Any]]:
        """交还池子里所有视频的一份拷贝——外面乱改也戳不到真账本。"""
        return list(self._pool)

    async def add_to_pool(self, videos: list[dict[str, Any]], max_size: int = 200) -> int:
        """新视频按 bvid 去重合入池子（在池的就别挤了），返回真正新增了几条。"""
        existing_bvids = {v["bvid"] for v in self._pool}
        added = 0
        for v in videos:
            if v["bvid"] not in existing_bvids:
                self._pool.append(v)
                existing_bvids.add(v["bvid"])
                added += 1

        # 池子超了 max_size 就按抓取时间排队，最老的先下班
        if len(self._pool) > max_size:
            self._pool.sort(key=lambda v: v.get("fetched_at", 0))
            # 淘汰数按「裁剪前的池长 − max_size」算，不能拿本批新增数去减——
            # 那个算法可能算出负数，和真正赶走的数量也毫无关系
            before = len(self._pool)
            self._pool[:] = self._pool[-max_size:]
            logger.debug(
                f"Raw Pool 淘汰 {before - max_size} 个最旧视频"
                f"（池长 {before} → {len(self._pool)}，上限 {max_size}）"
            )

        await self.save_pool()
        logger.info(f"Raw Pool +{added} 新视频，当前 {len(self._pool)} 个")
        return added

    # ── 目标状态 ──

    def get_target_state(self, target_key: str) -> dict[str, Any]:
        """查某个目标的状态；账本上没这号人，就先立一页空白账（时间全 0、没发过任何视频）再交出。"""
        if target_key not in self._states:
            self._states[target_key] = {
                "last_send_time": 0,
                "next_send_time": 0,
                "sent_bvids": [],
            }
        return self._states[target_key]

    async def update_target_state(self, target_key: str, **updates: Any) -> None:
        """改某个目标的状态，随手立刻落盘。"""
        state = self.get_target_state(target_key)
        state.update(updates)
        await self.save_states()

    def get_unwatched_videos(self, target_key: str, pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """从池子里挑出还没寄给过这个目标的视频。"""
        state = self.get_target_state(target_key)
        sent_bvids = set(state.get("sent_bvids", []))
        return [v for v in pool if v["bvid"] not in sent_bvids]

    async def mark_as_sent(self, target_key: str, bvid: str) -> None:
        """记一笔「这片子已寄给过这个目标」，并给历史长度设上限。

        历史保持「先发的在前」的顺序（列表追加，不转集合）：超上限时淘汰最旧的，
        而不是像以前那样「集合转列表后随手砍一刀」——那会随机丢掉几条刚发过的，
        同名视频下次又被推一遍。
        """
        state = self.get_target_state(target_key)
        sent = [item for item in state.get("sent_bvids", []) if item != bvid]
        sent.append(bvid)
        if len(sent) > _MAX_SENT_BVIDS:
            sent = sent[-_MAX_SENT_BVIDS:]
        state["sent_bvids"] = sent
        await self.save_states()
