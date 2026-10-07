# -*- coding: utf-8 -*-
"""M3 · 效果语义层 (effects.py)

职责:
  1. 把「唱词人」FeatureDesc 编译为确定性规则 (DB 的 Trigger/Remove 池指向军事政策,
     是占位数据 —— 实测 8/9 位唱词人的 FeatureDesc 与 30000-30007 政策描述不匹配,
     且唱词人6「后续择律收益+10%」对应政策 30005 的 EffectList 为空, 故以
     FeatureDesc 为权威语义来源)。
  2. 把「政策」效果编译为: 开局一次性 / 每次择律触发 / 被动加算 三类修正器。
  3. 解析 1030「条件效果」: 其 value 是**子政策 ID**, 条件存在子政策的
     policy_condition.condition_effect / condition_values 上。
     取值语义: condition_values = [A, B] 且 condition_effect = E 时,
       有界区间 (A > B):  A <= state[E] <= B
       下界区间 (A <= B): state[E] >= A
     例: 声声慢 31681 -> E=民心(51), values=[0,39] -> 民心 <= 39;
         明黜陟 30601 -> E=词元(32514), values=[50,99999] -> 词元 >= 50。
  4. 词句池构建与解锁门控。

本模块 import 时不连库; 数据库负载通过 load_* 函数显式触发。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterable, Mapping, Sequence

import config as C
import model as M

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

SU_SHI_POET_ID = 23          # SongCiPoetConfig.ID = 23 -> 苏轼
SU_SHI_POET_NAME = "苏轼"

# 量词效果: 不映射到 ATTRIBUTE_LABELS, 仅计数/记录 (设计文档 §4.3)
# 注: 「军规」不是 EffectType 而是 881 子 ID 28399, 由 simulator 的 grant 分支处理,
#     故此处不再列 213 (213 真名是「降低敌军规模」, 与本项目无关)。
PROSPERITY_EFFECT_IDS: frozenset[int] = frozenset({
    C.EFF_PROSPERITY,        # 繁荣 31480 (李清照「如梦令」: 民心-5, 繁荣+1)
    C.EFF_DONGPODAN,         # 东坡食单 (待标注 U3)
    C.EFF_DONGPODAN2,
    C.EFF_VISIT_TIMES,       # 巡访次数
    C.EFF_HANQING,           # 汗青
})

# 无数值语义的「管线」效果: 只表示系统开关/状态/政策引用, 展示与落库都应排除
PIPELINE_EFFECT_IDS: frozenset[int] = frozenset({
    C.EFF_SYSTEM_SWITCH,     # 2807 显示谋主面板 (开启择律系统的标记)
    C.EFF_GRANT_CARD,        # 881 国家状态变更 (军规/变法/繁荣等子 ID)
    C.EFF_CONDITIONAL,       # 1030 添加政策 (引用子政策)
    32850,                   # 添加朝臣
    1039,                    # 兵种解锁
    999, 3200, 3202,         # 特殊解锁标记 / 效果器开关
})

# 已知但不映射为数值属性的效果 (仅计数或忽略)
IGNORED_EFFECT_IDS: frozenset[int] = frozenset({
    C.EFF_SYSTEM_SWITCH,     # 系统开关
    C.EFF_GRANT_CARD,        # 授予卡牌/状态
    # 以下「国政量词」效果本模拟不跟踪其数值 (不参与择律收益统计), 仅计数:
    #   103 金钱产量 / 152 粮食产量 / 202 兵甲钱消耗 / 203 兵甲粮消耗 / 211 军团规模
    # 注: 这些效果在 DB 中既有 add_type=2 (百分比) 也有 add_type=0 (绝对值) 变体,
    #     本模拟不解释其量纲, 统一只计数, 避免把 0.08 这类值错加到别的属性上。
    103, 152, 202, 203, 211,
    # 32850「添加朝臣」: 名臣择律政策里的附带登记, 无对应数值属性, 仅计数
    32850,
    999, 465, 2910, 3033, 3410, 2018, 590, 454, 459, 456, 751, 927, 1093, 32665,
})

# 视为"开局一次性"的词人政策 ID (SongCiPoetConfig.RewardPolicyId)
STARTUP_POET_POLICY_IDS: frozenset[int] = frozenset({
    30008, 30009, 30010, 30011, 30012, 30013, 30014, 30015, 30016, 30017,
    30018, 30019, 30020, 30021, 30022, 30023, 30024, 30027, 30029,
    40021,   # 苏轼词人: 豪放+5 / 婉约+5 / 择律开题消耗-1
})

# 视为"开局一次性"的名臣政策 ID —— 即「该名臣在本模拟中纳入的全部择律政策」。
# 注1: **不含 40014「苏轸效果」** —— 该政策的文案是
#     「正确择苏轼词时 获得歌板+4、豪放和婉约词情+1」, 属于**唱词人9 苏轸的逐次效果**,
#     而非开局一次性。若把它当开局政策, 会导致"开局苏轼白拿歌板+3"。
#     苏轸效果按 SINGER_RULES[9] 的 su_shi 模块逐次结算 (歌板+4, 两种词情各+1)。
# 注2: 「背嵬军」3010-3012 /「声声慢」3168-3170 /「金石录」3171-3172 是后来补入的 ——
#     它们的逐次部分本已由 `MINISTER_DRAW_POLICY_IDS` 生效, 但开局一次性部分缺失,
#     典型后果是李清照拿不到「金石录 ★2」的**文化点+5**。
#     depth=0 效果实测: 3010-3012 = (1039 兵种解锁, 1030); 3168-3170 = 仅 1030;
#     3171 = 仅 1030; 3172 = (552 文化点+5, 1030)。故补入后只有文化点+5 是新增数值。
# 注3: **「示儿」3111-3113 故意不补** —— 其 depth=0 的 `(32513, 0.005)` 是
#     「每 1 词元 +0.5%」的**系数**(即每 10 词元 +5%), 由逐次规则 `benefit_per_10`
#     按当前词元结算; 若当开局一次性补入, 会凭空多出一个固定 +0.5%。
# 注4: 「病牛」3093-3095 已补入 (用户 2026-10-03 给出机制): ★2/★3 的
#     `(881, 28319)` 是「抽到婉约词时同时视为抽到豪放词」开关, 由
#     `load_graceful_as_bold_ministers()` 解析为 GameData.graceful_as_bold_ministers;
#     ★1/★2/★3 的 1030 子政策是「钱粮消耗-10%」(202/203, 本模拟仅计数)。
STARTUP_MINISTER_POLICY_IDS: frozenset[int] = frozenset({
    3010, 3011, 3012, 3013, 3060, 3061, 3062, 3090, 3091, 3092,
    3093, 3094, 3095,
    3108, 3109, 3110,
    3157, 3165, 3166, 3167, 3168, 3169, 3170, 3171, 3172,
    3200, 3201, 3202, 3206, 3207, 3208,
    3230, 3231, 3232, 3233, 3236, 3237, 3238,
})

# 881 子 ID -> 机制说明 (没有数值语义、但会改变模拟行为的"开关型"标记)
GRANT_SUBID_NOTE: dict[int, str] = {
    C.GRANT_SUBID_GRACEFUL_AS_BOLD:
        "抽到婉约词时**同时视为抽到豪放词**（同时 +豪放词情、同时掷豪放词情触发、"
        "同时触发所有「抽到豪放词」条件的政策）",
}

# effect_type -> 模拟属性键 (含繁荣等直通效果)
# 注: 「军规」不是 EffectType 而是 881 子 ID 28399, 见 GRANT_SUBID_ATTRIBUTE。
EFFECT_ATTRIBUTE: dict[int, str] = dict(M.EFFECT_TO_ATTRIBUTE)
EFFECT_ATTRIBUTE.update({
    C.EFF_PROSPERITY: "prosperity",     # 31480 繁荣 (李清照「如梦令」)
})

# 881「授予卡牌/状态」的子 ID -> 模拟属性键。
# 这类子 ID 是独立量词, 不能笼统计入 card_grants, 否则会丢失身份。
GRANT_SUBID_ATTRIBUTE: dict[int, str] = {
    C.GRANT_SUBID_MILITARY_RULE: "military_rule",   # 28399 军规 (陆游「豪放」)
    C.GRANT_SUBID_REFORM: "reform",                 # 30556 变法 (王安石「以词言志」)
    C.GRANT_SUBID_PROSPERITY: "prosperity",         # 31480 繁荣
}

# 热路径查表: effect_type -> 属性键。仅含直通属性效果 (不含 881/1030/词情等特殊效果),
# 供 simulator 内联使用, 避免每条效果一次 dict.get 之外的额外分支。
EFFECT_VALUE_ATTR: dict[int, str] = dict(EFFECT_ATTRIBUTE)
for _special in (C.EFF_GRANT_CARD, C.EFF_CONDITIONAL, C.EFF_BOLD_SENTIMENT,
                 C.EFF_GRACEFUL_SENTIMENT, C.EFF_BENEFIT_BONUS, C.EFF_DRAW_COST_MOD,
                 C.EFF_SYSTEM_SWITCH):
    EFFECT_VALUE_ATTR.pop(_special, None)
del _special

# 词人政策中「逐次生效」者: 其开局一次性部分不结算, 由 DrawRule 每次择律处理
PER_DRAW_POET_POLICY_IDS: frozenset[int] = frozenset({
    30028,      # 王安石: 每次择律正确且 词元∈[10,100] -> 词元-2 / 后续择律收益+1%
                # (其开局一次性部分不结算, 由 DrawRule `wanganshi_30028` 每轮处理)
})


# ---------------------------------------------------------------------------
# 唱词人规则
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SingerRule:
    """一位唱词人的确定性效果规则。module 决定 simulator 如何执行。"""

    singer_id: int
    feature_desc: str
    module: str                       # none|every_correct|on_style|on_poet_unlock|passive_bonus|su_shi
    effect_type: int | None = None
    effect_value: float = 0.0
    style: int | None = None          # on_style 用
    bonus: float = 0.0                # passive_bonus 用
    sentiment_bonus: int = 0          # su_shi 用
    target_poet_id: int | None = None
    target_poet_name: str = ""

    def __post_init__(self) -> None:
        if not self.feature_desc:
            raise ValueError(f"唱词人 {self.singer_id} 缺少 FeatureDesc, 无法推导效果")


_POLICY_ID_RE = re.compile(r"唱词人\s*(\d+)\s*效果")
_UNLOCK_RE = re.compile(r"解锁新词人")
_PASSIVE_RE = re.compile(r"后续择律收益\s*([+-]?\d+(?:\.\d+)?)\s*%")
_NUM_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)")


def parse_singer_rule(singer_id: int, feature_desc: str) -> SingerRule:
    """把 FeatureDesc 解析为 SingerRule。未识别时抛 ValueError。"""
    d = (feature_desc or "").replace("\r", "").replace("\n", "").strip()
    if not d:
        raise ValueError(f"唱词人 {singer_id} FeatureDesc 为空")

    if _UNLOCK_RE.search(d):
        nums = _NUM_RE.findall(d)
        val = float(nums[-1]) if nums else 0.0
        return SingerRule(singer_id=singer_id, feature_desc=d, module="on_poet_unlock",
                          effect_type=C.EFF_BOARD, effect_value=val)

    m = _PASSIVE_RE.search(d)
    if m:
        return SingerRule(singer_id=singer_id, feature_desc=d, module="passive_bonus",
                          bonus=float(m.group(1)) / 100.0)

    if "苏轼" in d:
        nums = [float(x) for x in _NUM_RE.findall(d)]
        val = nums[0] if nums else 0.0
        sent = nums[1] if len(nums) > 1 else 0.0
        return SingerRule(singer_id=singer_id, feature_desc=d, module="su_shi",
                          effect_type=C.EFF_BOARD, effect_value=val,
                          sentiment_bonus=int(sent),
                          target_poet_id=SU_SHI_POET_ID, target_poet_name=SU_SHI_POET_NAME)

    # 抽出效果类型与数值: 支持 歌板/词元/军心/民心/战斗力/腐化/威望/文化点
    eff_type: int | None = None
    for kw, et in (("歌板", C.EFF_BOARD), ("词元", C.EFF_VERSE_POINT),
                   ("军心", C.EFF_ARMY_MORALE), ("民心", C.EFF_POPULAR_SUPPORT),
                   ("战斗力", C.EFF_COMBAT_POWER), ("腐化", C.EFF_CORRUPTION),
                   ("威望", C.EFF_REPUTATION), ("文化点", C.EFF_CULTURE_POINT)):
        if kw in d:
            eff_type = et
            break
    if eff_type is None:
        raise ValueError(f"唱词人 {singer_id} 无法解析效果类型: {d!r}")

    nums = _NUM_RE.findall(d)
    val = float(nums[-1]) if nums else 0.0
    sign = -1.0 if re.search(r"[-−]\s*\d", d) else 1.0
    val = val * sign

    if "错误" in d:
        # 本模拟无错误分支 (用户 Q3), 效果永不触发
        return SingerRule(singer_id=singer_id, feature_desc=d, module="none")

    if "正确择豪放律" in d:
        return SingerRule(singer_id=singer_id, feature_desc=d, module="on_style",
                          effect_type=eff_type, effect_value=val, style=M.STYLE_BOLD)
    if "正确择婉约律" in d:
        return SingerRule(singer_id=singer_id, feature_desc=d, module="on_style",
                          effect_type=eff_type, effect_value=val, style=M.STYLE_GRACEFUL)
    if "正确" in d:
        return SingerRule(singer_id=singer_id, feature_desc=d, module="every_correct",
                          effect_type=eff_type, effect_value=val)
    return SingerRule(singer_id=singer_id, feature_desc=d, module="every_correct",
                      effect_type=eff_type, effect_value=val)


def build_singer_rules(features: Mapping[int, str]) -> dict[int, SingerRule]:
    return {sid: parse_singer_rule(sid, desc) for sid, desc in features.items()}


def load_singer_features() -> dict[int, str]:
    import db
    rows = db.fetch_all("SELECT id, feature_desc FROM singer ORDER BY id")
    return {int(r[0]): (r[1] or "") for r in rows}


def load_singer_names() -> dict[int, str]:
    """唱词人 ID -> 名称 (供日志/文案显示)。"""
    import db
    return {int(r[0]): (r[1] or "") for r in db.fetch_all("SELECT id, name FROM singer")}


# 模块级只读配方表 (测试可独立使用, 不触发连库)
SINGER_RULES: dict[int, SingerRule] = {
    1: SingerRule(singer_id=1, feature_desc="每次择律错误时，获得歌板+1", module="none"),
    2: SingerRule(singer_id=2, feature_desc="每次择律正确时，获得歌板+1",
                  module="every_correct", effect_type=C.EFF_BOARD, effect_value=1.0),
    3: SingerRule(singer_id=3, feature_desc="正确择婉约律时，获得词元+2",
                  module="on_style", effect_type=C.EFF_VERSE_POINT, effect_value=2.0,
                  style=M.STYLE_GRACEFUL),
    4: SingerRule(singer_id=4, feature_desc="每次解锁新词人时，获得歌板+2",
                  module="on_poet_unlock", effect_type=C.EFF_BOARD, effect_value=2.0),
    5: SingerRule(singer_id=5, feature_desc="每次择律正确时，获得词元+2",
                  module="every_correct", effect_type=C.EFF_VERSE_POINT, effect_value=2.0),
    6: SingerRule(singer_id=6, feature_desc="后续择律收益+10%",
                  module="passive_bonus", bonus=0.10),
    7: SingerRule(singer_id=7, feature_desc="正确择豪放律时，获得歌板+1",
                  module="on_style", effect_type=C.EFF_BOARD, effect_value=1.0,
                  style=M.STYLE_BOLD),
    8: SingerRule(singer_id=8, feature_desc="正确择婉约律时，获得歌板+1",
                  module="on_style", effect_type=C.EFF_BOARD, effect_value=1.0,
                  style=M.STYLE_GRACEFUL),
    9: SingerRule(singer_id=9, feature_desc="正确择苏轼词时，获得歌板+4、豪放和婉约词情+1",
                  module="su_shi", effect_type=C.EFF_BOARD, effect_value=4.0,
                  sentiment_bonus=1, target_poet_id=SU_SHI_POET_ID,
                  target_poet_name=SU_SHI_POET_NAME),
}


def singer_rule(singer_id: int | None) -> SingerRule | None:
    if singer_id is None:
        return None
    return SINGER_RULES.get(int(singer_id))


# ---------------------------------------------------------------------------
# 效果规格与修正器
# ---------------------------------------------------------------------------

@dataclass
class ModifierSet:
    """一个开局配置编译出的全部修正。"""

    startup: dict[int, float] = field(default_factory=dict)         # effect_type -> 累计值 (开局一次性)
    startup_attrs: dict[str, float] = field(default_factory=dict)   # attribute -> 累计加算值
    startup_mults: dict[str, float] = field(default_factory=dict)   # attribute -> 累计百分比倍率 (add_type=2)
    per_draw: dict[int, float] = field(default_factory=dict)        # effect_type -> 每次择律固定增量
    benefit_bonus: float = 0.0                                      # 累加式百分比
    draw_cost_mod: float = 0.0                                      # 每次择律歌板消耗修正
    sentiment_bonus_bold: int = 0                                   # 开局词情层数
    sentiment_bonus_graceful: int = 0
    counted_effects: dict[int, float] = field(default_factory=dict)  # 量词效果累计

    _bonus_layers: int = 0

    # ---- 构建 ----

    def apply_startup(self, effect_type: int, value: float,
                      add_type: int = C.ADD_TYPE_ADDITIVE) -> None:
        """开局一次性效果。

        `add_type=2` 为百分比乘算 (0.08 = +8%), 记入 `startup_mults`,
        结算时按 `值 × (1+Σ倍率)` 处理, 不能当绝对值相加。
        """
        self.startup[effect_type] = self.startup.get(effect_type, 0.0) + float(value)
        attr = EFFECT_ATTRIBUTE.get(int(effect_type))
        if attr:
            if int(add_type) == C.ADD_TYPE_MULTIPLIER:
                self.startup_mults[attr] = self.startup_mults.get(attr, 0.0) + float(value)
            else:
                self.startup_attrs[attr] = self.startup_attrs.get(attr, 0.0) + float(value)

    def apply_per_draw(self, effect_type: int, value: float) -> None:
        self.per_draw[effect_type] = self.per_draw.get(effect_type, 0.0) + float(value)

    def add_benefit_bonus(self, value: float, cap: int | None = None) -> None:
        """累加收益加成。cap 为「至多 N 层」时最多累加 cap 次。"""
        if cap is not None and self._bonus_layers >= int(cap):
            return
        self.benefit_bonus += float(value)
        self._bonus_layers += 1

    def add_draw_cost_mod(self, value: float) -> None:
        self.draw_cost_mod += float(value)

    def add_sentiment_bonus(self, bold: int = 0, graceful: int = 0) -> None:
        self.sentiment_bonus_bold += int(bold)
        self.sentiment_bonus_graceful += int(graceful)

    def count_effect(self, effect_type: int, value: float) -> None:
        self.counted_effects[effect_type] = self.counted_effects.get(effect_type, 0.0) + float(value)

    # ---- 合并 ----

    def merge(self, other: "ModifierSet") -> "ModifierSet":
        """把 other 合并进 self (返回 self)。不得修改 other。"""
        for k, v in other.startup.items():
            self.startup[k] = self.startup.get(k, 0.0) + v
        for k, v in other.startup_attrs.items():
            self.startup_attrs[k] = self.startup_attrs.get(k, 0.0) + v
        for k, v in other.startup_mults.items():
            self.startup_mults[k] = self.startup_mults.get(k, 0.0) + v
        for k, v in other.per_draw.items():
            self.per_draw[k] = self.per_draw.get(k, 0.0) + v
        for k, v in other.counted_effects.items():
            self.counted_effects[k] = self.counted_effects.get(k, 0.0) + v
        self.benefit_bonus += other.benefit_bonus
        self._bonus_layers += other._bonus_layers
        self.draw_cost_mod += other.draw_cost_mod
        self.sentiment_bonus_bold += other.sentiment_bonus_bold
        self.sentiment_bonus_graceful += other.sentiment_bonus_graceful
        return self

    def copy(self) -> "ModifierSet":
        return ModifierSet(
            startup=dict(self.startup),
            startup_attrs=dict(self.startup_attrs),
            startup_mults=dict(self.startup_mults),
            per_draw=dict(self.per_draw),
            benefit_bonus=self.benefit_bonus,
            draw_cost_mod=self.draw_cost_mod,
            sentiment_bonus_bold=self.sentiment_bonus_bold,
            sentiment_bonus_graceful=self.sentiment_bonus_graceful,
            counted_effects=dict(self.counted_effects),
            _bonus_layers=self._bonus_layers,
        )


# ---------------------------------------------------------------------------
# 每次择律触发的政策规则
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DrawRule:
    """一条「每次择律」触发的政策规则。

    gate:
      ""              -> 总是
      "bold"/"graceful" -> 仅当抽中该风格
      "verse_point"   -> 需要词元阈值 (由 min_value 指定)
      "culture_point" -> 需要文化点阈值
      "popular_support" -> 需要民心阈值
    handler:
      "grant"          -> 给 effects (受收益加成)
      "grant_unscaled" -> 给 effects (不受收益加成)
      "benefit_per_10" -> 每 10 点某属性 +pct, 上限 max_layers
      "benefit_missing"-> 每低于 threshold 10 点 +pct, 上限 max_layers

    max_grants:
      该规则本局最多触发次数 (0 = 不限)。用于落实政策文案里的「至多N层」/「上限X%」。
      例: 陆游「豪放」军规+1%(至多20层) -> 20;
          岳飞「背嵬军」战斗力+8%(上限24%) -> 3 次 (3×8%=24%)。

    min_attr:
      **附加**属性条件 (与 gate 同时生效)。gate 只能表达一个条件, 而
      李清照「如梦令」31651 需要同时满足「抽到婉约词」**且**「民心达到40」
      (DB: condition_effect=51, condition_values=[40, 99999]), 故用本字段补充。

    base_layers:
      仅 `benefit_missing` 使用: 满足「低于 threshold」时的**起始层数**。
      李清照「声声慢」文案为「若民心低于N，后续择律收益+10%；每低10点，+10%」,
      即 N 层 = base(1) + floor(缺额/10)。

    max_value:
      条件属性的**上限**(0 = 不设上限)。王安石词人政策 `30028` 的
      `Condition_Values=[10, 100]` 即「词元 10 ~ 100 之间」才触发。
    """

    rule_id: str
    source_policy_id: int
    gate: str = ""
    min_value: float = 0.0
    max_value: float = 0.0
    gate_attr: str = ""
    min_attr: str = ""
    handler: str = "grant"
    effects: tuple[tuple[int, float], ...] = ()
    attr_key: str = ""
    pct: float = 0.0
    per: float = 10.0
    max_layers: int = 10
    base_layers: int = 0
    threshold: float = 0.0
    max_grants: int = 0
    label: str = ""


def filter_draw_rules_by_star(rules: Sequence["DrawRule"]) -> tuple["DrawRule", ...]:
    """按「同一政策的不同星级不能并存, 只保留最好的」(用户 2026-10-03 确认) 过滤逐次规则。

    实测结构 (`PolicyConfig`):
      * 同一 `PolicyName` 对应多个 `ID`, 各带不同 `StarCnt` —— 即为不同星级的同名政策。
        例: 豪放 = 3108(★1,+20) / 3109(★2,+25) / 3110(★3,+30);
            背嵬军 = 3010(★1) / 3011(★2) / 3012(★3);
            声声慢 = 3168(★1) / 3169(★2) / 3170(★3)。
      * 星级的替换关系通过 `1030`「条件效果」表达: 父政策(星级档)引用自己的子政策。
          - 背嵬军  ★1→31407, ★2→31408, ★3→31409   (每档引用不同子政策)
          - 声声慢  ★1→31681/31682, ★2→31683/31684, ★3→31685/31686
          - 金石录  ★1→31711/31712, ★2→31711/31712   (两档引用**相同**子政策, 不存在替换)
          - 示儿    ★1→31111, ★2→31112, ★3→31113

    因此对每条规则判定:
      1. 唱词人效果池规则: 独立政策, 不参与星级去重 → 保留
      2. 规则源政策本身是多星级家族的成员 (如声声慢以 3168/3169/3170 为源):
         仅最高星级成员生效 → 只有 `source == 家族最高星级 ID` 才保留
      3. 规则源政策是某个 `1030` 被引用项:
         只有当**引用它的最高星级父政策**确实引用了它时才生效。
         若更高星级的同族父政策引用了**另一组**子政策 (整档替换), 则本档丢弃;
         若更高星级父政策引用的是**同一组**子政策 (如金石录两档同子), 则本档保留。
      4. 其余 (无父政策的独立规则, 如 30051-30054) → 保留

    DB 不可用时返回全表, 保证模块导入与单元测试可离线运行。
    """
    import db
    rules = list(rules)
    if not rules:
        return ()
    try:
        fams = db.fetch_all("""
            SELECT policy_name,
                   (array_agg(id ORDER BY star_cnt DESC NULLS LAST, id DESC))[1] AS best_id,
                   (array_agg(star_cnt ORDER BY star_cnt DESC NULLS LAST))[1] AS best_star
            FROM policy GROUP BY policy_name""")
        multi = db.fetch_all("""
            SELECT policy_name FROM policy
            GROUP BY policy_name HAVING count(DISTINCT star_cnt) > 1""")
        edges = db.fetch_all("""
            SELECT p.id AS parent_id, p.policy_name, p.star_cnt,
                   pe.effect_value::int AS child_id
            FROM policy_effect pe JOIN policy p ON p.key_id = pe.policy_key_id
            WHERE pe.effect_type = 1030 AND pe.depth = 0 AND pe.effect_value IS NOT NULL""")
    except Exception:
        return tuple(rules)

    best_by_name = {r[0]: r[1] for r in fams}          # 家族 -> 最高星级 ID
    multi_names = {r[0] for r in multi}

    # 父政策 -> 其子政策集合 ; 子政策 -> 引用它的父政策集合
    children_of: dict[int, set[int]] = {}
    parents_of: dict[int, set[int]] = {}
    star_of: dict[int, int] = {}
    name_of: dict[int, str] = {}
    for pid, pname, star, cid in edges:
        pid, cid = int(pid), int(cid)
        children_of.setdefault(pid, set()).add(cid)
        parents_of.setdefault(cid, set()).add(pid)
        star_of[pid] = int(star or 0)
        name_of[pid] = pname

    # 规则源政策是否本身就是某个多星级家族的成员
    fam_of_src: dict[int, str] = {}
    for r in db.fetch_all("SELECT id, policy_name FROM policy"):
        if r[1] in multi_names:
            fam_of_src.setdefault(int(r[0]), r[1])

    out: list[DrawRule] = []
    for r in rules:
        if r.rule_id.startswith("singer"):
            out.append(r)
            continue
        src = r.source_policy_id

        # 情形 4: 无父政策的独立规则 → 保留
        # (先判此项, 以便把「被父政策引用的子政策」留给情形 3 处理)
        if src not in parents_of:
            # 情形 2: 源政策本身就是一个多星级家族成员 (如声声慢 3168/3169/3170),
            #         它不是任何父政策的子政策, 故这里按「只保留最高星级」处理
            fam = fam_of_src.get(src)
            if fam is None or best_by_name.get(fam) == src:
                out.append(r)
            continue

        # 情形 3: src 是被 `1030` 引用的子政策 → 看最高星级父政策是否仍引用它
        refs = parents_of[src]
        fam_name = name_of.get(max(refs, key=lambda p: star_of.get(p, 0)))
        best_id = best_by_name.get(fam_name)
        if best_id is None:
            out.append(r)
            continue
        if best_id in refs:
            # 最高星级父政策**确实引用了本子政策** → 保留 (与档位无关)。
            # 例: 以词言志 ★4(3233)→32301; 尚古文 ★3(3238)→32360; 明黜陟 ★3(3062)→30621;
            #     金石录 ★2(3172)→31711/31712; 豪放 ★3(3110)→31081;
            #     如梦令 ★3(3167)→31651; 十事 ★3(3092)→30902。
            out.append(r)
            continue
        # 最高星级父政策引用了**另一组**子政策 → 本档被整档替换, 丢弃。
        # 例: 恋情词 ★3(3202)→31641, 而 31621/31631 仅被 ★1/★2 引用;
        #     背嵬军 ★3(3012)→31409, 而 31407/31408 仅被 ★1/★2 引用。
        if max(star_of.get(p, 0) for p in refs) < star_of.get(best_id, 0):
            continue
        out.append(r)
    return tuple(out)


@lru_cache(maxsize=1)
def build_draw_rules() -> tuple[DrawRule, ...]:
    """宋词相关的「每次择律」政策规则表 (依据设计文档 §4.3 + DB 实测效果)。

    经 `filter_draw_rules_by_star` 按最高星级过滤 (连库时); DB 不可用时返回全表,
    以保持单元测试可离线运行。

    注: 结果**进程内缓存** —— 该表是静态只读数据, 而过滤要连库 (单次约 0.4 s);
    不缓存时 GUI 生成 21+10 条勾选项文案会重复调用, 启动要 50 s 以上 (实测)。
    """
    return filter_draw_rules_by_star(_all_draw_rules())


def _all_draw_rules() -> tuple[DrawRule, ...]:
    R = DrawRule
    return (
        # ---- 唱词人效果池 (30000-30007 / 40014 / 40036) ----
        R("singer_30000", 30000, gate="", handler="none", label="唱词人1 错误效果(不触发)"),
        R("singer_30001", 30001, handler="grant", effects=((C.EFF_BOARD, 1.0),),
          label="唱词人2 每次择律 歌板+1"),
        R("singer_30002", 30002, gate="graceful", handler="grant",
          effects=((C.EFF_VERSE_POINT, 2.0),), label="唱词人3 正确择婉约律 词元+2"),
        R("singer_30003", 30003, handler="none", label="唱词人4 解锁新词人 歌板+2(由唱词人规则处理)"),
        R("singer_30004", 30004, handler="grant", effects=((C.EFF_VERSE_POINT, 2.0),),
          label="唱词人5 每次择律正确 词元+2"),
        R("singer_30005", 30005, handler="none", label="唱词人6 后续收益+10%(由唱词人规则处理)"),
        R("singer_30006", 30006, gate="bold", handler="grant",
          effects=((C.EFF_BOARD, 1.0),), label="唱词人7 正确择豪放律 歌板+1"),
        R("singer_30007", 30007, gate="graceful", handler="grant",
          effects=((C.EFF_BOARD, 1.0),), label="唱词人8 正确择婉约律 歌板+1"),
        R("singer_40036", 40036, handler="grant", effects=((C.EFF_VERSE_POINT, 2.0),),
          label="苏轸 每次择律正确 词元+2"),

        # ---- 词人政策 (30008-30029, 40021) 的逐次部分 ----
        # 王安石 词人政策 30028: 文案「每次择律正确时，若词元达到10，词元-2，后续择律收益+1%」。
        # `TriggerTime=81`(每次择律正确) -> 真正的逐次规则。
        # DB `Condition_Values=[10, 100]` 里的 100 **按用户 2026-10-03 裁定视为无上限**
        # (全库同类 16 条里 15 条写 9999999/99999; 词元自身 MaxValue=-1)。
        # 注: 这是**词人**政策, 与名臣「以词言志」(3233→32301, 词元≥8 -> 词元-8) 是两条独立效果;
        #     早期只实现了名臣那条, 词人这条被漏掉, 导致王安石完全没有 +1%/次 的择律收益。
        R("wanganshi_30028", 30028, gate="verse_point", min_value=10.0,
          handler="grant_unscaled",
          effects=((C.EFF_VERSE_POINT, -2.0, 4), (C.EFF_BENEFIT_BONUS, 0.01, 4)),
          label="王安石词人: 词元≥10 词元-2 后续择律收益+1% (无上限)"),
        R("wanganshi_32300", 32300, gate="verse_point", min_value=8.0, handler="grant",
          effects=((C.EFF_VERSE_POINT, -8.0), (C.EFF_REPUTATION, -10.0), (C.EFF_GRANT_CARD, 30556.0)),
          label="王安石 以词言志(威望-10)"),
        R("wanganshi_32301", 32301, gate="verse_point", min_value=8.0, handler="grant",
          effects=((C.EFF_VERSE_POINT, -8.0), (C.EFF_REPUTATION, -5.0), (C.EFF_GRANT_CARD, 30556.0)),
          label="王安石 以词言志(威望-5)"),

        # ---- 词人政策里「TriggerTime=0 但带条件」的 4 条 ----
        # 用户 2026-10-03 裁定: 这 4 条是「条件首次满足时**一次性**生效」,
        # 不是每轮触发 -> 用 `max_grants=1` 落实 (否则「军心+10」会被发上千次)。
        # 30051 陆游词人: 若词元达到50，钱粮消耗-5%
        # 30052 贺铸词人: 词元达到100时，军队规模+8%
        # 30053 范仲淹词人: 词元达到40时，军队规模+4%
        # 30054 李纲词人: 若词元达到30，军心+10
        R("luyou_30051", 30051, gate="verse_point", min_value=50.0, handler="grant_unscaled",
          max_grants=1,
          effects=((203, -0.05, 2), (202, -0.05, 2), (203, -0.05, 2), (202, -0.05, 2)),
          label="陆游词人: 词元≥50 钱粮消耗-5% (一次性)"),
        R("hezhu_30052", 30052, gate="verse_point", min_value=100.0, handler="grant_unscaled",
          max_grants=1,
          # 211 军团规模: EffectTypeConfig.MinValue=40 即 1%, DB 实测值 320 = +8%
          effects=((211, 320.0, 0),), label="贺铸词人: 词元≥100 军队规模+8% (一次性)"),
        R("fanzhongyan_30053", 30053, gate="verse_point", min_value=40.0, handler="grant_unscaled",
          max_grants=1,
          effects=((211, 160.0, 0),), label="范仲淹词人: 词元≥40 军队规模+4% (一次性)"),
        R("ligang_30054", 30054, gate="verse_point", min_value=30.0, handler="grant_unscaled",
          max_grants=1,
          effects=((C.EFF_ARMY_MORALE, 10.0),), label="李纲词人: 词元≥30 军心+10 (一次性)"),

        # ---- 名臣择律政策 (WordTypeList=15 的逐次部分) ----
        # 陆游「豪放」: 军规+1%(至多20层)。军规不是 EffectType, 而是 881 子 ID 28399,
        # 故以 EFF_GRANT_CARD + 子 ID 表达, 由 GRANT_SUBID_ATTRIBUTE 映射为 military_rule。
        R("luyou_31081", 31081, gate="bold", handler="grant_unscaled",
          effects=((C.EFF_GRANT_CARD, float(C.GRANT_SUBID_MILITARY_RULE)),),
          max_grants=20,
          label="陆游 豪放: 军规+1% (至多20层)"),
        R("qingzhaozhao_31651", 31651, gate="graceful",
          min_attr="popular_support", min_value=40.0,
          handler="grant_unscaled",
          effects=((C.EFF_POPULAR_SUPPORT, -5.0), (C.EFF_PROSPERITY, 1.0)),
          label="李清照 如梦令: 抽到婉约词且民心≥40 民心-5 繁荣+1"),
        # 注: 李清照「金石录」的 31711 分支是**择律失败**效果
        #     (「错择 文化点-1 歌板+3」)。因用户确认「默认无条件正确」,
        #     不存在失败分支, 故该规则不纳入实现。
        R("qingzhaozhao_31712", 31712, gate="verse_point", min_value=10.0, handler="grant_unscaled",
          effects=((C.EFF_VERSE_POINT, -3.0), (C.EFF_BOARD, 3.0)),
          label="李清照 金石录: 词元-3 歌板+3"),
        R("liuyong_31621", 31621, gate="graceful", handler="grant_unscaled",
          effects=((C.EFF_BOARD, 2.0),), label="柳永 恋情词(1级): 歌板+2"),
        R("liuyong_31631", 31631, gate="graceful", handler="grant_unscaled",
          effects=((C.EFF_BOARD, 3.0),), label="柳永 恋情词(2级): 歌板+3"),
        R("liuyong_31641", 31641, gate="graceful", handler="grant_unscaled",
          effects=((C.EFF_BOARD, 4.0),), label="柳永 恋情词(3级): 歌板+4"),
        R("ouyangxiu_32360", 32360, gate="culture_point", min_value=2.0, handler="grant_unscaled",
          effects=((C.EFF_CULTURE_POINT, -2.0), (C.EFF_BOARD, 4.0), (C.EFF_REPUTATION, 4.0)),
          label="欧阳修 尚古文: 文化点-2 歌板+4 威望+4"),
        # 注: 李纲「十事」(30901/30902) **不纳入实现** —— 其 TriggerTime=27
        #     =「每当完成政策后」，不是择律事件；当逐次规则会每轮白送资源。
        #     (用户 2026-10-03 裁定「李纲的政策错误要停掉」)
        R("yuefei_31407", 31407, gate="verse_point", min_value=10.0, handler="grant_unscaled",
          effects=((C.EFF_VERSE_POINT, -10.0), (C.EFF_COMBAT_POWER, 0.04)),
          label="岳飞 背嵬军(1级): 词元-10 战斗力+4%"),
        R("yuefei_31408", 31408, gate="verse_point", min_value=10.0, handler="grant_unscaled",
          effects=((C.EFF_VERSE_POINT, -10.0), (C.EFF_COMBAT_POWER, 0.06)),
          label="岳飞 背嵬军(2级): 词元-10 战斗力+6%"),
        R("yuefei_31409", 31409, gate="verse_point", min_value=10.0, handler="grant_unscaled",
          # 战斗力 add_type=2 -> 百分比乘算: +8% 即 ×1.08 (不是绝对值 +0.08)
          # 上限: 文案 1/2 星级写「上限12%」「上限18%」, 即每档固定 3 次 (3×8%=24%)
          effects=((C.EFF_VERSE_POINT, -10.0, 4), (C.EFF_COMBAT_POWER, 0.08, 2)),
          max_grants=3,
          label="岳飞 背嵬军(3级): 词元-10 战斗力+8% (上限24%, 即至多3次)"),
        R("fanzhongyan_30601", 30601, gate="verse_point", min_value=50.0, handler="grant_unscaled",
          effects=((C.EFF_VERSE_POINT, -12.0), (454, 1.0)),
          label="范仲淹 明黜陟: 词元-12 士子+1"),
        R("fanzhongyan_30621", 30621, gate="verse_point", min_value=50.0, handler="grant_unscaled",
          effects=((C.EFF_VERSE_POINT, -8.0), (454, 1.0)),
          label="范仲淹 明黜陟: 词元-8 士子+1"),

        # ---- 词人政策 30008-30029 的条件子政策 ----
        # (陆游 30051 / 贺铸 30052 / 范仲淹 30053 / 李纲 30054 已上移到
        #  「TriggerTime=0 + 条件 → 一次性」那一组, 每个只能触发 1 次)

        # ---- 累积型收益加成 ----
        R("luyou_3111", 3111, handler="benefit_per_10", attr_key="verse_point",
          pct=0.05, per=10.0, max_layers=10, label="陆游 示儿: 每10词元 +5% 收益(至多10层)"),
        R("luyou_3112", 3112, handler="benefit_per_10", attr_key="verse_point",
          pct=0.05, per=10.0, max_layers=10, label="陆游 示儿: 每10词元 +5% 收益(至多10层)"),
        R("luyou_3113", 3113, handler="benefit_per_10", attr_key="verse_point",
          pct=0.05, per=10.0, max_layers=10, label="陆游 示儿: 每10词元 +5% 收益(至多10层)"),
        # 声声慢 (李清照): 文案「若民心低于N，后续择律收益+10%；每低10点，+10%」
        # DB 实测子政策: 31681(+0.5)/31682(-0.01) 等, 净额 = (0.4+0.1N) - 0.01×民心,
        # 即 民心=0 时 ★1/★2/★3 = +50%/+60%/+70% -> 层数 = base(1) + floor(缺额/10)。
        R("qingzhaozhao_3168", 3168, handler="benefit_missing", attr_key="popular_support",
          pct=0.10, per=10.0, base_layers=1, max_layers=5, threshold=40.0,
          label="李清照 声声慢: 民心低于40 +10%, 每低10点再+10% (至多5层)"),
        R("qingzhaozhao_3169", 3169, handler="benefit_missing", attr_key="popular_support",
          pct=0.10, per=10.0, base_layers=1, max_layers=6, threshold=50.0,
          label="李清照 声声慢: 民心低于50 +10%, 每低10点再+10% (至多6层)"),
        R("qingzhaozhao_3170", 3170, handler="benefit_missing", attr_key="popular_support",
          pct=0.10, per=10.0, base_layers=1, max_layers=7, threshold=60.0,
          label="李清照 声声慢: 民心低于60 +10%, 每低10点再+10% (至多7层)"),
    )


# ---------------------------------------------------------------------------
# 开局一次性政策
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StartupPolicy:
    policy_id: int
    effects: tuple[tuple[int, float, int], ...]
    label: str = ""

    def effect_value(self, effect_type: int) -> float:
        """按效果类型取数值 (同类型重复登记时累加, 与 DB 结算口径一致)。"""
        return sum(float(v) for t, v, *_ in self.effects if int(t) == int(effect_type))

    def effect_map(self) -> dict[int, float]:
        """效果类型 -> 累加数值。"""
        out: dict[int, float] = {}
        for t, v, *_ in self.effects:
            out[int(t)] = out.get(int(t), 0.0) + float(v)
        return out


# 词人政策 30008-30029 / 40021 的开局一次性效果 (**照抄 DB `policy_effect` 的
# depth=0 数值行, 含 add_type**; 重复登记的行也照抄, 由 display.format_effects 合并为 ×N)。
# 与 DB 查询口径一致地排除 2807(系统开关)/881(国家状态变更)/1030(条件效果) 等管线效果。
# 注: 本表只用于**展示/文档**, 模拟结算走 `load_startup_policy_mods()` 直读 DB。
STARTUP_POLICIES: dict[int, StartupPolicy] = {
    30008: StartupPolicy(30008, ((211, 400.0, 4), (553, -20.0, 4)),
                         "岳飞: 军团规模+10% 威望-20"),
    30009: StartupPolicy(30009, ((204, 25.0, 4), (553, -20.0, 4)),
                         "辛弃疾: 战斗力+25 威望-20"),
    30010: StartupPolicy(30010, ((757, 0.69445, 4), (32510, 5.0, 4)),
                         "柳永: 发展年数+0.7 歌板+5"),
    30011: StartupPolicy(30011, ((32510, 12.0, 4), (51, -10.0, 4)),
                         "李清照: 歌板+12 民心-10"),
    30012: StartupPolicy(30012, ((32510, 5.0, 4),),
                         "张孝祥: 歌板+5 (另登记 881 子ID 29338)"),
    30013: StartupPolicy(30013, ((152, 0.07, 2), (757, 0.69445, 4)),
                         "范成大: 粮食产量+7% 发展年数+0.7"),
    30014: StartupPolicy(30014, ((211, 160.0, 0), (209, 5.0, 4), (351, 10.0, 4)),
                         "陈亮: 军团规模+4% 军心+5 腐化+10"),
    30015: StartupPolicy(30015, ((553, 10.0, 4), (152, 0.05, 2), (103, 0.05, 2)),
                         "晏殊: 威望+10 粮食产量+5% 金钱产量+5%"),
    # DB 中 202/203 各登记两次 -> 展示为 ×2 (不是文案重复)
    30016: StartupPolicy(30016, ((32510, 12.0, 4), (203, 0.03, 2), (202, 0.03, 2),
                                 (203, 0.03, 2), (202, 0.03, 2)),
                         "晏几道: 歌板+12 兵甲粮/兵甲钱消耗+3%×2"),
    30017: StartupPolicy(30017, ((209, 5.0, 4),), "陆游: 军心+5"),
    30018: StartupPolicy(30018, ((590, 1.0, 4),), "秦观: 乐感等级+1"),
    30019: StartupPolicy(30019, ((51, -10.0, 4),), "贺铸: 民心-10"),
    30020: StartupPolicy(30020, ((2018, 1.0, 4),), "朱敦儒: 修行等级+1"),
    30021: StartupPolicy(30021, ((553, 10.0, 4),), "范仲淹: 威望+10"),
    30022: StartupPolicy(30022, ((32510, 5.0, 4), (32514, 10.0, 4)),
                         "周邦彦: 歌板+5 词元+10"),
    30023: StartupPolicy(30023, ((757, 1.38889, 4),), "黄庭坚: 发展年数+1.4"),
    # DB 中 202/203 各登记两次 -> 展示为 ×2 (不是文案重复)
    30024: StartupPolicy(30024, ((152, 0.02, 2), (103, 0.02, 2), (203, 0.02, 2),
                                 (202, 0.02, 2), (203, 0.02, 2), (202, 0.02, 2)),
                         "李纲: 粮食/金钱产量+2% 兵甲粮/兵甲钱消耗+2%×2"),
    30027: StartupPolicy(30027, ((552, 5.0, 4),), "欧阳修: 文化点+5"),
    30028: StartupPolicy(30028, ((32514, -2.0, 4), (32513, 0.01, 4)),
                         "王安石词人: 逐次生效（每次择律且词元 10~100 → 词元-2 后续择律收益+1%）"),
    30029: StartupPolicy(30029, ((553, -5.0, 4), (459, 1.0, 4), (32513, 0.1, 4)),
                         "唐婉: 威望-5 丝绸+1 后续择律收益+10%"),
    40021: StartupPolicy(40021, ((32515, 5.0, 4), (32516, 5.0, 4), (32517, -1.0, 4)),
                         "苏轼词人: 豪放词情+5 婉约词情+5 择律开题消耗-1"),
}


def startup_policy_ids() -> frozenset[int]:
    return STARTUP_POET_POLICY_IDS | STARTUP_MINISTER_POLICY_IDS


def _effective_startup_policy_ids() -> frozenset[int]:
    """LOAD 时使用的开局政策集合: 排除「逐次生效」的词人政策 (避免重复结算)。"""
    return startup_policy_ids() - PER_DRAW_POET_POLICY_IDS


# 注: 原 `ChildPolicy` / `compile_policy_payload` / `ConditionalEffect` /
#     `ModifierSet.conditionals` / `condition_satisfied` 这条「1030 条件效果」路径
#     已删除 (用户 2026-10-03 裁定清理死代码) ——
#     实测开局政策里的 1030 子政策**全部**由 `_all_draw_rules` 的 DrawRule 表覆盖
#     (或按裁定停用), `ModifierSet.conditionals` 恒为空, 该路径从未生效。


# ---------------------------------------------------------------------------
# 词句池
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VerseRow:
    id: int
    ci_name: str
    style: int
    poet_ids: tuple[int, ...]
    effects: tuple[tuple[int, float], ...]
    unlock_minister_ids: tuple[int, ...] = ()
    is_special: bool = False
    # 预计算: (属性键, 值) 列表 —— 热路径直接使用, 避免每轮查表
    attr_effects: tuple[tuple[str, float], ...] = ()
    # 预计算: 非属性效果 (881/词情/未知等) —— 走慢路径
    special_effects: tuple[tuple[int, float], ...] = ()

    def __post_init__(self) -> None:
        # is_special 由解锁名臣推导 (999 等特殊值), 也可显式传入
        if not self.is_special and any(mid in C.SPECIAL_UNLOCK_MINISTER_IDS
                                       for mid in self.unlock_minister_ids):
            object.__setattr__(self, "is_special", True)
        if not self.attr_effects and not self.special_effects and self.effects:
            pre: list[tuple[str, float]] = []
            special: list[tuple[int, float]] = []
            for etype, val in self.effects:
                attr = EFFECT_VALUE_ATTR.get(int(etype))
                if attr is not None:
                    pre.append((attr, float(val)))
                else:
                    special.append((int(etype), float(val)))
            object.__setattr__(self, "attr_effects", tuple(pre))
            object.__setattr__(self, "special_effects", tuple(special))


class VersePool:
    """按风格索引的词句池, 带解锁门控过滤。

    性能: 预先按风格 + 是否受门控分桶, 取池时只遍历「无门控」+「已解锁」的前缀,
    避免每次调用都重扫全部词句 (模拟内核每局调用 ~250 次)。
    """

    def __init__(self, rows: Iterable[VerseRow]) -> None:
        self._all: tuple[VerseRow, ...] = tuple(rows)
        self._by_id: dict[int, VerseRow] = {v.id: v for v in self._all}
        self._free: dict[int, tuple[VerseRow, ...]] = {}
        self._gated: dict[int, tuple[VerseRow, ...]] = {}
        self._all_by_style: dict[int, tuple[VerseRow, ...]] = {}
        for style in (M.STYLE_BOLD, M.STYLE_GRACEFUL):
            rows_s = [v for v in self._all if v.style == style]
            self._all_by_style[style] = tuple(rows_s)
            self._free[style] = tuple(v for v in rows_s if not v.unlock_minister_ids)
            self._gated[style] = tuple(v for v in rows_s if v.unlock_minister_ids)

    @property
    def all_verses(self) -> tuple[VerseRow, ...]:
        return self._all

    def by_id(self, verse_id: int) -> VerseRow | None:
        return self._by_id.get(int(verse_id))

    def get(self, style: int,
            unlock_all: bool = False,
            owned_minister_ids: Iterable[int] | None = None) -> tuple[VerseRow, ...]:
        """取某风格的可抽取池。

        `unlock_all=True` = **解锁所有词句库** (忽略名臣解锁门控; 用户 2026-10-03 口径),
        直接返回该风格的全部词句。否则返回「无门控 + 已拥有名臣可解锁」的条目。

        参数顺序: (style, unlock_all, owned_minister_ids)
        —— 与 tests/test_effects.py 的既有调用约定一致。
        """
        style = int(style)
        if unlock_all:
            return self._all_by_style.get(style, ())
        free = self._free.get(style, ())
        gated = self._gated.get(style, ())
        if not gated:
            return free
        owned = set(owned_minister_ids or ()) if owned_minister_ids else None
        out = list(free)
        if owned:
            out.extend(v for v in gated
                       if any(mid in owned for mid in v.unlock_minister_ids))
        return tuple(out)

    def for_poet(self, style: int, poet_id: int,
                 unlock_all: bool = False,
                 owned_minister_ids: Iterable[int] | None = None) -> tuple[VerseRow, ...]:
        return tuple(v for v in self.get(style, unlock_all, owned_minister_ids)
                     if poet_id in v.poet_ids)

    def verse_has_poet(self, verse_id: int, poet_id: int) -> bool:
        v = self.by_id(verse_id)
        return bool(v and poet_id in v.poet_ids)


# ---------------------------------------------------------------------------
# 运行期数据包
# ---------------------------------------------------------------------------

@dataclass
class GameData:
    """一次模拟所需的全部只读数据 (由 load_game_data 从数据库构建)。"""

    verses: VersePool
    mods: ModifierSet
    singer_rules: Mapping[int, SingerRule] = field(default_factory=lambda: dict(SINGER_RULES))
    singer_favors: Mapping[int, tuple[int, int]] = field(default_factory=dict)
    singer_names: Mapping[int, str] = field(default_factory=dict)
    poet_mods: Mapping[int, ModifierSet] = field(default_factory=dict)
    draw_rules: tuple[DrawRule, ...] = field(default_factory=lambda: build_draw_rules())
    startup_policies: Mapping[int, StartupPolicy] = field(default_factory=lambda: dict(STARTUP_POLICIES))
    verse_poet_ids: Mapping[int, tuple[int, ...]] = field(default_factory=dict)
    poet_names: Mapping[int, str] = field(default_factory=dict)
    poet_to_minister: Mapping[int, int] = field(default_factory=dict)
    rules_by_policy: Mapping[int, tuple[DrawRule, ...]] = field(default_factory=dict)
    # 逐次规则 ID -> 人类可读来源标签 (如「唱词人暮烟」「名臣柳永·恋情词3」), 用于日志归因
    rule_labels: Mapping[str, str] = field(default_factory=dict)
    # 东坡食单: 菜品 ID -> 该菜单独的修正 (每道菜互不覆盖, 因为 32513 是累加)
    dongpo_mods: Mapping[int, ModifierSet] = field(default_factory=dict)
    # 9 道菜全收集时的豪放词情层数 (老饕 ★2 = 20); 需另加苏轼名臣
    dongpo_complete_bold: float = 0.0
    # 「抽到婉约词时同时视为抽到豪放词」的名臣 (李纲「病牛」★2/★3)
    graceful_as_bold_ministers: frozenset[int] = frozenset()
    warnings: tuple[str, ...] = ()

    def poet_name(self, poet_id: int | None) -> str:
        if poet_id is None:
            return ""
        return self.poet_names.get(int(poet_id), f"词人#{poet_id}")

    def rules_for_policies(self, policy_ids: Iterable[int]) -> list[DrawRule]:
        """取给定政策 ID 对应的逐次规则 (去重)。"""
        want = {int(x) for x in policy_ids}
        if not want:
            return []
        return [r for pid in want for r in self.rules_by_policy.get(pid, ())]

    def singer_favor(self, singer_id: int | None) -> tuple[int, int]:
        if singer_id is None:
            return (0, 0)
        return tuple(self.singer_favors.get(int(singer_id), (0, 0)))    # type: ignore[return-value]

    def singer_name(self, singer_id: int | None) -> str:
        if singer_id is None:
            return ""
        return self.singer_names.get(int(singer_id), f"唱词人#{singer_id}")


# ---------------------------------------------------------------------------
# 数据库装配
# ---------------------------------------------------------------------------

def load_singer_favors() -> dict[int, tuple[int, int]]:
    import db
    rows = db.fetch_all("SELECT id, bold_favor, graceful_favor FROM singer ORDER BY id")
    return {int(r[0]): (int(r[1] or 0), int(r[2] or 0)) for r in rows}


def load_poet_names() -> dict[int, str]:
    import db
    rows = db.fetch_all("SELECT id, name FROM poet ORDER BY id")
    return {int(r[0]): (r[1] or f"词人#{r[0]}") for r in rows}


def load_poet_minister_map() -> dict[int, int]:
    """词人 ID -> 对应名臣 ID (minister_id > 0 才计入)。

    解锁词人时应同时把该名臣计入"已拥有", 否则需该名臣解锁的词句永远抽不到。
    """
    import db
    rows = db.fetch_all("SELECT id, minister_id FROM poet WHERE minister_id > 0 ORDER BY id")
    return {int(r[0]): int(r[1]) for r in rows}


def index_rules_by_policy(rules: Iterable["DrawRule"]) -> dict[int, tuple["DrawRule", ...]]:
    out: dict[int, list[DrawRule]] = {}
    for r in rules:
        out.setdefault(int(r.source_policy_id), []).append(r)
    return {k: tuple(v) for k, v in out.items()}


def load_verse_rows() -> list[VerseRow]:
    import db
    eff_rows = db.fetch_all(
        "SELECT verse_id, effect_type, effect_value FROM verse_effect "
        "WHERE effect_type IS NOT NULL ORDER BY verse_id, ordinal")
    eff_map: dict[int, list[tuple[int, float]]] = {}
    for vid, etype, val in eff_rows:
        eff_map.setdefault(int(vid), []).append((int(etype), float(val or 0.0)))

    poet_rows = db.fetch_all("SELECT verse_id, poet_id FROM verse_poet_relation ORDER BY verse_id")
    poet_map: dict[int, list[int]] = {}
    for vid, pid in poet_rows:
        poet_map.setdefault(int(vid), []).append(int(pid))

    unlock_rows = db.fetch_all(
        "SELECT verse_id, minister_id, is_special FROM verse_unlock_requirement ORDER BY verse_id, ordinal")
    unlock_map: dict[int, list[int]] = {}
    special_map: dict[int, bool] = {}
    for vid, mid, spec in unlock_rows:
        vid = int(vid)
        if mid is None or int(mid) == 0:
            continue
        unlock_map.setdefault(vid, []).append(int(mid))
        if spec:
            special_map[vid] = True

    out: list[VerseRow] = []
    for vid, name, style in db.fetch_all("SELECT id, ci_name, style FROM verse ORDER BY id"):
        vid = int(vid)
        out.append(VerseRow(
            id=vid,
            ci_name=name or "",
            style=int(style),
            poet_ids=tuple(poet_map.get(vid, ())),
            effects=tuple(eff_map.get(vid, ())),
            unlock_minister_ids=tuple(unlock_map.get(vid, ())),
            is_special=special_map.get(vid, False),
        ))
    return out


def load_poet_mods() -> tuple[dict[int, ModifierSet], tuple[str, ...]]:
    """词人 ID -> 其 RewardPolicyId 编译出的修正 (开局一次性部分)。"""
    import db
    rows = db.fetch_all("""
        SELECT pp.poet_id, p.id, pe.effect_type, pe.effect_value, pe.add_type
        FROM poet_policy pp
        JOIN policy p ON p.key_id = pp.policy_key_id
        JOIN policy_effect pe ON pe.policy_key_id = p.key_id
        ORDER BY pp.poet_id, pe.ordinal""")
    grouped: dict[int, list[tuple[int, float, int]]] = {}
    for poet_id, pid, etype, val, att in rows:
        # 逐次生效的词人政策: 其效果改由 DrawRule 每轮结算, 此处不并入开局一次性
        if int(pid) in PER_DRAW_POET_POLICY_IDS:
            continue
        grouped.setdefault(int(poet_id), []).append(
            (int(etype), float(val or 0.0), int(att) if att is not None else 4))

    mods: dict[int, ModifierSet] = {}
    warnings: list[str] = []
    for poet_id, effs in grouped.items():
        m = ModifierSet()
        for etype, val, att in effs:
            if etype == C.EFF_BENEFIT_BONUS:
                m.add_benefit_bonus(val)
            elif etype == C.EFF_DRAW_COST_MOD:
                m.add_draw_cost_mod(val)
            elif etype == C.EFF_BOLD_SENTIMENT:
                m.add_sentiment_bonus(bold=int(round(val)))
            elif etype == C.EFF_GRACEFUL_SENTIMENT:
                m.add_sentiment_bonus(graceful=int(round(val)))
            elif etype == C.EFF_CONDITIONAL:
                continue          # 逐次部分由 DrawRule 处理
            elif etype in PROSPERITY_EFFECT_IDS or etype in IGNORED_EFFECT_IDS:
                m.count_effect(etype, val)
            else:
                m.apply_startup(etype, val, att)
        mods[poet_id] = m
    return mods, tuple(warnings)


def _route_effect(mods: ModifierSet, etype: int, val: float, att: int) -> None:
    """把一条开局效果按语义路由到 ModifierSet 的对应字段 (三处 loader 共用)。"""
    if etype == C.EFF_BENEFIT_BONUS:
        mods.add_benefit_bonus(val)
    elif etype == C.EFF_DRAW_COST_MOD:
        mods.add_draw_cost_mod(val)
    elif etype == C.EFF_BOLD_SENTIMENT:
        mods.add_sentiment_bonus(bold=int(round(val)))
    elif etype == C.EFF_GRACEFUL_SENTIMENT:
        mods.add_sentiment_bonus(graceful=int(round(val)))
    elif etype == C.EFF_CONDITIONAL:
        return
    elif etype in PROSPERITY_EFFECT_IDS or etype in IGNORED_EFFECT_IDS:
        mods.count_effect(etype, val)
    else:
        mods.apply_startup(etype, val, att)


def load_startup_policy_mods(policy_ids: Iterable[int]) -> ModifierSet:
    """把「开局一次性」政策编译为修正 (用于 config.policy_key_ids 与名臣开局政策)。"""
    import db
    ids = [int(x) for x in policy_ids]
    if not ids:
        return ModifierSet()
    rows = db.fetch_all("""
        SELECT p.id, pe.effect_type, pe.effect_value, pe.add_type
        FROM policy p JOIN policy_effect pe ON pe.policy_key_id = p.key_id
        WHERE p.id = ANY(%s) AND pe.depth = 0 AND pe.source_policy_id = p.id
        ORDER BY p.id, pe.ordinal""", (ids,))
    mods = ModifierSet()
    for _pid, etype, val, att in rows:
        _route_effect(mods, int(etype), float(val or 0.0), att)
    return mods


def dongpo_food_mods(food_ids: Iterable[int]) -> tuple[dict[int, ModifierSet], float]:
    """把勾选的东坡食单编译为**每道菜单独一份**的修正 + 全收集奖励的豪放词情层数。

    效果来源: `dongpo_food.pool_id -> common_effect_pool_effect`
    (即 `CommonEffectPoolConfig.json` 的 652-660)。

    价格 (`CiYuanPrice`) **不收取** —— 用户 2026-10-03 明确「价格不管」。
    全收集奖励 = 老饕 ★2 的「收集所有食单 豪放词情+20层」, 需另加苏轼名臣 (见 simulator)。
    """
    import db
    ids = sorted({int(x) for x in food_ids})
    if not ids:
        return {}, 0.0
    rows = db.fetch_all("""
        SELECT f.id, pe.effect_type, pe.effect_value, pe.add_type
        FROM dongpo_food f
        JOIN common_effect_pool_effect pe ON pe.pool_id = f.pool_id
        WHERE f.id = ANY(%s) ORDER BY f.id, pe.ordinal""", (ids,))
    per: dict[int, ModifierSet] = {i: ModifierSet() for i in ids}
    for fid, etype, val, att in rows:
        _route_effect(per[int(fid)], int(etype), float(val or 0.0), att)
    complete = (float(C.DONGPO_COMPLETE_BOLD_SENTIMENT)
                if set(ids) >= set(C.DONGPO_COMPLETE_FOOD_IDS) else 0.0)
    return per, complete


def load_minister_startup_mods(minister_ids: Iterable[int]) -> ModifierSet:
    """名臣的「开启择律系统」类政策 -> 开局一次性。

    两个必须遵守的规则 (均经实测核实):

    1) **关联键是 `policy.MinisterId`，不是 `minister_policy`。**
       实测: 岳飞(300) 的 `PolicyList` 里**没有**「仁勇」(ID 3013)，
       仁勇是靠 `policy.MinisterId = 300` 指向岳飞的。
       早期实现误用 `minister_policy`(KeyID + PolicyList) 关联，
       导致选任何名臣都取不到择律政策 → 实测 10 位名臣全部落空。

    2) **星级规则** (用户 2026-10-03 确认): 同一政策的不同星级不能并存，只保留星级最高的。
       实测结构: 星级不是 KeyID 变体的属性，而是按 ID 家族区分 ——
       同一 `PolicyName` 对应多个 `ID`，各带不同 `StarCnt`
       (例: 豪放 = 3108(★1,+20) / 3109(★2,+25) / 3110(★3,+30);
             明黜陟 = 3060(★1,+30) / 3061(★2,+40) / 3062(★3,+40))。
       因此用 `DISTINCT ON (policy_name) ... ORDER BY star_cnt DESC` 只取最高星级，
       否则会同族全加 (实测 37 条 → 1113 歌板，严重高估)。

    同 ID 的多个 KeyID 是**不同时代**的变体 (TimeTypeList 不同)，不是星级，
    故在同一 ID 内保留全部 KeyID。
    """
    import db
    mids = [int(x) for x in minister_ids]
    if not mids:
        return ModifierSet()
    rows = db.fetch_all("""
        WITH kept AS (
            SELECT DISTINCT ON (p.policy_name) p.id, p.key_id, p.star_cnt, p.policy_name
            FROM policy p
            WHERE p.minister_id = ANY(%s)
              AND p.id = ANY(%s)
            ORDER BY p.policy_name, p.star_cnt DESC NULLS LAST, p.id DESC, p.key_id
        )
        SELECT k.id, pe.effect_type, pe.effect_value, pe.add_type
        FROM kept k
        JOIN policy_effect pe ON pe.policy_key_id = k.key_id
        WHERE pe.depth = 0 AND pe.source_policy_id = k.id
        ORDER BY k.id, pe.ordinal""", (mids, sorted(STARTUP_MINISTER_POLICY_IDS)))
    mods = ModifierSet()
    for _pid, etype, val, att in rows:
        _route_effect(mods, int(etype), float(val or 0.0), att)
    return mods


def load_graceful_as_bold_ministers() -> frozenset[int]:
    """找出「抽到婉约词时同时视为抽到豪放词」的名臣 (数据驱动)。

    判定依据: 该名臣**生效的最高星级**择律政策里带有 `881` 子 ID
    `GRANT_SUBID_GRACEFUL_AS_BOLD`(28319) —— 实测只有李纲「病牛」★2/★3 带有它。
    """
    import db
    try:
        rows = db.fetch_all("""
            WITH kept AS (
                SELECT DISTINCT ON (p.minister_id, p.policy_name) p.key_id, p.minister_id
                FROM policy p
                JOIN model_scope ms ON ms.kind = 'minister_policy' AND ms.ref_id = p.id
                WHERE p.minister_id IS NOT NULL AND p.minister_id <> 0
                ORDER BY p.minister_id, p.policy_name,
                         p.star_cnt DESC NULLS LAST, p.id DESC, p.key_id
            )
            SELECT DISTINCT k.minister_id
            FROM kept k JOIN policy_effect pe ON pe.policy_key_id = k.key_id
            WHERE pe.depth = 0 AND pe.effect_type = %s AND pe.effect_value = %s""",
            (C.EFF_GRANT_CARD, float(C.GRANT_SUBID_GRACEFUL_AS_BOLD)))
    except Exception:
        return frozenset()
    return frozenset(int(r[0]) for r in rows)


def load_rule_labels() -> dict[str, str]:
    """逐次规则 ID -> 人类可读来源标签, 供日志归因 (「这一笔歌板是谁给的」)。

    形如 ``唱词人暮烟`` / ``词人柳永·恋情词3`` / ``名臣柳永·恋情词3``;
    数据不可用时返回空表 (日志退化为显示规则 ID)。
    """
    import db
    out: dict[str, str] = {}
    try:
        # 归属映射 (唱词人/词人/名臣各自的逐次政策) 定义在 simulator 里, 故此处惰性导入,
        # 避免 effects <-> simulator 的模块级循环依赖 (调用发生在装配期, 两个模块都已加载)。
        import simulator as _S

        rules = build_draw_rules()
        policy_ids = sorted({int(r.source_policy_id) for r in rules if r.source_policy_id})
        if not policy_ids:
            return out
        names = {int(r[0]): (r[1] or "") for r in db.fetch_all(
            "SELECT id, policy_name FROM policy WHERE id = ANY(%s)", (policy_ids,))}
        singer_names = {int(r[0]): (r[1] or "") for r in db.fetch_all(
            "SELECT id, name FROM singer")}
        poet_names = load_poet_names()
        minister_names = {int(r[0]): (r[1] or "") for r in db.fetch_all(
            "SELECT id, name FROM minister")}
        owners: dict[int, str] = {}
        for sid, ids in _S._SINGER_DRAW_POLICY_IDS.items():
            for pid in ids:
                owners[int(pid)] = f"唱词人{singer_names.get(int(sid), sid)}"
        for pid, ids in _S.POET_DRAW_POLICY_IDS.items():
            for pol in ids:
                owners[int(pol)] = f"词人{poet_names.get(int(pid), pid)}"
        for mid, ids in _S.MINISTER_DRAW_POLICY_IDS.items():
            for pol in ids:
                owners[int(pol)] = f"名臣{minister_names.get(int(mid), mid)}"
        for r in rules:
            if not r.source_policy_id:
                continue
            owner = owners.get(int(r.source_policy_id), "规则")
            pname = names.get(int(r.source_policy_id), "")
            out[r.rule_id] = f"{owner}·{pname}" if pname else owner
    except Exception:
        return out
    return out


def load_game_data(poet_ids: Iterable[int] = (),
                   minister_ids: Iterable[int] = (),
                   policy_ids: Iterable[int] = ()) -> GameData:
    """从数据库装配一次模拟所需的全部只读数据 (每批模拟只调用一次)。"""
    import db
    warnings: list[str] = []
    singer_features = load_singer_features()
    rules = build_singer_rules(singer_features)

    # 一致性校验: DB 的 FeatureDesc 推导结果应与静态配方表一致
    for sid, rule in SINGER_RULES.items():
        live = rules.get(sid)
        if live is None:
            warnings.append(f"唱词人 {sid} 在数据库中缺失")
        elif (live.module != rule.module or live.effect_type != rule.effect_type
              or abs(live.effect_value - rule.effect_value) > 1e-9
              or live.style != rule.style or abs(live.bonus - rule.bonus) > 1e-9):
            warnings.append(
                f"唱词人 {sid} FeatureDesc 推导结果与静态配方表不一致: "
                f"live={live.module}/{live.effect_type}/{live.effect_value} "
                f"static={rule.module}/{rule.effect_type}/{rule.effect_value}")

    favors = load_singer_favors()
    verses = load_verse_rows()
    poet_mods, w0 = load_poet_mods()
    warnings.extend(w0)

    # 开局一次性政策: 只结算「显式指定的政策」+「开局词人的奖励政策」
    # (名臣的开局政策由 load_minister_startup_mods 按 minister_ids 处理)。
    # 绝不能把全部词人政策一并结算, 否则歌板等初始资源会被无关词人的政策污染。
    startup_ids: set[int] = {int(x) for x in policy_ids}
    poet_id_list = [int(x) for x in poet_ids]
    if poet_id_list:
        rows = db.fetch_all(
            "SELECT DISTINCT p.id FROM poet_policy pp "
            "JOIN policy p ON p.key_id = pp.policy_key_id "
            "WHERE pp.poet_id = ANY(%s)", (poet_id_list,))
        startup_ids |= {int(r[0]) for r in rows}
    startup_ids &= _effective_startup_policy_ids()

    mods = ModifierSet()
    mods.merge(load_startup_policy_mods(startup_ids))
    mods.merge(load_minister_startup_mods(minister_ids))
    # 注意: 开局词人的政策已由上面的 load_startup_policy_mods 按 policy.id 全量编译
    # (该函数正确处理 32513/32517/32515/32516), 因此不再叠加 poet_mods, 否则会重复结算。

    # 东坡食单: 每道菜单独编译 (由 run_once 按 config.dongpo_food_ids 合并)
    all_food_ids = [int(r[0]) for r in db.fetch_all("SELECT id FROM dongpo_food ORDER BY id")]
    dongpo_mods, complete_bold = dongpo_food_mods(all_food_ids)

    return GameData(
        verses=VersePool(verses),
        mods=mods,
        singer_rules=rules,
        singer_favors=favors,
        singer_names=load_singer_names(),
        poet_mods=poet_mods,
        draw_rules=build_draw_rules(),
        startup_policies=dict(STARTUP_POLICIES),
        verse_poet_ids={v.id: v.poet_ids for v in verses},
        poet_names=load_poet_names(),
        poet_to_minister=load_poet_minister_map(),
        rules_by_policy=index_rules_by_policy(build_draw_rules()),
        rule_labels=load_rule_labels(),
        dongpo_mods=dongpo_mods,
        dongpo_complete_bold=complete_bold,
        graceful_as_bold_ministers=load_graceful_as_bold_ministers(),
        warnings=tuple(warnings),
    )
