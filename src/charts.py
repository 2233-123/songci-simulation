# -*- coding: utf-8 -*-
"""M4 · 中文图表渲染 (matplotlib Agg 后端)。

约定
----
* 每个 ``chart_*`` 函数签名为 ``chart_xxx(stats, out_dir, *, ...) -> Path | None``;
  **数据缺失时返回 None 并跳过, 绝不抛异常** (由调用方汇总跳过清单)。
* 300 dpi, 浅色网格 (``grid(alpha=0.3, linestyle="--")``)。
* 中文字体: 依次探测 Windows / macOS / Linux 常见中文字体 (SimHei、PingFang SC、
  Noto Sans CJK SC、文泉驿…); 全部缺失时不报错, 仅在标题追加提示 (文字可能显示为方框)。
* 轨迹类图若无事件流, 按 task-2 要求降级为「择律次数 vs 波动」散点并注明。
"""
from __future__ import annotations

import math
import warnings
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")  # 必须在 pyplot 之前

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

if __package__:  # 作为包导入 (python -m src.charts)
    from . import display as DISP  # type: ignore
else:             # 顶层导入 (sys.path 指向 src 后 import charts)
    import display as DISP  # type: ignore

# ---------------------------------------------------------------------------
# 全局样式
# ---------------------------------------------------------------------------

_CJK_CANDIDATES = [
    # Windows
    "SimHei", "Microsoft YaHei", "SimSun", "KaiTi", "FangSong",
    # macOS
    "PingFang SC", "Hiragino Sans GB", "Heiti SC", "STHeiti",
    # Linux (Noto / 文泉驿 / 文鼎 / Android)
    "Noto Sans CJK SC", "Noto Serif CJK SC", "Source Han Sans SC",
    "WenQuanYi Zen Hei", "WenQuanYi Micro Hei", "AR PL UMing CN",
    "Droid Sans Fallback",
]

DPI = 300
FIG_DPI = 100

_CHINESE_FONT: str | None = None
_FONT_WARNING: str | None = None


def available_chinese_font() -> str | None:
    """返回本机可用的首个中文字体名; 无则 None (只探测一次)。"""
    global _CHINESE_FONT, _FONT_WARNING
    if _CHINESE_FONT is not None or _FONT_WARNING is not None:
        return _CHINESE_FONT
    try:
        from matplotlib import font_manager

        names = {f.name for f in font_manager.fontManager.ttflist}
        for cand in _CJK_CANDIDATES:
            if cand in names:
                _CHINESE_FONT = cand
                return _CHINESE_FONT
        _FONT_WARNING = (
            "未找到中文字体 (" + "/".join(_CJK_CANDIDATES) + "), 图中中文可能显示为方框"
        )
    except Exception as exc:  # pragma: no cover
        _FONT_WARNING = f"中文字体探测失败: {exc}"
    return _CHINESE_FONT


def setup_style() -> str:
    """配置 rcParams 并返回状态说明 (空串 = 正常)。"""
    font = available_chinese_font()
    plt.rcParams["font.sans-serif"] = _CJK_CANDIDATES + ["DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False      # 负号正常显示
    plt.rcParams["figure.dpi"] = FIG_DPI
    plt.rcParams["savefig.dpi"] = DPI
    plt.rcParams["axes.grid"] = True
    plt.rcParams["grid.alpha"] = 0.3
    plt.rcParams["grid.linestyle"] = "--"
    plt.rcParams["figure.autolayout"] = True
    return _FONT_WARNING or ""


def _light_grid(ax) -> None:
    ax.grid(True, alpha=0.3, linestyle="--")
    ax.set_axisbelow(True)


def _save(fig, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fig.savefig(path, dpi=DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return path


def _empty_note(ax, text: str = "无可绘制数据") -> None:
    ax.text(0.5, 0.5, text, ha="center", va="center", fontsize=12, color="#888888",
            transform=ax.transAxes)
    ax.set_xticks([])
    ax.set_yticks([])


# ---------------------------------------------------------------------------
# 1. 择律次数分布
# ---------------------------------------------------------------------------


def _log_bar_edges(xs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """在对数横轴上给出**互不重叠**的柱子左右边界 (相邻值取几何平均当中点)。

    早期用 `width = x*0.35` 且 `align="center"`, 在 symlog 轴上右端柱子会被放得
    极宽并互相压盖, 画成一片阶梯状的糊图 (用户 2026-10-04 指出的那张图)。
    返回 (left, right), 调用方用 ``ax.bar(left, ys, width=right-left, align="edge")``。
    """
    xs = np.asarray(xs, dtype=float)
    off = 1.0 if float(xs.min()) <= 0.0 else 0.0     # 0 无法取几何平均, 整体平移
    t = xs + off
    left = np.empty_like(t)
    right = np.empty_like(t)
    if t.size == 1:
        left[0], right[0] = t[0] / 1.25, t[0] * 1.25
    else:
        mid = np.sqrt(t[:-1] * t[1:])
        left[0] = t[0] * t[0] / t[1]
        right[-1] = t[-1] * t[-1] / t[-2]
        left[1:] = mid
        right[:-1] = mid
    return left - off, right - off


def _tick_values(lo: float, hi: float, linthresh: float) -> list[float]:
    """横轴刻度: 0 原点 + 1/2/5×10^k 对数档; 档位太少时用整齐步长补齐。"""
    ticks = {0.0}
    k = 1.0
    while k <= hi * 1.05:
        for m in (1, 2, 5):
            v = m * k
            if linthresh <= v <= hi * 1.05:
                ticks.add(v)
        k *= 10.0

    def _nice_step(raw: float) -> float:
        mag = 10.0 ** math.floor(math.log10(raw)) if raw > 0 else 1.0
        for m in (1.0, 2.0, 2.5, 5.0, 10.0):
            if raw <= m * mag:
                return m * mag
        return 10.0 * mag

    # 数据跨度小于一个数量级时, 1/2/5 档可能只剩 1~2 个刻度 (横轴就只剩「0 和 20」),
    # 此时线性补齐, 保证至少 4 个刻度可读。
    if len(ticks) < 4:
        step = _nice_step(max(hi - lo, 1.0) / 4.0)
        v = math.ceil(lo / step) * step
        while v <= hi * 1.05 and len(ticks) < 12:
            ticks.add(round(v, 6))
            v += step
    # 注: 「0 ~ linthresh」线性区被 linscale 压得很窄, 在其中再加刻度会互相压盖
    #     (实测长局 41~450 会出现「0 12.5 25」糊在一起), 故不补。
    return sorted(ticks)


def chart_turn_distribution(stats: dict[str, Any], out_dir: str | Path) -> Path | None:
    """择律次数分布 (**横轴对数、纵轴普通线性且从 0 起**) —— 并标注触及歌板上限阈值的频率。

    * 纵轴「局数」用**普通坐标轴**, 底部从 **0** 开始 (对数纵轴会把大量 count=1 的
      长尾挤成一条"厚底", 反而看不出形状)。
    * 横轴「择律次数」用**对数坐标轴**, 且左端保留 **0** 原点 (symlog 的线性段),
      这样既能把长尾展开, 又不会因为 log 无法表示 0 而丢掉原点。

    图中另给出「达到歌板阈值」的局数与占比 —— 即「触及歌板上限阈值」的频率。
    """
    dist = stats.get("turn_distribution") or {}
    values = dist.get("values") or []
    counts = dist.get("counts") or []
    if not values or not counts:
        return None

    out_dir = Path(out_dir)
    xs = np.asarray(values, dtype=float)
    ys = np.asarray(counts, dtype=float)

    turns = stats.get("turns") or {}
    mean_v, med_v = turns.get("mean"), turns.get("p50")
    reach = stats.get("threshold_reach") or {}

    fig, ax = plt.subplots(figsize=(11, 6.5))

    # 横轴: 对数刻度 + 0 原点。
    #   symlog 在 ±linthresh 内为线性段 —— 把 linthresh 设成**数据最小值附近**，
    #   并用很小的 linscale，这样「0 原点」只占极窄一条，对数段能把数据铺满，
    #   避免出现「0 ~ 最小轮次」一大片空白 (坐标轴随数据自适应)。
    lo_d, hi_d = float(xs.min()), float(xs.max())
    linthresh = max(1.0, lo_d * 0.9)
    # linscale 决定「0 ~ linthresh」这段线性区占多宽 (单位: 一个数量级) ——
    # 取很小值, 让 0 原点只占极窄一条, 数据段铺满横轴 (否则数据离 0 较远时,
    # 左半边会是一大片空白)。
    ax.set_xscale("symlog", linthresh=linthresh, linscale=0.02)
    ax.set_xlim(0.0, max(hi_d, 1.0) * 1.08)

    if len(values) <= 60:
        left_edges, right_edges = _log_bar_edges(xs)
        ax.bar(left_edges, ys, width=np.maximum(right_edges - left_edges, 0.0),
               align="edge", color="#4C78A8", edgecolor="white", linewidth=0.5,
               label="频数")
    else:
        # 只画折线, **不填充到 0** —— 否则几乎所有轮次都至少 1 局,
        # 会把图底部涂成一条厚带 (用户明确不要这种"厚底")。
        ax.step(xs, ys, where="mid", color="#2E5C8A", linewidth=1.0, label="频数")

    # 纵轴: 普通线性, 底部 0
    ax.set_yscale("linear")
    ax.set_ylim(bottom=0.0)

    if mean_v is not None:
        ax.axvline(mean_v, color="#E45756", linestyle="-", linewidth=1.6,
                   label=f"均值 {mean_v:.1f}")
    if med_v is not None:
        ax.axvline(med_v, color="#54A24B", linestyle="--", linewidth=1.6,
                   label=f"中位数 {med_v:.1f}")

    # 对数横轴的主刻度: 0 原点 + 1/2/5×10^k, 档位太少时线性补齐 (见 _tick_values)
    hi = float(max(hi_d, 1.0))
    lo = float(max(lo_d, 0.0))
    ticks = _tick_values(lo, hi, linthresh)
    if len(ticks) > 12:      # 太密则只保留 1/5 档
        ticks = [t for i, t in enumerate(ticks) if t == 0 or i % 2 == 0]
    ax.set_xticks(ticks)
    ax.set_xticklabels([f"{t:g}" for t in ticks])
    ax.minorticks_off()

    ax.set_xlabel("择律次数（对数刻度）")
    ax.set_ylabel("局数")
    ax.set_title(f"择律次数分布（{stats.get('meta', {}).get('runs', 0)} 局；横轴对数、纵轴线性）")
    ax.legend(loc="upper right")
    _light_grid(ax)

    # ---- 触及歌板上限阈值的频率 (醒目文本框)
    if reach:
        share = reach.get("share")
        share_txt = f"{share * 100:.2f}%" if share is not None else "—"
        txt = (f"触达歌板上限阈值（{reach.get('threshold', 0):g}）："
               f"{reach.get('runs_reached', 0)} / {reach.get('runs', 0)} 局 = {share_txt}")
        ax.text(0.985, 0.55, txt, transform=ax.transAxes, ha="right", va="bottom",
                fontsize=10.5, color="#0B3D2E",
                bbox=dict(boxstyle="round,pad=0.45", facecolor="#DFF2E1",
                          edgecolor="#54A24B", linewidth=1.1))

    return _save(fig, out_dir / "择律次数分布.png")


# ---------------------------------------------------------------------------
# 2. 各属性分布 (每个属性独立坐标轴)
# ---------------------------------------------------------------------------


def chart_attribute_distribution(stats: dict[str, Any], out_dir: str | Path,
                                 *, max_panels: int = 12, cols: int = 3) -> Path | None:
    """各属性分布 —— **每个属性一个独立子图 (各自 y 轴)**。歌板=终值, 其余=波动。

    不同属性的量纲差异极大 (军心波动可达上千、歌板波动仅百级), 共用一根 y 轴时
    小量纲属性会被压成一条线, 故按属性分面板绘制箱线图。
    """
    attrs = stats.get("attributes") or {}
    if not attrs:
        return None
    # 空样本 (所有属性 count=0) 视为"数据缺失", 优雅跳过
    if not any(st.get("count", 0) > 0 for st in attrs.values()):
        return None

    out_dir = Path(out_dir)
    keys: list[str] = []
    for key, st in attrs.items():
        if st.get("count", 0) == 0:
            continue
        if st.get("min") == st.get("max"):     # 所有局波动相同 -> 无分布可看
            continue
        keys.append(key)
    keys = keys[:max_panels]

    if not keys:
        fig, ax = plt.subplots(figsize=(9, 5.5))
        _empty_note(ax, "所有属性在各局之间完全相同，无分布可绘制")
        ax.set_title("各属性分布")
        return _save(fig, out_dir / "各属性分布.png")

    # 取原始逐局数据 (stats 层已裁剪, 这里需要完整样本)
    series_map = (stats.get("attribute_series") or {})
    panels: list[tuple[str, np.ndarray, str]] = []
    for key in keys:
        vals = series_map.get(key)
        if not vals:
            continue
        data = np.asarray(vals, dtype=float)
        label = attrs[key].get("label", key)
        panels.append((key, data, label))
    if not panels:
        return None

    cols = max(1, min(cols, len(panels)))
    rows = int(np.ceil(len(panels) / cols))
    fig, axes = plt.subplots(rows, cols, figsize=(4.6 * cols, 3.4 * rows),
                             squeeze=False)
    flat = [axes[r][c] for r in range(rows) for c in range(cols)]
    # matplotlib >= 3.9 把 boxplot 的 labels 改名为 tick_labels (3.11 移除旧名)
    _version = tuple(int(p) for p in matplotlib.__version__.split(".")[:2] if p.isdigit())
    _label_kw = "tick_labels" if _version >= (3, 9) else "labels"

    for (key, data, label), ax in zip(panels, flat):
        bp = ax.boxplot([data], patch_artist=True, showmeans=True, widths=0.45,
                        **{_label_kw: [""]},
                        meanprops=dict(marker="D", markerfacecolor="#E45756",
                                       markeredgecolor="#E45756", markersize=6),
                        medianprops=dict(color="#333333", linewidth=1.6),
                        flierprops=dict(marker="o", markersize=2.2, alpha=0.3,
                                        markerfacecolor="#888888", markeredgecolor="none"))
        for patch in bp["boxes"]:
            patch.set_facecolor("#4C78A8")
            patch.set_alpha(0.45)
        st = attrs.get(key, {})
        mean_v, med_v = st.get("mean"), st.get("p50")
        note = f"均值 {mean_v:.4g}" if mean_v is not None else ""
        if med_v is not None:
            note += f"　中位 {med_v:.4g}"
        n_out = st.get("count", 0)
        ax.set_title(f"{label}（{note}）", fontsize=10.5)
        ax.set_ylabel("波动")
        ax.set_xticks([])
        # **按数据自适应 y 轴**: 用 P1~P99 定范围 (再留 12% 余量), 否则少数离群值会把
        # 箱体压成一条线; 完整范围写在下方脚注里, 不丢信息。
        lo_d, hi_d = float(np.nanmin(data)), float(np.nanmax(data))
        q_lo, q_hi = (float(np.nanpercentile(data, 1)),
                      float(np.nanpercentile(data, 99))) if data.size > 4 else (lo_d, hi_d)
        if not np.isfinite(q_lo) or not np.isfinite(q_hi) or q_hi <= q_lo:
            q_lo, q_hi = lo_d, hi_d
        pad = (q_hi - q_lo) * 0.12 or max(abs(q_hi) * 0.1, 1.0)
        ax.set_ylim(q_lo - pad, q_hi + pad)
        clipped = (lo_d < q_lo - pad) or (hi_d > q_hi + pad)
        ax.text(0.5, -0.16, f"范围 {lo_d:g} ~ {hi_d:g}　n={n_out}"
                             + ("　（显示区按 P1~P99 自适应）" if clipped else ""),
                transform=ax.transAxes, ha="center", va="top",
                fontsize=8, color="#888888")
        _light_grid(ax)

    for ax in flat[len(panels):]:
        ax.axis("off")

    fig.suptitle("各属性分布（歌板=终值，其余=波动；红菱形=均值）", fontsize=13)
    fig.tight_layout(rect=(0, 0.02, 1, 0.97))
    return _save(fig, out_dir / "各属性分布.png")


# ---------------------------------------------------------------------------
# 汇总入口
# ---------------------------------------------------------------------------

#: 图表注册表 (只有这两张; 轨迹/频次/解锁类图按用户要求移除)
CHART_REGISTRY: tuple[tuple[str, Any], ...] = (
    ("择律次数分布.png", chart_turn_distribution),
    ("各属性分布.png", chart_attribute_distribution),
)


def render_all_charts(stats: dict[str, Any], out_dir: str | Path) -> dict[str, Any]:
    """生成全部图表。

    返回 ``{"generated": {filename: Path}, "skipped": {filename: reason}, "font_warning": str}``

    任何单张图失败都不会影响其余图表 (逐张 try/except)。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    font_warning = setup_style()

    generated: dict[str, Path] = {}
    skipped: dict[str, str] = {}

    for filename, fn in CHART_REGISTRY:
        try:
            path = fn(stats, out_dir)
        except Exception as exc:  # 健壮性: 单张失败不影响整体
            skipped[filename] = f"绘制异常: {type(exc).__name__}: {exc}"
            continue
        if path is None:
            skipped[filename] = "数据缺失，已优雅跳过"
        else:
            generated[filename] = Path(path)

    return {"generated": generated, "skipped": skipped, "font_warning": font_warning}
