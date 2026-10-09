# -*- coding: utf-8 -*-
"""M3 · 蒙特卡洛模拟内核 (simulator.py)

纯逻辑, 无 GUI 依赖。规则依据 docs/宋词择律模拟-设计文档.md §2/§4.3 与
task-1 描述的 11 条验收规则。

单次择律顺序:
  1. 判定可择律:  歌板 >= 本次消耗 (= draw_base_cost + 32517 修正, 下限 0)
  2. 扣歌板
  3. 按唱词人 BoldFavor/GracefulFavor 加权决定抽哪一侧, 再从该侧可抽取池均匀抽一条
     (**没有"选择题律"这一步** —— 抽豪放还是婉约只由这两个权重决定)
  4. 应用词句效果 (受后续择律收益加成影响, 默认只放大正值), 歌板/词元下限 0
  5. 若词句归属词人尚未拥有 -> 解锁, 其政策修正自本次起生效
  6. 词情累加 (抽中豪放 -> 豪放词情+1, 否则婉约词情+1), 上限 sentiment_cap
  7. 词情触发 (默认答对 => 抽到豪放词即「正确择豪放律」, 抽到婉约词即「正确择婉约律」):
     概率 = min(1.0, floor(层数/10) * 0.10), 层数永久累积不消耗
       抽到豪放词 -> 豪放词情判定: 歌板+1、军心+1
       抽到婉约词 -> 婉约词情判定: 歌板+2、民心-1
  8. 结算终止: 歌板 >= end_threshold, 或歌板 < 下次消耗, 或 达到 max_turns
"""
from __future__ import annotations

import dataclasses
import math
import random
from typing import Any, Iterable, Mapping, Sequence

import config as C
import game_rules as GR
import effects as E
import model as M

__all__ = ["run_once", "run_batch", "sentiment_trigger_probability"]


# ---------------------------------------------------------------------------
# 效果落库
# ---------------------------------------------------------------------------

# 仅计数、不映射为模拟属性的效果
_COUNTED_ONLY: frozenset[int] = frozenset({
    C.EFF_SYSTEM_SWITCH, C.EFF_DONGPODAN, C.EFF_DONGPODAN2,
    C.EFF_VISIT_TIMES, C.EFF_HANQING, C.EFF_CONDITIONAL, 454, 459, 456, 465,
    751, 927, 1093, 2018, 2910, 3033, 3410, 999, 32665,
    # 已知但本模拟不跟踪数值的「国政量词」效果, 与 E.IGNORED_EFFECT_IDS 保持一致:
    #   103 金钱产量 / 152 粮食产量 / 202/203 兵甲消耗 / 211 军团规模 / 32850 添加朝臣
    # (否则逐次路径会把它们算进「未识别效果值」, 而开局路径只计数 -> 两条路径不一致)
    103, 152, 202, 203, 211, 32850, 2807,
})

SENTIMENT_BOLD_KEY = "__sentiment_bold__"
SENTIMENT_GRACEFUL_KEY = "__sentiment_graceful__"
# 逐次结算的「后续择律收益」加成 (32513), 由 run_once 从 granted 里取出累加进 bonus
BENEFIT_BONUS_KEY = "__benefit_bonus__"
UNKNOWN_KEY = "__unknown__"
# 881 子 ID 映射为属性时, 每个子 ID 记 1 单位的"层" (军规+1层 / 变法+1层 / 繁荣+1)
GRANT_SUBID_UNIT = 1.0


def _trunc_abs(v: float) -> float:
    """取绝对值向下取整, 保留符号 (即向 0 截断): 7.9 -> 7, -7.9 -> -7, 0.69 -> 0。

    用户 2026-10-03 / 2026-10-05: **择律产生的所有绝对值**一律舍弃小数部分
    (民心 / 威望 / 军心 / 腐化 / 歌板 / 词元 …), 不只是词句条目效果。
    """
    return float(math.copysign(math.floor(abs(float(v))), float(v)))


#: 这些 granted 键不是"属性", 不参与取整 (加成 / 词情 / 未识别累计)
_NON_ATTR_GRANT_KEYS = frozenset({BENEFIT_BONUS_KEY, UNKNOWN_KEY,
                                  SENTIMENT_BOLD_KEY, SENTIMENT_GRACEFUL_KEY})


def _floor_grant(attr: str, value: float) -> float:
    """择律给出的一笔属性增减: 绝对值向下取整, 保留符号。

    用户 2026-10-05 指出: 早期只对**词句条目**取整, 于是
    唱词人「歌板+1」乘上 1.05 的择律收益加成后会写出 1.05 这种小数
    (事件流里能看到 108.1 / 118.21 / 17.1 这类歌板余额)。
    现在凡是 add_type = 绝对值/加算 的属性增减, 无论来自词句、词人、名臣还是
    唱词人, 都统一取整; 百分比效果 (add_type=2) 走乘算通道, 不属于"绝对值"。
    """
    if attr in _NON_ATTR_GRANT_KEYS:
        return float(value)
    return _trunc_abs(value)

# 个别子 ID 的**原始量纲**与"层"不同, 需要按游戏口径折算:
#   军规 28399 —— 文案「正确择豪放律时，军规+1%（至多20层）」，即 1 层 = 1 个百分点;
#   而军规原始值按用户确认的「具体数值 / 20000 = 百分比」口径展示，
#   故 1 层应累加 20000/100 = 200 原始单位，否则报告里的军规终值会缩小 200 倍
#   (20 层本该是 +20%，旧实现显示成 0.1%)。
#   变法 30556(「获得1层【变法】」) / 繁荣 31480(「繁荣+1」) 按层/点计数, 维持 1.0。
GRANT_SUBID_UNIT_BY_ID: dict[int, float] = {
    C.GRANT_SUBID_MILITARY_RULE: 200.0,
}


def _norm_effects(effects: Iterable[tuple]) -> list[tuple[int, float, int]]:
    """把效果元组统一成 (effect_type, value, add_type)。

    支持 (type, value) 与 (type, value, add_type) 两种写法; 缺省 add_type=4 (加算)。
    """
    out = []
    for e in effects:
        if len(e) >= 3:
            out.append((int(e[0]), float(e[1]), int(e[2])))
        else:
            out.append((int(e[0]), float(e[1]), C.ADD_TYPE_ADDITIVE))
    return out


def _apply_multiplier(multipliers: dict[str, float], counters: dict[str, int],
                      effect_type: int, value: float) -> None:
    """add_type=2 的百分比乘算效果: 记为倍率修正, 结算时 值×(1+Σ倍率)。"""
    attr = E.EFFECT_VALUE_ATTR.get(int(effect_type))
    if attr is not None:
        multipliers[attr] = multipliers.get(attr, 0.0) + float(value)
    else:
        key = f"__mult__{int(effect_type)}"
        multipliers[key] = multipliers.get(key, 0.0) + float(value)
        counters["counted"] = counters.get("counted", 0) + 1


def _apply_effect(granted: dict[str, float], counters: dict[str, int],
                  effect_type: int, value: float,
                  add_type: int = C.ADD_TYPE_ADDITIVE) -> None:
    """单条效果的慢路径 (单条调用)。批量路径见 _apply_effects。"""
    if int(add_type) == C.ADD_TYPE_MULTIPLIER:
        # 无 multipliers 上下文时退化为计数 (极少数调用点)
        counters["counted"] = counters.get("counted", 0) + 1
        return
    fast = E.EFFECT_VALUE_ATTR.get(int(effect_type))
    if fast is not None:
        granted[fast] = granted.get(fast, 0.0) + _floor_grant(fast, value)
        return
    etype = int(effect_type)
    if etype == C.EFF_GRANT_CARD:
        # 881 的子 ID 是独立量词 (军规 28399 / 变法 30556 / 繁荣 31480)。
        # 能映射到属性的按属性累加, 否则仅计数, 避免丢失子 ID 身份。
        sub_attr = E.GRANT_SUBID_ATTRIBUTE.get(int(value))
        if sub_attr is not None:
            unit = GRANT_SUBID_UNIT_BY_ID.get(int(value), GRANT_SUBID_UNIT)
            granted[sub_attr] = granted.get(sub_attr, 0.0) + unit
        else:
            counters["card_grants"] = counters.get("card_grants", 0) + 1
        return
    if etype == C.EFF_BENEFIT_BONUS:
        # 「后续择律收益」逐次加成 (王安石词人政策 30028: 每次 +1%):
        # 记到特殊键, 由 run_once 累加进本局 bonus (不能用 apply_startup, 那是一次性修正)
        granted[BENEFIT_BONUS_KEY] = granted.get(BENEFIT_BONUS_KEY, 0.0) + value
        return
    if etype == C.EFF_BOLD_SENTIMENT:
        granted[SENTIMENT_BOLD_KEY] = granted.get(SENTIMENT_BOLD_KEY, 0.0) + value
        return
    if etype == C.EFF_GRACEFUL_SENTIMENT:
        granted[SENTIMENT_GRACEFUL_KEY] = granted.get(SENTIMENT_GRACEFUL_KEY, 0.0) + value
        return
    if etype in E.PROSPERITY_EFFECT_IDS or etype in _COUNTED_ONLY:
        counters["counted"] = counters.get("counted", 0) + 1
        return
    counters["unknown"] = counters.get("unknown", 0) + 1
    counters["unknown_value"] = counters.get("unknown_value", 0.0) + float(value)
    granted[UNKNOWN_KEY] = granted.get(UNKNOWN_KEY, 0.0) + value


def _apply_effects(granted: dict[str, float], counters: dict[str, int],
                   effects: Iterable[tuple],
                   bonus: float = 0.0,
                   positive_only: bool = True,
                   amplify: bool = True,
                   multipliers: dict[str, float] | None = None,
                   whitelist_only: bool = True) -> None:
    """批量应用效果 (热路径)。

    绝大多数效果是「属性效果」(EFFECT_VALUE_ATTR 命中), 此处内联处理以避免
    每条效果一次函数调用 —— 实测该路径占单局耗时的主要部分。

    `add_type=2` 的百分比效果会被路由到 `multipliers` (结算时乘算)。
    """
    attr_map = E.EFFECT_VALUE_ATTR
    g_get = granted.get
    m = multipliers if multipliers is not None else {}
    rows = _norm_effects(effects)
    if not amplify or not bonus:
        for etype, value, add_t in rows:
            if add_t == C.ADD_TYPE_MULTIPLIER:
                _apply_multiplier(m, counters, etype, value)
                continue
            attr = attr_map.get(etype)
            if attr is not None:
                granted[attr] = g_get(attr, 0.0) + _floor_grant(attr, value)
            else:
                _apply_effect(granted, counters, etype, value)
        return
    factor = 1.0 + bonus
    for etype, value, add_t in rows:
        if add_t == C.ADD_TYPE_MULTIPLIER:
            _apply_multiplier(m, counters, etype, value)
            continue
        attr = attr_map.get(etype)
        if attr is not None:
            if whitelist_only and etype not in GR.REWARD_MULTIPLY_TYPES:
                granted[attr] = g_get(attr, 0.0) + _floor_grant(attr, value)
                continue
            if value > 0.0:
                value = value * factor
            elif not positive_only:
                value = value * factor
            granted[attr] = g_get(attr, 0.0) + _floor_grant(attr, value)
        else:
            if value > 0.0:
                value = value * factor
            elif not positive_only:
                value = value * factor
            _apply_effect(granted, counters, etype, value)


def sentiment_trigger_probability(layers: int) -> float:
    """词情触发概率 = min(1.0, floor(层数 / 10) * 0.10)。"""
    return min(1.0, float(int(layers) // C.SENTIMENT_STEP_LAYERS) * C.SENTIMENT_TRIGGER_PER_STEP)


# ---------------------------------------------------------------------------
# 抽取风格
# ---------------------------------------------------------------------------

def _pick_weighted_style(config: M.SimulationConfig, game: E.GameData,
                         rng: random.Random) -> int:
    bold, graceful = game.singer_favor(config.singer_id)
    total = bold + graceful
    if total <= 0:
        return M.STYLE_BOLD if rng.random() < 0.5 else M.STYLE_GRACEFUL
    return M.STYLE_BOLD if rng.random() < (bold / total) else M.STYLE_GRACEFUL


# ---------------------------------------------------------------------------
# 每次择律的政策规则
# ---------------------------------------------------------------------------

def _rules_for(config: M.SimulationConfig, game: E.GameData) -> list[E.DrawRule]:
    """装配本次配置下生效的「每次择律」规则。

    只纳入「确实可达」的规则:
      - 唱词人自身的效果池政策
      - 开局拥有的词人 / 名臣对应的逐次政策
      - 显式指定的政策
    局内新解锁的词人/名臣分别在解锁时追加 (见 run_once)。
    """
    allowed: set[int] = set()
    if config.singer_id is not None:
        allowed |= _SINGER_DRAW_POLICY_IDS.get(int(config.singer_id), frozenset())
    for pid in config.poet_ids:
        allowed |= POET_DRAW_POLICY_IDS.get(int(pid), frozenset())
    allowed |= _minister_draw_policies(config.minister_ids)
    allowed |= {int(x) for x in config.policy_key_ids}
    if not allowed:
        return []
    return [r for r in game.draw_rules if r.source_policy_id in allowed]


def _minister_draw_policies(minister_ids: Iterable[int]) -> frozenset[int]:
    """名臣择律政策只有「被纳入的名臣」才生效, 避免每轮空转 36 条规则。"""
    mids = {int(x) for x in minister_ids}
    if not mids:
        return frozenset()
    out: set[int] = set()
    for mid in mids:
        out |= MINISTER_DRAW_POLICY_IDS.get(mid, frozenset())
    return frozenset(out)


def _update_growth_rules(rules: Sequence[E.DrawRule], layers: dict[str, int],
                         state: Mapping[str, float], bonus: float) -> float:
    """处理「每 N 点属性 +pct」类阶梯规则。

    两类语义**不同**, 分别按文案落实 (用户 2026-10-03 裁定)：

    * `benefit_per_10`（陆游「示儿」: 每有10词元 +5%，**至多10层**）
      —— 文案用「层」，是**累积层数**：只增不减，封顶 `max_layers`。
    * `benefit_missing`（李清照「声声慢」: 若民心低于60，+10%；每低10点，+10%）
      —— 文案是「每低N点」的**阶梯**，**不是永久的**：每轮按**当前**民心重算层数，
        民心回升到阈值以上时加成会**撤销**。
    """
    for r in rules:
        if r.handler not in ("benefit_per_10", "benefit_missing"):
            continue
        cur = float(state.get(r.attr_key, 0.0))
        if r.handler == "benefit_per_10":
            want = min(int(r.max_layers), int(cur // r.per)) if r.per else 0
            have = int(layers.get(r.rule_id, 0))
            if want > have:                     # 累积型: 只增不减
                bonus += (want - have) * r.pct
                layers[r.rule_id] = want
            continue
        # benefit_missing: 阶梯型 -> 每轮重算, 可增可减
        if cur >= float(r.threshold):
            want = 0
        else:
            missing = max(0.0, float(r.threshold) - cur)
            want = min(int(r.max_layers),
                       int(r.base_layers) + (int(missing // r.per) if r.per else 0))
        have = int(layers.get(r.rule_id, 0))
        if want != have:
            bonus += (want - have) * r.pct
            layers[r.rule_id] = want
    return bonus


_GATE_ATTR: dict[str, int] = {
    "verse_point": C.EFF_VERSE_POINT,
    "culture_point": C.EFF_CULTURE_POINT,
    "popular_support": C.EFF_POPULAR_SUPPORT,
}


def _apply_draw_rules(rules: Sequence[E.DrawRule], state: Mapping[str, float],
                      granted: dict[str, float], counters: dict[str, int],
                      drawn_style,
                      bonus: float, positive_only: bool,
                      singer_policy_ids: frozenset[int] = frozenset(),
                      multipliers: dict[str, float] | None = None,
                      grant_counts: dict[str, int] | None = None,
                      trace: dict[str, float] | None = None,
                      labels: Mapping[str, str] | None = None,
                      ) -> tuple[int, int]:
    """执行每次择律触发的政策规则。

    返回 (总触发次数, 其中属于唱词人效果池的触发次数)。

    `drawn_style` 可以是单个风格常量, 也可以是**风格集合** —— 李纲「病牛」★2/★3
    的「正确择【婉约】律时，同时视为择【豪放】律」会让一次婉约抽取同时对
    `bold` 与 `graceful` 两类条件成立。

    gate='bold'/'graceful' 比较的是**本次抽到的词句风格**:
    本系统没有"玩家选择题律"这一步 —— 抽豪放还是婉约只由唱词人两个权重决定,
    且默认答对 (用户确认「默认无条件正确」), 故「正确择豪放律」== 抽到豪放词。

    `grant_counts` 记录每条规则已触发次数, 用于落实 `max_grants` (「至多N层」/「上限X%」)。

    **收益加成 (32513) 不作用于本函数的规则收益**（用户 2026-10-07 实机口径）：
    只有词句自身的属性加成吃「择律收益」加成，唱词人/词人/名臣等规则收益一律按原值结算。
    因此 `handler` 的 `grant` 与 `grant_unscaled` 现在数值等价（保留字段以备后用）。
    """
    styles = ({int(drawn_style)} if isinstance(drawn_style, int)
              else {int(x) for x in drawn_style})
    fired = 0
    singer_fired = 0
    counts = grant_counts if grant_counts is not None else {}
    for r in rules:
        if r.handler in ("none", "benefit_per_10", "benefit_missing"):
            continue
        if r.max_grants and counts.get(r.rule_id, 0) >= r.max_grants:
            continue
        if r.gate == "bold" and M.STYLE_BOLD not in styles:
            continue
        if r.gate == "graceful" and M.STYLE_GRACEFUL not in styles:
            continue
        if r.gate in _GATE_ATTR:
            attr = E.EFFECT_ATTRIBUTE.get(_GATE_ATTR[r.gate], r.gate)
            v = float(state.get(attr, 0.0))
            if v < r.min_value:
                continue
            if r.max_value and v > r.max_value:
                continue
        # 附加属性条件 (与 gate 同时生效): 李清照「如梦令」31651 需「抽到婉约词」
        # **且**「民心≥40」, DB condition_effect=51 / condition_values=[40, 99999]。
        if r.min_attr:
            eff_id = _GATE_ATTR.get(r.min_attr)
            attr = E.EFFECT_ATTRIBUTE.get(eff_id, r.min_attr) if eff_id else r.min_attr
            v = float(state.get(attr, 0.0))
            if v < r.min_value:
                continue
            if r.max_value and v > r.max_value:
                continue
        if not r.effects:
            continue
        # **「择律收益」加成只放大词句来源的收益** (用户 2026-10-07 裁定实机口径):
        #   词句自身的属性加成吃加成; 唱词人 / 词人 / 名臣等政策规则的收益**不吃**加成。
        #   早期实现把 handler="grant" 的规则也一并放大, 于是唱词人 8 暮烟
        #   「歌板+1」会随加成涨到 +2.9/轮, 显著高估收入 (达标率 99% vs 59%)。
        _b = granted.get("board", 0.0) if trace is not None else 0.0
        _apply_effects(granted, counters, r.effects, bonus, positive_only,
                       amplify=False, multipliers=multipliers)
        if trace is not None:
            d = granted.get("board", 0.0) - _b
            if d:
                label = (labels or {}).get(r.rule_id) or r.rule_id
                trace[label] = trace.get(label, 0.0) + d
        counts[r.rule_id] = counts.get(r.rule_id, 0) + 1
        fired += 1
        if r.source_policy_id in singer_policy_ids:
            singer_fired += 1
    return fired, singer_fired


# ---------------------------------------------------------------------------
# 单局
# ---------------------------------------------------------------------------

def run_once(game: E.GameData, config: M.SimulationConfig,
             rng: random.Random | None = None) -> M.SimulationResult:
    """运行一局模拟。rng 由调用方提供以保证可复现。"""
    if game is None:
        raise ValueError("game 不能为 None; 请先用 effects.load_game_data() 装配")

    rng = rng if rng is not None else random.Random(config.seed)
    record = bool(config.record_events)
    positive_only = bool(config.bias_bonus_to_positive_only)
    cap = int(config.sentiment_cap)

    counters: dict[str, int] = {"card_grants": 0, "unknown": 0, "unknown_value": 0.0, "counted": 0}
    runtime = game.mods.copy()

    # ---- 拥有关系 ----
    # 注: 开局词人的一次性效果**已经**由 `load_game_data` 编译进 `game.mods`
    #     (`load_startup_policy_mods` 按 policy.id 全量编译)。此处**绝不能**再 merge
    #     `game.poet_mods[pid]`, 否则每个开局词人的效果都会被结算两次
    #     —— 苏轼「豪放/婉约词情+5」会变成 +10、「择律开题消耗-1」会变成 -2、
    #     岳飞「威望-20」会变成 -40。用户 2026-10-03 指出苏轼效果可疑, 即此因。
    #     `poet_mods` 仅用于**局中解锁**的新词人 (见下面第 6 步)。
    owned_poets: set[int] = {int(x) for x in config.poet_ids}
    owned_ministers: set[int] = {int(x) for x in config.minister_ids}

    # ---- 东坡食单 (苏轼「老饕」开启; 未勾苏轼名臣则不生效) ----
    # 每道菜的效果来自 CommonEffectPoolConfig 652-660; 价格不收取 (用户 2026-10-03)。
    # 9 道全收集且勾了苏轼 -> 老饕 ★2「收集所有食单 豪放词情+20层」。
    dongpo_ids: list[int] = []
    if C.SU_SHI_MINISTER_ID in owned_ministers:
        dongpo_ids = sorted({int(x) for x in (config.dongpo_food_ids or [])})
        for fid in dongpo_ids:
            dm = game.dongpo_mods.get(fid)
            if dm:
                runtime.merge(dm)
        if set(dongpo_ids) >= set(C.DONGPO_COMPLETE_FOOD_IDS):
            runtime.add_sentiment_bonus(bold=int(round(game.dongpo_complete_bold)))

    # ---- 初始状态 ----
    state: dict[str, float] = {k: float(v) for k, v in config.init_attributes.items()}
    state["board"] = float(config.init_board or 0.0)
    state["verse_point"] = float(config.init_verse_point)
    for key in M.ATTRIBUTE_LABELS:
        state.setdefault(key, 0.0)
    # 民心/威望/军心/腐化 的**设定值**有值域 0~100 (用户 2026-10-05);
    # 只钳制开局设定值, 结算过程中不受此约束。
    for attr, (lo, hi) in M.INIT_ATTRIBUTE_BOUNDS.items():
        state[attr] = min(max(state.get(attr, 0.0), lo), hi)
    for attr, delta in runtime.startup_attrs.items():
        state[attr] = state.get(attr, 0.0) + _floor_grant(attr, delta)
    # 开局一次性里的百分比效果 (add_type=2): 后于加算生效, 结果同样取整
    for attr, mult in runtime.startup_mults.items():
        state[attr] = _floor_grant(attr, state.get(attr, 0.0) * (1.0 + mult))

    bold = max(0, min(cap, int(config.init_bold_sentiment) + int(runtime.sentiment_bonus_bold)))
    graceful = max(0, min(cap, int(config.init_graceful_sentiment) + int(runtime.sentiment_bonus_graceful)))
    bonus = float(config.init_benefit_bonus) + runtime.benefit_bonus

    # 繁荣上限 (EffectTypeConfig 31480 的 MaxValue=50; 用户 2026-10-03 确认设上限)
    prosperity_cap: float | None = float(C.PROSPERITY_CAP)
    state["prosperity"] = min(state.get("prosperity", 0.0), prosperity_cap)

    # ---- 生效规则 ----
    active_rules = _rules_for(config, game)
    rule_layers: dict[str, int] = {}
    # 每条逐次规则的已触发次数 (用于 max_grants 上限)
    grant_counts: dict[str, int] = {}
    # 本局「已抽过的词条」(按风格各自不放回的依据); 每局重置
    drawn_verse_ids: set[int] = set()
    # 本局局中解锁的词人及其解锁轮次 (供结果查看统计, 不依赖事件流)
    unlock_turns: list[tuple[int, int]] = []

    singer = game.singer_rules.get(int(config.singer_id)) if config.singer_id is not None else None
    singer_policy_ids: frozenset[int] = (
        _SINGER_DRAW_POLICY_IDS.get(int(config.singer_id), frozenset())
        if config.singer_id is not None else frozenset())
    if singer is not None and singer.module == "passive_bonus":
        bonus += singer.bonus

    result = M.SimulationResult()
    # 第一次择律之前的属性快照: 结果报告里的「波动」以此为零点
    result.initial_attributes = dict(state)
    turn = 0
    stop_reason = M.STOP_INSUFFICIENT
    # 李纲「病牛」★2/★3 的开关: 持有该开关名臣时, 抽到婉约词亦视为抽到豪放词
    _as_bold_ministers = set(game.graceful_as_bold_ministers)

    while True:
        if turn >= int(config.max_turns):
            stop_reason = M.STOP_MAX_TURNS
            break

        cost = max(0.0, float(config.draw_base_cost) + runtime.draw_cost_mod)
        if state["board"] < cost:
            stop_reason = M.STOP_INSUFFICIENT
            break

        # 3. 抽取: 按唱词人权重先定风格, 再从该风格池抽一条
        #    抽取规则 (用户确认): **前期不放回** —— 按风格各自不放回,
        #    候选池 = 该风格「当前可抽条目」中的未抽条目;
        #    仅当候选为空 (该风格当前可抽池已被抽干) 时才转为放回抽取 (允许重复)。
        #    池子会随词人解锁而动态扩大, 故"抽干"是按当前可抽池判定。
        drawn_style = _pick_weighted_style(config, game, rng)
        full_pool = game.verses.get(drawn_style,
                                    unlock_all=config.unlock_all_verses,
                                    owned_minister_ids=owned_ministers)
        # 修正: 「擅长词牌」此前完全未参与抽取 (README 的 C-2 / 明确未建模)。
        # 真实谓词是 **含「全部」 ∨ 风格匹配 ∨ 词牌∈擅长词牌**，因此擅长词牌是
        # **跨风格放宽**（能在别的律下唱自己擅长的词牌），过滤后不足候选数时回退全量。
        # 谓词: 含「全部」 ∨ 词句风格 == 抽到的律 ∨ 词牌 ∈ 擅长词牌；
        # 过滤后不足候选数时回退全量。
        if getattr(config, "specialty_pool_rule", True) and getattr(game, "singer_specialty", None):
            spec = game.singer_specialty.get(int(config.singer_id or -1), ())
            if spec:
                all_pool = tuple(
                    v for st in (M.STYLE_BOLD, M.STYLE_GRACEFUL)
                    for v in game.verses.get(st, unlock_all=config.unlock_all_verses,
                                             owned_minister_ids=owned_ministers))
                full_pool = tuple(GR.specialty_pool(
                    all_pool, spec, drawn_style,
                    int(GR.SONGCI_VALUE["OptionCount"]), all_verses=full_pool))
        if not full_pool:
            stop_reason = M.STOP_INSUFFICIENT
            break

        candidates = [v for v in full_pool if v.id not in drawn_verse_ids]
        in_replacement = not candidates
        if in_replacement:
            verse = full_pool[rng.randrange(len(full_pool))]
            result.repeat_draws += 1
            is_new_verse = False
        else:
            verse = candidates[rng.randrange(len(candidates))]
            is_new_verse = True
            drawn_verse_ids.add(verse.id)
            result.new_verse_draws += 1

        turn += 1

        state["board"] -= cost

        bonus = _update_growth_rules(active_rules, rule_layers, state, bonus)
        factor = 1.0 + bonus
        benefit_applied = bonus          # 本轮实际生效的择律收益加成

        snapshot = dict(state)
        granted: dict[str, float] = {}
        # add_type=2 的百分比乘算修正 (本轮的): attribute -> Σ倍率
        multipliers: dict[str, float] = {}
        bold_triggered = False
        graceful_triggered = False
        # **歌板变动归因** (仅在记录事件流时收集, 不影响热路径性能):
        #   标签 -> 本笔增量, 满足 余额 = 上一次余额 − 消耗 + Σ(trace)
        board_trace: dict[str, float] = {}
        board_src_label = f"词句{verse.id}({verse.ci_name})"

        def _take(label: str, before: float) -> None:
            """把「本笔对歌板的净影响」记进归因表 (0 不记)。"""
            if not record:
                return
            d = granted.get("board", 0.0) - before
            if d:
                board_trace[label] = board_trace.get(label, 0.0) + d

        # 5. 词句效果: 预计算的属性效果内联 (热路径), 特殊效果 (881/词情等) 另走慢路径
        _b = granted.get("board", 0.0)
        for attr, value in verse.attr_effects:
            # 修正: 「后续择律收益提升」(32513) **只放大 7 种效果**
            # （只有 game_rules.REWARD_MULTIPLY_TYPES 里的 7 种吃倍率）。
            # 战斗力/发展年数/乐感/词情/诗意/灵犀等都不吃这个倍率。
            if bonus and (attr in GR.REWARD_MULTIPLY_ATTRS
                          or not getattr(config, "reward_multiply_whitelist", True)):
                if value > 0.0:
                    value = value * factor
                elif not positive_only:
                    value = value * factor
            # 择律得到的绝对值一律取整 (用户 2026-10-03 / 2026-10-05):
            # 例: 条目基础 2 歌板 × 收益系数 3.95 = 7.9 -> **7**。
            value = _floor_grant(attr, value)
            granted[attr] = granted.get(attr, 0.0) + value
        _take(board_src_label, _b)
        if verse.special_effects:
            _b = granted.get("board", 0.0)
            _apply_effects(granted, counters, verse.special_effects, bonus, positive_only,
                           whitelist_only=getattr(config, "reward_multiply_whitelist", True))
            _take(f"{board_src_label}·特殊效果", _b)

        # 6. 词人解锁 —— **只结算「词人效果」, 不带名臣效果**
        #    (用户 2026-10-03 确认: 作为词人被解锁后只会有词人效果而不会有名臣效果。
        #     早期实现会把同名词人的名臣政策一并并入, 属于错误。)
        poet_unlocked: int | None = None
        for pid in verse.poet_ids:
            if pid in owned_poets:
                continue
            owned_poets.add(pid)
            poet_unlocked = pid
            unlock_turns.append((int(pid), int(turn)))
            pm = game.poet_mods.get(pid)
            if pm:
                runtime.merge(pm)
                _b = granted.get("board", 0.0)
                for attr, delta in pm.startup_attrs.items():
                    # 局中解锁词人带来的属性增减也属于"择律"产物 -> 同样取整
                    granted[attr] = granted.get(attr, 0.0) + _floor_grant(attr, delta)
                _take(f"解锁词人{pid}·{game.poet_name(pid)}", _b)
                bonus += pm.benefit_bonus
                # **词情层数加成也必须立刻生效** (苏轼词人「豪放+5 / 婉约+5」)。
                # 早期只补了 startup_attrs 与 benefit_bonus, 漏掉词情 ——
                # 于是 61 条婉约词全抽完也只有 61 层, 而不是 61+5=66 层 (用户指出)。
                if pm.sentiment_bonus_bold:
                    bold = max(0, min(cap, bold + int(pm.sentiment_bonus_bold)))
                if pm.sentiment_bonus_graceful:
                    graceful = max(0, min(cap, graceful + int(pm.sentiment_bonus_graceful)))
            newly = POET_DRAW_POLICY_IDS.get(pid, frozenset())
            if newly:
                # 防重复: 同一规则已在生效集合里就不再加 (否则会每轮结算两次)
                have = {id(r) for r in active_rules}
                active_rules.extend(r for r in game.rules_for_policies(newly)
                                    if id(r) not in have)
            break
        if poet_unlocked is not None:
            result.poets_unlocked += 1
            if singer is not None and singer.module == "on_poet_unlock" \
                    and singer.effect_type is not None:
                _b = granted.get("board", 0.0)
                _apply_effect(granted, counters, singer.effect_type, singer.effect_value)
                _take(f"唱词人{game.singer_name(singer.singer_id)}·解锁词人时", _b)
                result.singer_triggered += 1

        # 唱词人 苏轸: 正确择苏轼词
        if singer is not None and singer.module == "su_shi" \
                and singer.target_poet_id in verse.poet_ids:
            _b = granted.get("board", 0.0)
            if singer.effect_type is not None:
                _apply_effect(granted, counters, singer.effect_type, singer.effect_value)
            if singer.sentiment_bonus:
                _apply_effect(granted, counters, C.EFF_BOLD_SENTIMENT,
                              float(singer.sentiment_bonus))
                _apply_effect(granted, counters, C.EFF_GRACEFUL_SENTIMENT,
                              float(singer.sentiment_bonus))
            _take(f"唱词人{game.singer_name(singer.singer_id)}·唱中本命词人", _b)
            result.singer_triggered += 1

        # 每次择律触发的政策规则; singer_policy_ids 内的触发同时计入 singer_triggered
        # 李纲「病牛」★2/★3: 抽到婉约词时**同时视为**抽到豪放词 ——
        # 于是 bold / graceful 两类条件同时成立 (词情累加与触发判定见下)。
        as_bold = (drawn_style == M.STYLE_BOLD) or (
            drawn_style == M.STYLE_GRACEFUL
            and bool(_as_bold_ministers & owned_ministers))
        as_graceful = (drawn_style == M.STYLE_GRACEFUL)
        eff_styles = set()
        if as_bold:
            eff_styles.add(M.STYLE_BOLD)
        if as_graceful:
            eff_styles.add(M.STYLE_GRACEFUL)

        fired, singer_fired = _apply_draw_rules(
            active_rules, state, granted, counters, eff_styles, bonus,
            positive_only, singer_policy_ids=singer_policy_ids,
            multipliers=multipliers, grant_counts=grant_counts,
            trace=board_trace if record else None, labels=game.rule_labels)
        result.singer_triggered += singer_fired
        _ = fired

        # 7. 词情累加 (用户确认: **只有抽到本局尚未抽过的条目才叠加**;
        #    重复条目不再叠加。层数上限 cap)
        #    注: 放回阶段抽到的必然都是已抽过的条目, 故不叠加。
        if is_new_verse:
            if as_bold:
                bold = min(cap, bold + 1)
            if as_graceful:
                graceful = min(cap, graceful + 1)

        sent_bold = int(granted.pop(SENTIMENT_BOLD_KEY, 0.0))
        sent_graceful = int(granted.pop(SENTIMENT_GRACEFUL_KEY, 0.0))
        if sent_bold:
            bold = min(cap, bold + sent_bold)
        if sent_graceful:
            graceful = min(cap, graceful + sent_graceful)

        # 8. 词情触发 (层数永久累积、不消耗)
        # 无"选择题律"步骤, 默认答对 => 抽到豪放词即「正确择豪放律」, 抽到婉约词即「正确择婉约律」
        # 病牛开关下, 一次婉约抽取会**同时**掷豪放词情与婉约词情两个判定。
        if as_bold and bold > 0:
            if rng.random() < sentiment_trigger_probability(bold):
                bold_triggered = True
                result.sentiment_trigger_count += 1
                result.bold_triggers += 1
                _b = granted.get("board", 0.0)
                _apply_effect(granted, counters, C.EFF_BOARD, 1.0)
                _apply_effect(granted, counters, C.EFF_ARMY_MORALE, 1.0)
                _take("豪放词情触发", _b)
        if as_graceful and graceful > 0:
            if rng.random() < sentiment_trigger_probability(graceful):
                graceful_triggered = True
                result.sentiment_trigger_count += 1
                result.graceful_triggers += 1
                _b = granted.get("board", 0.0)
                _apply_effect(granted, counters, C.EFF_BOARD, 2.0)
                _apply_effect(granted, counters, C.EFF_POPULAR_SUPPORT, -1.0)
                _take("婉约词情触发", _b)

        # 逐次「后续择律收益」加成 (王安石词人政策 30028: 每次 +1%) -> 累加进本局 bonus
        _gain = granted.pop(BENEFIT_BONUS_KEY, 0.0)
        if _gain:
            bonus += float(_gain)

        # 统一结算到 state
        for attr, delta in granted.items():
            if attr.startswith("__"):
                continue
            state[attr] = state.get(attr, 0.0) + delta
        # add_type=2 的百分比乘算: 先加算后乘算 (顺序: 加算 -> 乘算)
        # 乘算结果同样取整 —— 否则属性会留下小数 (用户要求所有择律结果都是整数)。
        for attr, mult in multipliers.items():
            if attr.startswith("__"):
                continue
            _b = state.get(attr, 0.0)
            state[attr] = _floor_grant(attr, state.get(attr, 0.0) * (1.0 + mult))
            if record and attr == "board" and state[attr] != _b:
                board_trace["百分比乘算"] = board_trace.get("百分比乘算", 0.0) + (state[attr] - _b)
        # 下限: 只有歌板/词元不允许为负 (M.NON_NEGATIVE_ATTRIBUTES)。
        # 民心/军心/腐化 **不设上下限** —— 虽然 `EffectTypeConfig` 里它们写着 `[0,100]`,
        # 但用户 2026-10-03 明确「民心军心腐化都不设上下限」, 故允许为负、也不封顶。
        for attr in M.NON_NEGATIVE_ATTRIBUTES:
            if state.get(attr, 0.0) < 0.0:
                if record and attr == "board":
                    board_trace["下限钳制(歌板不为负)"] = (
                        board_trace.get("下限钳制(歌板不为负)", 0.0) - state[attr])
                state[attr] = 0.0
        # 上限: 只有繁荣 (EffectTypeConfig 31480 的 MaxValue=50)
        if prosperity_cap is not None:
            state["prosperity"] = min(state.get("prosperity", 0.0), prosperity_cap)

        if counters["card_grants"]:
            result.card_grants += counters["card_grants"]
            counters["card_grants"] = 0

        result.style_counts[drawn_style] = result.style_counts.get(drawn_style, 0) + 1
        result.verse_counts[verse.id] = result.verse_counts.get(verse.id, 0) + 1

        if record:
            result.events.append(M.TurnEvent(
                turn=turn,
                drawn_style=drawn_style,
                verse_id=verse.id,
                verse_name=verse.ci_name,
                is_new_verse=is_new_verse,
                in_replacement=in_replacement,
                poet_unlocked=poet_unlocked,
                poet_name=game.poet_name(poet_unlocked),
                cost=cost,
                board_after=state.get("board", 0.0),
                verse_point_after=state.get("verse_point", 0.0),
                bold_sentiment=bold,
                graceful_sentiment=graceful,
                bold_triggered=bold_triggered,
                graceful_triggered=graceful_triggered,
                treat_as_bold=bool(as_bold and drawn_style != M.STYLE_BOLD),
                benefit_applied=float(benefit_applied),
                benefit_bonus_after=float(bonus),
                granted={k: v for k, v in granted.items() if not k.startswith("__")},
                board_trace=tuple(board_trace.items()),
            ))

        if state.get("board", 0.0) >= float(config.end_threshold):
            stop_reason = M.STOP_THRESHOLD
            break

    result.turns = turn
    result.stop_reason = stop_reason
    result.bold_sentiment = bold
    result.graceful_sentiment = graceful
    result.benefit_bonus = float(bonus)
    result.poet_unlock_turns = tuple(unlock_turns)
    result.attributes = dict(state)
    result.unknown_effect_grants = float(counters.get("unknown_value", 0.0))
    return result


# ---------------------------------------------------------------------------
# 唱词人效果池映射
# ---------------------------------------------------------------------------

# 说明: SongCiSingerConfig 的 Trigger/RemoveCommonEffectPoolConfigId
# (451-458/650/651) 实测指向**军事政策** (徵兵制/募兵制/弩兵/…), 是占位数据;
# 真正的唱词人效果政策是 30000-30007 (KeyID 4219-4226) 与 40014/40036,
# 经与 FeatureDesc 逐字比对确认 (9/9 唱词人中有 2 位逐字相同、其余为逻辑等价)。
_SINGER_DRAW_POLICY_IDS: dict[int, frozenset[int]] = {
    1: frozenset({30000}),
    2: frozenset({30001}),
    3: frozenset({30002}),
    4: frozenset({30003}),
    5: frozenset({30004}),
    6: frozenset({30005}),
    7: frozenset({30006}),
    8: frozenset({30007}),
    9: frozenset({40014, 40036}),
}

# 词人 ID -> 其逐次生效的子政策 ID (词人解锁后并入生效规则)
POET_DRAW_POLICY_IDS: dict[int, frozenset[int]] = {
    10: frozenset({30051}),      # 陆游
    12: frozenset({30052}),      # 贺铸
    14: frozenset({30053}),      # 范仲淹
    17: frozenset({30054}),      # 李纲
    21: frozenset({30028}),      # 王安石 (词人政策本身的逐次效果)
}

# 名臣 ID -> 其逐次生效的择律政策 ID (只纳入实际配置的名臣, 避免空转)
# 注: 李纲(311) 的「十事」(20901/30902) **已停用** —— TriggerTime=27 =「每当完成政策后」,
#     不是择律事件; 其病牛「视为豪放」开关走 `graceful_as_bold_ministers`, 不在此表。
MINISTER_DRAW_POLICY_IDS: dict[int, frozenset[int]] = {
    300: frozenset({31407, 31408, 31409}),          # 岳飞 背嵬军
    307: frozenset({30601, 30621}),                  # 范仲淹 明黜陟
    311: frozenset(),                                # 李纲 (十事已停用)
    314: frozenset({31081, 3111, 3112, 3113}),       # 陆游 豪放 + 示儿
    321: frozenset(),                                # 辛弃疾 (逐次部分未实现)
    322: frozenset({31651, 31711, 31712, 3168, 3169, 3170}),   # 李清照
    327: frozenset({31621, 31631, 31641}),           # 柳永 恋情词
    328: frozenset({40014}),                         # 苏轼 苏轸效果
    331: frozenset({32300, 32301}),                  # 王安石 以词言志
    332: frozenset({32360}),                         # 欧阳修 尚古文
}


# ---------------------------------------------------------------------------
# 批量
# ---------------------------------------------------------------------------

def run_batch(game: E.GameData, config: M.SimulationConfig,
              progress: Any = None, cancel: Any = None) -> M.BatchSummary:
    """按 config.runs 跑多局, 共享同一 Random(seed) 流以保证可复现。

    progress: 可选回调 progress(done, total); cancel: 可选可调用对象, 返回 True 时中止。
    """
    master = random.Random(config.seed)
    results: list[M.SimulationResult] = []
    total = max(0, int(config.runs))
    # 事件流只用于 `raw/择律事件流.csv`。用户 2026-10-05 口径:
    #   **不设上限, 但只随机挑 10 局记录全部流程** —— 既能看到完整逐次过程,
    #   又不会因为 10000 局 × ~400 轮把内存撑爆 (旧实现是全局 20 万条上限)。
    record_idx: set[int] = set()
    if config.record_events and total:
        k = min(int(C.EVENT_FULL_RUNS), total)
        # 用配置种子派生, 保证同一配置+种子可复现
        record_idx = set(random.Random(f"{config.seed}:events").sample(range(total), k))
    for i in range(total):
        if cancel is not None and cancel():
            break
        cfg_i = config
        if config.record_events and i not in record_idx:
            cfg_i = dataclasses.replace(config, record_events=False)
        results.append(run_once(game, cfg_i, master))
        if progress is not None:
            progress(i + 1, total)
    summary = M.BatchSummary(config=config, runs=len(results), results=results)
    summary.event_run_indices = tuple(sorted(record_idx))
    return summary
