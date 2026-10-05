# -*- coding: utf-8 -*-
"""效果数值的统一展示口径 (GUI / 文档 / 报告共用)。

展示规则 (依据逐一核对的游戏文案与用户确认):

| 效果 | 展示方式 |
|---|---|
| 军规 (881 子ID 28399) | `具体数值 / 20000`, **以百分比展示** |
| 粮食产量(152)/金钱产量(103)/兵甲消耗(202,203)/后续择律收益(32513) | **百分比** (DB 值为小数, 0.07 = 7%) |
| 军团规模(211) | **百分比** = `值 / 40` (`EffectTypeConfig.MinValue=40` 即 1%; 400=+10%, 320=+8%, 160=+4%) |
| 发展年数(757) | **年**, 保留一位小数 (0.69445 -> 0.7, 1.3889 -> 1.4) |
| add_type=2 的乘算效果 | **百分比** (0.08 = +8%) |
| 重复登记的相同效果 | 合并为一条, 以 `×N` 标注登记次数 (不重复堆叠文案) |
| 其余 | 原样数值 |
"""
from __future__ import annotations

import config as C

# DB 值为小数、应显示为百分比的效果类型
RATIO_EFFECT_IDS: frozenset[int] = frozenset({152, 103, 202, 203, 32513})

# 军团规模 (211): EffectTypeConfig.MinValue = 40 对应 1%, 故 百分比 = 值 / 40
EFF_LEGION = 211
LEGION_UNIT = 40.0

# 发展年数 (年, 两位小数)
EFF_DEVELOP_YEARS = 757

# 军规: 881 子 ID
MILITARY_RULE_DIVISOR = 20000.0

CN_NAMES: dict[int, str] = {
    C.EFF_BOARD: "歌板",
    C.EFF_VERSE_POINT: "词元",
    C.EFF_BENEFIT_BONUS: "后续择律收益",
    C.EFF_BOLD_SENTIMENT: "豪放词情",
    C.EFF_GRACEFUL_SENTIMENT: "婉约词情",
    C.EFF_DRAW_COST_MOD: "择律开题消耗",
    1: "居民",
    51: "民心", 209: "军心", 204: "战斗力", 351: "腐化",
    552: "文化点", 553: "威望", 590: "乐感等级",
    211: "军团规模", 152: "粮食产量", 103: "金钱产量",
    202: "兵甲钱消耗", 203: "兵甲粮消耗", 459: "丝绸", 454: "士子",
    C.EFF_PROSPERITY: "繁荣", C.GRANT_SUBID_REFORM: "变法",
    757: "发展年数", 2018: "修行等级", 927: "巡访次数",
    C.EFF_GRANT_CARD: "授予卡牌/状态", C.EFF_SYSTEM_SWITCH: "系统开关",
}


def effect_name(effect_type: int) -> str:
    t = int(effect_type)
    return CN_NAMES.get(t) or C.NUMERIC_ATTRIBUTE_EFFECTS.get(t) or f"效果{t}"


def format_effect(effect_type: int, value: float, add_type: int = C.ADD_TYPE_ADDITIVE,
                  with_name: bool = True) -> str:
    """把单条效果格式化为展示文本。"""
    t = int(effect_type)
    v = float(value)
    name = effect_name(t) if with_name else ""

    # 军规 (881 子 ID 28399): 这里的 v 是**子 ID**而不是数值,
    # 实际每次触发的军规增量由政策文案给出 (陆游「豪放」= 每次 +1%)
    if t == C.EFF_GRANT_CARD and int(round(v)) == int(C.GRANT_SUBID_MILITARY_RULE):
        return f"{name}军规" if name else "军规"

    # 发展年数: 年, 保留一位小数 (0.69445 -> 0.7, 1.3889 -> 1.4)
    if t == EFF_DEVELOP_YEARS:
        return f"{name}{v:+.1f}"

    # add_type=2 的乘算效果: 百分比
    if int(add_type) == C.ADD_TYPE_MULTIPLIER:
        return f"{name}{v * 100:+g}%"

    # 军团规模 (211): MinValue=40 即 1% (实测 400=+10%, 320=+8%, 160=+4%)
    if t == EFF_LEGION and abs(v) >= LEGION_UNIT:
        return f"{name}{v / LEGION_UNIT:+g}%"

    # 比例型 (粮食产量/金钱产量/兵甲消耗/后续择律收益)
    if t in RATIO_EFFECT_IDS:
        return f"{name}{v * 100:+g}%"

    return f"{name}{v:+g}"


def format_effects(effects, with_name: bool = True) -> str:
    """批量格式化, 兼容 (type, value) 与 (type, value, add_type)。

    同一效果被 DB **重复登记**时 (例如 李纲 30024 / 晏几道 30016 的
    `202`/`203` 各出现两次) 合并为一条并以 `×N` 标注, 避免文案重复。
    """
    merged: list[tuple[int, float, int, int]] = []      # (type, value, add_type, count)
    index: dict[tuple[int, float, int], int] = {}
    for e in effects:
        t = int(e[0])
        v = float(e[1])
        att = int(e[2]) if len(e) >= 3 else C.ADD_TYPE_ADDITIVE
        key = (t, v, att)
        if key in index:
            i = index[key]
            merged[i] = (merged[i][0], merged[i][1], merged[i][2], merged[i][3] + 1)
        else:
            index[key] = len(merged)
            merged.append((t, v, att, 1))

    parts = []
    for t, v, att, n in merged:
        text = format_effect(t, v, att, with_name)
        parts.append(f"{text}×{n}" if n > 1 else text)
    return "，".join(parts)


# 模拟属性键 -> 展示口径
def format_attribute(attr_key: str, value: float) -> str:
    """按属性的展示口径格式化 (目前只有军规需要换算)。"""
    v = float(value)
    if attr_key == "military_rule":
        # 军规表述 = 具体数值 / 20000, 以百分比展示
        return f"{v / MILITARY_RULE_DIVISOR * 100:.4g}%"
    if attr_key == "develop_years":
        return f"{v:+.1f}"
    return f"{v:g}"
