# -*- coding: utf-8 -*-
"""M3/M4 共享数据契约: 开局配置、单局结果、逐次事件。

M4(统计/图表/报告) 只依赖本模块, 不依赖 simulator 内部实现。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

import config as C

# 模拟中的属性键 (英文) -> 中文显示名
ATTRIBUTE_LABELS: dict[str, str] = {
    "board": "歌板",
    "verse_point": "词元",
    "popular_support": "民心",
    "army_morale": "军心",
    "combat_power": "战斗力",
    "corruption": "腐化",
    "culture_point": "文化点",
    "reputation": "威望",
    "develop_years": "发展年数",
    "music_level": "乐感等级",
    "military_rule": "军规",
    "prosperity": "繁荣",
    "reform": "变法",
}

# 效果 ID -> 模拟属性键
EFFECT_TO_ATTRIBUTE: dict[int, str] = {
    C.EFF_BOARD: "board",
    C.EFF_VERSE_POINT: "verse_point",
    C.EFF_POPULAR_SUPPORT: "popular_support",
    C.EFF_ARMY_MORALE: "army_morale",
    C.EFF_COMBAT_POWER: "combat_power",
    C.EFF_CORRUPTION: "corruption",
    C.EFF_CULTURE_POINT: "culture_point",
    C.EFF_REPUTATION: "reputation",
    C.EFF_DEVELOP_YEARS: "develop_years",
    C.EFF_MUSIC_LEVEL: "music_level",
}

# 可为负、且不允许低于 0 的属性。
# 注意: 只有这两个有下限; 民心/军心/腐化**不设上下限**（用户 2026-10-03 确认，
# 尽管 `EffectTypeConfig` 给它们标了 `[0,100]`），繁荣另设上限 50。
NON_NEGATIVE_ATTRIBUTES: frozenset[str] = frozenset({"board", "verse_point"})

#: **不再统计**的五个属性 (用户 2026-10-05): 文化点 / 乐感等级 / 军规 / 繁荣 / 变法。
#: 它们仍参与内部结算与政策条件判定, 但任何**输出**（事件流列 / 单局明细列 / summary /
#: 图表 / 统计 / 批量汇总 / 配置快照的初始属性）都必须彻底抹除,
#: 且**不得**在结果里解释"为什么不统计"。
HIDDEN_ATTRIBUTE_KEYS: frozenset[str] = frozenset({
    "culture_point", "music_level", "military_rule", "prosperity", "reform",
})


#: 初始值有值域约束的属性 (只约束**开局设定值**, 不约束结算过程)
INIT_ATTRIBUTE_BOUNDS: dict[str, tuple[float, float]] = {
    "popular_support": (0.0, 100.0),
    "army_morale": (0.0, 100.0),
    "corruption": (0.0, 100.0),
    "reputation": (0.0, 100.0),
}


def reported_attribute_keys() -> tuple[str, ...]:
    """结果输出里保留的属性键（按 ATTRIBUTE_LABELS 的原始顺序）。"""
    return tuple(k for k in ATTRIBUTE_LABELS if k not in HIDDEN_ATTRIBUTE_KEYS)


#: 结果报告口径（用户 2026-10-05）：这些属性报**波动**（终值 − 开局结算后）；
#: 其余（只有**歌板**）报**终值** —— 歌板是择律的资源与终止条件，看终值才有意义。
DELTA_ATTRIBUTE_KEYS: frozenset[str] = frozenset(
    k for k in reported_attribute_keys() if k != "board")


def uses_delta(key: str) -> bool:
    """该属性在结果报告里是否按「波动」口径输出。"""
    return key in DELTA_ATTRIBUTE_KEYS

# 终止原因
STOP_THRESHOLD = "达到歌板阈值"
STOP_INSUFFICIENT = "歌板不足一次择律"
STOP_MAX_TURNS = "达到安全上限"

# 词律
STYLE_BOLD = 1
STYLE_GRACEFUL = 2
STYLE_LABELS = {STYLE_BOLD: "豪放", STYLE_GRACEFUL: "婉约"}


@dataclass
class SimulationConfig:
    """一次模拟的完整开局配置。可序列化为 JSON 以保证可复现。"""

    # 名称 (用于输出文件夹命名)
    name: str = "默认配置"

    # 唱词人 ID (SongCiSingerConfig.ID); None = 不使用唱词人
    singer_id: int | None = 1

    # 开局词人 ID 列表 (SongCiPoetConfig.ID), 其效果政策立即生效
    poet_ids: list[int] = field(default_factory=list)

    # 开局额外启用的名臣 ID 列表 (MinisterBaseConfig.ID),
    # 其效果链触达宋词系统的政策并入被动修正
    minister_ids: list[int] = field(default_factory=list)

    # 东坡食单 ID 列表 (DongPoFoodConfig.ID, 1-9) —— 苏轼「老饕」政策开启,
    # 需同时勾选苏轼名臣 (328) 才生效; 每道菜的效果见 CommonEffectPoolConfig 652-660。
    dongpo_food_ids: list[int] = field(default_factory=list)

    # 额外启用的政策 KeyID 列表 (PolicyConfig.KeyID)
    policy_key_ids: list[int] = field(default_factory=list)

    # 初始资源
    # init_board: 开局歌板 —— 默认 0（由用户在 GUI / CLI 填写）。
    #   阈值 1000 是刻意设定的目标值；歌板只出不进，故绝大多数局会因
    #   「歌板不足一次择律」而截断，这是预期结果，不是配置错误。
    init_board: float | None = 0.0
    init_verse_point: float = 0.0       # 词元
    # 初始「后续择律收益加成」(32513), 例如 0.1 表示 +10%
    init_benefit_bonus: float = 0.0
    init_bold_sentiment: int = 0        # 豪放词情层数
    init_graceful_sentiment: int = 0    # 婉约词情层数
    # 初始属性。
    # 民心/威望/军心/腐化: 默认 **50**, 设定值域 **0~100** (用户 2026-10-05)。
    #   注意: 值域只约束**开局设定值**; 词句对这四项的增减益**不受值域约束**,
    #   结算过程中可以突破 0~100 (用户 2026-10-03 已确认不设上下限)。
    init_attributes: dict[str, float] = field(default_factory=lambda: {
        "popular_support": 50.0,
        "army_morale": 50.0,
        "corruption": 50.0,
        "reputation": 50.0,
        "combat_power": 0.0,
        "develop_years": 0.0,
        "culture_point": 0.0,
        "music_level": 0.0,
        "military_rule": 0.0,
        "prosperity": 0.0,
        "reform": 0.0,
    })

    # 规则参数
    draw_base_cost: float = C.DEFAULT_DRAW_BASE_COST   # 每次择律基础歌板消耗
    end_threshold: float = C.DEFAULT_END_THRESHOLD     # 歌板达到该值结束
    sentiment_cap: int = C.DEFAULT_SENTIMENT_CAP       # 词情层数上限
    max_turns: int = C.DEFAULT_MAX_TURNS               # 安全上限
    bias_bonus_to_positive_only: bool = True           # 收益加成是否只放大正向
    unlock_all_verses: bool = False                    # 解锁所有词句库 (忽略名臣解锁门控)
    record_events: bool = True                         # 是否记录逐次事件流

    # 模拟控制
    runs: int = C.DEFAULT_RUNS
    seed: int = C.DEFAULT_SEED

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SimulationConfig":
        known = {f for f in cls.__dataclass_fields__}          # type: ignore[attr-defined]
        data = {k: v for k, v in d.items() if k in known}
        # 旧键名兼容: `allow_special_unlock_verses` (只解锁 999 特殊词句)
        # 已被 `unlock_all_verses` (解锁所有词句库) 取代
        if "allow_special_unlock_verses" in d and "unlock_all_verses" not in data:
            data["unlock_all_verses"] = bool(d["allow_special_unlock_verses"])
        return cls(**data)

    def safe_name(self) -> str:
        bad = '<>:"/\\|?*'
        s = "".join("_" if ch in bad else ch for ch in self.name).strip()
        return s or "未命名配置"


@dataclass
class TurnEvent:
    """单次择律事件。"""

    turn: int                 # 第几次择律 (从 1 开始)
    drawn_style: int          # 本次抽到的词句风格 (1 豪放 / 2 婉约)
    verse_id: int             # 抽中词句 ID
    verse_name: str
    is_new_verse: bool = True  # 本局首次抽到该条目 (False = 已抽过, 池子已抽干转放回)
    in_replacement: bool = False  # 本次抽取时该风格池的未抽条目已耗尽
    poet_unlocked: int | None = None   # 本次新解锁的词人 ID
    poet_name: str = ""                # 本次新解锁的词人名 (由 simulator 填充, 可为空)
    cost: float = 0.0                  # 本次消耗歌板
    board_after: float = 0.0
    verse_point_after: float = 0.0
    bold_sentiment: int = 0
    graceful_sentiment: int = 0
    bold_triggered: bool = False       # 豪放词情是否触发
    graceful_triggered: bool = False   # 婉约词情是否触发
    treat_as_bold: bool = False        # 本次是否「同时视为抽到豪放词」(李纲「病牛」)
    granted: dict[str, float] = field(default_factory=dict)   # 本次各属性净变化
    # **歌板**本次变动的逐笔来源 (标签 -> 增量), 供审计日志使用。
    # 标签形如: 词句67(关山魂梦长) / 唱词人暮烟 / 名臣柳永·恋情词3 / 婉约词情触发 / 解锁词人4·李清照
    # 恒满足: 歌板余额 = 上一次余额 − 本次消耗 + Σ(本明细)
    board_trace: tuple[tuple[str, float], ...] = ()
    # 择律收益(32513)加成: 本轮**实际生效**的加成 / 本轮**结束后**的累计加成
    benefit_applied: float = 0.0
    benefit_bonus_after: float = 0.0


@dataclass
class SimulationResult:
    """单局结果。"""

    turns: int = 0                     # 择律次数
    stop_reason: str = ""
    attributes: dict[str, float] = field(default_factory=dict)
    # 开局一次性政策结算后、**第一次择律之前**的属性值。
    # 结果报告里的「波动」= 终值 − 该值, 亦即**纯择律带来的变化** (用户 2026-10-05)。
    initial_attributes: dict[str, float] = field(default_factory=dict)
    bold_sentiment: int = 0
    graceful_sentiment: int = 0
    poets_unlocked: int = 0
    singer_triggered: int = 0          # 唱词人效果触发次数
    sentiment_trigger_count: int = 0   # 词情触发总次数
    bold_triggers: int = 0             # 豪放词情触发次数 (逐局汇总, 不依赖事件流)
    graceful_triggers: int = 0         # 婉约词情触发次数 (逐局汇总, 不依赖事件流)
    style_counts: dict[int, int] = field(default_factory=dict)   # 抽到各风格的次数
    verse_counts: dict[int, int] = field(default_factory=dict)   # 各词句抽取次数
    new_verse_draws: int = 0           # 本局"首次抽到新条目"的次数 (= 词情叠加次数)
    repeat_draws: int = 0              # 本局在"已抽干转放回"阶段的抽取次数
    benefit_bonus: float = 0.0         # 本局结束时的「后续择律收益」累计加成 (0.5 = +50%)
    # 本局局中解锁的词人 ((词人ID, 解锁轮次), ...) —— 结果查看不依赖事件流
    poet_unlock_turns: tuple[tuple[int, int], ...] = ()
    card_grants: int = 0               # 效果 881 触发次数 (仅计数)
    unknown_effect_grants: float = 0.0
    events: list[TurnEvent] = field(default_factory=list)

    #: 批次中被完整记录事件流的局号 (0-based); 由 run_batch 填充
    event_run_indices: tuple[int, ...] = ()

    def to_dict(self, with_events: bool = False) -> dict[str, Any]:
        d = asdict(self)
        if not with_events:
            d.pop("events", None)
        return d


@dataclass
class BatchSummary:
    """多局聚合。由 M4 消费。"""

    config: SimulationConfig = field(default_factory=SimulationConfig)
    runs: int = 0
    results: list[SimulationResult] = field(default_factory=list)

    def turns_list(self) -> list[int]:
        return [r.turns for r in self.results]

    def attribute_series(self, key: str) -> list[float]:
        return [r.attributes.get(key, 0.0) for r in self.results]
