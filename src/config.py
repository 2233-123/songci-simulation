# -*- coding: utf-8 -*-
"""全局配置: 路径、数据库连接、效果 ID 语义表。"""
from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------- 路径
#
# 所有路径都可以用环境变量覆盖, 缺省值一律**相对本文件**推导 ——
# clone / 换机器 / 改目录名都不需要改代码:
#   SONGCI_PROJECT_ROOT    项目根目录      (缺省: 本文件所在目录的上一级)
#   SONGCI_GAME_DATA_DIR   游戏数据目录    (缺省: <项目根>/游戏数据)
PROJECT_ROOT = Path(
    os.environ.get("SONGCI_PROJECT_ROOT") or Path(__file__).resolve().parents[1]
).resolve()
GAME_DATA_DIR = Path(
    os.environ.get("SONGCI_GAME_DATA_DIR") or (PROJECT_ROOT / "游戏数据")
).resolve()

SRC_DIR = PROJECT_ROOT / "src"
DOCS_DIR = PROJECT_ROOT / "docs"
TESTS_DIR = PROJECT_ROOT / "tests"
OUTPUT_DIR = PROJECT_ROOT / "输出结果"
TOOLS_DIR = PROJECT_ROOT / "tools"


def _data_file(name: str) -> Path:
    """定位一个原始 JSON: 依次在 GAME_DATA_DIR、PROJECT_ROOT 下查找。

    两者都没有时返回 ``GAME_DATA_DIR/name`` (由调用方给出可读报错)。
    这样既支持"解包数据放一个目录", 也支持"JSON 直接摊在项目根目录"。
    """
    for d in (GAME_DATA_DIR, PROJECT_ROOT):
        p = d / name
        if p.exists():
            return p
    return GAME_DATA_DIR / name


# 输入配置 (只读, 绝不写回)
VERSE_JSON = _data_file("SongCiVerseConfig.json")
POET_JSON = _data_file("SongCiPoetConfig.json")
SINGER_JSON = _data_file("SongCiSingerConfig.json")
POLICY_JSON = _data_file("PolicyConfig.json")
EFFECT_TYPE_JSON = _data_file("EffectTypeConfig.json")
MINISTER_JSON = _data_file("MinisterBaseConfig.json")
DONGPO_JSON = _data_file("DongPoFoodConfig.json")
COMMON_EFFECT_POOL_JSON = _data_file("CommonEffectPoolConfig.json")

SCHEMA_SQL = SRC_DIR / "schema.sql"
VIEWS_SQL = SRC_DIR / "views.sql"

# 解包 JSON 的顶层 records 容器键
DATA_LIST_KEY = "dataList"

# ---------------------------------------------------------------- 数据库
#
# 全部可用环境变量覆盖; **仓库里不含任何口令**。
# 本地开发可以把口令写进 <项目根>/.db_password (该文件已在 .gitignore 里)。

DB_HOST = os.environ.get("SONGCI_DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("SONGCI_DB_PORT", "5432"))
DB_NAME = os.environ.get("SONGCI_DB_NAME", "songci")
DB_USER = os.environ.get("SONGCI_DB_USER", "songci")
DB_PASSWORD = os.environ.get("SONGCI_DB_PASSWORD", "")
if not DB_PASSWORD:
    _pw_file = PROJECT_ROOT / ".db_password"
    if _pw_file.exists():
        DB_PASSWORD = _pw_file.read_text(encoding="utf-8").strip()
DB_SCHEMA = "songci"

DSN = (
    f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} "
    f"user={DB_USER} " + (f"password={DB_PASSWORD} " if DB_PASSWORD else "")
    + "client_encoding=UTF8"
)

# ---------------------------------------------------------------- 效果语义

# EffectAddTypeList 取值语义 (实测归纳):
#   0 = 绝对值 (点数/个)
#   2 = **百分比乘算**: 数值为小数, 0.08 表示 +8% (即 最终值 × (1+0.08))
#   4 = 加算 (整数增量)
# 模拟必须按此区分量纲, 否则会把「战斗力+8%」错算成「战斗力+0.08」。
ADD_TYPE_ABSOLUTE = 0
ADD_TYPE_MULTIPLIER = 2
ADD_TYPE_ADDITIVE = 4

# 词句效果全部为 add_type=4 (加算), 无需乘算处理;
# 乘算只出现在政策效果里 (如 岳飞背嵬军 战斗力+8%、陆游 钱粮消耗-5%)。

EFF_BOARD = 32510            # 歌板
EFF_VERSE_POINT = 32514      # 词元
EFF_BENEFIT_BONUS = 32513    # 后续择律收益提升 (乘算修正)
EFF_BOLD_SENTIMENT = 32515   # 豪放词情 (层)
EFF_GRACEFUL_SENTIMENT = 32516  # 婉约词情 (层)
EFF_DRAW_COST_MOD = 32517    # 择律开题消耗修正 (= 每次择律歌板消耗修正)
EFF_CONDITIONAL = 1030       # 条件效果: Condition_Values = [阈值, 子政策ID...]
EFF_GRANT_CARD = 881         # 授予卡牌/状态/事件, 不产生数值属性
EFF_SYSTEM_SWITCH = 2807     # 系统开关 (开启择律系统)
EFF_CULTURE_POINT = 552      # 文化点
EFF_REPUTATION = 553         # 威望
EFF_POPULAR_SUPPORT = 51     # 民心
EFF_ARMY_MORALE = 209        # 军心
EFF_COMBAT_POWER = 204       # 战斗力
EFF_CORRUPTION = 351         # 腐化
EFF_DEVELOP_YEARS = 757      # 发展年数
EFF_MUSIC_LEVEL = 590        # 乐感等级
EFF_VISIT_TIMES = 927        # 巡访次数
EFF_HANQING = 1093           # 汗青
EFF_DONGPODAN = 3200         # 东坡食单 (待标注 U3)
EFF_DONGPODAN2 = 3202        # 东坡食单 (待标注 U3)
EFF_PROSPERITY = 31480       # 繁荣 (李清照「如梦令」); 实测 137 在 effect_type 中不存在
# 繁荣上限: EffectTypeConfig 里 31480 的 `MaxValue = 50` (用户 2026-10-03 确认设上限)
# 注: 民心(51)/军心(209)/腐化(351) 在 EffectTypeConfig 里虽是 [0,100], 但用户明确
#     「不设上下限」, 故只有繁荣被钳制; 歌板/词元另有下限 0。
PROSPERITY_CAP = 50.0
# 注意: 「军规」不是 EffectType, 而是 881 的子 ID 28399
#       (见 policy id=31081「陆游-豪放」: EffectList=[881], EffectValueList=[28399.0])
GRANT_SUBID_MILITARY_RULE = 28399   # 军规 (881 子 ID)
GRANT_SUBID_REFORM = 30556          # 变法 (881 子 ID)
GRANT_SUBID_PROSPERITY = 31480      # 繁荣 (881 子 ID, 与 EffectType 同号)
# 李纲「病牛」★2/★3: 「正确择【婉约】律时，同时视为择【豪放】律」
GRANT_SUBID_GRACEFUL_AS_BOLD = 28319

# 「宋词择律系统」相关效果集合: 用于计算 include_scope = songci_core
SONGCI_EFFECT_IDS: frozenset[int] = frozenset({
    EFF_BOARD,
    EFF_VERSE_POINT,
    EFF_BENEFIT_BONUS,
    EFF_BOLD_SENTIMENT,
    EFF_GRACEFUL_SENTIMENT,
    EFF_DRAW_COST_MOD,
    EFF_SYSTEM_SWITCH,
    EFF_CONDITIONAL,
})

# 数值属性效果: 这些参与「各属性终值」统计; 其余忽略或仅计数
NUMERIC_ATTRIBUTE_EFFECTS: dict[int, str] = {
    EFF_BOARD: "歌板",
    EFF_VERSE_POINT: "词元",
    EFF_POPULAR_SUPPORT: "民心",
    EFF_ARMY_MORALE: "军心",
    EFF_COMBAT_POWER: "战斗力",
    EFF_CORRUPTION: "腐化",
    EFF_CULTURE_POINT: "文化点",
    EFF_REPUTATION: "威望",
    EFF_DEVELOP_YEARS: "发展年数",
    EFF_MUSIC_LEVEL: "乐感等级",
}

# 效果 ID -> 中文名 (缺失时回退查 effect_type 表)
EFFECT_NAME_FALLBACK: dict[int, str] = {
    EFF_BOARD: "歌板",
    EFF_VERSE_POINT: "词元",
    EFF_BENEFIT_BONUS: "后续择律收益提升",
    EFF_BOLD_SENTIMENT: "豪放词情",
    EFF_GRACEFUL_SENTIMENT: "婉约词情",
    EFF_DRAW_COST_MOD: "择律开题消耗",
    EFF_CONDITIONAL: "条件效果",
    EFF_GRANT_CARD: "授予卡牌/状态",
    EFF_SYSTEM_SWITCH: "系统开关",
    EFF_DONGPODAN: "东坡食单",
    EFF_DONGPODAN2: "东坡食单",
}

# ---------------------------------------------------------------- 模拟默认值

DEFAULT_DRAW_BASE_COST = 10          # 每次择律基础歌板消耗
DEFAULT_END_THRESHOLD = 1000         # 歌板达到该值即结束 (可配置)
DEFAULT_SENTIMENT_CAP = 100          # 词情层数上限
DEFAULT_SENTIMENT_STEP = 10          # 每 N 层 +10% 触发概率
SENTIMENT_TRIGGER_PER_STEP = 0.10    # 每步 10%
SENTIMENT_STEP_LAYERS = 10
DEFAULT_MAX_TURNS = 1_000_000        # 安全上限, 防死循环
EVENT_FULL_RUNS = 10            # 事件流只记录「随机 N 局」的全部流程 (不设条数上限)
DEFAULT_RUNS = 10_000
DEFAULT_SEED = 42

# 政策效果递归展开上限
MAX_EFFECT_DEPTH = 8

# 词句解锁特殊名臣 ID
SPECIAL_UNLOCK_MINISTER_IDS: frozenset[int] = frozenset({999})

# ---------------------------------------------------------------- 东坡食单
# 苏轼「老饕」(政策 3212★1 / 3213★2) 开启东坡食单; 菜品数据见 DongPoFoodConfig.json,
# 效果见 CommonEffectPoolConfig.json (ID 652-660)。
SU_SHI_POET_ID = 23
SU_SHI_MINISTER_ID = 328
# 「收集所有食单」奖励 (老饕 ★2): 豪放词情+20 层
DONGPO_COMPLETE_FOOD_IDS: frozenset[int] = frozenset(range(1, 10))
DONGPO_COMPLETE_BOLD_SENTIMENT = 20

# include_scope 取值
SCOPE_SONGCI_CORE = "songci_core"       # 效果链触达宋词效果系统
SCOPE_POET_POLICY = "poet_policy"       # 21 条词人政策
SCOPE_SINGER_POLICY = "singer_policy"   # 唱词人池政策
SCOPE_MINISTER_11X = "minister_11x"     # 名臣 TimeTypeList 含 11/1101/1102
SCOPE_TT15 = "tt15"                     # TimeTypeList 含 15
SCOPE_RAW_ONLY = "raw_only"             # 仅存档

# 名臣 TimeTypeList 关注值
MINISTER_TT_FOCUS: frozenset[int] = frozenset({11, 1101, 1102})
# 政策 TimeTypeList 关注值 (任务书第 1 条)
POLICY_TT_FOCUS = 15
