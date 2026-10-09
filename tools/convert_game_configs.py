#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把游戏导出的配置 JSON 转成本项目需要的 8 份文件（解决"仓库跑不起来"）。

本项目 `README` 的「数据准备」要求把导出的 JSON 放进 `游戏数据/`，但仓库里没有。
本脚本按本项目的契约把它们落盘并校验：

    python tools/convert_game_configs.py --raw-dir <配置 JSON 目录> [--out 游戏数据]

`<配置 JSON 目录>` 里应有下列文件（原始数组或已含 `dataList` 键的对象都可）：

    SongCiVerseConfig.json  SongCiPoetConfig.json  SongCiSingerConfig.json
    PolicyConfig.json       EffectTypeConfig.json  MinisterBaseConfig.json
    DongPoFoodConfig.json   CommonEffectPoolConfig.json

产出格式：`{"dataList": [...]}`（本项目的 `config.DATA_LIST_KEY`）。
脚本只做搬运与校验，**不改数值**，并会打印每张表的行数供核对。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

#: 文件名 -> (说明, 期望行数区间)
TABLES = {
    "SongCiVerseConfig.json": ("词句", (50, 300)),
    "SongCiPoetConfig.json": ("词人", (10, 60)),
    "SongCiSingerConfig.json": ("唱词人", (5, 20)),
    "PolicyConfig.json": ("政策", (1000, 20000)),
    "EffectTypeConfig.json": ("效果类型字典", (200, 2000)),
    "MinisterBaseConfig.json": ("名臣", (100, 2000)),
    "DongPoFoodConfig.json": ("东坡食单", (5, 20)),
    "CommonEffectPoolConfig.json": ("效果池", (100, 3000)),
}


def load_rows(path: Path) -> list:
    with open(path, "r", encoding="utf-8-sig") as f:
        obj = json.load(f)
    if isinstance(obj, list):
        return obj
    for key in ("dataList", "records", "data"):
        if isinstance(obj, dict) and key in obj and isinstance(obj[key], list):
            return obj[key]
    raise ValueError(f"{path.name}: 找不到记录数组（顶层既不是 list 也没有 dataList）")


def main() -> int:
    ap = argparse.ArgumentParser(description="把配置 JSON 转成本项目需要的 8 份文件")
    ap.add_argument("--raw-dir", required=True,
                    help="配置 JSON 所在目录（含上面 8 个 .json）")
    ap.add_argument("--out", default="游戏数据", help="输出目录（默认 游戏数据/）")
    args = ap.parse_args()

    raw = Path(args.raw_dir)
    if not raw.is_dir():
        print(f"[错误] 目录不存在: {raw}", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    missing, bad = [], []
    for name, (label, (lo, hi)) in TABLES.items():
        src = raw / name
        if not src.exists():
            # 兼容小写/带前缀的命名
            cands = [p for p in raw.glob("*.json") if p.stem.lower() == Path(name).stem.lower()]
            if not cands:
                missing.append(f"{name}（{label}）")
                continue
            src = cands[0]
        rows = load_rows(src)
        if not (lo <= len(rows) <= hi):
            bad.append(f"{name}: {len(rows)} 行（期望 {lo}~{hi}）")
        dst = out / name
        with open(dst, "w", encoding="utf-8") as f:
            json.dump({"dataList": rows}, f, ensure_ascii=False)
        print(f"[ok] {label:12s} {len(rows):>6d} 行  -> {dst}")

    if missing:
        print("\n[错误] 缺少以下文件：", file=sys.stderr)
        for m in missing:
            print(f"  - {m}", file=sys.stderr)
        print("\n提示：请把这 8 张表导出为 JSON（顶层为数组，或含 `dataList` 键）。",
              file=sys.stderr)
        return 1
    if bad:
        print("\n[警告] 行数不在预期区间（可能版本不同，请确认）：", file=sys.stderr)
        for b in bad:
            print(f"  - {b}", file=sys.stderr)

    print(f"\n[完成] 8 份数据已写入 {out}/；现在可以：")
    print("    python src/ingest.py           # 建库")
    print("    python src/run_simulation.py   # 跑一局")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
