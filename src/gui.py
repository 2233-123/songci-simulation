# -*- coding: utf-8 -*-
"""M5 · tkinter GUI 入口

四个标签页: 开局配置 / 筛选查询 / 模拟运行 / 结果查看

依赖:
  - src/model.py    (已冻结契约, 只读)
  - src/db.py       (查询配置数据)
  - src/simulator.py / stats.py / charts.py / report.py  (由 M3/M4 提供, 懒加载)
"""
from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tkinter as tk
from tkinter import messagebox, ttk

import psycopg2

import config as C
import db as D
import display as DISP        # 效果数值的统一展示口径
import effects as E          # 名臣政策 ID / 开局一次性效果 (供名臣选择器使用)
import model as M

# 可选依赖: 缺失时不崩溃, 仅在该功能被使用时报错
try:
    import effects as EFFECTS
except Exception:  # pragma: no cover - M3 未就绪时
    EFFECTS = None  # type: ignore[assignment]
try:
    import simulator as SIM
except Exception:  # pragma: no cover - M3 未就绪时
    SIM = None  # type: ignore[assignment]
try:
    import stats as STATS
except Exception:  # pragma: no cover
    STATS = None  # type: ignore[assignment]
try:
    import charts as CHARTS
except Exception:  # pragma: no cover
    CHARTS = None  # type: ignore[assignment]
try:
    import report as REPORT
except Exception:  # pragma: no cover
    REPORT = None  # type: ignore[assignment]


# ---------------------------------------------------------------- 适配层

def _sim_run_batch(config: M.SimulationConfig,
                   progress_cb: Callable[[int, int], None] | None = None,
                   cancel_flag: Callable[[], bool] | None = None):
    """调用 M3 内核。

    真实签名 (src/simulator.py):
        run_batch(game: effects.GameData, config, progress=None, cancel=None)
    其中 game 由 effects.load_game_data(poet_ids, minister_ids, policy_ids) 装配 (每批一次)。
    """
    if SIM is None:
        raise RuntimeError("src/simulator.py 尚未就绪")
    if EFFECTS is None:
        raise RuntimeError("src/effects.py 尚未就绪")

    game = EFFECTS.load_game_data(
        poet_ids=config.poet_ids,
        minister_ids=config.minister_ids,
        policy_ids=config.policy_key_ids,
    )
    fn = getattr(SIM, "run_batch", None)
    if not callable(fn):
        raise RuntimeError("simulator 模块未提供 run_batch 函数")
    return fn(game, config, progress=progress_cb, cancel=cancel_flag)


def _write_outputs(summary, out_dir: Path) -> dict[str, Path]:
    """调用 M4 输出层。"""
    produced: dict[str, Path] = {}
    if STATS is None and REPORT is None:
        raise RuntimeError("src/stats.py 与 src/report.py 均未就绪")
    stats_fn = None
    if STATS is not None:
        for name in ("summarize", "compute_stats", "aggregate"):
            stats_fn = getattr(STATS, name, None)
            if callable(stats_fn):
                break
    if REPORT is not None:
        for name in ("write_outputs", "write_report", "generate"):
            fn = getattr(REPORT, name, None)
            if callable(fn):
                try:
                    produced = fn(summary, out_dir)
                except TypeError:
                    produced = fn(summary, out_dir, stats_fn) if stats_fn else fn(summary, out_dir)
                break
    if CHARTS is not None and not produced:
        for name in ("make_all_charts", "render_all", "make_all"):
            fn = getattr(CHARTS, name, None)
            if callable(fn):
                produced = {"charts": fn(summary, out_dir / "charts")}
                break
    return produced or {}


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        webbrowser.open(path.as_uri())


# ---------------------------------------------------------------- 主窗口

class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("宋词择律模拟器 — 无悔华夏")
        self.geometry("1180x820")
        self.minsize(980, 680)

        # 运行状态
        self._worker: threading.Thread | None = None
        self._cancel = threading.Event()
        self._queue: queue.Queue = queue.Queue()
        self._last_output_dir: Path | None = None
        self._last_summary = None

        # 缓存配置数据
        self._singers: list[tuple] = []
        self._poets: list[tuple] = []
        self._ministers: list[tuple] = []
        self._load_lookups()

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.tab_config = ttk.Frame(nb)
        self.tab_sql = ttk.Frame(nb)
        self.tab_run = ttk.Frame(nb)
        self.tab_result = ttk.Frame(nb)
        nb.add(self.tab_config, text="① 开局配置")
        nb.add(self.tab_sql, text="② 筛选查询")
        nb.add(self.tab_run, text="③ 模拟运行")
        nb.add(self.tab_result, text="④ 结果查看")
        self.nb = nb

        self._build_config_tab()
        self._build_sql_tab()
        self._build_run_tab()
        self._build_result_tab()

        self._status = tk.StringVar(value="就绪")
        bar = ttk.Frame(self)
        bar.pack(fill="x", side="bottom")
        ttk.Label(bar, textvariable=self._status, anchor="w").pack(fill="x", padx=8, pady=3)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------ 数据加载

    def _load_lookups(self) -> None:
        try:
            self._singers = D.fetch_all("""
                SELECT id, name, bold_favor, graceful_favor, feature_desc
                FROM songci.singer ORDER BY id""")
            self._poets = D.fetch_all("""
                SELECT p.id, p.name, pp.policy_id, po.policy_name
                FROM songci.poet p
                LEFT JOIN songci.poet_policy pp ON pp.poet_id = p.id
                LEFT JOIN songci.policy po ON po.key_id = pp.policy_key_id
                WHERE p.raw_json->>'_placeholder' IS DISTINCT FROM 'true'
                ORDER BY p.id""")
            self._ministers = D.fetch_all("""
                SELECT m.id, m.name
                FROM songci.minister m
                WHERE m.time_type_list && ARRAY[11,1101,1102]::integer[]
                ORDER BY m.id""")
            # 东坡食单 (DongPoFoodConfig + CommonEffectPoolConfig 652-660)
            self._dongpo = D.fetch_all("""
                SELECT f.id, f.name, f.ci_yuan_price, f.pool_id, p.effect_desc
                FROM songci.dongpo_food f
                LEFT JOIN songci.common_effect_pool p ON p.id = f.pool_id
                ORDER BY f.id""")
        except Exception as exc:  # 数据库不可用时 GUI 仍应打开
            self._status_set(f"配置数据加载失败: {exc}")

    def _status_set(self, msg: str) -> None:
        self._status.set(msg)

    # ------------------------------------------------------------ ① 开局配置

    def _build_config_tab(self) -> None:
        root = self.tab_config

        # 左列: 唱词人 + 开局词人
        left = ttk.LabelFrame(root, text="唱词人 / 开局词人")
        left.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        right = ttk.LabelFrame(root, text="初始资源与规则")
        right.grid(row=0, column=1, sticky="nsew", padx=6, pady=6)
        root.columnconfigure(0, weight=3)
        root.columnconfigure(1, weight=2)
        root.rowconfigure(0, weight=1)

        ttk.Label(left, text="唱词人").grid(row=0, column=0, sticky="w", padx=4, pady=2)
        self.var_singer = tk.StringVar()
        self.cmb_singer = ttk.Combobox(left, textvariable=self.var_singer,
                                       state="readonly", width=52)
        self.cmb_singer.grid(row=0, column=1, sticky="ew", padx=4, pady=2)
        self._singer_labels = [
            f"[{r[0]}] {r[1]}  豪放偏好{r[2]} / 婉约偏好{r[3]}  — {r[4] or ''}"
            for r in self._singers
        ]
        self.cmb_singer["values"] = self._singer_labels
        if self._singer_labels:
            self.cmb_singer.current(8 if len(self._singer_labels) > 8 else 0)
        self.cmb_singer.bind("<<ComboboxSelected>>", lambda e: self._on_singer_change())

        self.lbl_singer_detail = ttk.Label(left, text="", wraplength=520, foreground="#555")
        self.lbl_singer_detail.grid(row=1, column=0, columnspan=2, sticky="w", padx=4, pady=(0, 6))
        self._on_singer_change()

        ttk.Label(left, text="开局词人（勾选，其效果开局即生效）").grid(
            row=2, column=0, columnspan=2, sticky="w", padx=4)
        box = ttk.Frame(left)
        box.grid(row=3, column=0, columnspan=2, sticky="nsew", padx=4, pady=4)
        left.rowconfigure(3, weight=1)
        left.columnconfigure(1, weight=1)
        self._poet_vars: list[tk.BooleanVar] = []
        body_p = self._scroll_area(box)
        self._poet_checks: list[ttk.Checkbutton] = []
        for r in self._poets:
            var = tk.BooleanVar(value=False)
            cb = ttk.Checkbutton(body_p, text=self._poet_effect_label(r), variable=var,
                                 takefocus=False)
            cb.pack(anchor="w", fill="x")
            self._poet_vars.append(var)
            self._poet_checks.append(cb)

        ttk.Label(left, text="开局名臣（勾选后自动勾选同名词人）").grid(
            row=4, column=0, columnspan=2, sticky="w", padx=4, pady=(8, 0))
        box2 = ttk.Frame(left)
        box2.grid(row=5, column=0, columnspan=2, sticky="nsew", padx=4, pady=4)
        left.rowconfigure(5, weight=1)
        # 只列出拥有「开启择律系统」类政策的名臣 (其余名臣与本模拟无关)
        self._ministers = [r for r in self._ministers if self._minister_has_songci_policy(r[0])]
        self._minister_vars: list[tk.BooleanVar] = []
        self._minister_checks: list[ttk.Checkbutton] = []
        body_m = self._scroll_area(box2)
        for r in self._ministers:
            var = tk.BooleanVar(value=False)
            cb = ttk.Checkbutton(body_m, text=self._minister_effect_label(r), variable=var,
                                 takefocus=False,
                                 command=lambda v=var, m=r[0]: self._on_minister_toggle(v, m))
            cb.pack(anchor="w", fill="x")
            self._minister_vars.append(var)
            self._minister_checks.append(cb)

        # ---- 东坡食单 (苏轼「老饕」开启; 未勾苏轼名臣时不可勾选) ----
        lf_dp = ttk.LabelFrame(right, text="东坡食单（需勾选苏轼名臣）")
        lf_dp.grid(row=0, column=0, columnspan=2, sticky="nsew", padx=4, pady=(0, 6))
        right.rowconfigure(0, weight=1)
        self._dongpo_vars: list[tk.BooleanVar] = []
        self._dongpo_checks: list[ttk.Checkbutton] = []
        self._dongpo_hint = ttk.Label(lf_dp, text="", foreground="#a00", wraplength=380)
        self._dongpo_hint.pack(anchor="w", padx=4, pady=(2, 0))
        box_dp = ttk.Frame(lf_dp)
        box_dp.pack(fill="both", expand=True, padx=2, pady=2)
        body_dp = self._scroll_area(box_dp)
        for d in getattr(self, "_dongpo", []):
            var = tk.BooleanVar(value=False)
            cb = ttk.Checkbutton(body_dp, text=self._dongpo_label(d), variable=var,
                                 takefocus=False)
            cb.pack(anchor="w", fill="x")
            self._dongpo_vars.append(var)
            self._dongpo_checks.append(cb)
        for cb in self._dongpo_checks:
            cb.state(["disabled"])

        # 右列: 初始值与规则 (不显示任何提示性注释)
        r = 1

        def add_entry(parent, label: str, var: tk.StringVar, r: int) -> None:
            ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", padx=4, pady=2)
            ttk.Entry(parent, textvariable=var, width=14).grid(row=r, column=1, sticky="w", padx=4, pady=2)

        self.fields: dict[str, tk.StringVar] = {}
        init_specs = [
            ("init_board", "初始歌板（可为负）", "0"),
            ("init_verse_point", "初始词元", "0"),
            ("init_bold_sentiment", "初始豪放词情", "0"),
            ("init_graceful_sentiment", "初始婉约词情", "0"),
            ("init_benefit_bonus", "初始择律收益（%）", "0"),
        ]
        for key, label, default in init_specs:
            v = tk.StringVar(value=default)
            self.fields[key] = v
            add_entry(right, label, v, r)
            r += 1

        ttk.Separator(right, orient="horizontal").grid(
            row=r, column=0, columnspan=2, sticky="ew", pady=8)
        r += 1
        rule_specs = [
            ("end_threshold", "结束阈值（歌板）", "1000"),
            ("draw_base_cost", "每次基础消耗", "10"),
            ("runs", "模拟局数", "10000"),
            ("seed", "随机种子", "42"),
        ]
        for key, label, default in rule_specs:
            v = tk.StringVar(value=default)
            self.fields[key] = v
            add_entry(right, label, v, r)
            r += 1
        # 词情层数上限固定 100, 不暴露给用户 (仍写入配置快照以便复现)
        self.fields["sentiment_cap"] = tk.StringVar(value="100")

        self.var_unlock_all = tk.BooleanVar(value=False)
        ttk.Checkbutton(right, text="解锁所有词句库（忽略名臣解锁门控）",
                        variable=self.var_unlock_all).grid(
            row=r, column=0, columnspan=2, sticky="w", padx=4, pady=2)
        r += 1
        self.var_record_events = tk.BooleanVar(value=True)
        ttk.Checkbutton(right, text="记录逐次择律事件流（体积较大）",
                        variable=self.var_record_events).grid(
            row=r, column=0, columnspan=2, sticky="w", padx=4, pady=2)
        r += 1

        self.var_cfgname = tk.StringVar(value="默认配置")
        ttk.Label(right, text="配置名称").grid(row=r, column=0, sticky="w", padx=4, pady=2)
        ttk.Entry(right, textvariable=self.var_cfgname, width=20).grid(
            row=r, column=1, columnspan=2, sticky="w", padx=4, pady=2)
        r += 1

        btns = ttk.Frame(right)
        btns.grid(row=r, column=0, columnspan=2, sticky="w", padx=4, pady=8)
        ttk.Button(btns, text="保存配置到 JSON", command=self._save_config).pack(side="left", padx=2)
        ttk.Button(btns, text="从 JSON 载入", command=self._load_config).pack(side="left", padx=2)

    # ------------------------------------------------------------ 勾选列表辅助

    @staticmethod
    def _scroll_area(parent: ttk.Frame) -> tk.Frame:
        """在 parent 内建一个带竖直滚动条的容器, 返回可往里 pack 的 Frame。

        滚动只作用于本容器: 鼠标滚轮在**本区域内**才生效 (不再用 bind_all,
        否则多个滚动区会互相抢焦点/乱滚)。
        """
        canvas = tk.Canvas(parent, highlightthickness=0, borderwidth=0)
        sb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        body = ttk.Frame(canvas)
        win = canvas.create_window((0, 0), window=body, anchor="nw")

        def _sync(_evt=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _fit(evt):
            canvas.itemconfigure(win, width=evt.width)

        def _wheel(evt):
            # 仅当鼠标位于本区域内且内容高于视口时才滚动
            x, y = canvas.winfo_pointerxy()
            cx, cy = canvas.winfo_rootx(), canvas.winfo_rooty()
            inside = (cx <= x <= cx + canvas.winfo_width()
                      and cy <= y <= cy + canvas.winfo_height())
            if inside and body.winfo_reqheight() > canvas.winfo_height():
                canvas.yview_scroll(int(-evt.delta / 120), "units")
                return "break"
            return None

        body.bind("<Configure>", _sync)
        canvas.bind("<Configure>", _fit)
        # 全局监听滚轮, 但只有指针落在本区域内才生效 —— 多个滚动区因此互不干扰
        canvas.bind_all("<MouseWheel>", _wheel)
        return body

    # ------------------------------------------------------------ 勾选联动

    def _on_minister_toggle(self, var: tk.BooleanVar, minister_id: int) -> None:
        """勾选开局名臣时, 自动勾选同名的开局词人; 并刷新东坡食单可用状态。"""
        self._update_dongpo_state()
        if not var.get():
            return
        name = next((r[1] for r in self._ministers if r[0] == minister_id), "")
        for row, pvar in zip(self._poets, self._poet_vars):
            if row[1] == name:
                pvar.set(True)

    def _dongpo_label(self, row) -> str:
        """东坡食单勾选项文案: `[ID] 菜名：效果`。

        价格 (`CiYuanPrice`) 不显示 —— 用户 2026-10-03 明确「价格不管」。
        """
        return f"[{row[0]}] {row[1]}：{row[4] or '（无效果）'}"

    def _update_dongpo_state(self) -> None:
        """东坡食单由苏轼「老饕」政策开启: 未勾苏轼名臣时禁用菜品勾选。"""
        checks = getattr(self, "_dongpo_checks", [])
        if not checks:
            return
        on = any(row[0] == C.SU_SHI_MINISTER_ID and v.get()
                 for row, v in zip(self._ministers, self._minister_vars))
        for cb in checks:
            cb.state(["!disabled"] if on else ["disabled"])
        hint = getattr(self, "_dongpo_hint", None)
        if hint is not None:
            hint.configure(text="" if on else "需先勾选「苏轼」名臣（政策「老饕」开启东坡食单）")

    # ------------------------------------------------------------ 效果文案

    EffText = "str"

    # 语义上确为「比例」的效果 (其 DB 值为小数, 0.07 = 7%); 其余不加百分号
    _RATIO_EFF_IDS = frozenset({152, 103, 202, 203, 211})

    def _fmt_effects(self, pairs) -> str:
        """统一走 display 模块的展示口径 (军规/粮食产量/发展年数等)。"""
        return DISP.format_effects(pairs)

    @staticmethod
    def _fmt_military_rule(value: float) -> str:
        """军规表述 = 具体数值 / 20000, 以百分比展示。"""
        return f"军规{value / DISP.MILITARY_RULE_DIVISOR * 100:+g}%"

    @staticmethod
    def _effect_text(label: str) -> str:
        """从 DrawRule.label 中剥掉「名臣 政策名: 」前缀, 只留具体效果。

        例: "岳飞 背嵬军(3级): 词元-10 战斗力+8%" -> "词元-10 战斗力+8%"
            "陆游: 词元≥50 钱粮消耗-5%"           -> "词元≥50 钱粮消耗-5%"
        """
        head, sep, tail = label.partition(": ")
        if sep and head and " " in head.strip() and len(head) <= 24:
            return tail.strip()
        if sep and head and "：" not in head and len(head) <= 12:
            return tail.strip()
        return label.strip()

    # 管线类效果: 没有数值语义, 不进入「开局效果」文案
    _PIPELINE_EFF_IDS = E.PIPELINE_EFFECT_IDS

    def _poet_effect_label(self, row) -> str:
        """开局词人: **严格按游戏文案描述** (用户 2026-10-03 裁定)。

        * 不再把 DB 里那些「文案没写数字」的效果展开成精确数值
          (例如 晏几道 只写「钱粮消耗少量提升」, 就照写, 不写 +3%×2)。
        * 唯一例外: `757 发展年数` —— 文案写「发展时间+N分钟」, 按用户裁定的展示口径
          渲染成「发展年数+X」。
        * 逐次规则若带**上限条件**(王安石 词元 10~100), 在文案后补注实际条件。
        """
        pid, name = row[0], row[1]
        reward_pid = row[2] if len(row) > 2 else None
        body = self._apply_display_units(self._policy_desc_text(reward_pid), reward_pid)
        note = self._rule_condition_note(reward_pid)
        if note:
            body = f"{body}（{note}）" if body else note
        if not body:
            # 文案为空时才退回数值摘要
            try:
                effs = D.fetch_all("""
                    SELECT pe.effect_type, pe.effect_value, pe.add_type
                    FROM songci.poet_policy pp
                    JOIN songci.policy_effect pe ON pe.policy_key_id = pp.policy_key_id
                    WHERE pp.poet_id = %s AND pe.depth = 0
                      AND pe.effect_type <> ALL(%s)
                    ORDER BY pe.ordinal""", (pid, sorted(self._PIPELINE_EFF_IDS)))
            except Exception:
                effs = []
            if reward_pid is not None and int(reward_pid) in E.PER_DRAW_POET_POLICY_IDS:
                effs = []
            body = self._fmt_effects(effs) if effs else ""
        return f"[{pid}] {name}：{body}" if body else f"[{pid}] {name}"

    def _apply_display_units(self, text: str, policy_id) -> str:
        """把文案里的「发展时间+N分钟」按展示口径换成「发展年数+X」。"""
        if not text or not policy_id:
            return text
        try:
            row = D.fetch_one("""
                SELECT effect_value FROM songci.policy_effect
                WHERE policy_key_id = (SELECT key_id FROM songci.policy
                                       WHERE id = %s ORDER BY key_id LIMIT 1)
                  AND depth = 0 AND effect_type = %s ORDER BY ordinal LIMIT 1""",
                (int(policy_id), C.EFF_DEVELOP_YEARS))
        except Exception:
            return text
        if not row or row[0] is None:
            return text
        rendered = DISP.format_effect(C.EFF_DEVELOP_YEARS, float(row[0]))
        return re.sub(r"发展(?:时间|年数)\s*[+＋]?\s*[\d.]+\s*(?:分钟|分|年)?",
                      rendered, text, count=1)

    def _rule_condition_note(self, policy_id) -> str:
        """逐次规则的**实际条件**(当文案没写全时补注, 例如上限)。"""
        if not policy_id:
            return ""
        for r in E.build_draw_rules():
            if r.source_policy_id != int(policy_id) or not getattr(r, "max_value", 0.0):
                continue
            name = self._GATE_TEXT.get(r.gate, r.gate)
            return f"实际条件：{name} {r.min_value:g}~{r.max_value:g}"
        return ""

    # 富文本里的资源占位符 -> 中文名 (只列本项目会用到的)
    _RES_NAME = {"Drink": "醴", "SiChou": "丝绸", "ShiZi": "士子", "JingShu": "经书",
                 "Caravan": "商队", "MoBao": "墨宝"}

    def _policy_desc_text(self, policy_id) -> str:
        """取政策的 `effect_desc` 原文 (去富文本, 去层数说明)。"""
        if not policy_id:
            return ""
        try:
            row = D.fetch_one("""
                SELECT effect_desc FROM songci.policy
                WHERE id = %s ORDER BY key_id LIMIT 1""", (int(policy_id),))
        except Exception:
            return ""
        if not row or not row[0]:
            return ""
        body = " ".join(str(row[0]).split())
        for key, cn in self._RES_NAME.items():
            body = body.replace(f"{{ResSwitcn;{key}}}", cn)
        body = re.sub(r"<[^>]*>", "", body)
        body = re.sub(r"\{[^}]*\}", "", body)          # 其余占位符直接去掉
        body = body.replace("【】", "")                  # 占位符被去掉后剩下的空书名号
        body = body.replace("正确择豪放律", "抽到豪放词").replace("正确择婉约律", "抽到婉约词")
        body = re.sub(r"[（(][^）)]*至多[^）)]*[）)]", "", body)
        return re.sub(r"\s+", " ", body).strip("， ")

    # 简单的中文名映射 (避免每次调用都查库)
    _EFF_CN = {
        32510: "歌板", 32514: "词元", 32515: "豪放词情", 32516: "婉约词情",
        32517: "择律消耗", 32513: "后续择律收益", 51: "民心", 209: "军心",
        204: "战斗力", 351: "腐化", 552: "文化点", 553: "威望", 757: "发展年数",
        590: "乐感等级", 211: "军团规模", 152: "粮食产量", 103: "金钱产量",
        202: "兵甲钱消耗", 203: "兵甲粮消耗", 459: "丝绸", 454: "士子", 31480: "繁荣",
    }

    def _cn(self, t: int) -> str:
        return (self._EFF_CN.get(int(t))
                or C.NUMERIC_ATTRIBUTE_EFFECTS.get(int(t))
                or f"效果{t}")

    def _startup_effects(self, kept) -> list:
        """把若干政策的 depth=0 效果汇总为 [(type, value, add_type)]。"""
        out: list = []
        for key, _pid, _pname in kept:
            try:
                rows = D.fetch_all("""
                    SELECT effect_type, effect_value, add_type FROM songci.policy_effect
                    WHERE policy_key_id = %s AND depth = 0 ORDER BY ordinal""", (key,))
            except Exception:
                rows = []
            for t, v, a in rows:
                if int(t) in self._PIPELINE_EFF_IDS:
                    continue
                out.append((int(t), float(v or 0.0),
                            int(a) if a is not None else C.ADD_TYPE_ADDITIVE))
        return out

    def _startup_notes(self, kept) -> list[str]:
        """政策里「开关型」881 子 ID 的机制说明 (无数值语义但改变模拟行为)。"""
        notes: list[str] = []
        for key, _pid, _pname in kept:
            try:
                rows = D.fetch_all("""
                    SELECT effect_value FROM songci.policy_effect
                    WHERE policy_key_id = %s AND depth = 0 AND effect_type = 881""", (key,))
            except Exception:
                rows = []
            for (v,) in rows:
                text = E.GRANT_SUBID_NOTE.get(int(v or 0))
                if text and text not in notes:
                    notes.append(text)
        return notes

    def _minister_effect_label(self, row) -> str:
        """开局名臣: 显示「开局一次性效果」+「对应的择律效果」。

        逐次效果的归属**直接查 `MINISTER_DRAW_POLICY_IDS`** (与 simulator 实际生效
        的映射同一个源), 而不是反查 `1030` 的父政策 —— 后者会漏掉「规则源政策本身
        就是父政策」的条目 (陆游「示儿」3113、李清照「声声慢」3168/3169/3170)。
        开局部分也不只看歌板, 而是该名臣**全部择律政策**的数值效果
        (如李清照「金石录」的文化点+5)。
        """
        mid, name = row[0], row[1]
        ids = sorted(E.STARTUP_MINISTER_POLICY_IDS)
        try:
            kept = D.fetch_all("""
                SELECT DISTINCT ON (p.policy_name) p.key_id, p.id, p.policy_name
                FROM songci.policy p
                WHERE p.minister_id = %s AND p.id = ANY(%s)
                ORDER BY p.policy_name, p.star_cnt DESC NULLS LAST, p.id DESC, p.key_id""", (mid, ids))
        except Exception:
            kept = []
        startup = self._startup_effects(kept)
        txt = self._fmt_effects(startup) if startup else ""
        notes = self._startup_notes(kept)
        if notes:
            plain = [n.replace("**", "") for n in notes]
            txt = (txt + "　" if txt else "") + "；".join(plain)
        owned = frozenset(getattr(SIM, "MINISTER_DRAW_POLICY_IDS", {}).get(mid, ())) \
            if SIM is not None else frozenset()
        per: list[str] = []
        for r in E.build_draw_rules():
            if r.rule_id.startswith("singer"):
                continue
            if owned:
                if r.source_policy_id not in owned:
                    continue
            else:   # 退化路径: simulator 不可用时仍按 1030 父政策反查
                par = D.fetch_all("""
                    SELECT DISTINCT p.minister_id FROM songci.policy_effect pe
                    JOIN songci.policy p ON p.key_id = pe.policy_key_id
                    WHERE pe.effect_type = 1030 AND pe.depth = 0 AND pe.effect_value = %s""",
                    (r.source_policy_id,))
                if not any(x[0] == mid for x in par):
                    continue
            text = self._sub_policy_effect_text(r)
            if text not in per:
                per.append(text)
        # 不写「（无…）」占位: 没有的部分直接省略
        parts = []
        if txt:
            parts.append(f"开局：{txt}")
        if per:
            parts.append(f"择律：{'；'.join(per)}")
        body = "　".join(parts)
        return f"[{mid}] {name}　{body}" if body else f"[{mid}] {name}"

    # 触发条件的中文表述 (与 simulator._GATE_ATTR / _apply_draw_rules 同源)
    _GATE_TEXT = {
        "bold": "抽到豪放词", "graceful": "抽到婉约词",
        "verse_point": "词元", "culture_point": "文化点", "popular_support": "民心",
    }

    # 游戏文案里的「条件前缀」子句 (会被代码给出的权威条件替换)
    _COND_PREFIX_RE = re.compile(
        r"^(?:每次择律(?:正确|错误)?时?|抽到[\u4e00-\u9fff]+词(?:后|时)?"
        r"|正确择[\u4e00-\u9fff]+律(?:后|时)?|若[\u4e00-\u9fff]+?达到\d+"
        r"|[\u4e00-\u9fff]{1,4}达到\d+|第\d+次择律)[，,]?\s*")

    def _rule_condition_text(self, rule) -> str:
        """从规则本身的 gate/min_attr 生成触发条件文案 (权威, 与模拟一致)。"""
        parts: list[str] = []
        if rule.gate in ("bold", "graceful"):
            parts.append(self._GATE_TEXT[rule.gate])
        elif rule.gate and rule.gate in self._GATE_TEXT:
            text = f"{self._GATE_TEXT[rule.gate]}≥{rule.min_value:g}"
            if getattr(rule, "max_value", 0.0):
                text += f" 且 ≤{rule.max_value:g}"
            parts.append(text)
        if rule.min_attr:
            text = f"{self._GATE_TEXT.get(rule.min_attr, rule.min_attr)}≥{rule.min_value:g}"
            if getattr(rule, "max_value", 0.0):
                text += f" 且 ≤{rule.max_value:g}"
            parts.append(text)
        return " 且 ".join(parts)

    def _sub_policy_effect_text(self, rule) -> str:
        """逐次效果的展示文案。

        效果正文取子政策的 `EffectDesc` 原文 (游戏内文案); **触发条件改由代码给出**
        (游戏子政策的文案可能过期, 例: 王安石 32301 文案写「词元达到10」,
        而 `condition_values` 与父政策 3233 均为 8)。
        去掉游戏富文本标记; 「正确择X律」统一改写为「抽到X词」;
        并去掉「(至多N层)」这类层数说明 —— 显示的是实际数值而非层数上限。
        """
        cond = self._rule_condition_text(rule)
        # 累积/缺失型规则没有 policy_effect 正文, 直接用规则自身的精确文案
        if rule.handler in ("benefit_per_10", "benefit_missing", "none"):
            body = self._effect_text(rule.label)
            return f"{cond}：{body}" if cond else body

        desc = ""
        try:
            row = D.fetch_one("""
                SELECT effect_desc FROM songci.policy
                WHERE id = %s ORDER BY key_id LIMIT 1""", (rule.source_policy_id,))
            if row and row[0]:
                desc = " ".join(str(row[0]).split())
        except Exception:
            desc = ""
        if not desc:
            body = self._effect_text(rule.label)
            return f"{cond}：{body}" if cond else body
        body = re.sub(r"<[^>]*>", "", desc)
        body = re.sub(r"\{[^}]*\}", "", body)
        body = body.replace("正确择豪放律", "抽到豪放词").replace("正确择婉约律", "抽到婉约词")
        body = body.replace("择律正确", "择律")
        body = re.sub(r"[（(][^）)]*至多[^）)]*[）)]", "", body)   # 去掉「(至多20层)」
        # 剥掉游戏文案自带的条件前缀, 由代码给出的条件替代
        for _ in range(4):
            new = self._COND_PREFIX_RE.sub("", body, count=1)
            if new == body:
                break
            body = new
        body = re.sub(r"(\S)时(?=[，,：:])", r"\1", body)
        body = re.sub(r"，\s*", "，", body)
        body = re.sub(r"\s+", " ", body).strip().strip("，").strip()
        body = re.sub(r"^(?:时|后)[，,]?\s*", "", body).strip()
        return f"{cond}：{body}" if cond else body

    def _minister_has_songci_policy(self, mid: int) -> bool:
        """该名臣是否拥有「开启择律系统」类政策 (决定是否列入名臣选择列表)。"""
        try:
            return bool(D.scalar("""
                SELECT count(*) FROM songci.policy
                WHERE minister_id = %s AND id = ANY(%s)""",
                (mid, sorted(E.STARTUP_MINISTER_POLICY_IDS))))
        except Exception:
            return False

    def _on_singer_change(self) -> None:
        idx = self.cmb_singer.current()
        if 0 <= idx < len(self._singers):
            r = self._singers[idx]
            total = (r[2] or 0) + (r[3] or 0)
            p = (r[2] / total) if total else 0.0
            self.lbl_singer_detail.configure(
                text=f"效果：{r[4] or '无'}    P(抽到豪放)={p:.3f}")

    def _collect_config(self) -> M.SimulationConfig:
        def fnum(key: str) -> float:
            raw = self.fields[key].get().strip()
            if raw == "":
                raise ValueError(f"「{key}」不能为空")
            try:
                return float(raw)
            except ValueError:
                raise ValueError(f"「{key}」不是合法数字：{raw!r}")

        def inum(key: str) -> int:
            return int(round(fnum(key)))

        sidx = self.cmb_singer.current()
        singer_id = self._singers[sidx][0] if 0 <= sidx < len(self._singers) else None
        poet_ids = [r[0] for r, v in zip(self._poets, self._poet_vars) if v.get()]
        minister_ids = [r[0] for r, v in zip(self._ministers, self._minister_vars) if v.get()]
        # 东坡食单: 仅在勾选苏轼名臣时收集 (与引擎的生效条件一致)
        if C.SU_SHI_MINISTER_ID in minister_ids:
            dongpo_food_ids = [r[0] for r, v in zip(getattr(self, "_dongpo", []),
                                                    self._dongpo_vars) if v.get()]
        else:
            dongpo_food_ids = []

        cfg = M.SimulationConfig(
            name=self.var_cfgname.get().strip() or "未命名配置",
            singer_id=singer_id,
            poet_ids=poet_ids,
            minister_ids=minister_ids,
            dongpo_food_ids=dongpo_food_ids,
            init_board=fnum("init_board"),
            init_verse_point=fnum("init_verse_point"),
            init_bold_sentiment=inum("init_bold_sentiment"),
            init_graceful_sentiment=inum("init_graceful_sentiment"),
            init_benefit_bonus=fnum("init_benefit_bonus") / 100.0,
            draw_base_cost=fnum("draw_base_cost"),
            end_threshold=fnum("end_threshold"),
            sentiment_cap=inum("sentiment_cap"),
            runs=inum("runs"),
            seed=inum("seed"),
            unlock_all_verses=self.var_unlock_all.get(),
            record_events=self.var_record_events.get(),
        )
        # 载入配置时带进来的初始属性 (民心/军心/威望/腐化…) 必须保留, 不能被打回默认值;
        # 但「不再统计」的五个属性一律剔除 (文化点/乐感等级/军规/繁荣/变法)。
        cfg.init_attributes.update(getattr(self, "_init_attrs", {}) or {})
        for k in list(cfg.init_attributes):
            if k in M.HIDDEN_ATTRIBUTE_KEYS:
                cfg.init_attributes.pop(k, None)
        return cfg

    def _save_config(self) -> None:
        try:
            cfg = self._collect_config()
        except ValueError as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        path = C.PROJECT_ROOT / f"配置_{cfg.safe_name()}.json"
        path.write_text(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        self._status_set(f"配置已保存：{path}")
        messagebox.showinfo("已保存", str(path))

    def _load_config(self) -> None:
        from tkinter import filedialog
        fp = filedialog.askopenfilename(initialdir=str(C.PROJECT_ROOT),
                                        filetypes=[("JSON", "*.json")])
        if not fp:
            return
        try:
            data = json.loads(Path(fp).read_text(encoding="utf-8"))
            cfg = M.SimulationConfig.from_dict(data)
        except Exception as exc:
            messagebox.showerror("载入失败", str(exc))
            return
        self.var_cfgname.set(cfg.name)
        self.fields["init_board"].set(str(cfg.init_board))
        self.fields["init_verse_point"].set(str(cfg.init_verse_point))
        self.fields["init_bold_sentiment"].set(str(cfg.init_bold_sentiment))
        self.fields["init_graceful_sentiment"].set(str(cfg.init_graceful_sentiment))
        # GUI 里「初始择律收益」以**百分比**为单位填写 (10 = +10%), 配置内部存小数
        self.fields["init_benefit_bonus"].set(f"{float(cfg.init_benefit_bonus) * 100:g}")
        self.fields["end_threshold"].set(str(cfg.end_threshold))
        self.fields["draw_base_cost"].set(str(cfg.draw_base_cost))
        self.fields["sentiment_cap"].set(str(cfg.sentiment_cap))
        self.fields["runs"].set(str(cfg.runs))
        self.fields["seed"].set(str(cfg.seed))
        self._init_attrs = {k: v for k, v in (cfg.init_attributes or {}).items()
                            if k not in M.HIDDEN_ATTRIBUTE_KEYS}
        self.var_unlock_all.set(cfg.unlock_all_verses)
        self.var_record_events.set(cfg.record_events)
        for row, var in zip(self._poets, self._poet_vars):
            var.set(row[0] in cfg.poet_ids)
        for row, var in zip(self._ministers, self._minister_vars):
            var.set(row[0] in cfg.minister_ids)
        for row, var in zip(getattr(self, "_dongpo", []), self._dongpo_vars):
            var.set(row[0] in (cfg.dongpo_food_ids or []))
        self._update_dongpo_state()
        if cfg.singer_id is not None:
            for i, row in enumerate(self._singers):
                if row[0] == cfg.singer_id:
                    self.cmb_singer.current(i)
                    self._on_singer_change()
                    break
        self._status_set(f"已载入：{fp}")

    # ------------------------------------------------------------ ② 筛选查询

    PRESET_VIEWS = [
        ("词句收益宽表", "SELECT * FROM songci.v_verse_reward ORDER BY id"),
        ("词人/名臣效果表", "SELECT * FROM songci.v_reward_overview"),
        ("唱词人效果总览", "SELECT * FROM songci.v_singer_overview"),
        ("东坡食单", "SELECT * FROM songci.v_dongpo_food"),
    ]

    def _build_sql_tab(self) -> None:
        root = self.tab_sql
        top = ttk.Frame(root)
        top.pack(fill="x", padx=6, pady=6)
        ttk.Label(top, text="预设口径").pack(side="left")
        self.var_view = tk.StringVar(value=self.PRESET_VIEWS[0][0])
        cmb = ttk.Combobox(top, textvariable=self.var_view, state="readonly",
                           width=46, values=[v[0] for v in self.PRESET_VIEWS])
        cmb.pack(side="left", padx=6)
        cmb.bind("<<ComboboxSelected>>", lambda e: self._apply_preset())
        ttk.Button(top, text="执行查询", command=self._run_sql).pack(side="left", padx=6)
        ttk.Button(top, text="导出 CSV", command=self._export_csv).pack(side="left", padx=2)
        ttk.Button(top, text="统计行数", command=self._count_views).pack(side="left", padx=2)

        self.txt_sql = tk.Text(root, height=5, wrap="none")
        self.txt_sql.pack(fill="x", padx=6)
        self.txt_sql.insert("1.0", self.PRESET_VIEWS[0][1])

        frame = ttk.Frame(root)
        frame.pack(fill="both", expand=True, padx=6, pady=6)
        self.tree_sql = ttk.Treeview(frame, show="headings")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree_sql.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=self.tree_sql.xview)
        self.tree_sql.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree_sql.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self._sql_rows: list[tuple] = []

    def _apply_preset(self) -> None:
        label = self.var_view.get()
        for lab, sql in self.PRESET_VIEWS:
            if lab == label:
                self.txt_sql.delete("1.0", "end")
                self.txt_sql.insert("1.0", sql)
                break

    def _run_sql(self) -> None:
        sql = self.txt_sql.get("1.0", "end").strip().rstrip(";")
        if not sql:
            return
        if not sql.lower().lstrip().startswith(("select", "with")):
            messagebox.showwarning("只读限制", "此处只允许执行 SELECT / WITH 查询，以防误改数据。")
            return
        cols: list[str] = []
        rows: list[tuple] = []
        try:
            conn = psycopg2.connect(D.DSN)
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    if cur.description:
                        cols = [d.name for d in cur.description]
                    rows = cur.fetchall() if cur.description else []
            finally:
                conn.close()
        except Exception as exc:
            messagebox.showerror("查询失败", str(exc))
            return
        self._sql_rows = rows
        self._render_tree(self.tree_sql, cols, rows)
        self._status_set(f"查询返回 {len(rows)} 行")

    def _render_tree(self, tree: ttk.Treeview, cols: list[str], rows: list[tuple]) -> None:
        tree.delete(*tree.get_children())
        tree["columns"] = cols
        for c in cols:
            tree.heading(c, text=c)
            width = max(80, min(320, len(str(c)) * 14))
            tree.column(c, width=width, anchor="w", stretch=True)
        for i, row in enumerate(rows[:5000]):
            vals = [("" if v is None else str(v)) for v in row]
            tree.insert("", "end", iid=str(i), values=vals)

    def _export_csv(self) -> None:
        if not self._sql_rows:
            messagebox.showinfo("无数据", "请先执行查询。")
            return
        import csv
        from tkinter import filedialog
        fp = filedialog.asksaveasfilename(defaultextension=".csv",
                                         filetypes=[("CSV", "*.csv")],
                                         initialdir=str(C.PROJECT_ROOT))
        if not fp:
            return
        with open(fp, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow([self.tree_sql.heading(c)["text"] for c in self.tree_sql["columns"]])
            w.writerows(self._sql_rows)
        self._status_set(f"已导出 {fp}")

    def _count_views(self) -> None:
        views = [
            ("v_verse_reward", "词句收益宽表"),
            ("v_reward_overview", "词人/名臣效果表"),
            ("v_singer_overview", "唱词人效果总览"),
            ("v_dongpo_food", "东坡食单"),
        ]
        lines = []
        for name, label in views:
            try:
                n = D.scalar(f"SELECT count(*) FROM songci.{name}")
            except Exception as exc:
                n = f"错误: {exc}"
            lines.append(f"{label:16s} {name:24s} {n} 行")
        messagebox.showinfo("各视图行数", "\n".join(lines))
        self._status_set("已完成视图统计")

    # ------------------------------------------------------------ ③ 模拟运行

    def _build_run_tab(self) -> None:
        root = self.tab_run
        info = ttk.LabelFrame(root, text="即将运行的配置")
        info.pack(fill="x", padx=6, pady=6)
        self.lbl_run_cfg = ttk.Label(info, text="（尚未读取）", justify="left", wraplength=1000)
        self.lbl_run_cfg.pack(anchor="w", padx=6, pady=6)

        btns = ttk.Frame(root)
        btns.pack(fill="x", padx=6)
        ttk.Button(btns, text="读取当前配置", command=self._preview_cfg).pack(side="left", padx=2)
        self.btn_start = ttk.Button(btns, text="开始模拟", command=self._start_run)
        self.btn_start.pack(side="left", padx=2)
        self.btn_cancel = ttk.Button(btns, text="取消", command=self._cancel_run, state="disabled")
        self.btn_cancel.pack(side="left", padx=2)

        self.pbar = ttk.Progressbar(root, mode="determinate", maximum=100)
        self.pbar.pack(fill="x", padx=6, pady=8)
        self.lbl_progress = ttk.Label(root, text="等待开始")
        self.lbl_progress.pack(anchor="w", padx=6)

        logf = ttk.LabelFrame(root, text="运行日志")
        logf.pack(fill="both", expand=True, padx=6, pady=6)
        self.txt_log = tk.Text(logf, height=18, wrap="word", state="disabled")
        sb = ttk.Scrollbar(logf, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=sb.set)
        self.txt_log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    def _preview_cfg(self) -> None:
        try:
            cfg = self._collect_config()
        except ValueError as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        sname = next((f"[{r[0]}] {r[1]}" for r in self._singers if r[0] == cfg.singer_id), "无")
        pnames = [f"[{r[0]}] {r[1]}" for r in self._poets if r[0] in cfg.poet_ids]
        mnames = [f"[{r[0]}] {r[1]}" for r in self._ministers if r[0] in cfg.minister_ids]
        deps = []
        if SIM is None:
            deps.append("src/simulator.py 未就绪（M3 进行中）")
        if STATS is None:
            deps.append("src/stats.py 未就绪（M4 进行中）")
        if REPORT is None:
            deps.append("src/report.py 未就绪（M4 进行中）")
        self.lbl_run_cfg.configure(text=(
            f"配置名：{cfg.name}\n"
            f"唱词人：{sname}\n"
            f"开局词人（{len(cfg.poet_ids)}）：{('、'.join(pnames)) or '无'}\n"
            f"开局名臣（{len(cfg.minister_ids)}）：{('、'.join(mnames)) or '无'}\n"
            f"初始歌板 {cfg.init_board:g} / 词元 {cfg.init_verse_point:g} / "
            f"豪放词情 {cfg.init_bold_sentiment} / 婉约词情 {cfg.init_graceful_sentiment}\n"
            f"结束阈值 {cfg.end_threshold:g} / 每次基础消耗 {cfg.draw_base_cost:g} / "
            f"词情上限 {cfg.sentiment_cap} / 局数 {cfg.runs} / 种子 {cfg.seed}\n"
            + (("⚠ 依赖缺失：" + "；".join(deps)) if deps else "✓ 依赖齐备")
        ))

    def _log(self, msg: str) -> None:
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", msg + "\n")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def _start_run(self) -> None:
        if self._worker and self._worker.is_alive():
            messagebox.showinfo("正在运行", "已有模拟在运行中。")
            return
        try:
            cfg = self._collect_config()
        except ValueError as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        if cfg.end_threshold <= 0:
            messagebox.showerror("配置错误", "结束阈值必须大于 0。")
            return
        # 注意: 初始歌板允许为 0 (默认值), 故不能用 `not cfg.init_board` 判断
        if cfg.init_board is None:
            messagebox.showerror("配置错误", "「初始歌板」不能为空，请填写数字（0 表示开局即无歌板）。")
            return
        # **允许为负** (用户 2026-10-04): 负初始歌板 + 名臣/词人的开局歌板加成 = 更灵活的开局条件。
        # 例: 初始 -30 且勾选「陆游(豪放)」歌板+30 → 净 0;  净额不足一次择律则 0 轮结束。
        self._cancel.clear()
        self.btn_start.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.pbar.configure(value=0)
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")
        self._log(f"[{datetime.now():%H:%M:%S}] 开始模拟：{cfg.name}，{cfg.runs} 局，种子 {cfg.seed}")

        self._worker = threading.Thread(target=self._run_worker, args=(cfg,), daemon=True)
        self._worker.start()
        self.after(120, self._drain_queue)

    def _cancel_run(self) -> None:
        self._cancel.set()
        self._log(f"[{datetime.now():%H:%M:%S}] 收到取消请求，将在当前批次后停止…")
        self.btn_cancel.configure(state="disabled")

    def _run_worker(self, cfg: M.SimulationConfig) -> None:
        q = self._queue
        try:
            def progress(done: int, total: int) -> None:
                q.put(("progress", (done, total)))

            summary = _sim_run_batch(cfg, progress_cb=progress,
                                     cancel_flag=self._cancel.is_set)
            if summary is None:
                q.put(("error", "内核返回 None"))
                return
            q.put(("log", "模拟完成，开始统计与出图…"))

            stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            out_dir = C.OUTPUT_DIR / f"{stamp}_{cfg.safe_name()}"
            (out_dir / "charts").mkdir(parents=True, exist_ok=True)
            (out_dir / "raw").mkdir(parents=True, exist_ok=True)

            produced = _write_outputs(summary, out_dir)
            (out_dir / "配置快照.json").write_text(
                json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            q.put(("done", (out_dir, produced)))
        except Exception:
            q.put(("error", traceback.format_exc()))

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "progress":
                    done, total = payload
                    pct = (done / total * 100) if total else 0
                    self.pbar.configure(value=pct)
                    self.lbl_progress.configure(text=f"已完成 {done}/{total} 局（{pct:.1f}%）")
                elif kind == "log":
                    self._log(f"[{datetime.now():%H:%M:%S}] {payload}")
                elif kind == "done":
                    out_dir, produced = payload
                    self._last_output_dir = out_dir
                    self._on_run_done(out_dir, produced)
                    return
                elif kind == "error":
                    self._log("发生错误：\n" + str(payload))
                    messagebox.showerror("模拟失败", str(payload)[-1500:])
                    self._reset_run_buttons()
                    return
        except queue.Empty:
            pass
        if self._worker and self._worker.is_alive():
            self.after(150, self._drain_queue)
        else:
            self._reset_run_buttons()

    def _on_run_done(self, out_dir: Path, produced: dict) -> None:
        self.pbar.configure(value=100)
        self.lbl_progress.configure(text="已完成")
        self._log(f"[{datetime.now():%H:%M:%S}] 输出目录：{out_dir}")
        for k, v in (produced or {}).items():
            self._log(f"  {k}: {v}")
        self._reset_run_buttons()
        self._refresh_result_tab(out_dir)
        self.nb.select(self.tab_result)
        messagebox.showinfo("模拟完成", f"结果已输出到：\n{out_dir}")

    def _reset_run_buttons(self) -> None:
        self.btn_start.configure(state="normal")
        self.btn_cancel.configure(state="disabled")

    # ------------------------------------------------------------ ④ 结果查看

    def _build_result_tab(self) -> None:
        root = self.tab_result
        top = ttk.Frame(root)
        top.pack(fill="x", padx=6, pady=6)
        ttk.Label(top, text="输出目录").pack(side="left")
        self.var_outdir = tk.StringVar(value=str(C.OUTPUT_DIR))
        ttk.Entry(top, textvariable=self.var_outdir, width=62).pack(side="left", padx=6)
        ttk.Button(top, text="刷新", command=self._refresh_result_tab).pack(side="left", padx=2)
        ttk.Button(top, text="打开文件夹",
                   command=lambda: open_folder(Path(self.var_outdir.get()))).pack(side="left", padx=2)
        ttk.Button(top, text="查看 summary.md",
                   command=self._open_summary).pack(side="left", padx=2)

        mid = ttk.Frame(root)
        mid.pack(fill="both", expand=True, padx=6)
        leftf = ttk.LabelFrame(mid, text="产出文件")
        leftf.pack(side="left", fill="both", expand=True)
        self.lst_files = tk.Listbox(leftf)
        self.lst_files.pack(fill="both", expand=True, padx=4, pady=4)
        self.lst_files.bind("<Double-Button-1>", lambda e: self._open_selected())

        rightf = ttk.LabelFrame(mid, text="预览（summary.md）")
        rightf.pack(side="right", fill="both", expand=True)
        self.txt_preview = tk.Text(rightf, wrap="word", width=60)
        self.txt_preview.pack(fill="both", expand=True, padx=4, pady=4)

        ttk.Button(root, text="在浏览器中打开全部图表缩略图",
                   command=self._open_charts).pack(anchor="w", padx=6, pady=6)

    def _refresh_result_tab(self, out_dir: Path | None = None) -> None:
        if out_dir is not None:
            self.var_outdir.set(str(out_dir))
        target = Path(self.var_outdir.get())
        self.lst_files.delete(0, "end")
        if not target.exists():
            return
        for p in sorted(target.rglob("*")):
            if p.is_file():
                self.lst_files.insert("end", str(p.relative_to(target)))
        self._load_summary_preview(target)

    def _load_summary_preview(self, target: Path) -> None:
        self.txt_preview.delete("1.0", "end")
        md = target / "summary.md"
        if md.exists():
            text = md.read_text(encoding="utf-8", errors="replace")
            self.txt_preview.insert("1.0", text[:20000])
        else:
            self.txt_preview.insert("1.0", "（尚未找到 summary.md）")

    def _open_summary(self) -> None:
        md = Path(self.var_outdir.get()) / "summary.md"
        if not md.exists():
            messagebox.showinfo("未找到", "输出目录中还没有 summary.md。")
            return
        os.startfile(str(md)) if sys.platform.startswith("win") else webbrowser.open(md.as_uri())

    def _open_selected(self) -> None:
        sel = self.lst_files.curselection()
        if not sel:
            return
        target = Path(self.var_outdir.get()) / self.lst_files.get(sel[0])
        if target.exists():
            if sys.platform.startswith("win"):
                os.startfile(str(target))  # type: ignore[attr-defined]
            else:
                webbrowser.open(target.as_uri())

    def _open_charts(self) -> None:
        d = Path(self.var_outdir.get()) / "charts"
        if not d.exists():
            messagebox.showinfo("未找到", "输出目录中还没有 charts 文件夹。")
            return
        open_folder(d)

    # ------------------------------------------------------------ 收尾

    def _on_close(self) -> None:
        if self._worker and self._worker.is_alive():
            if not messagebox.askyesno("正在运行", "模拟仍在运行，确定退出吗？"):
                return
            self._cancel.set()
        self.destroy()


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
