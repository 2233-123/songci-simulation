# -*- coding: utf-8 -*-
"""M1 · 规范化入库

读取 6 份原始配置 (只读), 展开二级政策引用, 写入 PostgreSQL。

用法:
    python src/ingest.py            # 建表 + 入库 + 校验
    python src/ingest.py --verify   # 只校验
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as C
import db as D


# ---------------------------------------------------------------- 工具

def load_records(path: Path, label: str) -> list[dict]:
    """读取解包 JSON 的 dataList。"""
    with open(path, "r", encoding="utf-8-sig") as f:
        obj = json.load(f)
    if C.DATA_LIST_KEY not in obj:
        raise ValueError(f"{label}: 缺少顶层键 {C.DATA_LIST_KEY!r}, 实际键={list(obj)[:10]}")
    recs = obj[C.DATA_LIST_KEY]
    D.log(f"[load] {label:14s} {len(recs):>5d} 行  <- {path.name}")
    return recs


def as_list(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def as_floats(v: Any) -> list[float]:
    return [float(x) for x in as_list(v) if x is not None]


def as_ints(v: Any) -> list[int]:
    return [int(x) for x in as_list(v) if x is not None]


def num(v: Any):
    return None if v is None else float(v)


def integer(v: Any):
    return None if v is None else int(v)


# ---------------------------------------------------------------- 政策效果递归展开

class PolicyEffectExpander:
    """把政策的 EffectList 展开为扁平效果列表, 并递归展开二级政策引用。

    判别规则 (实测得出):
      - 若某值存在于 effect_type 表 -> 视为效果
      - 否则若存在于 policy 表 -> 视为二级政策引用, 递归展开
      - 两者都不是 -> 记为未知引用 (effect_type = NULL, 保留现场)
    """

    def __init__(self, policies: list[dict], effect_ids: set[int]):
        self.by_id: dict[int, dict] = {}
        for p in policies:
            # ID 有重复, 首次出现优先
            self.by_id.setdefault(int(p["ID"]), p)
        self.effect_ids = effect_ids
        self.refs: list[tuple[int, int, int]] = []   # (root_key_id, child_policy_id, depth)
        self._unknown_warned: set[tuple[int, int]] = set()

    def expand(self, root: dict) -> list[tuple]:
        """返回 (policy_key_id, source_policy_id, effect_type, value, add_type, name, depth, ordinal)"""
        root_key = int(root["KeyID"])
        out: list[tuple] = []
        visited: set[int] = set()
        counter = {"n": 0}

        def walk(pol: dict, depth: int) -> None:
            pid = int(pol["ID"])
            if depth > C.MAX_EFFECT_DEPTH:
                D.log(f"[warn] 政策 {root_key} 效果深度超过 {C.MAX_EFFECT_DEPTH}, 停止展开 (policy {pid})")
                return
            types = as_list(pol.get("EffectList"))
            values = as_list(pol.get("EffectValueList"))
            adds = as_list(pol.get("EffectAddTypeList"))
            for i, t in enumerate(types):
                if t is None:
                    continue
                t = int(t)
                val = float(values[i]) if i < len(values) and values[i] is not None else None
                add = int(adds[i]) if i < len(adds) and adds[i] is not None else None
                if t in self.effect_ids:
                    counter["n"] += 1
                    name = C.EFFECT_NAME_FALLBACK.get(t)
                    out.append((root_key, pid, t, val, add, name, depth, counter["n"]))
                elif t in self.by_id:
                    if t in visited:
                        continue
                    visited.add(t)
                    self.refs.append((root_key, t, depth + 1))
                    walk(self.by_id[t], depth + 1)
                else:
                    counter["n"] += 1
                    out.append((root_key, pid, None, val, add, f"未知引用 {t}", depth, counter["n"]))
                    if (root_key, t) not in self._unknown_warned:
                        self._unknown_warned.add((root_key, t))
                        D.log(f"[info] 政策 {root_key} 的效果值 {t} 既非效果也非政策 (按未知引用保留)")

        walk(root, 0)
        return out


# ---------------------------------------------------------------- 分层计算

def compute_scopes(policies: list[dict], expander: PolicyEffectExpander) -> dict[int, set[str]]:
    """为每条政策计算 include_scope 集合。绝不据此删行。"""
    scopes: dict[int, set[str]] = {int(p["KeyID"]): set() for p in policies}
    by_id = expander.by_id

    for p in policies:
        key = int(p["KeyID"])
        effects = expander.expand(p)
        if any(e[2] in C.SONGCI_EFFECT_IDS for e in effects if e[2] is not None):
            scopes[key].add(C.SCOPE_SONGCI_CORE)

    return scopes


def add_seed_scopes(scopes: dict[int, set[str]], poet_policy_ids: set[int],
                    singer_policy_ids: set[int], policies: list[dict],
                    minister_focus_ids: set[int]) -> None:
    for p in policies:
        key = int(p["KeyID"])
        pid = int(p["ID"])
        s = scopes[key]
        if C.POLICY_TT_FOCUS in as_ints(p.get("TimeTypeList")):
            s.add(C.SCOPE_TT15)
        if pid in poet_policy_ids:
            s.add(C.SCOPE_POET_POLICY)
        if pid in singer_policy_ids:
            s.add(C.SCOPE_SINGER_POLICY)
        mid = p.get("MinisterId")
        if mid and int(mid) in minister_focus_ids:
            s.add(C.SCOPE_MINISTER_11X)
        if not s:
            s.add(C.SCOPE_RAW_ONLY)


# ---------------------------------------------------------------- 主流程

def ingest() -> None:
    D.log("=" * 70)
    D.log("M1 · 规范化入库")
    D.log("=" * 70)

    # ---- 读取原始配置
    verses = load_records(C.VERSE_JSON, "SongCiVerseConfig")
    # 剔除「需特殊名臣 999 解锁」的词句 (用户 2026-10-05 要求从库中删除)。
    # 实测只有 2 条: ID=9「水天空阔」(念奴娇) / ID=87「秋色正萧洒」(贺新郎),
    # 归属词人 ID=19 —— 该词人不在 SongCiPoetConfig 里 (据词句提示文案为「文天祥」),
    # 其政策 30026 也是空壳 (无效果行), 故这 2 条词句没有任何可实现的效果。
    # 这里统一过滤, 保证重新入库不会把它们带回来。
    _before = len(verses)
    verses = [v for v in verses
              if not (set(as_ints(v.get("UnlockNeedMinister"))) & C.SPECIAL_UNLOCK_MINISTER_IDS)]
    if len(verses) != _before:
        D.log(f"[info] 已剔除需特殊名臣 {sorted(C.SPECIAL_UNLOCK_MINISTER_IDS)} 解锁的词句 "
              f"{_before - len(verses)} 条")
    poets = load_records(C.POET_JSON, "SongCiPoetConfig")
    singers = load_records(C.SINGER_JSON, "SongCiSingerConfig")
    policies = load_records(C.POLICY_JSON, "PolicyConfig")
    effects = load_records(C.EFFECT_TYPE_JSON, "EffectTypeConfig")
    ministers = load_records(C.MINISTER_JSON, "MinisterBaseConfig")
    pools = load_records(C.COMMON_EFFECT_POOL_JSON, "CommonEffectPoolConfig")
    dongpo = load_records(C.DONGPO_JSON, "DongPoFoodConfig")

    effect_ids = {int(e["EffectType"]) for e in effects}
    effect_names = {int(e["EffectType"]): e.get("EffectName") for e in effects}
    by_key = {int(p["KeyID"]): p for p in policies}
    by_id: dict[int, dict] = {}
    for p in policies:
        by_id.setdefault(int(p["ID"]), p)
    pool_by_id = {int(p["ID"]): p for p in pools}

    # ---- 建表
    D.run_sql_file(C.SCHEMA_SQL)

    # ---- 通用效果池 / 东坡食单 (可独立重放, 见 tools/apply_new_tables.py)
    ingest_effect_pools()
    ingest_dongpo()

    # ---- 原始表
    D.bulk_insert("effect_type", [
        "effect_type", "effect_name", "add_desc", "reduce_desc", "add_desc_sp",
        "reduce_desc_sp", "effect_desc", "character_type", "is_show",
        "min_value", "max_value", "raw_json",
    ], [(
        int(e["EffectType"]), e.get("EffectName"), e.get("AddDesc"), e.get("ReduceDesc"),
        e.get("AddDesc_SP"), e.get("ReduceDesc_SP"), e.get("EffectDesc"),
        integer(e.get("CharacterType")), integer(e.get("IsShow")),
        num(e.get("MinValue")), num(e.get("MaxValue")), D.jsonb(e),
    ) for e in effects])

    D.bulk_insert("verse", [
        "id", "ci_name", "ci_pai_name", "style", "content", "descr",
        "unlock_reward_effect_pool_id", "raw_json",
    ], [(
        int(v["ID"]), v.get("CiName"), v.get("CiPaiName"), int(v["Style"]),
        v.get("Content"), v.get("Desc"),
        integer(v.get("UnlockRewardCommonEffectPoolId")), D.jsonb(v),
    ) for v in verses])

    D.bulk_insert("poet", [
        "id", "name", "reward_policy_id", "minister_id", "img_id", "anim_name", "raw_json",
    ], [(
        int(p["ID"]), p.get("Name"), integer(p.get("RewardPolicyId")),
        integer(p.get("MinisterId")), integer(p.get("ImgId")), p.get("AnimName"), D.jsonb(p),
    ) for p in poets])

    D.bulk_insert("singer", [
        "id", "name", "bold_favor", "graceful_favor", "feature_desc", "description",
        "unlock_condition", "trigger_pool_policy_id", "remove_pool_policy_id",
        "specialty_ci_pai_names", "raw_json",
    ], [(
        int(s["ID"]), s.get("Name"), int(s.get("BoldFavor") or 0), int(s.get("GracefulFavor") or 0),
        s.get("FeatureDesc"), s.get("Description"), s.get("UnlockCondition"),
        integer(s.get("TriggerCommonEffectPoolConfigId")),
        integer(s.get("RemoveCommonEffectPoolConfigId")),
        [str(x) for x in as_list(s.get("SpecialtyCiPaiNames"))], D.jsonb(s),
    ) for s in singers])

    D.bulk_insert("minister", [
        "id", "name", "minister_type", "time_type_list", "policy_list",
        "start_year_list", "end_year_list", "raw_json",
    ], [(
        int(m["ID"]), m.get("Name"), integer(m.get("MinisterType")),
        as_ints(m.get("TimeTypeList")), as_ints(m.get("PolicyList")),
        as_ints(m.get("StartYearList")), as_ints(m.get("EndYearList")), D.jsonb(m),
    ) for m in ministers])

    # ---- 分层计算 (先于 policy 插入, 因 include_scope 是列)
    expander = PolicyEffectExpander(policies, effect_ids)
    scopes = compute_scopes(policies, expander)

    poet_policy_ids = {int(p["RewardPolicyId"]) for p in poets if p.get("RewardPolicyId")}
    singer_policy_ids = set()
    for s in singers:
        for k in ("TriggerCommonEffectPoolConfigId", "RemoveCommonEffectPoolConfigId"):
            if s.get(k):
                singer_policy_ids.add(int(s[k]))
    minister_focus_ids = {
        int(m["ID"]) for m in ministers
        if C.MINISTER_TT_FOCUS & set(as_ints(m.get("TimeTypeList")))
    }
    add_seed_scopes(scopes, poet_policy_ids, singer_policy_ids, policies, minister_focus_ids)

    D.bulk_insert("policy", [
        "key_id", "id", "policy_name", "time_type_list", "policy_type", "minister_id",
        "effect_desc", "effect_up_desc", "effect_down_desc", "descr", "desc_from",
        "star_cnt", "cost_time", "pre_list", "condition_effect", "condition_values",
        "include_scope", "raw_json",
    ], [(
        int(p["KeyID"]), int(p["ID"]), p.get("PolicyName"), as_ints(p.get("TimeTypeList")),
        integer(p.get("PolicyType")), integer(p.get("MinisterId")),
        p.get("EffectDesc"), p.get("EffectUpDesc"), p.get("EffectDownDesc"),
        p.get("Desc"), p.get("DescFrom"), integer(p.get("StarCnt")), integer(p.get("CostTime")),
        as_ints(p.get("PreList")), integer(p.get("Condition_Effect")),
        as_floats(p.get("Condition_Values")),
        sorted(scopes[int(p["KeyID"])]), D.jsonb(p),
    ) for p in policies], page_size=500)

    # ---- 展开表
    rows = []
    for v in verses:
        vid = int(v["ID"])
        types = as_list(v.get("EffectTypeList"))
        values = as_list(v.get("EffectParamList"))
        adds = as_list(v.get("EffectAddTypeList"))
        for i, t in enumerate(types):
            if t is None:
                continue
            t = int(t)
            rows.append((
                vid, i, t,
                float(values[i]) if i < len(values) and values[i] is not None else None,
                int(adds[i]) if i < len(adds) and adds[i] is not None else None,
                effect_names.get(t) or C.EFFECT_NAME_FALLBACK.get(t),
            ))
    D.bulk_insert("verse_effect",
                  ["verse_id", "ordinal", "effect_type", "effect_value", "add_type", "effect_name"],
                  rows)

    rows = [(int(v["ID"]), int(pid))
            for v in verses for pid in {int(x) for x in as_list(v.get("BelongPoetIds"))}]
    D.bulk_insert("verse_poet_relation", ["verse_id", "poet_id"], rows)

    # 补占位词人: 词句 BelongPoetIds 中引用了 poet 表不存在的 ID (实测 ID=19)
    # 实测: 词句 9「水天空阔」与 87「秋色正萧洒」归属 poet_id=19, 且解锁需名臣 999(特殊)
    referenced = {int(x) for v in verses for x in as_list(v.get("BelongPoetIds"))}
    known = {int(p["ID"]) for p in poets}
    for missing in sorted(referenced - known):
        D.bulk_insert("poet", [
            "id", "name", "reward_policy_id", "minister_id", "img_id", "anim_name", "raw_json",
        ], [(
            missing, f"[占位] 未收录词人 {missing}", None, None, None, None,
            D.jsonb({"_placeholder": True, "_reason": "词句 BelongPoetIds 引用了 SongCiPoetConfig 中不存在的词人 ID",
                     "_referenced_by_verses": sorted(
                         int(v["ID"]) for v in verses if missing in {int(x) for x in as_list(v.get("BelongPoetIds"))}),
                     "_unlock_minister": 999}),
        )])
        D.log(f"[info] 词句归属引用了 poet 表中不存在的词人 ID={missing}, 已插入占位行")

    rows = []
    for v in verses:
        vid = int(v["ID"])
        mins = as_list(v.get("UnlockNeedMinister"))
        stars = as_list(v.get("UnlockNeedMinisterStar"))
        pols = as_list(v.get("UnlockNeedPolicyId"))
        n = max(len(mins), len(stars), len(pols), 0)
        for i in range(n):
            mid = int(mins[i]) if i < len(mins) and mins[i] is not None else None
            rows.append((
                vid, i, mid,
                int(stars[i]) if i < len(stars) and stars[i] is not None else None,
                int(pols[i]) if i < len(pols) and pols[i] is not None else None,
                bool(mid is not None and mid in C.SPECIAL_UNLOCK_MINISTER_IDS),
            ))
        if n == 0:
            rows.append((vid, 0, None, None, None, False))
    D.bulk_insert("verse_unlock_requirement",
                  ["verse_id", "ordinal", "minister_id", "star", "policy_id", "is_special"], rows)

    # 政策效果 (已递归展开) —— 重新为每条政策收集, 同时记录 refs
    expander = PolicyEffectExpander(policies, effect_ids)
    eff_rows = []
    ref_rows = []
    for p in policies:
        for (root_key, src, t, val, add, name, depth, ordinal) in expander.expand(p):
            eff_rows.append((root_key, src, t, val, add, name, depth, ordinal))
    for (root_key, child, depth) in expander.refs:
        ref_rows.append((root_key, child, depth))
    # 去重 (同一 (root, child, depth) 可能被多次发现)
    ref_rows = sorted(set(ref_rows))
    D.bulk_insert("policy_effect", [
        "policy_key_id", "source_policy_id", "effect_type", "effect_value",
        "add_type", "effect_name", "depth", "ordinal",
    ], eff_rows, page_size=2000)
    D.bulk_insert("policy_effect_ref",
                  ["policy_key_id", "child_policy_id", "depth"], ref_rows, page_size=2000)

    # 名臣 -> 政策 (按 KeyID)
    # 注意: MinisterBaseConfig.PolicyList 内部存在重复项, 需去重 (保留最小 ordinal)
    rows = []
    for m in ministers:
        mid = int(m["ID"])
        seen: set[int] = set()
        for i, k in enumerate(as_ints(m.get("PolicyList"))):
            if k in by_key and k not in seen:
                seen.add(k)
                rows.append((mid, k, i))
    D.bulk_insert("minister_policy", ["minister_id", "policy_key_id", "ordinal"], rows)

    # 词人 -> 政策
    rows = []
    for p in poets:
        pid = integer(p.get("RewardPolicyId"))
        pol = by_id.get(pid) if pid else None
        rows.append((int(p["ID"]), int(pol["KeyID"]) if pol else None, pid, pol is not None))
    D.bulk_insert("poet_policy", ["poet_id", "policy_key_id", "policy_id", "resolved"], rows)

    # 唱词人 -> 效果池 (Trigger/RemoveCommonEffectPoolConfigId)
    # **重要**: 这些 ID 属于 `CommonEffectPoolConfig` 的编号空间, 不是 `PolicyConfig`。
    # 早期按 PolicyConfig 解析会命中同号的军事政策 (451=徵兵制/452=募兵制…),
    # 于是 singer_effect 存的全是错数据。
    ingest_singer_pools()

    ingest_singer_rules()
    ingest_model_scope()

    # 政策条件
    rows = []
    for p in policies:
        key = int(p["KeyID"])
        ce = p.get("Condition_Effect")
        cv = as_floats(p.get("Condition_Values"))
        if ce is None and not cv:
            continue
        rows.append((key, integer(ce), cv, 0))
    D.bulk_insert("policy_condition",
                  ["policy_key_id", "condition_effect", "condition_values", "ordinal"], rows)

    D.log("-" * 70)
    verify()


# ---------------------------------------------------------------- 唱词人规则

def ingest_effect_pools() -> None:
    """`CommonEffectPoolConfig.json` -> `common_effect_pool` (+ `_effect`)。

    唱词人效果池 (451-458/650/651) 与 东坡食单 (652-660) 都指向这张表。
    **注意**: 这些 ID 与 `PolicyConfig.ID` 编号空间不同 —— 451 在池表里是
    「当前唱词人id为1」, 在政策表里却是「徵兵制」。
    """
    pools = load_records(C.COMMON_EFFECT_POOL_JSON, "CommonEffectPoolConfig")
    D.bulk_insert("common_effect_pool", [
        "id", "name", "condition_lib_id", "condition_desc", "trigger_target",
        "effect_desc", "raw_json",
    ], [(
        int(p["ID"]), p.get("Name"), integer(p.get("ConditionLibId")),
        p.get("ConditionDesc"), integer(p.get("TriggerTarget")), p.get("EffectDesc"),
        D.jsonb(p),
    ) for p in pools])
    rows = []
    for p in pools:
        types = as_list(p.get("EffectList"))
        values = as_list(p.get("EffectValueList"))
        adds = as_list(p.get("EffectAddTypeList"))
        for i, t in enumerate(types):
            if t is None:
                continue
            rows.append((
                int(p["ID"]), i, int(t),
                float(values[i]) if i < len(values) and values[i] is not None else None,
                int(adds[i]) if i < len(adds) and adds[i] is not None else None,
            ))
    D.bulk_insert("common_effect_pool_effect",
                  ["pool_id", "ordinal", "effect_type", "effect_value", "add_type"],
                  rows, page_size=2000)
    D.log(f"[ingest] common_effect_pool    {len(pools):>5d} 池 / {len(rows)} 效果行")


def ingest_dongpo() -> None:
    """`DongPoFoodConfig.json` -> `dongpo_food` (9 道菜)。"""
    D.bulk_insert("dongpo_food", [
        "id", "name", "ci_yuan_price", "pool_id", "descr", "icon", "raw_json",
    ], [(
        int(f["ID"]), f.get("Name"), num(f.get("CiYuanPrice")),
        integer(f.get("CommonEffectPoolId")), f.get("Desc"), f.get("Icon"), D.jsonb(f),
    ) for f in load_records(C.DONGPO_JSON, "DongPoFoodConfig")])
    D.log(f"[ingest] dongpo_food           {D.scalar('SELECT count(*) FROM dongpo_food'):>5d} 行")


def ingest_singer_pools() -> None:
    """`SongCiSingerConfig` 的 Trigger/RemoveCommonEffectPoolConfigId -> `singer_pool` + `singer_effect`。"""
    singers = load_records(C.SINGER_JSON, "SongCiSingerConfig")
    pools = load_records(C.COMMON_EFFECT_POOL_JSON, "CommonEffectPoolConfig")
    pool_by_id = {int(p["ID"]): p for p in pools}

    D.bulk_insert("singer_pool", ["singer_id", "kind", "pool_id"], [
        (int(s["ID"]), kind, integer(s.get(field)))
        for s in singers
        for kind, field in (("trigger", "TriggerCommonEffectPoolConfigId"),
                            ("remove", "RemoveCommonEffectPoolConfigId"))
    ])

    rows = []
    for s in singers:
        sid = int(s["ID"])
        for ptype, field in (("trigger", "TriggerCommonEffectPoolConfigId"),
                             ("remove", "RemoveCommonEffectPoolConfigId")):
            qid = s.get(field)
            if not qid:
                continue
            pool = pool_by_id.get(int(qid))
            if not pool:
                rows.append((sid, ptype, 0, None, None, None, None, None))
                continue
            types = as_list(pool.get("EffectList"))
            values = as_list(pool.get("EffectValueList"))
            adds = as_list(pool.get("EffectAddTypeList"))
            for i, t in enumerate(types):
                if t is None:
                    continue
                t = int(t)
                v = float(values[i]) if i < len(values) and values[i] is not None else None
                # 池里的 1030 = 「添加政策 <v>」, 把被引用的政策 ID 记到 policy_id
                rows.append((
                    sid, ptype, i, None,
                    int(v) if (t == C.EFF_CONDITIONAL and v is not None) else None,
                    t, v,
                    int(adds[i]) if i < len(adds) and adds[i] is not None else None,
                ))
    D.bulk_insert("singer_effect", [
        "singer_id", "pool_type", "ordinal", "policy_key_id", "policy_id",
        "effect_type", "effect_value", "add_type",
    ], rows)
    D.log(f"[ingest] singer_pool / singer_effect  {len(rows)} 行 <- CommonEffectPoolConfig")


_SINGER_TRIGGER = {
    "none": "不触发",
    "every_correct": "每次择律正确",
    "on_poet_unlock": "每次解锁新词人",
    "passive_bonus": "被动常驻",
    "su_shi": "抽到苏轼词",
}

_SINGER_NOTE = {
    1: "「每次择律错误」在本模拟中永不触发（择律默认无条件正确），故该效果恒为 0",
    2: ("FeatureDesc 与效果池政策 30001 的文案不一致"
        "（后者写「必定唱出词句，但后续择律收益-10%」）；**以 FeatureDesc 为准**"),
    9: "仅当抽中的词句归属苏轼（Poet ID 23）时才触发",
}


def ingest_singer_rules() -> None:
    """把 `effects.SINGER_RULES` 落库到 `songci.singer_rule`。

    权威口径是 `SongCiSingerConfig.FeatureDesc` 的**文字描述** ——
    `SINGER_RULES` 就是按它逐字推导出来的确定性规则; 早期 `v_singer_overview`
    改用的 `singer_effect` 表来自 `TriggerCommonEffectPoolConfigId`,
    实测指向占位军事政策 (451-458/650/651), 数值全是错的。
    """
    import display as DISP
    import effects as E
    import model as M

    rows = []
    for sid in sorted(E.SINGER_RULES):
        r = E.SINGER_RULES[sid]
        if r.module == "on_style":
            trigger = f"抽到{M.STYLE_LABELS.get(r.style, r.style)}词"
        else:
            trigger = _SINGER_TRIGGER.get(r.module, r.module)

        if r.module == "none":
            effect_text = "—（无收益）"
        elif r.module == "passive_bonus":
            effect_text = f"后续择律收益{r.bonus * 100:+g}%"
        elif r.module == "su_shi":
            n = int(r.sentiment_bonus)
            effect_text = (f"{DISP.format_effect(r.effect_type, r.effect_value)}，"
                           f"豪放词情+{n}，婉约词情+{n}")
        else:
            effect_text = f"{DISP.format_effect(r.effect_type, r.effect_value)} / 次"

        rows.append((
            int(sid), r.module, trigger, effect_text,
            r.effect_type, float(r.effect_value or 0.0), r.style,
            float(r.bonus or 0.0), int(r.sentiment_bonus or 0),
            r.target_poet_id, _SINGER_NOTE.get(sid, ""),
        ))
    D.bulk_insert("singer_rule", [
        "singer_id", "module", "trigger_desc", "effect_text",
        "effect_type", "effect_value", "style", "bonus",
        "sentiment_bonus", "target_poet_id", "note",
    ], rows)
    D.log(f"[ingest] singer_rule          {len(rows):>5d} 行  <- FeatureDesc 推导")


def ingest_model_scope() -> None:
    """把 `effects.py` 的「纳入范围」常量落库到 `songci.model_scope`。

    这样 SQL 视图 (如 `v_reward_overview`) 可以直接 JOIN 这张表来筛选,
    不必把政策 ID 清单硬编码进 SQL, 避免两处清单漂移。
    """
    import effects as E
    import simulator as SIM

    rows: list[tuple[str, int]] = []
    rows += [("minister_policy", int(x)) for x in sorted(E.STARTUP_MINISTER_POLICY_IDS)]
    for pid_map, kind in ((SIM.POET_DRAW_POLICY_IDS, "poet_draw_policy"),
                          (SIM.MINISTER_DRAW_POLICY_IDS, "minister_draw_policy"),
                          (SIM._SINGER_DRAW_POLICY_IDS, "singer_draw_policy")):
        seen: set[int] = set()
        for v in pid_map.values():
            seen |= {int(x) for x in v}
        rows += [(kind, x) for x in sorted(seen)]
    rows += [("pipeline_effect", int(x)) for x in sorted(E.PIPELINE_EFFECT_IDS)]

    D.bulk_insert("model_scope", ["kind", "ref_id"], rows)
    D.log(f"[ingest] model_scope          {len(rows):>5d} 行  <- effects.py / simulator.py")


# ---------------------------------------------------------------- 校验

EXPECTED = {
    "verse": 99, "poet": 21, "singer": 9, "policy": 9275,
    "effect_type": 639, "minister": 317,
}
# 词句 BelongPoetIds 引用了 SongCiPoetConfig 未收录的 ID, 会补占位词人
EXPECTED_PLACEHOLDER = {"poet": 1}


def verify() -> None:
    D.log("M1 · 校验")
    D.log("-" * 70)
    ok = True
    for tbl, want in EXPECTED.items():
        extra = EXPECTED_PLACEHOLDER.get(tbl, 0)
        got = D.scalar(f"SELECT count(*) FROM {C.DB_SCHEMA}.{tbl}")
        flag = "OK " if got == want + extra else "FAIL"
        if got != want + extra:
            ok = False
        note = f" (+{extra} 占位)" if extra else ""
        D.log(f"  [{flag}] {tbl:20s} 期望 {want:>6d}{note}  实际 {got:>6d}")

    D.log("")
    D.log("  展开表行数:")
    for tbl in ("verse_effect", "verse_poet_relation", "verse_unlock_requirement",
                "policy_effect", "policy_effect_ref", "minister_policy",
                "poet_policy", "singer_effect", "singer_pool", "singer_rule",
                "common_effect_pool", "common_effect_pool_effect", "dongpo_food",
                "model_scope", "policy_condition"):
        got = D.scalar(f"SELECT count(*) FROM {C.DB_SCHEMA}.{tbl}")
        D.log(f"    {tbl:26s} {got:>7d}")

    # 关联完整性
    D.log("")
    D.log("  关联完整性:")
    checks = [
        ("词人 RewardPolicyId 全部命中", f"""
            SELECT count(*) FROM {C.DB_SCHEMA}.poet_policy WHERE NOT resolved""", 0),
        ("唱词人池 ID 全部命中", f"""
            SELECT count(*) FROM {C.DB_SCHEMA}.singer_effect WHERE policy_key_id IS NULL""", 0),
        ("名臣->政策未命中 KeyID (集合差)", f"""
            SELECT count(*) FROM (
              SELECT DISTINCT m.id, k FROM {C.DB_SCHEMA}.minister m,
                     unnest(m.policy_list) AS k
            ) t WHERE t.k NOT IN (SELECT key_id FROM {C.DB_SCHEMA}.policy)""", 0),
        ("词句归属词人未命中 poet 表", f"""
            SELECT count(*) FROM {C.DB_SCHEMA}.verse_poet_relation r
            LEFT JOIN {C.DB_SCHEMA}.poet p ON p.id = r.poet_id WHERE p.id IS NULL""", 0),
        ("verse_effect 效果类型为 NULL", f"""
            SELECT count(*) FROM {C.DB_SCHEMA}.verse_effect WHERE effect_type IS NULL""", 0),
        ("policy_effect 未知引用行数 (应为 15)", f"""
            SELECT count(*) FROM {C.DB_SCHEMA}.policy_effect WHERE effect_type IS NULL""", 15),
        ("占位词人行数 (应为 1)", f"""
            SELECT count(*) FROM {C.DB_SCHEMA}.poet WHERE raw_json->>'_placeholder' = 'true'""", 1),
    ]
    for label, sql, want in checks:
        got = D.scalar(sql)
        flag = "OK " if got == want else "FAIL"
        if got != want:
            ok = False
        D.log(f"  [{flag}] {label:34s} = {got}")

    # 完整性问题: 名臣 PolicyList 中的重复项
    dup = D.scalar(f"""
        SELECT coalesce(sum(c - d), 0) FROM (
          SELECT m.id,
                 cardinality(m.policy_list) AS c,
                 (SELECT count(DISTINCT k) FROM unnest(m.policy_list) AS k) AS d
          FROM {C.DB_SCHEMA}.minister m
        ) t WHERE c > d""")
    D.log(f"  [info] 名臣 PolicyList 中的重复引用数 (已去重): {int(dup)}")

    # 未知引用 (非效果也非政策)
    unknowns = D.fetch_all(f"""
        SELECT effect_name, count(*) FROM {C.DB_SCHEMA}.policy_effect
        WHERE effect_type IS NULL GROUP BY 1 ORDER BY 2 DESC""")
    D.log(f"  [info] 未知引用种类: {len(unknowns)}")
    for name, n in unknowns:
        D.log(f"         {name}  x{n}")

    D.log("")
    D.log("  分层规模 (include_scope 数组元素计数):")
    for scope in (C.SCOPE_SONGCI_CORE, C.SCOPE_POET_POLICY, C.SCOPE_SINGER_POLICY,
                  C.SCOPE_MINISTER_11X, C.SCOPE_TT15, C.SCOPE_RAW_ONLY):
        n = D.scalar(f"SELECT count(*) FROM {C.DB_SCHEMA}.policy WHERE %s = ANY(include_scope)", (scope,))
        D.log(f"    {scope:18s} {n:>6d}")

    D.log("")
    D.log("  抽查: 二级政策引用展开")
    for key, label in ((2019, "背嵬军 KeyID=2019 (ID=3010): 应含 1039 + 1030(->31407)"),
                       (2120, "陆游示儿 KeyID=2120 (ID=3111): 应含 32513 + 1030(->31111)")):
        D.log(f"    {label}")
        for row in D.fetch_all(f"""
            SELECT source_policy_id, effect_type, effect_value, add_type, depth, effect_name
            FROM {C.DB_SCHEMA}.policy_effect WHERE policy_key_id = %s
            ORDER BY depth, ordinal""", (key,)):
            D.log(f"      src={row[0]:>7d} type={str(row[1]):>6s} val={str(row[2]):>9s} "
                  f"add={str(row[3]):>4s} depth={row[4]} {row[5] or ''}")

    D.log("")
    D.log("  抽查: 词句 1「怒发冲冠」的三个效果应与原始 JSON 一致")
    for row in D.fetch_all(f"""
        SELECT ordinal, effect_type, effect_value, add_type, effect_name
        FROM {C.DB_SCHEMA}.verse_effect WHERE verse_id = 1 ORDER BY ordinal"""):
        D.log(f"    ord={row[0]} type={row[1]} val={row[2]} add={row[3]} {row[4] or ''}")
    D.log("    原始 JSON: EffectTypeList=[32514,51,204] EffectParamList=[10.0,-10.0,10.0] AddType=[4,4,4]")

    D.log("")
    D.log("=" * 70)
    D.log("校验结果: " + ("全部通过" if ok else "存在 FAIL, 请检查"))
    D.log("=" * 70)
    if not ok:
        sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true", help="只校验, 不入库")
    args = ap.parse_args()
    if args.verify:
        verify()
    else:
        ingest()


if __name__ == "__main__":
    main()
