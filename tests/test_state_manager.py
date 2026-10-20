# -*- coding: utf-8 -*-
"""StateManager 的行为单测（框架层用替身，纯内存 JSON）。

盯住三件事：
1. 池子按 bvid 去重、超上限按 fetched_at 淘汰最旧的；
2. 已发送历史保持「先发在前」的顺序，超 1000 条淘汰最旧的一条
   （旧实现是集合转列表后随机砍，会丢掉刚发过的视频、导致重复推送）；
3. 账本读回脏数据时不炸。
"""

from __future__ import annotations

import asyncio
import unittest

from _stubs import install_storage, import_service

state_manager = import_service("state_manager")
StateManager = state_manager.StateManager
_MAX_SENT_BVIDS = state_manager._MAX_SENT_BVIDS


def run(coro):
    return asyncio.run(coro)


class PoolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.spy = {}
        install_storage(self.spy)
        self.manager = StateManager()

    def test_add_dedupes_by_bvid(self) -> None:
        videos = [
            {"bvid": "BV1", "fetched_at": 1},
            {"bvid": "BV2", "fetched_at": 2},
        ]
        self.assertEqual(run(self.manager.add_to_pool(videos)), 2)
        self.assertEqual(run(self.manager.add_to_pool([{"bvid": "BV1", "fetched_at": 9}])), 0)
        self.assertEqual([v["bvid"] for v in self.manager.get_all_videos()], ["BV1", "BV2"])

    def test_pool_cap_drops_oldest(self) -> None:
        videos = [{"bvid": f"BV{i}", "fetched_at": i} for i in range(10)]
        run(self.manager.add_to_pool(videos, max_size=4))
        kept = self.manager.get_all_videos()
        self.assertEqual(len(kept), 4)
        self.assertEqual([v["bvid"] for v in kept], ["BV6", "BV7", "BV8", "BV9"])

    def test_load_tolerates_dirty_json(self) -> None:
        self.spy["data"] = {"raw_pool": "not-a-list", "target_states": [1, 2, 3]}
        run(self.manager.load())
        self.assertEqual(self.manager.get_all_videos(), [])


class SentHistoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.spy = {}
        install_storage(self.spy)
        self.manager = StateManager()

    def test_mark_as_sent_keeps_order_and_does_not_duplicate(self) -> None:
        key = "group_1"
        for bvid in ("BV1", "BV2", "BV3", "BV2"):
            run(self.manager.mark_as_sent(key, bvid))
        self.assertEqual(
            self.manager.get_target_state(key)["sent_bvids"],
            ["BV1", "BV3", "BV2"],
            "重复发送同一视频时应把它挪到队尾，而不是留下两份",
        )

    def test_trim_keeps_newest(self) -> None:
        key = "group_1"
        state = self.manager.get_target_state(key)
        state["sent_bvids"] = [f"BV{i}" for i in range(_MAX_SENT_BVIDS)]
        run(self.manager.mark_as_sent(key, "BV_NEW"))
        sent = state["sent_bvids"]
        self.assertEqual(len(sent), _MAX_SENT_BVIDS)
        self.assertEqual(sent[-1], "BV_NEW")
        self.assertEqual(sent[0], "BV1", "应淘汰最旧的一条，而不是随机丢")
        self.assertNotIn("BV0", sent)

    def test_unwatched_excludes_sent(self) -> None:
        key = "group_1"
        run(self.manager.mark_as_sent(key, "BV1"))
        pool = [{"bvid": "BV1"}, {"bvid": "BV2"}]
        self.assertEqual([v["bvid"] for v in self.manager.get_unwatched_videos(key, pool)], ["BV2"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
