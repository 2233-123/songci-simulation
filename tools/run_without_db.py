#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**不装 PostgreSQL 也能跑上游的模拟内核**（给评审者用）

上游的完整流程要 PostgreSQL（`src/db.py` → psycopg2）。但模拟内核
`simulator.run_once(game, config, rng)` 只要求一个 `effects.GameData` 对象，
而它可以从 8 份 JSON 直接构造出来 —— 本脚本就做这件事。

用途：
  * 复核本次修正（收益加成白名单 / 唱词人 9 的数值 / 唱词人 1 的答错效果 / 答错分支）
    在**上游自己的代码**里的影响，不需要建库；
  * 快速跑单局或批量，不必等 ingest。

用法：
    python tools/convert_game_configs.py --raw-dir <配置 JSON 目录>
    python tools/run_without_db.py --runs 2000                      # 修正口径
    python tools/run_without_db.py --runs 2000 --legacy             # 旧口径（对比）
    python tools/run_without_db.py --singer 9 --accuracy 0.6

注意：本脚本只搭 `verses/singer_favors/poet_mods` 等**只读输入**；
`draw_rules` 使用仓库自带的静态表 `effects.build_draw_rules()`，不查库。
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

import effects as E          # noqa: E402
import model as M            # noqa: E402
import simulator as S        # noqa: E402


def load_table(name: str, root: Path) -> list[dict]:
    for cand in (root / "游戏数据" / name, root / name):
        if cand.exists():
            with open(cand, "r", encoding="utf-8-sig") as f:
                obj = json.load(f)
            rows = obj.get("dataList") if isinstance(obj, dict) else obj
            if isinstance(rows, list):
                return rows
    raise SystemExit(f"缺少 {name}；请先跑 tools/convert_game_configs.py")


def build_game(root: Path) -> E.GameData:
    verses_raw = load_table("SongCiVerseConfig.json", root)
    singers_raw = load_table("SongCiSingerConfig.json", root)
    poets_raw = load_table("SongCiPoetConfig.json", root)

    rows = []
    for v in verses_raw:
        effects = []
        for e, a, p in zip(v.get("EffectTypeList") or [],
                           v.get("EffectAddTypeList") or [],
                           v.get("EffectParamList") or []):
            # VerseRow.effects 的约定: (effect_type, value)，add_type 由 effects 模块内部处理
            effects.append((int(e), float(p)))
        unlock = tuple(int(x) for x in (v.get("UnlockNeedMinister") or []) if int(x) != 0)
        rows.append(E.VerseRow(
            id=int(v["ID"]),
            ci_name=str(v.get("CiName") or ""),
            ci_pai_name=str(v.get("CiPaiName") or ""),
            style=int(v.get("Style") or 0),
            poet_ids=tuple(int(x) for x in (v.get("BelongPoetIds") or [])),
            effects=tuple(effects),
            unlock_minister_ids=unlock,
        ))

    singer_favors = {int(s["ID"]): (int(s.get("BoldFavor") or 0),
                                    int(s.get("GracefulFavor") or 0)) for s in singers_raw}
    singer_names = {int(s["ID"]): str(s.get("Name") or "") for s in singers_raw}
    singer_specialty = {int(s["ID"]): tuple(str(x) for x in (s.get("SpecialtyCiPaiNames") or []))
                        for s in singers_raw}
    poet_names = {int(p["ID"]): str(p.get("Name") or "") for p in poets_raw}
    poem = {int(p["ID"]): int(p.get("MinisterId") or 0) for p in poets_raw}
    verse_poet_ids = {int(v["ID"]): tuple(int(x) for x in (v.get("BelongPoetIds") or []))
                      for v in verses_raw}

    # `build_draw_rules()` 会查库做「同名政策取最高星」的过滤；没有 psycopg2 时
    # 退回静态表 `_all_draw_rules()`（不做星级去重，对本对比无影响）。
    try:
        draw_rules = tuple(E.build_draw_rules())
    except Exception as exc:                                  # pragma: no cover
        print(f"[提示] 无数据库，draw_rules 退回静态表（{type(exc).__name__}）", file=sys.stderr)
        draw_rules = tuple(E._all_draw_rules())

    return E.GameData(
        verses=E.VersePool(rows),
        draw_rules=draw_rules,
        mods=E.ModifierSet(),
        singer_favors=singer_favors,
        singer_specialty=singer_specialty,
        singer_names=singer_names,
        poet_names=poet_names,
        poet_to_minister=poem,
        verse_poet_ids=verse_poet_ids,
        startup_policies=dict(E.STARTUP_POLICIES),
        dongpo_mods={},
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="无数据库跑上游模拟内核")
    ap.add_argument("--runs", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--singer", type=int, default=1)
    ap.add_argument("--board", type=float, default=40.0)
    ap.add_argument("--benefit", type=float, default=0.0)
    ap.add_argument("--max-turns", type=int, default=40)
    ap.add_argument("--legacy", action="store_true",
                    help="旧口径：收益加成放大全部效果 + 不按擅长词牌筛池")
    ap.add_argument("--root", default=str(ROOT))
    args = ap.parse_args()

    root = Path(args.root)
    game = build_game(root)

    cfg = M.SimulationConfig(
        name="无数据库对比",
        singer_id=args.singer,
        init_board=args.board,
        init_benefit_bonus=args.benefit,
        max_turns=args.max_turns,
        reward_multiply_whitelist=not args.legacy,
        specialty_pool_rule=not args.legacy,
    )

    rng = random.Random(args.seed)
    results = [S.run_once(game, cfg, rng) for _ in range(args.runs)]

    stops: dict[str, int] = {}
    for r in results:
        stops[r.stop_reason] = stops.get(r.stop_reason, 0) + 1

    def mean(attr: str) -> float:
        return statistics.fmean(r.attributes.get(attr, 0.0) for r in results)

    print(f"口径={'旧(legacy)' if args.legacy else '修正(fixed)'}  "
          f"singer={args.singer} 局数={args.runs} 开局歌板={args.board} "
          f"收益加成={args.benefit}")
    print(f"  平均题数      {statistics.fmean(r.turns for r in results):8.2f}")
    for attr in ("board", "verse_point", "popular_support", "army_morale",
                 "combat_power", "corruption", "reputation"):
        print(f"  {attr:<14}{mean(attr):8.2f}")
    print(f"  终止原因      {stops}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
