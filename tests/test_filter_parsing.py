# -*- coding: utf-8 -*-
"""批量判断的解析/构造纯函数单测（不联网、不花钱）。

`_parse_batch_response` 是整条 LLM 过滤链路最容易翻车的地方——模型爱包代码块、
爱把 index 写成 0-based、爱加解说词。这里把几种宽容与「宁可不认」的边界钉死。
"""

from __future__ import annotations

import unittest

from _stubs import import_service

filt = import_service("filter")


class ParseBatchResponseTest(unittest.TestCase):
    def test_plain_json(self) -> None:
        raw = '[{"index": 1, "matches": true, "reason": "对口"}, {"index": 2, "matches": false, "reason": "无关"}]'
        parsed = filt._parse_batch_response(raw, 2)
        self.assertIsNotNone(parsed)
        self.assertEqual([item["matches"] for item in parsed], [True, False])
        self.assertEqual([item["index"] for item in parsed], [0, 1])

    def test_fenced_code_block(self) -> None:
        raw = '```json\n[{"index": 1, "matches": false, "reason": "x"}]\n```'
        parsed = filt._parse_batch_response(raw, 1)
        self.assertIsNotNone(parsed)
        self.assertFalse(parsed[0]["matches"])

    def test_zero_based_index(self) -> None:
        raw = '[{"index": 0, "matches": true, "reason": "a"}, {"index": 1, "matches": true, "reason": "b"}]'
        parsed = filt._parse_batch_response(raw, 2)
        self.assertIsNotNone(parsed)
        self.assertEqual([item["index"] for item in parsed], [0, 1])

    def test_shuffled_index_is_sorted_back(self) -> None:
        raw = '[{"index": 2, "matches": true, "reason": "b"}, {"index": 1, "matches": false, "reason": "a"}]'
        parsed = filt._parse_batch_response(raw, 2)
        self.assertIsNotNone(parsed)
        self.assertEqual([item["matches"] for item in parsed], [False, True])

    def test_chinese_boolean_and_aliases(self) -> None:
        raw = '[{"index": 1, "match": "是", "原因": "好"}, {"index": 2, "matches": "否", "理由": "差"}]'
        parsed = filt._parse_batch_response(raw, 2)
        self.assertIsNotNone(parsed)
        self.assertEqual([item["matches"] for item in parsed], [True, False])

    def test_wrong_count_is_rejected(self) -> None:
        raw = '[{"index": 1, "matches": true, "reason": "a"}]'
        self.assertIsNone(filt._parse_batch_response(raw, 2))

    def test_garbage_is_rejected(self) -> None:
        for raw in ("", "我觉得都不错", "null", "{}", '[{"index": 1}]'):
            with self.subTest(raw=raw):
                self.assertIsNone(filt._parse_batch_response(raw, 1))

    def test_duplicate_index_is_rejected(self) -> None:
        raw = '[{"index": 1, "matches": true, "reason": "a"}, {"index": 1, "matches": false, "reason": "b"}]'
        self.assertIsNone(filt._parse_batch_response(raw, 2))

    def test_surrounding_chatter_is_tolerated(self) -> None:
        raw = '好的，结果如下：[{"index": 1, "matches": true, "reason": "a"}] 以上。'
        parsed = filt._parse_batch_response(raw, 1)
        self.assertIsNotNone(parsed)
        self.assertTrue(parsed[0]["matches"])


class BatchPromptTest(unittest.TestCase):
    def test_prompt_contains_numbered_items_and_contract(self) -> None:
        videos = [
            {"title": "标题一", "description": "描述" * 200, "category": "游戏", "tags": ["独立游戏"]},
            {"title": "标题二", "category": "科技", "tags": []},
        ]
        prompt = filt._build_batch_prompt(videos, "人设", ["游戏", "科技"], ["广告"])
        self.assertIn("共 2 个", prompt)
        self.assertIn("1. 标题：标题一", prompt)
        self.assertIn("2. 标题：标题二", prompt)
        self.assertIn("index 从 1 开始", prompt)
        self.assertIn("广告", prompt)
        # 描述只保留前 120 字，别让批量 prompt 虚胖
        self.assertNotIn("描述" * 200, prompt)

    def test_keyword_fallback_blocks_and_matches(self) -> None:
        matches, reason = filt.VideoFilter._keyword_match(
            {"title": "独立游戏推荐", "description": "", "category": "游戏", "tags": ["独立游戏"]},
            ["独立游戏"],
            ["广告"],
        )
        self.assertTrue(matches)
        self.assertIn("独立游戏", reason)

        blocked, reason = filt.VideoFilter._keyword_match(
            {"title": "这是广告", "description": "", "category": "游戏", "tags": []},
            ["游戏"],
            ["广告"],
        )
        self.assertFalse(blocked)
        self.assertIn("广告", reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
