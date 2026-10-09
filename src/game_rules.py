# -*- coding: utf-8 -*-
"""game_rules · 择律的权威规则层（不连库、纯函数、可单测）

把择律的几条口径集中成可测试的纯函数，供 `simulator` 与工具复用。
每条规则都说明**它是什么**，并可用 `tools/run_without_db.py` 把新旧口径
跑成两组数字自行对照。

被修正的作者自述（`src/effects.py:5-8`）::

    1. 把「唱词人」FeatureDesc 编译为确定性规则 (DB 的 Trigger/Remove 池指向军事政策,
       是占位数据 —— 实测 8/9 位唱词人的 FeatureDesc 与 30000-30007 政策描述不匹配,
       且唱词人6「后续择律收益+10%」对应政策 30005 的 EffectList 为空, 故以
       FeatureDesc 为权威语义来源)。

**结论：池不是占位数据。** 真实链路是
`SongCiSingerConfig.TriggerCommonEffectPoolConfigId` → `CommonEffectPoolConfig` →
`1030 AddPolicy` → `PolicyConfig`，且：

* 9 位唱词人里 **8 位**的 `FeatureDesc` 与**池的 `EffectDesc`** 逐字一致；
* 唯一不一致的是唱词人 2「铁衣」↔ 政策 30001：那条 `EffectDesc`
  「必定唱出词句，但后续择律收益-10%」是全表孤例、且没有配套的 32513 负值效果，
  而它的 `TriggerTime=81 (ChooseCiCorrect)` + `EffectList=[32510] value=1.0`
  正是「每次择律正确时，获得歌板+1」——**判定为策划文案残留，功能正确**；
* 唱词人 6「后续择律收益+10%」的政策 30005 `EffectList` 为空**是对的**：
  效果挂在池 456 上（`(32513, add_type=4, 0.1)`），不经政策跳转。

因此本模块以「池 + 政策的 `TriggerTime`/`EffectList`」为权威，
`FeatureDesc` 只作为展示文案与一致性校验。
"""

from __future__ import annotations

import base64
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------- 常量

#: 择律的固定数值（属于关卡脚本资产，不在配置表里）。
#: 开题消耗 10 歌板；重听 1 起、每次 +1；无免费重听；
#: 累计答错每满 2 次把开题消耗 +1 并清零；候选 3 个。
SONGCI_VALUE: Dict[str, Any] = {
    "StartCost": 10,          # 每次择律开题消耗的歌板
    "ReplayBaseCost": 1,      # 重听基础消耗
    "ReplayCostGrowth": 1,    # 每次重听的消耗增长
    "FreeReplayCount": 0,     # 免费重听次数
    "WrongThreshold": 2,      # 累计答错每满 N 次 -> 开题消耗 +1 并清零
    "OptionCount": 3,         # 候选词牌数
}

#: 「后续择律收益提升」(32513) 只放大这些效果类型。
#: 这几项是"资源/情绪/文化"类属性；战斗力、发展年数、乐感、词情、诗意、灵犀
#: 等即时结算量**不吃**这个倍率。合成方式：`最终值 = 基础值 × 倍率`（不是累加）。
REWARD_MULTIPLY_TYPES = frozenset({
    51,      # 民心
    209,     # 军心
    351,     # 腐化
    552,     # 文化点
    553,     # 威望
    32510,   # 歌板
    32514,   # 词元
})

#: 上表的**属性键**形式（供 `simulator.py` 热路径按属性名判断，
#: 因为那里 `verse.attr_effects` 已经丢掉了 effect_type）。
REWARD_MULTIPLY_ATTRS = frozenset({
    "popular_support", "army_morale", "corruption",
    "culture_point", "reputation", "board", "verse_point",
})

#: 触发时机枚举（`PolicyConfig.TriggerTime` 的取值域）。
TRIGGER_TIME_NAMES: Dict[int, str] = {
    0: "None", 18: "OccupyCity", 22: "OccupyCityFirst", 27: "PolicyFinish",
    32: "NewYear", 58: "ComposePoem", 65: "ComposeSRPoem", 68: "VerseNotRhyming",
    77: "ChooseCi", 78: "ChooseBoldRhythmCorrect", 79: "ChooseGracefulRhythmCorrect",
    80: "ChooseCiWrong", 81: "ChooseCiCorrect", 82: "UnlockNewSongCiPoet",
    101: "ChooseCiCorrectForPoet",
}

#: 择律相关触发 -> (本项目的模块名, 风格限定)
TRIGGER_TO_MODULE: Dict[int, Tuple[str, Optional[int]]] = {
    0: ("startup", None),
    77: ("every_draw", None),
    78: ("every_correct", 1),      # STYLE_BOLD
    79: ("every_correct", 2),      # STYLE_GRACEFUL
    80: ("every_wrong", None),
    81: ("every_correct", None),
    82: ("on_poet_unlock", None),
    101: ("every_correct", None),  # 正确择某词人词（如苏轸 -> 苏轼词）
}

#: 唱词人解锁条件里的「国家效果名」-> 属性键
#: （`ConditionLibConfig.CompleteType=8` 的 `CompleteParam="TechValue;10;99999"`）
COUNTRY_EFFECT_ATTR: Dict[str, str] = {
    "TechValue": "tech_point",
    "CultivationLevel": "cultivation_level",
    "SpyLevel": "spy_level",
    "PopulationHeart": "popular_support",
    "MusicSense": "music_level",
    "ArmyHeart": "army_morale",
    "Crisis": "crisis",
}

#: 唱词人「擅长词牌」里的"不做限制"标记
ALL_CI_PAI_MARKER = "全部"

#: `ConditionLibConfig.ConditionDesc` 的异或密钥。
#: 该字段是异或编码：先 base64 解码，再逐字节与密钥的 UTF-8 字节循环异或。
CONDITION_DESC_XOR_KEY = "invincibleXor"


# ---------------------------------------------------------------- 触发时机

def trigger_of(policy_row: Dict[str, Any]) -> Tuple[str, Optional[int], Optional[str]]:
    """一个政策/效果池的触发时机 -> `(module, style, trigger_name)`。

    证据: `PolicyConfig.TriggerTime` 就是 `eEffectTriggerType`；未知的非 0 取值
    属于事件/城市/战斗等外部系统，返回 `("external", None, 枚举名或数字)`。
    """
    raw = policy_row.get("TriggerTime")
    if raw is None:
        return "startup", None, None
    tt = int(raw)
    if tt in TRIGGER_TO_MODULE:
        module, style = TRIGGER_TO_MODULE[tt]
        return module, style, TRIGGER_TIME_NAMES.get(tt, str(tt))
    return "external", None, TRIGGER_TIME_NAMES.get(tt, str(tt))


# ---------------------------------------------------------------- 数值口径

def reward_scale(effect_type: int, multiplier: float) -> float:
    """「后续择律收益提升」对某效果类型的放大系数。

    只有 `REWARD_MULTIPLY_TYPES` 里的 7 种吃倍率，其余原样返回 1.0。
    证据见 `REWARD_MULTIPLY_TYPES` 注释。
    """
    return multiplier if effect_type in REWARD_MULTIPLY_TYPES else 1.0


# 说明：下面两个函数记录的是**游戏确实存在、但本模型不启用**的规则。
# 3 个候选里必有正解、且数据里每首词都有准确答案，所以"每次择律都正确"是
# 结构性前提；答错分支与自动重听费不会在模拟中发生。它们保留是因为
# **玩家/自动化确实会主动"重听一句"**（`TryReplay`），需要知道要花多少歌板。
def replay_cost(replay_count: int,
                base: int = SONGCI_VALUE["ReplayBaseCost"],
                growth: int = SONGCI_VALUE["ReplayCostGrowth"]) -> int:
    """重听费 = `growth × max(0, replay_count) + base`。

    注意 `replay_count` 是**本次择律的重听次数**，只有玩家**主动重听**才会 +1；
    答错自动重听只读它、不自增。所以没有主动重听过时，每次答错固定收 `base`。
    """
    return int(growth * max(0, int(replay_count)) + base)


def escalate_ticket(accumulated_wrong: int, wrong_this_turn: int,
                    threshold: int = SONGCI_VALUE["WrongThreshold"],
                    growth: float = 1.0) -> Tuple[int, int, float]:
    """累计答错推进「开题消耗涨价」。

    返回 `(新的累计答错, 涨价次数, 开题消耗增量)`。

    每次跨越阈值的增量是 **+1 歌板**，并把累计计数清零，形如
    "每累计错 N 次 → 开题价 +1，计数归零"。
    """
    escalations = 0
    acc = int(accumulated_wrong)
    for _ in range(max(0, int(wrong_this_turn))):
        if acc + 1 >= int(threshold):
            escalations += 1
            acc = 0
        else:
            acc += 1
    return acc, escalations, escalations * float(growth)


# ---------------------------------------------------------------- 唱词人效果

def _effect_triples(row: Dict[str, Any]) -> List[Tuple[int, int, float]]:
    out: List[Tuple[int, int, float]] = []
    for e, a, v in zip(row.get("EffectList") or [],
                       row.get("EffectAddTypeList") or [],
                       row.get("EffectValueList") or []):
        out.append((int(e), int(a), float(v)))
    return out


def singer_effects_from_pools(
    singer_row: Dict[str, Any],
    pools: Dict[int, Dict[str, Any]],
    policies_by_id: Dict[int, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """从「唱词人 → 效果池 → 1030 政策」解析出**权威**效果列表。

    每条返回 `{"module", "style", "trigger_name", "effects":[(type, add_type, value)],
    "source", "feature_desc", "desc"}`。

    链路依据: `SongCiSingerConfig.TriggerCommonEffectPoolConfigId`
    （唱词人 1-8 → 池 451-458，池里一条 `1030 AddPolicy` 指向政策 30000-30007；
    唱词人 9 苏轸 → 池 650，指向 40014 + 40036）。
    """
    pid = int(singer_row.get("TriggerCommonEffectPoolConfigId") or 0)
    pool = pools.get(pid) or {}
    desc = pool.get("EffectDesc") or singer_row.get("FeatureDesc") or ""
    module, style, tname = _pool_trigger(pool, policies_by_id)
    effects: List[Tuple[int, int, float]] = []
    for etype, add_t, value in _effect_triples(pool):
        if etype == 1030:                       # 添加政策
            child = policies_by_id.get(int(value))
            if child is not None:
                for ce, ca, cv in _effect_triples(child):
                    if ce == 1030:              # 再往下不再展开
                        continue
                    effects.append((ce, ca, cv))
        elif etype == 1036:                     # 移除政策（换唱词人时撤下）
            continue
        else:
            effects.append((etype, add_t, value))
    if not effects:
        effects = []
    return [{
        "module": module,
        "style": style,
        "trigger_name": tname,
        "effects": effects,
        "source": f"pool:{pid}",
        "desc": desc,
        "feature_desc": singer_row.get("FeatureDesc") or "",
        "matches_feature_desc": (singer_row.get("FeatureDesc") or "") == desc,
    }]


def _pool_trigger(pool: Dict[str, Any],
                  policies_by_id: Dict[int, Dict[str, Any]]) -> Tuple[str, Optional[int], Optional[str]]:
    """池本身没写触发时，取它指向的那个政策的 TriggerTime。"""
    for etype, _add, value in _effect_triples(pool):
        if etype == 1030:
            child = policies_by_id.get(int(value))
            if child is not None:
                return trigger_of(child)
    # 池直接挂效果（如唱词人 6 的 32513），用文案兜底
    desc = pool.get("EffectDesc") or ""
    if "错误" in desc:
        return "every_wrong", None, "ChooseCiWrong"
    if "收益" in desc:
        return "passive_bonus", None, "ChooseCi"
    if "正确" in desc:
        style = 1 if "豪放" in desc else (2 if "婉约" in desc else None)
        return "every_correct", style, "ChooseCiCorrect"
    return "every_draw", None, "ChooseCi"


def singer_effect_value(singer_row: Dict[str, Any],
                        pools: Dict[int, Dict[str, Any]],
                        policies_by_id: Dict[int, Dict[str, Any]],
                        effect_type: int) -> Optional[float]:
    """取某唱词人某效果类型的**实际数值**（用于纠正静态表里的手抄值）。"""
    for rule in singer_effects_from_pools(singer_row, pools, policies_by_id):
        for etype, _add, value in rule["effects"]:
            if etype == effect_type:
                return value
    return None


# ---------------------------------------------------------------- 词句池

def specialty_pool(verses: Sequence[Any], specialty: Sequence[str],
                   style: int,
                   option_count: int = SONGCI_VALUE["OptionCount"],
                   all_verses: Optional[Sequence[Any]] = None
                   ) -> List[Any]:
    """按唱词人擅长词牌筛「本题正确词句」的候选池。

    谓词: **含「全部」 ∨ 词句风格 == 抽到的律 ∨ 词牌 ∈ 擅长词牌**；
    过滤后不足 `option_count` 条时回退全量。

    注意这是 OR：擅长词牌表现为「跨风格放宽」（唱词人能在别的律下唱自己擅长的词牌），
    而擅长词牌为「全部」时（如铁衣）连风格都不筛。

    `verses` 传"已解锁且未收集"的列表；`all_verses` 传未过滤的同一列表（回退用）。
    """
    spec = [str(x) for x in (specialty or [])]
    if not spec or ALL_CI_PAI_MARKER in spec:
        return list(verses)                       # w20 = 1 -> 不筛
    specset = set(spec)
    out = [v for v in verses
           if int(getattr(v, "style", 0)) == int(style)
           or str(getattr(v, "ci_pai_name", "")) in specset]
    if len(out) < max(1, int(option_count)):
        return list(all_verses if all_verses is not None else verses)
    return out


# ---------------------------------------------------------------- 词人解锁

def poet_unlock_targets(verse_row: Dict[str, Any],
                        poets: Dict[int, Dict[str, Any]]) -> List[int]:
    """收集到一句词后可以解锁哪些词人（`SongCiPoetConfig` 里存在的那些）。

    解锁后会自动激活该词人的奖励政策（`SongCiPoetConfig.RewardPolicyId`）。
    候选由调用方给出（收集词句 → 其 `BelongPoetIds`），词人配置里不存在的 ID 忽略。
    """
    out = []
    for pid in verse_row.get("BelongPoetIds") or []:
        if int(pid) in poets:
            out.append(int(pid))
    return out


def poet_unlock_by_minister(minister_ids: Iterable[int],
                            poets: Dict[int, Dict[str, Any]]) -> List[int]:
    """拥有名臣 → 反向解锁对应词人（`SongCiPoetConfig.MinisterId`）。

    这正是 `effects.py` 里 `poet_to_minister` 那段 docstring 描述、但从未被实现的联动。
    """
    owned = {int(m) for m in minister_ids}
    return [pid for pid, p in poets.items() if int(p.get("MinisterId") or 0) in owned]


# ---------------------------------------------------------------- 条件文案解密

def decrypt_condition_desc(text: str, key: str = CONDITION_DESC_XOR_KEY) -> str:
    """解 `ConditionLibConfig.ConditionDesc`。

    算法: `base64 解码` → 逐字节与密钥的 UTF-8 字节循环异或。
    """
    if not text:
        return ""
    raw = base64.b64decode(text)
    kb = key.encode("utf-8")
    return bytes(b ^ kb[i % len(kb)] for i, b in enumerate(raw)).decode("utf-8", "replace")


def parse_unlock_condition(cond_row: Optional[Dict[str, Any]],
                           texts: Optional[Dict[int, Dict[str, Any]]] = None
                           ) -> Optional[Dict[str, Any]]:
    """把 `ConditionLibConfig` 行解析成可判定的条件。

    * `CompleteType=6` + `Negation=1` -> `{"kind": "auto"}`（如唱词人 1 新竹）
    * `CompleteType=8` -> `CompleteParam="TechValue;10;99999"` -> 属性阈值
    * `CompleteType=29` -> 政策已激活
    * `CompleteType=11` -> 满足任一子条件（子条件 ID 在 `CompleteParam` 分号后）
    """
    if not cond_row:
        return None
    ctype = cond_row.get("CompleteType")
    param = cond_row.get("CompleteParam") or ""
    if ctype == 6 and cond_row.get("Negation"):
        return {"kind": "auto"}
    if ctype == 8:
        parts = param.split(";")
        if parts and parts[0] in COUNTRY_EFFECT_ATTR:
            return {"kind": "attr", "attr": COUNTRY_EFFECT_ATTR[parts[0]],
                    "min": float(parts[1]) if len(parts) > 1 and parts[1] else 0.0}
    if ctype == 29:
        return {"kind": "policy", "policy_id": int(param.split(";")[0]) if param else 0}
    if ctype == 11:
        sub_ids = [int(x) for x in param.split(";")[1:] if str(x).lstrip("-").isdigit()]
        subs = []
        if texts:
            for sid in sub_ids:
                sub = texts.get(sid)
                parsed = parse_unlock_condition(sub or {})
                if parsed:
                    parsed["id"] = sid
                    subs.append(parsed)
        return {"kind": "any_of", "sub_ids": sub_ids, "subs": subs}
    return None
