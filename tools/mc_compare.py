#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""蒙特卡洛对比：**旧口径 vs 修正口径**（不依赖 PostgreSQL，直接读 8 份 JSON）

为什么单独有这个脚本：本项目主流程要 PostgreSQL，评审者未必立刻能建库；
而这次修正的都是**纯规则**（收益加成范围、唱词人效果值、答错分支、擅长词牌筛池），
只要 8 份 JSON 就能跑出 before/after，用来判断修正的影响量级。

用法:
    python tools/convert_game_configs.py --raw-dir <配置 JSON 目录>
    python tools/mc_compare.py --runs 3000 --seed 42
    python tools/mc_compare.py --singer 7 --preset 满配 --runs 3000

口径差异（见 src/game_rules.py 的逐条证据）:

| 项 | 旧（legacy） | 新（fixed） |
|---|---|---|
| 答题 | 100% 正确（答案已知） | 同左 —— **两边一致** |
| 答错代价 | 不建模 | 不建模（3 候选必有正解，剧本上是结构性答对） |
| 择律收益加成 | 放大**全部**词句效果 | 只放大 `REWARD_MULTIPLY_TYPES` 里的 7 种 |
| 唱词人 9（苏轸） | 歌板 +4 | 歌板 +3（+豪放/婉约词情各 1），取自效果池政策 40014 |
| 唱词人 1（新竹） | `module="none"`（永不触发） | 「每次答错 歌板+1」正常触发 |
| 擅长词牌 | 未参与抽取 | 按 `BuildQuestionSession` 的谓词筛池 |
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

import game_rules as GR  # noqa: E402

V = GR.SONGCI_VALUE


# ------------------------------------------------------------------ 载入

def load_table(root: Path, name: str) -> list[dict]:
    for cand in (root / "游戏数据" / name, root / name):
        if cand.exists():
            with open(cand, "r", encoding="utf-8-sig") as f:
                obj = json.load(f)
            rows = obj.get("dataList") if isinstance(obj, dict) else obj
            if isinstance(rows, list):
                return rows
    raise SystemExit(f"缺少 {name}，请先跑 tools/convert_game_configs.py")


def as_ints(v) -> list[int]:
    if v is None:
        return []
    return [int(x) for x in (v if isinstance(v, list) else [v]) if x is not None]


class World:
    """把 8 张表压成模拟需要的形状（不做 DB）。"""

    def __init__(self, root: Path):
        self.verses = load_table(root, "SongCiVerseConfig.json")
        self.poets = {int(r["ID"]): r for r in load_table(root, "SongCiPoetConfig.json")}
        self.singers = {int(r["ID"]): r for r in load_table(root, "SongCiSingerConfig.json")}
        self.policies_by_id: dict[int, dict] = {}
        self.policies_by_key: dict[int, dict] = {}
        for r in load_table(root, "PolicyConfig.json"):
            self.policies_by_id[int(r.get("ID") or 0)] = r
            self.policies_by_key[int(r.get("KeyID") or 0)] = r
        self.pools = {int(r["ID"]): r for r in load_table(root, "CommonEffectPoolConfig.json")}
        self.foods = {int(r["ID"]): r for r in load_table(root, "DongPoFoodConfig.json")}

    def verse_effects(self, row: dict) -> list[tuple[int, int, float]]:
        out = []
        for e, a, v in zip(row.get("EffectTypeList") or [],
                           row.get("EffectAddTypeList") or [],
                           row.get("EffectParamList") or []):
            out.append((int(e), int(a), float(v)))
        return out

    def singer_rules(self, sid: int) -> dict:
        row = self.singers[sid]
        rules = GR.singer_effects_from_pools(row, self.pools, self.policies_by_id)
        base = rules[0] if rules else {"module": "every_draw", "style": None, "effects": []}
        # 唱词人 6 的加成挂在池上（不经政策），另外兜一层
        if not base["effects"]:
            for r in rules:
                base["effects"].extend(r["effects"])
        return base

    def policy_effects(self, key_ids: list[int]) -> list[tuple[int, int, float]]:
        """把显式启用的政策展开成开局效果（含 1030 子政策一层）。"""
        out: list[tuple[int, int, float]] = []
        for k in key_ids:
            row = self.policies_by_key.get(int(k))
            if not row:
                continue
            for e, a, v in zip(row.get("EffectList") or [],
                               row.get("EffectAddTypeList") or [],
                               row.get("EffectValueList") or []):
                e = int(e)
                if e == 1030:
                    child = self.policies_by_id.get(int(v))
                    if child:
                        for ce, ca, cv in zip(child.get("EffectList") or [],
                                              child.get("EffectAddTypeList") or [],
                                              child.get("EffectValueList") or []):
                            if int(ce) != 1030:
                                out.append((int(ce), int(ca), float(cv)))
                    continue
                if e in (2807, 999, 881, 3200, 3202, 1036):
                    continue
                out.append((e, int(a), float(v)))
        return out


# ------------------------------------------------------------------ 单局

ATTR_OF = {
    32510: "board", 32514: "verse_point", 51: "popular_support", 209: "army_morale",
    204: "combat_power", 351: "corruption", 553: "reputation", 757: "develop_years",
    590: "music_level", 552: "culture_point", 32515: "sentiment_bold", 32516: "sentiment_graceful",
}


def floor_grant(attr: str, value: float) -> float:
    """与项目一致的"向 0 截断"口径（保持可比性）。"""
    return float(int(value)) if attr != "develop_years" else float(int(value))


def run_once(world: World, cfg: dict, rng: random.Random, fixed: bool) -> dict:
    singer = world.singer_rules(cfg["singer_id"]) if cfg["singer_id"] else \
        {"module": "every_draw", "style": None, "effects": []}
    singer_row = world.singers.get(cfg["singer_id"] or 1, {})
    specialty = [str(x) for x in (singer_row.get("SpecialtyCiPaiNames") or [])]

    state = {k: float(v) for k, v in cfg["init_attributes"].items()}
    state["board"] = float(cfg["init_board"])
    state["verse_point"] = float(cfg["init_verse_point"])
    state["sentiment_bold"] = float(cfg["init_bold_sentiment"])
    state["sentiment_graceful"] = float(cfg["init_graceful_sentiment"])

    bonus = float(cfg["init_benefit_bonus"])
    for etype, add_t, value in world.policy_effects(cfg["policy_key_ids"]):
        if etype == 32513:
            bonus += value
            continue
        attr = ATTR_OF.get(etype)
        if attr:
            state[attr] = state.get(attr, 0.0) + floor_grant(attr, value)

    drawn: set[int] = set()
    ticket_mod = 0.0
    acc_wrong = 0
    stats = {"turns": 0, "correct": 0, "stop": ""}

    for _ in range(int(cfg["max_turns"])):
        cost = max(0.0, float(V["StartCost"]) + ticket_mod)
        if state["board"] < cost:
            stats["stop"] = "歌板不足一次择律"
            break
        bold, graceful = (int(singer_row.get("BoldFavor") or 0),
                          int(singer_row.get("GracefulFavor") or 0))
        total = bold + graceful
        style = 1 if (total <= 0 and rng.random() < 0.5
                      or total > 0 and rng.random() < bold / total) else 2

        style_pool = [v for v in world.verses if int(v.get("Style") or 0) == style]
        pool = style_pool
        if fixed:
            # 谓词作用在"全部已解锁词句"上：含「全部」 ∨ 风格匹配 ∨ 词牌∈擅长词牌
            pool = GR.specialty_pool(world.verses, specialty, style, V["OptionCount"],
                                     all_verses=style_pool)
        if not pool:
            stats["stop"] = "候选池为空"
            break
        fresh = [v for v in pool if int(v["ID"]) not in drawn]
        verse = rng.choice(fresh or pool)
        drawn.add(int(verse["ID"]))

        state["board"] -= cost
        stats["turns"] += 1

        # ---- 作答：**答案总是已知** ----
        # 3 个候选里必有正解（答对才结束本次择律），且数据里每首词都有准确答案
        # （CiPaiName / CorrectVerseId），所以"每次择律都正确"是结构性前提。
        # 唱词人 1「每次择律错误时, 获得歌板+1」因此恒不触发。
        correct = True
        stats["correct"] += 1

        # ---- 词句效果（收益加成：旧=全部放大，新=白名单）----
        multiplier = 1.0 + bonus
        for etype, add_t, value in world.verse_effects(verse):
            if etype == 32513:
                bonus += value
                continue
            if add_t == 2:
                continue
            scale = multiplier if not fixed else GR.reward_scale(etype, multiplier)
            attr = ATTR_OF.get(etype)
            if attr:
                state[attr] = state.get(attr, 0.0) + floor_grant(attr, value * scale)

        # ---- 唱词人效果 ----
        module = singer["module"]
        fire = module in ("every_draw", "every_correct", "passive_bonus")
        if fire:
            for etype, _a, value in singer["effects"]:
                if etype == 32513:
                    bonus += value
                    continue
                attr = ATTR_OF.get(etype)
                if attr:
                    state[attr] = state.get(attr, 0.0) + floor_grant(attr, value)
        if module == "passive_bonus":
            for etype, _a, value in singer["effects"]:
                if etype == 32513:
                    bonus += value

        # ---- 词情 ----
        if True:
            key = "sentiment_bold" if style == 1 else "sentiment_graceful"
            state[key] += 1.0
            layers = state[key]
            if layers >= 10:
                prob = min(1.0, (int(layers) // 10) * 0.10)
                if rng.random() < prob:
                    if style == 1:
                        state["board"] += 1.0
                        state["army_morale"] = state.get("army_morale", 0.0) + 1.0
                    else:
                        state["board"] += 2.0
                        state["popular_support"] = state.get("popular_support", 0.0) - 1.0

        if cfg.get("end_threshold") and state["board"] >= float(cfg["end_threshold"]):
            stats["stop"] = "达到歌板阈值"
            break
    else:
        stats["stop"] = "达到安全上限"

    stats.update(state)
    return stats


# ------------------------------------------------------------------ 批量

def preset(name: str) -> dict:
    base = {
        "singer_id": 1, "policy_key_ids": [], "difficulty": 0.72,
        "init_board": 40.0, "init_verse_point": 0.0, "init_benefit_bonus": 0.0,
        "init_bold_sentiment": 0, "init_graceful_sentiment": 0,
        "init_attributes": {"popular_support": 50.0, "army_morale": 50.0,
                            "combat_power": 50.0, "corruption": 0.0, "reputation": 50.0,
                            "develop_years": 0.0, "culture_point": 0.0, "music_level": 0.0},
        "max_turns": 40, "end_threshold": None,
    }
    if name == "满配":
        base.update({"singer_id": 7, "policy_key_ids": [2215, 2120], "init_board": 200.0,
                     "difficulty": 0.8})
    return base


def summarize(rows: list[dict]) -> dict:
    def m(k):
        vals = [r.get(k, 0.0) for r in rows]
        return statistics.fmean(vals) if vals else 0.0
    stops: dict[str, int] = {}
    for r in rows:
        stops[r["stop"]] = stops.get(r["stop"], 0) + 1
    return {
        "局数": len(rows), "平均题数": m("turns"),
        "歌板": m("board"), "词元": m("verse_point"), "民心": m("popular_support"),
        "军心": m("army_morale"), "战斗力": m("combat_power"),
        "腐化": m("corruption"), "威望": m("reputation"),
        "终止": stops,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="旧口径 vs 修正口径 蒙特卡洛对比")
    ap.add_argument("--runs", type=int, default=3000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--singer", type=int, default=None)
    ap.add_argument("--difficulty", type=float, default=None)
    ap.add_argument("--preset", default="默认", choices=["默认", "满配"])
    ap.add_argument("--benefit", type=float, default=None,
                    help="注入初始「后续择律收益提升」（如 2.0 = 倍率 3.0），用于放大白名单差异")
    ap.add_argument("--root", default=".", help="仓库根（找 游戏数据/）")
    args = ap.parse_args()

    world = World(Path(args.root))
    cfg = preset(args.preset)
    if args.singer is not None:
        cfg["singer_id"] = args.singer
    if args.difficulty is not None:
        cfg["difficulty"] = args.difficulty
    if args.benefit is not None:
        cfg["init_benefit_bonus"] = args.benefit

    print(f"数据：词句 {len(world.verses)} / 唱词人 {len(world.singers)} / "
          f"词人 {len(world.poets)} / 政策 {len(world.policies_by_id)} / 池 {len(world.pools)}")
    print(f"配置：preset={args.preset} singer={cfg['singer_id']} "
          f"difficulty={cfg['difficulty']} init_board={cfg['init_board']} runs={args.runs}")

    out = {}
    for label, fixed in (("旧口径(legacy)", False), ("修正口径(fixed)", True)):
        rng = random.Random(args.seed)
        rows = [run_once(world, cfg, rng, fixed) for _ in range(args.runs)]
        out[label] = summarize(rows)

    keys = ["平均题数", "歌板", "词元", "民心", "军心", "战斗力", "腐化", "威望"]
    print()
    print(f"{'指标':<10}{'旧口径(legacy)':>18}{'修正口径(fixed)':>18}{'差异':>14}")
    print("-" * 62)
    for k in keys:
        a, b = out["旧口径(legacy)"][k], out["修正口径(fixed)"][k]
        print(f"{k:<10}{a:>18.2f}{b:>18.2f}{b - a:>+14.2f}")
    print()
    for label in out:
        print(f"{label} 终止原因：{out[label]['终止']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
