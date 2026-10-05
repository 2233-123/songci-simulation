# -*- coding: utf-8 -*-
"""M4 · 报告与输出文件写入。

产出目录结构 (设计文档 §5)::

    <out_dir>/
      配置快照.json          # SimulationConfig.to_dict(), 可复现
      summary.md             # 中文统计报告
      charts/*.png           # 2 张图表
      raw/单局明细.csv       # 每局一行
      raw/择律事件流.csv     # 仅当 config.record_events
      批量汇总.csv           # 指标级汇总 (每行一个指标的均值/中位/标准差/分位)

CSV 一律使用 ``utf-8-sig`` 编码, 以便 Excel 直接打开中文不乱码。
"""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

if __package__:  # 作为包导入 (python -m src.report)
    from . import config as C  # type: ignore
    from . import display as DISP  # type: ignore
    from . import model as M  # type: ignore
    from . import stats as S  # type: ignore
else:             # 顶层导入 (sys.path 指向 src 后 import report)
    import config as C  # type: ignore
    import display as DISP  # type: ignore
    import model as M  # type: ignore
    import stats as S  # type: ignore

#: summary.md 中每个 markdown 表格最多呈现的行数 (超出部分折叠)
MAX_TABLE_ROWS = 60

#: 单局明细 CSV 中最多写出的局数 (防 10 万局时文件过大)
MAX_DETAIL_ROWS = 200_000

CSV_ENCODING = "utf-8-sig"

#: 属性在 CSV 中的列顺序 (与 model.ATTRIBUTE_LABELS 的声明顺序一致)
_ATTRIBUTE_ORDER: tuple[str, ...] = M.reported_attribute_keys()


# ---------------------------------------------------------------------------
# 数值格式化
# ---------------------------------------------------------------------------


def fmt_num(value: Any, ndigits: int = 4) -> str:
    """按需格式化数字: None → '-', 整数不带小数点, 小数最多 ndigits 位去尾零。"""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "是" if value else "否"
    if not isinstance(value, (int, float)):
        return str(value)
    f = float(value)
    if f != f:  # NaN
        return "-"
    if f == int(f) and abs(f) < 1e15:
        return str(int(f))
    if f != 0 and (abs(f) < 1e-4 or abs(f) >= 1e7):
        return f"{f:.{ndigits}e}"
    return f"{f:.{ndigits}f}".rstrip("0").rstrip(".")


def fmt_pct(value: Any, ndigits: int = 2) -> str:
    """把 0~1 的比例格式化为百分比字符串。"""
    if value is None:
        return "-"
    try:
        return f"{float(value) * 100:.{ndigits}f}%"
    except (TypeError, ValueError):
        return "-"


# ---------------------------------------------------------------------------
# Markdown 表格
# ---------------------------------------------------------------------------


def markdown_table(headers: Sequence[str], rows: Sequence[Sequence[Any]],
                   *, align: Sequence[str] | None = None,
                   max_rows: int = MAX_TABLE_ROWS) -> str:
    """生成 markdown 表格文本 (不包含前后空行)。"""
    if not headers:
        return ""
    aligns = list(align) if align else ["---"] * len(headers)
    out = ["| " + " | ".join(str(h) for h in headers) + " |",
           "| " + " | ".join(aligns) + " |"]
    for row in rows[:max_rows]:
        out.append("| " + " | ".join(str(c) for c in row) + " |")
    if len(rows) > max_rows:
        out.append(f"| （其余 {len(rows) - max_rows} 行已省略） |" +
                   " |" * (len(headers) - 1))
    return "\n".join(out)


def _kv_table(pairs: Sequence[tuple[str, Any]]) -> str:
    return markdown_table(["项目", "值"], [[k, v] for k, v in pairs], align=["---", "---"])


# ---------------------------------------------------------------------------
# summary.md
# ---------------------------------------------------------------------------


def build_summary_markdown(stats: dict[str, Any], *,
                           chart_files: dict[str, Path] | None = None,
                           skipped_charts: dict[str, str] | None = None,
                           font_warning: str = "",
                           generated_at: datetime | None = None) -> str:
    """组装中文 summary.md 内容。"""
    cfg: M.SimulationConfig = stats["_config"]
    meta = stats.get("meta") or {}
    turns = stats.get("turns") or {}
    now = generated_at or datetime.now()

    lines: list[str] = []
    A = lines.append

    # ---------------- 标题
    A(f"# 宋词择律模拟统计报告 · {cfg.name}")
    A("")
    A(f"- 生成时间：{now:%Y-%m-%d %H:%M:%S}")
    A(f"- 随机种子：`{cfg.seed}`   模拟局数：**{meta.get('runs', 0)}**")
    A(f"- 标准差口径：**样本标准差（ddof=1）**，n≤1 时为 `-`")
    A(f"- 事件流记录：{'是' if meta.get('has_events') else '否'}"
      f"（事件总数 {meta.get('event_count', 0)}"
      f"{'，已达上限，仅覆盖前若干局' if (stats.get('branch_rates') or {}).get('events_truncated') else ''}）")
    if font_warning:
        A(f"- ⚠️ 图表字体：{font_warning}")
    A("")

    # ---------------- 1. 开局配置
    A("## 1. 开局配置")
    A("")
    A(_kv_table([
        ("配置名称", cfg.name),
        ("唱词人 ID", cfg.singer_id if cfg.singer_id is not None else "不使用"),
        ("开局词人 ID", _join_ids(cfg.poet_ids)),
        ("开局名臣 ID", _join_ids(cfg.minister_ids)),
        ("东坡食单", _dongpo_text(cfg)),
        ("额外启用政策 KeyID", _join_ids(cfg.policy_key_ids)),
        ("初始歌板", fmt_num(cfg.init_board)),
        ("初始词元", fmt_num(cfg.init_verse_point)),
        ("初始豪放词情", cfg.init_bold_sentiment),
        ("初始婉约词情", cfg.init_graceful_sentiment),
        ("初始择律收益加成", fmt_pct(cfg.init_benefit_bonus)),
        ("每次择律基础消耗", fmt_num(cfg.draw_base_cost)),
        ("结束阈值（歌板）", fmt_num(cfg.end_threshold)),
        ("词情层数上限", cfg.sentiment_cap),
        ("收益加成只放大正向", cfg.bias_bonus_to_positive_only),
        ("解锁所有词句库", cfg.unlock_all_verses),
        ("安全上限（择律次数）", fmt_num(cfg.max_turns)),
    ]))
    A("")
    if cfg.init_attributes:
        A("**初始属性**")
        A("")
        A(markdown_table(
            ["属性", "初值"],
            [[M.ATTRIBUTE_LABELS.get(k, k), fmt_num(v)]
             for k, v in cfg.init_attributes.items() if k not in M.HIDDEN_ATTRIBUTE_KEYS],
        ))
        A("")

    # ---------------- 2. 择律次数
    A("## 2. 择律次数统计")
    A("")
    A(markdown_table(
        ["指标", "值"],
        [
            ["样本量", turns.get("count")],
            ["均值", fmt_num(turns.get("mean"))],
            ["中位数 (P50)", fmt_num(turns.get("p50"))],
            ["标准差 (ddof=1)", fmt_num(turns.get("std"))],
            ["最小", fmt_num(turns.get("min"))],
            ["P5", fmt_num(turns.get("p5"))],
            ["P25", fmt_num(turns.get("p25"))],
            ["P75", fmt_num(turns.get("p75"))],
            ["P95", fmt_num(turns.get("p95"))],
            ["四分位距 IQR", fmt_num(turns.get("iqr"))],
            ["最大", fmt_num(turns.get("max"))],
        ],
    ))
    A("")

    # ---------------- 触及歌板上限阈值的频率
    tr = stats.get("threshold_reach") or {}
    if tr:
        A(f"**触及歌板上限阈值的频率（阈值 {fmt_num(tr.get('threshold'))}）**")
        A("")
        A(markdown_table(
            ["项目", "值"],
            [
                ["达到阈值局数", f"{tr.get('runs_reached', 0)} / {tr.get('runs', 0)}"],
                ["**触达频率**", fmt_pct(tr.get("share"))],
                ["对应终止原因", tr.get("stop_reason", "")],
            ],
        ))
        A("")
        A(f"> {tr.get('note', '')}")
        A("")

    # ---------------- 抽取规则 / 与理论下界对照：按用户要求整段删除

    # ---------------- 3. 各属性波动统计
    A("## 3. 各属性统计")
    A("")
    attrs = stats.get("attributes") or {}
    A(markdown_table(
        ["属性（键）", "均值", "标准差(ddof=1)", "最小", "最大", "P5", "P95"],
        [
            [f"{v.get('label', k)}（{k}）",
             fmt_num(v.get("mean")), fmt_num(v.get("std")),
             fmt_num(v.get("min")), fmt_num(v.get("max")),
             fmt_num(v.get("p5")), fmt_num(v.get("p95"))]
            for k, v in attrs.items()
        ],
    ))
    A("")
    A("> 注：**歌板报终值**（它是择律的资源与终止条件）；其余属性报**波动 = 每局终值 "
      "− 该局开局一次性政策结算后（第一次择律前）的值**，即纯择律带来的变化；发展年数量纲为「年」。")
    A("")

    # ---------------- 4. 词情 / 解锁 / 触发
    A("## 4. 词情、词人与触发统计")
    A("")
    senti = stats.get("sentiment") or {}
    A("**词情层数终值**")
    A("")
    A(markdown_table(
        ["指标", "豪放词情", "婉约词情", "合计"],
        [
            ["均值", fmt_num((senti.get("bold") or {}).get("mean")),
             fmt_num((senti.get("graceful") or {}).get("mean")),
             fmt_num((senti.get("total") or {}).get("mean"))],
            ["中位数", fmt_num((senti.get("bold") or {}).get("p50")),
             fmt_num((senti.get("graceful") or {}).get("p50")),
             fmt_num((senti.get("total") or {}).get("p50"))],
            ["标准差(ddof=1)", fmt_num((senti.get("bold") or {}).get("std")),
             fmt_num((senti.get("graceful") or {}).get("std")),
             fmt_num((senti.get("total") or {}).get("std"))],
            ["最大", fmt_num((senti.get("bold") or {}).get("max")),
             fmt_num((senti.get("graceful") or {}).get("max")),
             fmt_num((senti.get("total") or {}).get("max"))],
            ["层数上限", senti.get("cap"), senti.get("cap"), senti.get("cap")],
        ],
    ))
    A("")

    A("**计数类指标（逐局）**")
    A("")
    A(markdown_table(
        ["指标", "均值", "中位数", "标准差(ddof=1)", "最大"],
        [
            ["词人解锁数", *_four(stats.get("poets_unlocked"))],
            ["唱词人效果触发次数", *_four(stats.get("singer_triggered"))],
            ["词情触发次数", *_four(stats.get("sentiment_trigger_count"))],
        ],
    ))
    A("")

    unlock = stats.get("poet_unlock") or {}
    if unlock.get("per_poet"):
        A("**词人解锁时间（首次解锁时的择律序号）**")
        A("")
        A(markdown_table(
            ["词人", "解锁局数", "解锁率", "中位解锁序号", "P25", "P75"],
            [
                [p.get("poet_name") or p.get("poet_id"),
                 f"{p.get('unlock_count', 0)} / {unlock.get('runs', 0)}",
                 fmt_pct(p.get("unlock_rate")),
                 fmt_num(p.get("median_turn")), fmt_num(p.get("p25_turn")),
                 fmt_num(p.get("p75_turn"))]
                for p in unlock["per_poet"]
            ],
        ))
        A("")
    else:
        A("> 本批次没有任何词人在局中被解锁。")
        A("")

    # ---------------- 5. 词句抽取频次 Top20：按用户要求整段删除

    # ---------------- 6. 风格占比
    A("## 5. 词律风格抽取占比")
    A("")
    share = stats.get("style_share") or {}
    if (share.get("total") or 0) > 0:
        labels = share.get("labels") or {}
        A(markdown_table(
            ["风格", "抽取次数", "占比"],
            [
                [labels.get(k, k), v, fmt_pct((share.get("shares") or {}).get(k))]
                for k, v in (share.get("counts") or {}).items()
            ],
        ))
    else:
        A("> 无风格抽取记录。")
    A("")

    # ---------------- 7. 终止原因
    A("## 6. 终止原因分布")
    A("")
    reasons = (stats.get("stop_reasons") or {}).get("counts") or {}
    if reasons:
        A(markdown_table(
            ["终止原因", "局数", "占比"],
            [[k, v, fmt_pct(((stats.get("stop_reasons") or {}).get("shares") or {}).get(k))]
             for k, v in reasons.items()],
        ))
    else:
        A("> 无终止原因记录。")
    A("")

    # ---------------- 8. 分支触发率
    A("## 7. 分支触发率")
    A("")
    branches = (stats.get("branch_rates") or {}).get("branches") or []
    if branches:
        A(markdown_table(
            ["分支", "触发次数", "样本数", "触发率", "说明"],
            [[b["name"], b["hits"], b["total"], fmt_pct(b["rate"]), b.get("note", "")]
             for b in branches],
        ))
    else:
        A("> 无分支统计。")
    A("")

    # ---------------- 9. 图表清单
    A("## 8. 图表")
    A("")
    if chart_files or skipped_charts:
        rows: list[list[Any]] = []
        for name, path in (chart_files or {}).items():
            size = path.stat().st_size if Path(path).exists() else 0
            rows.append([name, f"`charts/{name}`", f"{size:,} 字节", "已生成"])
        for name, why in (skipped_charts or {}).items():
            rows.append([name, "-", "-", f"跳过：{why}"])
        A(markdown_table(["图表", "文件", "大小", "状态"], rows))
    else:
        A("> 本次运行未生成图表。")
    A("")

    # ---------------- 10. 异常与结论
    A("## 9. 异常提示与结论")
    A("")
    items = stats.get("anomalies") or []
    # 「提示（非异常）」是已知可能为 0 的分支说明, 不计入真正的告警
    hints = [w for w in items if w.startswith("提示（非异常）")]
    warns = [w for w in items if not w.startswith("提示（非异常）")]

    if hints:
        A(f"**说明性提示 {len(hints)} 条（非异常）**：")
        A("")
        for i, w in enumerate(hints, 1):
            A(f"{i}. {w}")
        A("")
    if warns:
        A(f"检测到 **{len(warns)}** 条告警：")
        A("")
        for i, w in enumerate(warns, 1):
            A(f"{i}. {w}")
        A("")
        A("**结论**：请核对上述告警项。若告警只涉及「某属性波动不变」，"
          "通常是因为该属性在当前开局配置下确实没有任何来源（例如未启用对应词人/名臣），"
          "属正常情况。")
    else:
        A("未检测到异常：所有被跟踪的分支均至少触发过一次，且各属性波动均发生过变化"
          "（若上方存在说明性提示，属于已知的正常情况）。")
        A("")
        A("**结论**：本批结果可用于后续对比分析。")
    A("")

    A("---")
    A("")
    A("_本报告由 `src/report.py` 自动生成；数值口径见文首说明。_")

    return "\n".join(lines) + "\n"


def _join_ids(ids: Iterable[Any] | None) -> str:
    vals = [str(x) for x in (ids or [])]
    if not vals:
        return "（无）"
    if len(vals) <= 12:
        return ", ".join(vals)
    return ", ".join(vals[:12]) + f" …（共 {len(vals)} 项）"


def _dongpo_text(cfg) -> str:
    """东坡食单: 显示菜名 (并从库取效果摘要); 未勾苏轼名臣时提示不生效。"""
    ids = [int(x) for x in (getattr(cfg, "dongpo_food_ids", []) or [])]
    if not ids:
        return "（无）"
    try:
        import db as D
        rows = D.fetch_all("""
            SELECT f.id, f.name, p.effect_desc
            FROM songci.dongpo_food f
            LEFT JOIN songci.common_effect_pool p ON p.id = f.pool_id
            WHERE f.id = ANY(%s) ORDER BY f.id""", (ids,))
        text = "；".join(f"{r[0]}.{r[1]}" for r in rows) or _join_ids(ids)
    except Exception:
        return _join_ids(ids)
    if C.SU_SHI_MINISTER_ID not in {int(x) for x in cfg.minister_ids}:
        text += "　⚠️ 未勾选苏轼名臣，东坡食单不生效"
    elif set(ids) >= set(C.DONGPO_COMPLETE_FOOD_IDS):
        text += f"　（9 道全收集 → 豪放词情+{C.DONGPO_COMPLETE_BOLD_SENTIMENT} 层）"
    return text


def _four(st: dict[str, Any] | None) -> list[Any]:
    st = st or {}
    return [fmt_num(st.get("mean")), fmt_num(st.get("p50")),
            fmt_num(st.get("std")), fmt_num(st.get("max"))]


# ---------------------------------------------------------------------------
# CSV 写出
# ---------------------------------------------------------------------------


def _write_csv(path: Path, headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding=CSV_ENCODING, newline="") as fh:
        w = csv.writer(fh)
        w.writerow(list(headers))
        for row in rows:
            w.writerow(list(row))
    return path


def detail_headers() -> list[str]:
    """单局明细 CSV 的表头。"""
    return ["局号", "择律次数", "终止原因"] + \
           [f"{M.ATTRIBUTE_LABELS[k]}{'' if k == 'board' else '波动'}({k})"
            for k in _ATTRIBUTE_ORDER] + \
           ["豪放词情", "婉约词情", "解锁词人数", "唱词人触发次数",
            "词情触发次数", "授予卡牌次数", "未识别效果值", "事件数",
            "择律收益加成终值"]


def write_detail_csv(summary: M.BatchSummary, path: Path,
                     *, max_rows: int = MAX_DETAIL_ROWS) -> Path:
    """写 raw/单局明细.csv (每局一行)。"""
    rows = []
    for i, r in enumerate(summary.results[:max_rows], 1):
        rows.append(
            [i, r.turns, r.stop_reason]
            + [fmt_num(float(r.attributes.get(k, 0.0))
                       if not M.uses_delta(k) else
                       float(r.attributes.get(k, 0.0))
                       - float((getattr(r, "initial_attributes", None) or {}).get(k, 0.0)))
               for k in _ATTRIBUTE_ORDER]
            + [r.bold_sentiment, r.graceful_sentiment, r.poets_unlocked,
               r.singer_triggered, r.sentiment_trigger_count, r.card_grants,
               fmt_num(r.unknown_effect_grants), len(getattr(r, "events", []) or []),
               fmt_num(1.0 + float(getattr(r, "benefit_bonus", 0.0)))]
        )
    return _write_csv(path, detail_headers(), rows)


def batch_summary_headers() -> list[str]:
    """批量汇总 CSV 的表头 (指标级汇总, 每行一个指标)。"""
    return ["指标", "样本量", "均值", "中位数", "标准差(ddof=1)",
            "P5", "P25", "P75", "P95", "最小", "最大"]


def _stat_row(label: str, st: dict | None) -> list:
    st = st or {}
    return [label, st.get("count"), fmt_num(st.get("mean")), fmt_num(st.get("p50")),
            fmt_num(st.get("std")), fmt_num(st.get("p5")), fmt_num(st.get("p25")),
            fmt_num(st.get("p75")), fmt_num(st.get("p95")),
            fmt_num(st.get("min")), fmt_num(st.get("max"))]


def write_batch_summary_csv(summary: M.BatchSummary, stats: dict, path: Path) -> Path:
    """写 批量汇总.csv —— **指标级汇总** (每行一个指标的均值/中位/标准差/分位)。

    注: 早期该文件是 `单局明细.csv` 的逐字复制 (两者内容完全相同, 纯冗余);
    现改为真正的"批量汇总", 逐局数据仍见 `raw/单局明细.csv`。
    """
    attrs = stats.get("attributes") or {}
    rows: list[list] = [
        _stat_row("择律次数", stats.get("turns")),
        _stat_row("解锁词人数", stats.get("poets_unlocked")),
        _stat_row("唱词人效果触发次数", stats.get("singer_triggered")),
        _stat_row("词情触发次数", stats.get("sentiment_trigger_count")),
        _stat_row("择律收益加成终值", stats.get("benefit_bonus_factor")),
        _stat_row("豪放词情层数", (stats.get("sentiment") or {}).get("bold")),
        _stat_row("婉约词情层数", (stats.get("sentiment") or {}).get("graceful")),
    ]
    for key in _ATTRIBUTE_ORDER:
        suffix = "" if key == "board" else "波动"
        rows.append(_stat_row(M.ATTRIBUTE_LABELS.get(key, key) + suffix, attrs.get(key)))
    return _write_csv(path, batch_summary_headers(), rows)


def event_headers() -> list[str]:
    """事件流 CSV 的表头。"""
    keys = sorted({k for k in _ATTRIBUTE_ORDER})
    return ["局号", "择律序号", "抽到风格", "词句ID", "词句名",
            "本次消耗歌板", "歌板余额", "词元余额", "豪放词情", "婉约词情",
            "豪放词情触发", "婉约词情触发", "新解锁词人",
            "本次择律收益加成", "操作后择律收益加成", "同时视为豪放词"] + \
           [f"本次变动_{M.ATTRIBUTE_LABELS[k]}" for k in keys]


def write_events_csv(summary: M.BatchSummary, path: Path) -> Path | None:
    """写 raw/择律事件流.csv。无事件流时返回 None (不创建空文件)。"""
    if not S.has_events(summary.results):
        return None
    keys = sorted(_ATTRIBUTE_ORDER)
    headers = event_headers()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding=CSV_ENCODING, newline="") as fh:
        w = csv.writer(fh)
        w.writerow(headers)
        for run_idx, r in enumerate(summary.results, 1):
            for e in getattr(r, "events", []) or []:
                granted = e.granted or {}
                w.writerow(
                    [run_idx, e.turn,
                     M.STYLE_LABELS.get(e.drawn_style, e.drawn_style),
                     e.verse_id, e.verse_name, fmt_num(e.cost), fmt_num(e.board_after),
                     fmt_num(e.verse_point_after), e.bold_sentiment, e.graceful_sentiment,
                     int(bool(e.bold_triggered)), int(bool(e.graceful_triggered)),
                     e.poet_unlocked if e.poet_unlocked is not None else "",
                     # 择律收益 (32513) 加成: 本轮生效值 / 本轮结束后累计值
                     fmt_num(1.0 + float(getattr(e, "benefit_applied", 0.0))),
                     fmt_num(1.0 + float(getattr(e, "benefit_bonus_after", 0.0))),
                     int(bool(getattr(e, "treat_as_bold", False)))]
                    + [fmt_num(granted.get(k, 0.0)) for k in keys]
                )
    return path


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

#: stats 字典中体积过大的内部键, 写入快照 JSON 前剔除
_HEAVY_KEYS = ("attribute_series", "trajectories", "scatter", "_config")


def write_outputs(summary: M.BatchSummary, out_dir: str | Path, *,
                  stats: dict[str, Any] | None = None,
                  render_charts: bool = True,
                  generated_at: datetime | None = None) -> dict[str, Path]:
    """写出全部输出文件, 返回 ``{逻辑名: Path}``。

    顺序: 先建目录 → 写配置快照 → 写 CSV → 生成图表 → 依据图表结果写 summary.md
    (summary.md 需要图表文件名与大小, 故必须在图表之后)。
    """
    out_dir = Path(out_dir)
    charts_dir = out_dir / "charts"
    raw_dir = out_dir / "raw"
    out_dir.mkdir(parents=True, exist_ok=True)

    if stats is None:
        stats = S.build_full_stats(summary)
    stats = dict(stats)
    stats["_config"] = summary.config

    files: dict[str, Path] = {}

    # 1) 配置快照 —— 初始属性里剔除「不再统计」的五个属性, 不留任何痕迹
    cfg_dict = summary.config.to_dict()
    cfg_dict["init_attributes"] = {
        k: v for k, v in (cfg_dict.get("init_attributes") or {}).items()
        if k not in M.HIDDEN_ATTRIBUTE_KEYS}
    snapshot = out_dir / "配置快照.json"
    snapshot.write_text(
        json.dumps({
            "config": cfg_dict,
            "runs": summary.runs,
            "generated_at": (generated_at or datetime.now()).isoformat(timespec="seconds"),
            "stats_light": {k: v for k, v in stats.items() if k not in _HEAVY_KEYS},
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    files["配置快照"] = snapshot

    # 2) CSV
    files["单局明细"] = write_detail_csv(summary, raw_dir / "单局明细.csv")
    ev = write_events_csv(summary, raw_dir / "择律事件流.csv")
    if ev is not None:
        files["择律事件流"] = ev
    files["批量汇总"] = write_batch_summary_csv(summary, stats, out_dir / "批量汇总.csv")

    # 3) 图表
    chart_files: dict[str, Path] = {}
    skipped: dict[str, str] = {}
    font_warning = ""
    if render_charts:
        try:
            if __package__:
                from . import charts as CH  # type: ignore
            else:
                import charts as CH  # type: ignore
        except ImportError:  # pragma: no cover
            CH = None  # type: ignore
        if CH is None:
            skipped = {}
            font_warning = "matplotlib 导入失败，未生成图表"
        else:
            res = CH.render_all_charts(stats, charts_dir)
            chart_files = res.get("generated", {})
            skipped = res.get("skipped", {})
            font_warning = res.get("font_warning", "")
    for name, p in chart_files.items():
        files[f"图表/{name}"] = p

    # 4) summary.md (在图表之后写, 以便附带图表清单与大小)
    md = build_summary_markdown(stats, chart_files=chart_files, skipped_charts=skipped,
                               font_warning=font_warning, generated_at=generated_at)
    md_path = out_dir / "summary.md"
    md_path.write_text(md, encoding="utf-8")
    files["summary.md"] = md_path

    return files
