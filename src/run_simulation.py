# -*- coding: utf-8 -*-
"""M6 · 无头 CLI 运行入口 (供验收与批处理使用)

用法示例:
    python src/run_simulation.py --runs 10000 --seed 42 --out 输出结果
    python src/run_simulation.py --runs 2000 --singer 9 --poets 23 --name 苏轸_苏轼
    python src/run_simulation.py --runs 500 --no-events --singer 7 --name 破阵_豪放
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as C
import effects as E
import model as M
import report as R
import simulator as SIM


def build_config(args: argparse.Namespace) -> M.SimulationConfig:
    if getattr(args, "config", None):
        # 导入配置快照 (GUI「导出配置」/ 根目录 `配置_*.json` 同格式)
        raw = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
        cfg = M.SimulationConfig.from_dict(raw)
        if args.name:
            cfg.name = args.name
        if args.runs is not None:          # 只在显式传入时覆盖
            cfg.runs = args.runs
        if args.seed is not None:
            cfg.seed = args.seed
        return cfg
    cfg = M.SimulationConfig(
        name=args.name,
        singer_id=args.singer,
        poet_ids=[int(x) for x in (args.poets or [])],
        minister_ids=[int(x) for x in (args.ministers or [])],
        dongpo_food_ids=[int(x) for x in (args.dongpo or [])],
        policy_key_ids=[int(x) for x in (args.policies or [])],
        init_board=args.board,
        init_verse_point=args.verse_point,
        init_bold_sentiment=args.bold_sentiment,
        init_graceful_sentiment=args.graceful_sentiment,
        init_benefit_bonus=args.benefit_bonus / 100.0,   # CLI 以百分比为单位
        draw_base_cost=args.cost,
        end_threshold=args.threshold,
        sentiment_cap=args.sentiment_cap,
        runs=args.runs if args.runs is not None else 1000,
        seed=args.seed if args.seed is not None else C.DEFAULT_SEED,
        bias_bonus_to_positive_only=not args.amplify_negative,
        unlock_all_verses=args.unlock_all,
        record_events=not args.no_events,
    )
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser(description="宋词择律蒙特卡洛模拟 (无头)")
    ap.add_argument("--name", default="")
    ap.add_argument("--config", default="",
                    help="导入配置 JSON (如 配置_陆游李纲柳永.json); "
                         "其余 --poets/--ministers 等参数被忽略, 仅 --runs/--seed/--out 可覆盖")
    ap.add_argument("--singer", type=int, default=1, help="唱词人 ID, 0=不使用")
    ap.add_argument("--poets", nargs="*", type=int, default=[])
    ap.add_argument("--ministers", nargs="*", type=int, default=[])
    ap.add_argument("--policies", nargs="*", type=int, default=[])
    ap.add_argument("--board", type=float, default=0.0,
                    help="初始歌板（默认 0；**允许为负**，用于配合名臣/词人的开局歌板加成）")
    ap.add_argument("--verse-point", type=float, default=0.0)
    ap.add_argument("--benefit-bonus", type=float, default=0.0,
                    help="初始「后续择律收益」加成, **以百分比为单位**, 如 10 表示 +10%%")
    ap.add_argument("--dongpo", nargs="*", type=int, default=[],
                    help="东坡食单 ID (1-9), 需同时 --ministers 328 (苏轼) 才生效")
    ap.add_argument("--bold-sentiment", type=int, default=0)
    ap.add_argument("--graceful-sentiment", type=int, default=0)
    ap.add_argument("--cost", type=float, default=float(C.DEFAULT_DRAW_BASE_COST))
    ap.add_argument("--threshold", type=float, default=float(C.DEFAULT_END_THRESHOLD))
    ap.add_argument("--sentiment-cap", type=int, default=C.DEFAULT_SENTIMENT_CAP)
    ap.add_argument("--runs", type=int, default=None, help="默认 1000 (使用 --config 时以配置为准)")
    ap.add_argument("--seed", type=int, default=None, help=f"默认 {C.DEFAULT_SEED}")
    ap.add_argument("--amplify-negative", action="store_true",
                    help="收益加成同时放大负向惩罚 (默认只放大正向)")
    ap.add_argument("--unlock-all", action="store_true",
                    help="解锁所有词句库 (忽略名臣解锁门控)")
    ap.add_argument("--no-events", action="store_true", help="不记录逐次事件流 (更快)")
    ap.add_argument("--out", default=str(C.OUTPUT_DIR))
    args = ap.parse_args()

    if args.singer == 0:
        args.singer = None

    cfg = build_config(args)
    print(f"[1/4] 装配只读数据 (词人 {len(cfg.poet_ids)} / 名臣 {len(cfg.minister_ids)}) …")
    t0 = time.perf_counter()
    game = E.load_game_data(poet_ids=cfg.poet_ids, minister_ids=cfg.minister_ids,
                            policy_ids=cfg.policy_key_ids)
    t_load = time.perf_counter() - t0

    print(f"[2/4] 运行 {cfg.runs} 局 …")
    t0 = time.perf_counter()
    summary = SIM.run_batch(game, cfg, progress=None, cancel=None)
    t_run = time.perf_counter() - t0
    per_run_ms = (t_run / max(1, summary.runs)) * 1000

    print(f"[3/4] 写报告与图表 …")
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out_dir = Path(args.out) / f"{stamp}_{cfg.safe_name()}"
    out_dir.mkdir(parents=True, exist_ok=True)
    produced = R.write_outputs(summary, out_dir)

    print(f"[4/4] 完成")
    print(f"  装配耗时      : {t_load:.2f} s")
    print(f"  模拟耗时      : {t_run:.2f} s  ({per_run_ms:.3f} ms/局)")
    print(f"  输出目录      : {out_dir}")
    for k, v in sorted(produced.items()):
        print(f"    {k:12s} -> {v}")

    # 事实校验 (不做推断, 直接读数据)
    turns = summary.turns_list()
    reasons: dict[str, int] = {}
    for r in summary.results:
        reasons[r.stop_reason] = reasons.get(r.stop_reason, 0) + 1
    turns_sorted = sorted(turns)
    n = len(turns_sorted)
    if n:
        med = turns_sorted[n // 2] if n % 2 else (turns_sorted[n // 2 - 1] + turns_sorted[n // 2]) / 2
        print(f"  择律次数      : 最小 {turns_sorted[0]}  中位 {med}  "
              f"均值 {sum(turns_sorted)/n:.2f}  最大 {turns_sorted[-1]}")
    print(f"  终止原因      : {reasons}")

    # 歌板收支闭合校验 (取第一局逐笔核对)
    # 两个必须考虑的因素:
    #   1. 启动期结算: 开局词人/名臣的"一次性"政策在第 1 轮之前生效, 不体现在 events 中
    #   2. 歌板被钳制在 >= 0 (用户确认「歌板不能为负数」)
    first = summary.results[0]
    if first.events:
        ev0 = first.events[0]
        startup_board = ev0.board_after - ev0.granted.get("board", 0.0) + ev0.cost   # 由第 1 轮反推
        board = startup_board
        for ev in first.events:
            board = max(0.0, board + ev.granted.get("board", 0.0) - ev.cost)
        final_board = first.attributes.get("board", 0.0)
        total_gain = sum(ev.granted.get("board", 0.0) for ev in first.events)
        total_cost = sum(ev.cost for ev in first.events)
        print(f"  启动期歌板: 初始 {cfg.init_board:g} → 结算后 {startup_board:g} "
              f"(增量 {startup_board - cfg.init_board:+g}, 来自开局一次性政策)")
        print(f"  歌板闭合(第1局): 逐轮钳制递推 = {board:.6f}  vs 实际 {final_board:.6f}  "
              f"差 {abs(board - final_board):.3e}")
        print(f"                  本局累计 收益 {total_gain:g} / 消耗 {total_cost:g} / 轮次 {first.turns}")

    (out_dir / "_运行参数.json").write_text(
        json.dumps({"args": vars(args), "t_load_s": t_load, "t_run_s": t_run,
                    "per_run_ms": per_run_ms, "produced": {k: str(v) for k, v in produced.items()}},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
