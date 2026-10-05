# -*- coding: utf-8 -*-
"""M4 · 统计聚合 (纯函数, 只消费 src/model.py 的契约)。

设计要点
--------
* **标准差口径统一为样本标准差 (ddof=1)**, 与 ``numpy.std(x, ddof=1)`` 一致。
  ddof=0 (总体标准差) 仅适用于"整个总体已观测完毕"的场景; 本项目中每局
  ``SimulationResult`` 只是蒙特卡洛抽样中的一个样本, 故一律用 ddof=1。
  单样本 (n=1) 时样本标准差无定义, 按约定返回 ``None``。
* 所有输出均为**可 JSON 序列化的原生 Python 类型** (float/int/list/dict),
  便于 ``配置快照.json`` / GUI / 后续报告直接消费。
* 不 import simulator, 仅依赖 model 契约。
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

import numpy as np

try:  # 允许以 `python src/stats.py` 或 `import stats` 两种方式加载
    import config as _C
    import model as M
except ImportError:  # pragma: no cover
    from . import config as _C  # type: ignore
    from . import model as M  # type: ignore

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 统计所用标准差自由度口径。1 = 样本标准差 (n-1)。**全项目统一**。
DD_OF = 1

#: 分位数口径: 线性插值, 等价 numpy.percentile(..., method="linear")
PERCENTILES: tuple[tuple[str, float], ...] = (
    ("p5", 5.0),
    ("p25", 25.0),
    ("p50", 50.0),
    ("p75", 75.0),
    ("p95", 95.0),
)

#: Top-N 词句抽取榜长度
TOP_N_VERSES = 20

#: 卡方检验显著性水平
CHI2_ALPHA = 0.05


# ---------------------------------------------------------------------------
# 基础统计工具
# ---------------------------------------------------------------------------


def to_float_list(values: Iterable[Any]) -> list[float]:
    """把任意可迭代数值转成 float 列表, 跳过 None/NaN/非数值。"""
    out: list[float] = []
    for v in values:
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isnan(f) or math.isinf(f):
            continue
        out.append(f)
    return out


def summarize_series(values: Sequence[float] | Iterable[float]) -> dict[str, Any]:
    """一组数值的描述性统计。

    返回键: count / mean / std / min / max / p5 / p25 / p50 / p75 / p95 / ddof / iqr

    * ``std`` 为**样本标准差 (ddof=1)**; n<=1 时为 ``None``。
    * 空输入时除 count(=0) 与 ddof 外全部为 ``None``, 不抛异常。
    """
    xs = to_float_list(values)
    n = len(xs)
    base: dict[str, Any] = {
        "count": n,
        "mean": None,
        "std": None,
        "min": None,
        "max": None,
        "p5": None,
        "p25": None,
        "p50": None,
        "p75": None,
        "p95": None,
        "iqr": None,
        "ddof": DD_OF,
    }
    if n == 0:
        return base

    arr = np.asarray(xs, dtype=float)
    base["mean"] = float(arr.mean())
    base["std"] = float(arr.std(ddof=DD_OF)) if n > 1 else None
    base["min"] = float(arr.min())
    base["max"] = float(arr.max())
    pcts = {name: float(np.percentile(arr, q, method="linear")) for name, q in PERCENTILES}
    base.update(pcts)
    base["iqr"] = float(pcts["p75"] - pcts["p25"])
    return base


def attribute_report_series(key: str, results: Sequence[M.SimulationResult]) -> list[float]:
    """按结果口径取某属性的逐局序列: 歌板取**终值**, 其余取**波动** (终值 − 开局结算后)。"""
    if not M.uses_delta(key):
        return [float(r.attributes.get(key, 0.0)) for r in results]
    return [float(r.attributes.get(key, 0.0))
            - float((getattr(r, "initial_attributes", None) or {}).get(key, 0.0))
            for r in results]


def median_of(values: Sequence[float] | Iterable[float]) -> float | None:
    xs = to_float_list(values)
    if not xs:
        return None
    return float(np.median(np.asarray(xs, dtype=float)))


# ---------------------------------------------------------------------------
# 卡方检验 (自实现, 不依赖 scipy)
# ---------------------------------------------------------------------------


def _gammaincc(a: float, x: float) -> float:
    """正则化上不完全 Gamma 函数 Q(a, x) = 1 - P(a, x)。

    数值方法: 级数展开 (x < a+1) 与 连分式 (x >= a+1), 见 Numerical Recipes 6.2。
    精度足以满足拟合优度检验 (p 值只需 1e-6 量级)。
    """
    if x < 0 or a <= 0:
        return float("nan")
    if x == 0:
        return 1.0

    gln = math.lgamma(a)
    eps = 1e-12
    itmax = 500
    tiny = 1e-300

    if x < a + 1.0:
        # 级数展开求 P(a, x)
        ap = a
        total = 1.0 / a
        delta = total
        for _ in range(itmax):
            ap += 1.0
            delta *= x / ap
            total += delta
            if abs(delta) < abs(total) * eps:
                break
        p = total * math.exp(-x + a * math.log(x) - gln)
        return max(0.0, min(1.0, 1.0 - p))

    # 连分式求 Q(a, x)
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, itmax + 1):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    q = math.exp(-x + a * math.log(x) - gln) * h
    return max(0.0, min(1.0, q))


def chi2_sf(statistic: float, df: int) -> float:
    """卡方分布上尾概率 P(X > statistic)。df 为自由度。"""
    if df <= 0:
        return float("nan")
    if statistic <= 0:
        return 1.0
    return _gammaincc(df / 2.0, statistic / 2.0)


def chi2_goodness_of_fit(
    observed: Sequence[float],
    expected: Sequence[float] | None = None,
    *,
    ddof_params: int = 0,
) -> dict[str, Any]:
    """卡方拟合优度检验。

    参数
    ----
    observed : 各单元格实际频数
    expected : 各单元格理论频数; None 时按"均匀分布"处理 (总和/单元格数)
    ddof_params : 估计参数个数 (如 Poisson 估计了 λ 则为 1), 用于扣自由度

    返回: ``{"statistic", "df", "p_value", "valid", "note", "cells"}``

    期望频数 < 5 的单元格会被合并 (尾部合并), 这是卡方检验的常规做法;
    若合并后仅剩 1 个单元格, 则标记 ``valid=False`` 并给出说明, 不抛异常。
    """
    obs = to_float_list(observed)
    if len(obs) == 0:
        return {"statistic": None, "df": 0, "p_value": None, "valid": False,
                "note": "无观测数据", "cells": 0}

    if expected is None:
        total = sum(obs)
        exp = [total / len(obs)] * len(obs)
    else:
        exp = to_float_list(expected)
        if len(exp) != len(obs):
            return {"statistic": None, "df": 0, "p_value": None, "valid": False,
                    "note": f"理论频数与实际频数长度不一致 ({len(exp)} vs {len(obs)})",
                    "cells": 0}

    # 归一化: 若理论频数总和与实际频数总和不等 (例如只取分布主体而截断了尾部),
    # 按比例缩放到同一总量, 否则卡方统计量会被"总量差"污染。
    sum_o = sum(obs)
    sum_e = sum(exp)
    if sum_o > 0 and sum_e > 0 and abs(sum_e - sum_o) > 1e-9 * max(sum_o, sum_e):
        scale = sum_o / sum_e
        exp = [e * scale for e in exp]
    exp_raw = list(exp)      # 合并前的归一化期望频数 (与 observed 一一对应)

    # 合并期望频数 < 5 的单元格 (从左到右累积尾合并)
    merged_obs: list[float] = []
    merged_exp: list[float] = []
    acc_o = acc_e = 0.0
    for o, e in zip(obs, exp):
        acc_o += o
        acc_e += e
        if acc_e >= 5.0:
            merged_obs.append(acc_o)
            merged_exp.append(acc_e)
            acc_o = acc_e = 0.0
    if acc_e > 0:  # 残留并入最后一格
        if merged_exp:
            merged_obs[-1] += acc_o
            merged_exp[-1] += acc_e
        else:
            merged_obs.append(acc_o)
            merged_exp.append(acc_e)

    k = len(merged_obs)
    if k < 2:
        total = sum(merged_exp)
        return {"statistic": 0.0 if total > 0 else None, "df": 0, "p_value": None,
                "valid": False,
                "note": "合并后期望频数不足以支撑卡方检验 (需 >=2 个单元格)",
                "cells": k}

    stat = 0.0
    for o, e in zip(merged_obs, merged_exp):
        if e <= 0:
            continue
        stat += (o - e) ** 2 / e

    df = k - 1 - max(0, ddof_params)
    if df <= 0:
        return {"statistic": float(stat), "df": 0, "p_value": None, "valid": False,
                "note": "自由度不足", "cells": k}

    return {
        "statistic": float(stat),
        "df": int(df),
        "p_value": float(chi2_sf(stat, df)),
        "valid": True,
        "note": "",
        "cells": k,
        # 合并后的实际/期望频数 (两者一一对应并在同一组内), 供调用方交叉验证。
        # 注意"尾合并"通常会使 df 变小、卡方统计量也变小, 故交叉验证必须在
        # 这两组数组上进行, 而不能与未合并的 scipy.chisquare 结果直接比较。
        "observed_merged": [float(o) for o in merged_obs],
        "expected_normalized": [float(e) for e in merged_exp],
        # 合并前的归一化期望频数 (与传入的 observed 一一对应)
        "expected_raw_normalized": [float(e) for e in exp_raw],
    }


def chi2_critical_value(df: int, alpha: float = CHI2_ALPHA) -> float:
    """卡方分布 alpha 上分位点 (用于不依赖 p 值的硬断言)。二分法求解。"""
    if df <= 0:
        return float("nan")
    lo, hi = 0.0, 1.0
    while chi2_sf(hi, df) > alpha and hi < 1e7:
        hi *= 2.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if chi2_sf(mid, df) > alpha:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def poisson_pmf(k: int, lam: float) -> float:
    """Poisson 概率质量函数 (k >= 0)。"""
    if k < 0 or lam <= 0:
        return 0.0
    return math.exp(-lam + k * math.log(lam) - math.lgamma(k + 1))


# ---------------------------------------------------------------------------
# 逐局 / 逐次采集
# ---------------------------------------------------------------------------


def collect_events(results: Sequence[M.SimulationResult]) -> list[M.TurnEvent]:
    """把所有局的事件流拉平成一个列表。"""
    out: list[M.TurnEvent] = []
    for r in results:
        ev = getattr(r, "events", None)
        if ev:
            out.extend(ev)
    return out


def has_events(results: Sequence[M.SimulationResult]) -> bool:
    """是否存在可用的事件流 (config.record_events=False 时为 False)。"""
    return any(getattr(r, "events", None) for r in results)


def trajectory_series(
    results: Sequence[M.SimulationResult],
    attr: str,
) -> dict[str, list[float | None]]:
    """逐次择律的轨迹聚合 (跨局按 turn 序号对齐)。

    attr 取值: ``"board"`` / ``"verse_point"`` / ``"sentiment"``
    返回 ``{"turns": [...], "p25": [...], "p50": [...], "p75": [...]}``,
    在每个 turn 序号上对所有"到达过该序号"的局取分位数。

    局长度不一致时, 短局在其末尾之后不再贡献样本 (不是补零!), 否则会
    把"已终止"错误地当成"歌板为 0"。样本数 < 3 的 turn 位置返回 None。
    """
    if not has_events(results):
        return {"turns": [], "p25": [], "p50": [], "p75": [], "mean": []}

    per_turn: dict[int, list[float]] = {}
    for r in results:
        for e in getattr(r, "events", []) or []:
            if attr == "board":
                v = e.board_after
            elif attr == "verse_point":
                v = e.verse_point_after
            elif attr == "sentiment":
                v = float((e.bold_sentiment or 0) + (e.graceful_sentiment or 0))
            else:
                raise ValueError(f"未知轨迹属性: {attr!r}")
            per_turn.setdefault(int(e.turn), []).append(float(v))

    if not per_turn:
        return {"turns": [], "p25": [], "p50": [], "p75": [], "mean": []}

    turns: list[int] = []
    p25: list[float | None] = []
    p50: list[float | None] = []
    p75: list[float | None] = []
    mean: list[float | None] = []
    for t in sorted(per_turn):
        vals = per_turn[t]
        turns.append(t)
        if len(vals) < 3:
            p25.append(None)
            p50.append(None)
            p75.append(None)
            mean.append(float(np.mean(vals)) if vals else None)
            continue
        arr = np.asarray(vals, dtype=float)
        p25.append(float(np.percentile(arr, 25, method="linear")))
        p50.append(float(np.percentile(arr, 50, method="linear")))
        p75.append(float(np.percentile(arr, 75, method="linear")))
        mean.append(float(arr.mean()))
    return {"turns": turns, "p25": p25, "p50": p50, "p75": p75, "mean": mean}


# ---------------------------------------------------------------------------
# 分支触发率 / 异常检测
# ---------------------------------------------------------------------------


def branch_trigger_rates(summary: M.BatchSummary) -> dict[str, Any]:
    """统计各"分支"的触发情况, 用于发现"某分支从未触发"的异常。

    核查对象:
      * 词情触发 (汇总层面)
      * 唱词人效果触发 (汇总层面)
      * 授予卡牌/状态 (效果 881)
      * 抽到豪放/婉约词句
      * 每个属性的终值是否发生过变化
    """
    results = summary.results
    n = len(results)
    branches: list[dict[str, Any]] = []

    def add(name: str, hits: int, total: int, note: str = "",
            zero_expected: bool = False) -> None:
        rate = (hits / total) if total > 0 else None
        branches.append({
            "name": name,
            "hits": int(hits),
            "total": int(total),
            "rate": rate,
            "note": note,
            "zero_expected": bool(zero_expected),
        })

    # --- 逐局层面
    add("词情触发 (任一)", sum(1 for r in results if r.sentiment_trigger_count > 0), n,
        "该局至少触发过一次词情")
    # 唱词人"被动型"效果 (如徽玉「后续择律收益+10%」) 是常驻修正, 不产生逐次触发计数,
    # 故计数为 0 属正常; 且「新竹」只在"择律错误"时触发, 而本模拟默认无错误分支。
    add("唱词人效果触发", sum(1 for r in results if r.singer_triggered > 0), n,
        "仅统计「逐次触发型」唱词人效果；被动常驻型(徽玉)与错误分支型(新竹)不计数",
        zero_expected=True)
    # 881「授予卡牌/状态」不再单独列示 (用户 2026-10-05 要求精简这类计数噪声)
    add("解锁新词人", sum(1 for r in results if r.poets_unlocked > 0), n)
    add("抽取到豪放词句", sum(1 for r in results if (r.style_counts or {}).get(M.STYLE_BOLD, 0) > 0), n)
    add("抽取到婉约词句", sum(1 for r in results if (r.style_counts or {}).get(M.STYLE_GRACEFUL, 0) > 0), n)

    # --- 逐次层面 (全部改用**每局汇总**, 不依赖事件流 —— 事件流为控内存会截断)
    total_turns = sum(int(r.turns) for r in results)
    if total_turns:
        add("抽到豪放词 (逐次)",
            sum(int((r.style_counts or {}).get(M.STYLE_BOLD, 0)) for r in results), total_turns)
        add("抽到婉约词 (逐次)",
            sum(int((r.style_counts or {}).get(M.STYLE_GRACEFUL, 0)) for r in results), total_turns)
        add("豪放词情触发 (逐次)", sum(int(getattr(r, "bold_triggers", 0)) for r in results),
            total_turns)
        add("婉约词情触发 (逐次)", sum(int(getattr(r, "graceful_triggers", 0)) for r in results),
            total_turns)
        add("逐次出现新解锁词人", sum(int(r.poets_unlocked) for r in results), total_turns)

    # --- 属性层面 (只统计输出口径内的属性; 样本数 = **局数**, 不再是 1)
    for key in M.reported_attribute_keys():
        label = M.ATTRIBUTE_LABELS[key]
        series = attribute_report_series(key, results)
        varied = len({round(float(v), 9) for v in series}) > 1
        suffix = "" if key == "board" else "波动"
        add(f"属性{suffix or '终值'}「{label}」", len(results) if varied else 0, len(results),
            f"各局{ suffix or '终值' }不完全相同" if varied else f"所有局{ suffix or '终值' }完全相同")

    return {
        "branches": branches,
        "has_events": has_events(results),
        "event_count": sum(len(getattr(r, "events", []) or []) for r in results),
        # 事件流只记录「随机 N 局」的全部流程 (用户 2026-10-05), 不再有条数上限
        "events_truncated": False,
    }


def detect_anomalies(summary: M.BatchSummary) -> list[str]:
    """返回中文告警条目列表 (可能为空)。"""
    warnings: list[str] = []
    results = summary.results
    n = len(results)

    if n == 0:
        return ["样本量为 0, 无可统计分析的数据。"]

    rates = branch_trigger_rates(summary)
    for b in rates["branches"]:
        if b["name"].startswith("属性终值变化"):
            # 属性用"是否发生变化"衡量, 措辞与分支不同: 不变化通常是正常的
            if b["hits"] == 0:
                warnings.append(
                    f"{b['name']}：{b['note']} —— 若预期该属性应受影响, "
                    "请确认是否有对应政策被启用。"
                )
            continue
        if b["total"] == 0:
            continue
        if b["hits"] == 0:
            if b.get("zero_expected"):
                # 已知可能为 0 的分支: 不作为异常, 仅给出中性说明
                warnings.append(
                    f"提示（非异常）：「{b['name']}」触发率为 0%（0/{b['total']}）。{b.get('note', '')}"
                )
            else:
                warnings.append(
                    f"分支「{b['name']}」触发率为 0%（0/{b['total']}）—— 若预期应当触发, "
                    "请检查开局配置与规则开关。"
                )

    if not rates["has_events"]:
        warnings.append(
            "本批模拟未记录逐次事件流（config.record_events=False）: "
            "轨迹类图表已降级为「择律次数 vs 终值」散点图。"
        )

    # 结束原因单一化说明
    # 注: 阈值 1000 是刻意设定的目标值, 而歌板只出不进, 故绝大多数局以
    #     「歌板不足一次择律」截断是**预期结果**, 不应作为异常告警。
    reasons = {r.stop_reason for r in results}
    single_reason = next(iter(reasons)) if len(reasons) == 1 else None
    if single_reason is not None:
        if single_reason == M.STOP_INSUFFICIENT:
            warnings.append(
                f"提示（非异常）：全部 {n} 局均因「歌板不足一次择律」结束, 未达到结束阈值 "
                f"{summary.config.end_threshold:g}。这是预期结果 —— 阈值是刻意设定的目标值, "
                "而歌板只出不进, 绝大多数局会被截断。"
            )
        else:
            warnings.append(
                f"提示（非异常）：全部 {n} 局均以同一原因结束（{single_reason}）。"
            )

    return warnings


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def compute_statistics(summary: M.BatchSummary) -> dict[str, Any]:
    """把 :class:`BatchSummary` 聚合成可序列化统计字典。

    返回结构 (键名稳定, 供 report/charts/GUI 消费)::

        {
          "meta": {...},                     # 样本量、配置摘要、事件流有无
          "turns": {...},                    # 择律次数描述统计
          "attributes": {key: {...}},        # 各属性终值描述统计
          "sentiment": {"bold": {...}, "graceful": {...}, "total": {...}},
          "poets_unlocked": {...},
          "singer_triggered": {...},
          "sentiment_trigger_count": {...},
          "card_grants": {...},
          "unknown_effect_grants": {...},
          "style_share": {...},              # 各风格抽取占比
          "top_verses": [...],               # Top20 词句
          "verse_total_draws": int,
          "stop_reasons": {...},             # 终止原因分布
          "branch_rates": {...},
          "anomalies": [...],
          "turn_distribution": {...},        # 直方图数据 (供卡方检验/绘图)
        }
    """
    results = summary.results
    n = len(results)
    cfg = summary.config

    turns_series = [r.turns for r in results]

    # ---------------- 各属性统计: 歌板=终值, 其余=波动 (终值 − 开局结算后)
    attributes: dict[str, dict[str, Any]] = {}
    for key in M.reported_attribute_keys():
        series = attribute_report_series(key, results)
        st = summarize_series(series)
        st["label"] = M.ATTRIBUTE_LABELS[key] + ("" if key == "board" else "波动")
        attributes[key] = st

    # ---------------- 词情
    sentiment = {
        "bold": summarize_series([r.bold_sentiment for r in results]),
        "graceful": summarize_series([r.graceful_sentiment for r in results]),
        "total": summarize_series(
            [(r.bold_sentiment or 0) + (r.graceful_sentiment or 0) for r in results]
        ),
    }
    sentiment["cap"] = int(cfg.sentiment_cap)

    # ---------------- 词句抽取频次
    verse_totals: dict[int, int] = {}
    for r in results:
        for vid, c in (r.verse_counts or {}).items():
            verse_totals[int(vid)] = verse_totals.get(int(vid), 0) + int(c)
    verse_total_draws = sum(verse_totals.values())

    # 词句名: 事件流里取; 事件流被截断或未记录时, 回查数据库补齐 (best-effort)
    verse_names: dict[int, str] = {}
    for e in collect_events(results):
        if e.verse_id not in verse_names and e.verse_name:
            verse_names[e.verse_id] = e.verse_name
    missing = [vid for vid in verse_totals if not verse_names.get(vid)]
    if missing:
        try:
            import db as _db
            for vid, name in _db.fetch_all(
                    "SELECT id, ci_name FROM verse WHERE id = ANY(%s)", (missing,)):
                if name:
                    verse_names[int(vid)] = str(name)
        except Exception:      # 无数据库连接时保持留空 (report 层会显示占位)
            pass

    top_verses = [
        {
            "verse_id": vid,
            "verse_name": verse_names.get(vid, ""),
            "count": cnt,
            "share": (cnt / verse_total_draws) if verse_total_draws else None,
        }
        for vid, cnt in sorted(verse_totals.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_N_VERSES]
    ]

    # ---------------- 风格占比
    style_totals: dict[int, int] = {M.STYLE_BOLD: 0, M.STYLE_GRACEFUL: 0}
    for r in results:
        for s, c in (r.style_counts or {}).items():
            style_totals[int(s)] = style_totals.get(int(s), 0) + int(c)
    style_sum = sum(style_totals.values())
    style_share = {
        "counts": {str(k): v for k, v in sorted(style_totals.items())},
        "labels": {str(k): M.STYLE_LABELS.get(k, str(k)) for k in sorted(style_totals)},
        "shares": {
            str(k): (v / style_sum if style_sum else None)
            for k, v in sorted(style_totals.items())
        },
        "total": style_sum,
    }

    # ---------------- 终止原因分布
    reason_counts: dict[str, int] = {}
    reason_turns: dict[str, list[int]] = {}
    for r in results:
        key = r.stop_reason or "(未标注)"
        reason_counts[key] = reason_counts.get(key, 0) + 1
        reason_turns.setdefault(key, []).append(r.turns)
    stop_reasons = {
        "counts": dict(sorted(reason_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "shares": {k: (v / n if n else None) for k, v in reason_counts.items()},
        # 各终止原因子样本的轮次摘要 (用于与条件性理论下界对照)
        "turns_by_reason": {
            k: {
                "count": len(v),
                "mean": (sum(v) / len(v)) if v else None,
                "min": min(v) if v else None,
                "max": max(v) if v else None,
            }
            for k, v in reason_turns.items()
        },
    }

    # ---------------- 触及歌板上限阈值 的频率 (结束条件「达到歌板阈值」)
    reached = int(reason_counts.get(M.STOP_THRESHOLD, 0))
    threshold_reach = {
        "threshold": float(getattr(cfg, "end_threshold", 0.0)),
        "stop_reason": M.STOP_THRESHOLD,
        "runs_reached": reached,
        "runs": n,
        "share": (reached / n) if n else None,
        "note": "「触及歌板上限阈值」= 本局结束时歌板曾达到/超过 end_threshold "
                "(终止原因 = 达到歌板阈值); 其余局多为「歌板不足一次择律」。",
    }

    # ---------------- 择律次数分布 (直方图 / 卡方用)
    turn_distribution = build_turn_distribution(turns_series)

    stats: dict[str, Any] = {
        "meta": {
            "config_name": cfg.name,
            "runs": n,
            "declared_runs": summary.runs,
            "has_events": has_events(results),
            "event_count": sum(len(getattr(r, "events", []) or []) for r in results),
            "ddof": DD_OF,
            "std_note": "标准差一律为样本标准差 (ddof=1); n<=1 时为 null",
            "end_threshold": cfg.end_threshold,
            "draw_base_cost": cfg.draw_base_cost,
            "sentiment_cap": cfg.sentiment_cap,
            "singer_id": cfg.singer_id,
            "seed": cfg.seed,
        },
        "turns": summarize_series(turns_series),
        "attributes": attributes,
        "sentiment": sentiment,
        "poets_unlocked": summarize_series([r.poets_unlocked for r in results]),
        "singer_triggered": summarize_series([r.singer_triggered for r in results]),
        "sentiment_trigger_count": summarize_series([r.sentiment_trigger_count for r in results]),
        "card_grants": summarize_series([r.card_grants for r in results]),
        "unknown_effect_grants": summarize_series([r.unknown_effect_grants for r in results]),
        "benefit_bonus_factor": summarize_series(
            [1.0 + float(getattr(r, "benefit_bonus", 0.0)) for r in results]),
        "style_share": style_share,
        "top_verses": top_verses,
        "verse_total_draws": verse_total_draws,
        "verse_distinct": len(verse_totals),
        "stop_reasons": stop_reasons,
        "threshold_reach": threshold_reach,
        "branch_rates": branch_trigger_rates(summary),
        "anomalies": detect_anomalies(summary),
        "turn_distribution": turn_distribution,
    }
    return stats


def build_turn_distribution(turns_series: Sequence[int]) -> dict[str, Any]:
    """择律次数的频数分布 (整数桶), 供绘图与卡方检验使用。

    与 :func:`integer_distribution` 同一口径, 保留函数名以稳定外部调用方。
    """
    return integer_distribution(turns_series)


def integer_distribution(values: Sequence[int]) -> dict[str, Any]:
    """整数序列的频数分布 (供直方图绘制)。

    返回 ``{"values": [...], "counts": [...], "min", "max", "unique", "count"}``
    """
    xs = [int(v) for v in values]
    if not xs:
        return {"values": [], "counts": [], "min": None, "max": None,
                "unique": 0, "count": 0}
    counts: dict[int, int] = {}
    for v in xs:
        counts[v] = counts.get(v, 0) + 1
    keys = sorted(counts)
    return {
        "values": keys,
        "counts": [counts[k] for k in keys],
        "min": keys[0],
        "max": keys[-1],
        "unique": len(keys),
        "count": len(xs),
    }


def poet_unlock_stats(results: Sequence[M.SimulationResult]) -> dict[str, Any]:
    """词人解锁统计 (只用**每局汇总**, 不依赖事件流)。

    给出每位词人「局中被解锁」的局数、解锁率与首次解锁轮次的中位/P25/P75。
    """
    from collections import defaultdict

    first_turn: dict[int, list[int]] = defaultdict(list)
    for r in results:
        seen: set[int] = set()
        for pid, turn in getattr(r, "poet_unlock_turns", ()) or ():
            pid = int(pid)
            if pid in seen:
                continue          # 只取该局的首次解锁时刻
            seen.add(pid)
            first_turn[pid].append(int(turn))

    names = load_poet_names()
    per_poet: list[dict[str, Any]] = []
    for pid, turns in first_turn.items():
        t = to_float_list(turns)
        per_poet.append({
            "poet_id": pid,
            "poet_name": names.get(pid, str(pid)),
            "unlock_count": len(t),
            "unlock_rate": (len(t) / len(results)) if results else None,
            "median_turn": float(np.median(np.asarray(t, dtype=float))) if t else None,
            "p25_turn": float(np.percentile(np.asarray(t, dtype=float), 25, method="linear")) if len(t) >= 3 else None,
            "p75_turn": float(np.percentile(np.asarray(t, dtype=float), 75, method="linear")) if len(t) >= 3 else None,
        })
    per_poet.sort(key=lambda d: (d["median_turn"] is None, d["median_turn"] or 0.0))
    return {
        "per_poet": per_poet,
        "runs": len(results),
        "distinct_poets": len(per_poet),
    }


def load_poet_names() -> dict[int, str]:
    """词人 ID -> 名称 (供结果查看显示名字而非 ID)。"""
    try:
        import db as _db
        return {int(r[0]): str(r[1]) for r in _db.fetch_all("SELECT id, name FROM poet")}
    except Exception:      # 无数据库时退化为 ID
        return {}


def build_chart_inputs(summary: M.BatchSummary) -> dict[str, Any]:
    """为图表层准备轻量数据 (轨迹 / 散点 / 直方图 / 词人解锁)。

    与 :func:`compute_statistics` 分开, 因为轨迹聚合对 10k 局有一定开销,
    而统计表本身不需要它。
    """
    results = summary.results
    has_ev = has_events(results)

    trajectories: dict[str, Any] = {}
    if has_ev:
        for attr in ("board", "verse_point", "sentiment"):
            trajectories[attr] = trajectory_series(results, attr)

    scatter = {
        "turns": [int(r.turns) for r in results],
        "board": [float(r.attributes.get("board", 0.0)) for r in results],
        "verse_point": [float(r.attributes.get("verse_point", 0.0)) for r in results],
        "sentiment_total": [
            float((r.bold_sentiment or 0) + (r.graceful_sentiment or 0)) for r in results
        ],
    }
    attribute_series = {
        key: attribute_report_series(key, results)
        for key in M.reported_attribute_keys()
    }

    return {
        "trajectories": trajectories,
        "scatter": scatter,
        "attribute_series": attribute_series,
        "poets_unlocked_dist": integer_distribution([r.poets_unlocked for r in results]),
        "sentiment_trigger_dist": integer_distribution(
            [r.sentiment_trigger_count for r in results]
        ),
        "singer_trigger_dist": integer_distribution([r.singer_triggered for r in results]),
        "poet_unlock": poet_unlock_stats(results),
    }


def build_full_stats(summary: M.BatchSummary) -> dict[str, Any]:
    """统计表 + 图表输入合并 (report/charts 的统一入口)。"""
    stats = compute_statistics(summary)
    stats.update(build_chart_inputs(summary))
    return stats
