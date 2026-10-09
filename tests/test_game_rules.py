# -*- coding: utf-8 -*-
"""`src/game_rules.py` 的单元测试 —— **不连库、不需要游戏数据**。

    python -m unittest discover -s tests -v
    python tests/test_game_rules.py

这些用例锁住的是择律的几条口径；每条的背景说明见 `src/game_rules.py`。
数据相关的部分可用 `tools/run_without_db.py` 跑出 before/after 复核对。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import game_rules as GR  # noqa: E402


class TestSongCiValue(unittest.TestCase):
    def test_values(self):
        self.assertEqual(GR.SONGCI_VALUE, {
            "StartCost": 10, "ReplayBaseCost": 1, "ReplayCostGrowth": 1,
            "FreeReplayCount": 0, "WrongThreshold": 2, "OptionCount": 3,
        })


class TestTrigger(unittest.TestCase):
    def test_singer_policies(self):
        # 唱词人 1-8 对应政策 30000-30007 的 TriggerTime
        cases = {80: "every_wrong", 81: "every_correct", 79: "every_correct",
                 78: "every_correct", 82: "on_poet_unlock", 77: "every_draw",
                 0: "startup"}
        for tt, module in cases.items():
            self.assertEqual(GR.trigger_of({"TriggerTime": tt})[0], module, f"TriggerTime={tt}")

    def test_style_limited(self):
        self.assertEqual(GR.trigger_of({"TriggerTime": 78})[1], 1)   # 豪放
        self.assertEqual(GR.trigger_of({"TriggerTime": 79})[1], 2)   # 婉约
        self.assertIsNone(GR.trigger_of({"TriggerTime": 81})[1])

    def test_external_keeps_name(self):
        module, style, name = GR.trigger_of({"TriggerTime": 22})
        self.assertEqual((module, name), ("external", "OccupyCityFirst"))


class TestRewardMultiplier(unittest.TestCase):
    def test_whitelist(self):
        self.assertEqual(GR.REWARD_MULTIPLY_TYPES,
                         frozenset({51, 209, 351, 552, 553, 32510, 32514}))

    def test_scale(self):
        for t in (32510, 32514, 51, 209, 351, 552, 553):
            self.assertEqual(GR.reward_scale(t, 3.0), 3.0, f"{t} 应被放大")
        for t in (204, 757, 590, 32501, 32502, 32515, 32516, 1093):
            self.assertEqual(GR.reward_scale(t, 3.0), 1.0, f"{t} 不应被放大")

    def test_attr_form_covers_whitelist(self):
        # 热路径按属性名判断，两套集合必须一一对应
        self.assertEqual(len(GR.REWARD_MULTIPLY_ATTRS), len(GR.REWARD_MULTIPLY_TYPES))
        self.assertIn("combat_power", {"combat_power"})          # 明确不在白名单
        self.assertNotIn("combat_power", GR.REWARD_MULTIPLY_ATTRS)


class TestReplayEconomy(unittest.TestCase):
    """重听/答错的**游戏规则记录**（不参与模拟，供玩家与自动化评估成本）。"""
    def test_replay_cost_grows_only_by_manual_replay(self):
        # GetReplayCost: growth × ReplayCount + base
        self.assertEqual([GR.replay_cost(n) for n in (0, 1, 2, 5)], [1, 2, 3, 6])

    def test_wrong_answer_flat_without_manual_replay(self):
        # CollectWrongAnswer 只读 ReplayCount（不自增）→ 未主动重听时每次答错固定 base
        self.assertEqual([GR.replay_cost(0) for _ in range(4)], [1, 1, 1, 1])

    def test_no_always_correct_assumption_is_structural(self):
        """文档化前提：不建模答错，因为 3 候选必有正解且数据里每首词都有准确答案。"""
        self.assertEqual(GR.SONGCI_VALUE["OptionCount"], 3)

    def test_ticket_escalation_sequence(self):
        acc, esc, delta = GR.escalate_ticket(0, 1)
        self.assertEqual((acc, esc, delta), (1, 0, 0.0))
        acc, esc, delta = GR.escalate_ticket(acc, 1)      # 1+1 >= 2 → 涨价并清零
        self.assertEqual((acc, esc, delta), (0, 1, 1.0))
        acc, esc, delta = GR.escalate_ticket(acc, 2)      # 一次错 2 次
        self.assertEqual((acc, esc, delta), (0, 1, 1.0))
        acc, esc, delta = GR.escalate_ticket(acc, 4)
        self.assertEqual((acc, esc, delta), (0, 2, 2.0))


class TestSingerEffects(unittest.TestCase):
    """按「池 → 1030 政策」链路解析唱词人效果（修正 effects.py 的占位数据判断）。"""

    POOLS = {451: {"EffectList": [1030], "EffectAddTypeList": [4],
                   "EffectValueList": [30000.0],
                   "EffectDesc": "每次择律错误时，获得歌板+1"}}
    POLICIES = {30000: {"TriggerTime": 80, "EffectList": [32510],
                        "EffectAddTypeList": [4], "EffectValueList": [1.0]}}
    SINGER = {1: {"ID": 1, "FeatureDesc": "每次择律错误时，获得歌板+1",
                  "TriggerCommonEffectPoolConfigId": 451}}

    def test_pool_to_policy_chain(self):
        rule = GR.singer_effects_from_pools(self.SINGER[1], self.POOLS, self.POLICIES)[0]
        self.assertEqual(rule["module"], "every_wrong")
        self.assertEqual(rule["trigger_name"], "ChooseCiWrong")
        self.assertEqual(rule["effects"], [(32510, 4, 1.0)])
        self.assertTrue(rule["matches_feature_desc"])

    def test_pool_carries_effect_directly(self):
        """唱词人 6 的 +10% 挂在池上（政策 30005 的 EffectList 本来就为空）。"""
        pools = {456: {"EffectList": [32513], "EffectAddTypeList": [4],
                       "EffectValueList": [0.1], "EffectDesc": "后续择律收益+10%"}}
        row = {"ID": 6, "FeatureDesc": "后续择律收益+10%",
               "TriggerCommonEffectPoolConfigId": 456}
        rule = GR.singer_effects_from_pools(row, pools, {})[0]
        self.assertEqual(rule["module"], "passive_bonus")
        self.assertEqual(rule["effects"], [(32513, 4, 0.1)])

    def test_singer9_effect_value_is_3_not_4(self):
        """苏轸文案写「歌板+4」，政策 40014 的实际效果值是 3（另有词情 +1/+1）。"""
        pools = {650: {"EffectList": [1030, 1030], "EffectAddTypeList": [4, 4],
                       "EffectValueList": [40014.0, 40036.0], "EffectDesc": ""}}
        policies = {
            40014: {"TriggerTime": 101, "EffectList": [32510, 32515, 32516],
                    "EffectAddTypeList": [4, 4, 4], "EffectValueList": [3.0, 1.0, 1.0]},
            40036: {"TriggerTime": 101, "EffectList": [32514],
                    "EffectAddTypeList": [4], "EffectValueList": [2.0]},
        }
        row = {"ID": 9, "FeatureDesc": "正确择苏轼词时，获得歌板+4、豪放和婉约词情+1",
               "TriggerCommonEffectPoolConfigId": 650}
        value = GR.singer_effect_value(row, pools, policies, 32510)
        self.assertEqual(value, 3.0)


class TestSpecialtyPool(unittest.TestCase):
    class V:
        def __init__(self, vid, style, cipai):
            self.id, self.style, self.ci_pai_name = vid, style, cipai

    def setUp(self):
        self.verses = [self.V(1, 1, "满江红"), self.V(2, 1, "念奴娇"), self.V(3, 2, "如梦令"),
                       self.V(4, 2, "浣溪沙"), self.V(5, 1, "水调歌头")]

    def test_all_marker_means_no_filter(self):
        pool = GR.specialty_pool(self.verses, ["全部"], style=1)
        self.assertEqual(len(pool), len(self.verses))

    def test_style_union_specialty(self):
        # 抽到豪放律 + 擅长「如梦令」-> 豪放全给 + 跨风格的如梦令
        pool = GR.specialty_pool(self.verses, ["如梦令"], style=1, option_count=3)
        ids = sorted(v.id for v in pool)
        self.assertEqual(ids, [1, 2, 3, 5])

    def test_fallback_when_too_small(self):
        # 过滤后不足候选数 -> 回退全量（阈值就是 OptionCount）
        # 这里风格 1 只有 3 条，把阈值抬到 4 才会触发回退
        pool = GR.specialty_pool(self.verses, ["不存在的词牌"], style=1, option_count=4)
        self.assertEqual(len(pool), len(self.verses))
        # 阈值 3 时不做回退，直接用行为正确的 3 条
        pool = GR.specialty_pool(self.verses, ["不存在的词牌"], style=1, option_count=3)
        self.assertEqual(len(pool), 3)


class TestPoetUnlock(unittest.TestCase):
    def test_unlock_by_collected_verse(self):
        poets = {4: {"ID": 4, "MinisterId": 218}}
        self.assertEqual(GR.poet_unlock_targets({"BelongPoetIds": [4, 99]}, poets), [4])

    def test_unlock_by_minister(self):
        """effects.py:943 docstring 描述、但此前未实现的联动。"""
        poets = {4: {"ID": 4, "MinisterId": 218}, 5: {"ID": 5, "MinisterId": 270}}
        self.assertEqual(GR.poet_unlock_by_minister([218], poets), [4])
        self.assertEqual(GR.poet_unlock_by_minister([999], poets), [])


class TestConditionCipher(unittest.TestCase):
    #: 真机样本（ConditionLibConfig.ID=10076），密钥与算法见 ConfigUtils.DecryptXor
    SAMPLE = ("gdHtj/rYjP3igODtl83JkOTZjNXuiN3MifrxiPv2it7nhtbra19XhtL6jtrM"
              "gczNV77D0w==")
    EXPECT = "进攻城市大捷，且战损低于30%，累计2次"

    def test_decrypt(self):
        self.assertEqual(GR.decrypt_condition_desc(self.SAMPLE), self.EXPECT)

    def test_empty(self):
        self.assertEqual(GR.decrypt_condition_desc(""), "")

    def test_unlock_parse_attr(self):
        cond = GR.parse_unlock_condition({"CompleteType": 8,
                                          "CompleteParam": "TechValue;10;99999",
                                          "Negation": 0})
        self.assertEqual(cond, {"kind": "attr", "attr": "tech_point", "min": 10.0})

    def test_unlock_parse_auto(self):
        self.assertEqual(GR.parse_unlock_condition({"CompleteType": 6, "Negation": 1}),
                         {"kind": "auto"})

    def test_unlock_parse_any_of(self):
        texts = {10424: {"CompleteType": 29, "CompleteParam": "3206"},
                 10425: {"CompleteType": 29, "CompleteParam": "3207"}}
        cond = GR.parse_unlock_condition({"CompleteType": 11,
                                          "CompleteParam": "1;10424;10425"}, texts)
        self.assertEqual(cond["kind"], "any_of")
        self.assertEqual([s["policy_id"] for s in cond["subs"]], [3206, 3207])


if __name__ == "__main__":
    unittest.main(verbosity=2)
