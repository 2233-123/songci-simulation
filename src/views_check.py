# -*- coding: utf-8 -*-
"""M2 · 执行筛选口径视图并输出对照报告。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as C
import db as D

OUT = C.DOCS_DIR / "筛选口径对照.md"


def main() -> None:
    C.DOCS_DIR.mkdir(parents=True, exist_ok=True)      # clone 后可能没有 docs/
    D.run_sql_file(C.VIEWS_SQL)

    lines: list[str] = []
    ok = True

    def emit(s: str = "") -> None:
        D.log(s)
        lines.append(s)

    def check(label: str, sql: str, want: int) -> int:
        nonlocal ok
        got = int(D.scalar(sql) or 0)
        flag = "OK " if got == want else "FAIL"
        if got != want:
            ok = False
        emit(f"| {label} | {want} | {got} | {flag} |")
        return got

    emit("# 筛选口径对照报告")
    emit()
    emit("任务书第 1-4 条的筛选规则在数据库中的等价视图，及实测规模。")
    emit()
    emit("**核心结论：**")
    emit()
    emit("1. 数据中同一 `ID` 对应多个 `KeyID`（时代专属变体），故政策主键一律取 `KeyID`。")
    emit("2. 第 1+4 条字面串联后保留 **278 条**政策（并非清空），但**会删掉全部 21 条词人政策**"
         "（它们 `MinisterId=0`，尽管都满足第 1 条）。")
    emit("3. 因此推荐用「效果链触达宋词系统」口径建库并标记分层，而非物理删除。")
    emit()

    emit("## 一、各口径规模")
    emit()
    emit("| 口径 | 期望 | 实测 | 结果 |")
    emit("|---|---|---|---|")
    check("第 3 条：名臣 TimeTypeList 含 11/1101/1102（`v_minister_11x`）",
          "SELECT count(*) FROM songci.v_minister_11x", 35)
    check("第 1 条：政策 TimeTypeList 含 15（`v_policy_tt15`）",
          "SELECT count(*) FROM songci.v_policy_tt15", 4515)
    check("第 2 条：词人的对应政策（`v_policy_poet`）",
          "SELECT count(*) FROM songci.v_policy_poet", 21)
    check("第 4 条：政策 MinisterId 命中保留名臣（`v_policy_minister_match`）",
          "SELECT count(*) FROM songci.v_policy_minister_match", 291)
    check("⚠️ 第 1+4 条字面串联（`v_policy_strict_rule_14`）",
          "SELECT count(*) FROM songci.v_policy_strict_rule_14", 278)
    check("推荐口径 A：效果链触达宋词系统（`v_songci_core_policy`）",
          "SELECT count(*) FROM songci.v_songci_core_policy", 830)
    check("对照口径 B：效果链含宋词专属效果 ID（`v_songci_effect_policy`）",
          "SELECT count(*) FROM songci.v_songci_effect_policy", 96)
    check("模拟实际使用：词人 ∪ 唱词人 ∪ 名臣择律 ∪ 宋词核心（`v_songci_related_policy`）",
          "SELECT count(*) FROM songci.v_songci_related_policy", 990)
    emit()

    emit("## 二、第 1+4 条字面执行的破坏性（实证）")
    emit()
    lost_poet = D.scalar("""
        SELECT count(*) FROM songci.v_policy_poet
        WHERE key_id NOT IN (SELECT key_id FROM songci.v_policy_strict_rule_14)""")
    kept_tt15 = D.scalar("""
        SELECT count(*) FROM songci.v_policy_poet WHERE time_type_list @> ARRAY[15]""")
    lost_min_eff = D.scalar("""
        SELECT count(DISTINCT p.key_id) FROM songci.v_songci_effect_policy p
        WHERE p.minister_id IS NOT NULL AND p.minister_id <> 0
          AND NOT (p.time_type_list @> ARRAY[15])""")
    emit("**结论：破坏发生在「第 4 条」，而不是「第 1 条」。**")
    emit()
    emit("| 项目 | 数量 | 说明 |")
    emit("|---|---|---|")
    emit(f"| 被第 4 条删除的词人政策 | **{lost_poet} / 21** | 21 条词人政策全部 `MinisterId=0`，"
         f"而它们全部满足第 1 条的 `TimeTypeList` 含 15（实测 {kept_tt15}/21）|")
    emit(f"| 被第 1 条删除的名臣择律政策 | {lost_min_eff} | 这些是**同 ID 的时代专属变体**，"
         f"同 ID 另有 `TimeTypeList` 含 15 的变体存活 |")
    emit()
    emit("第 1+4 条保留的 278 条政策构成：")
    emit()
    for label, sql in [
        ("其中属名臣择律政策（minister_11x）",
         "SELECT count(*) FROM songci.v_policy_strict_rule_14"),
        ("其中效果链触达宋词系统",
         """SELECT count(*) FROM songci.v_policy_strict_rule_14 v
            JOIN songci.policy p ON p.key_id=v.key_id
            WHERE p.include_scope @> ARRAY['songci_core']"""),
        ("其中属词人政策",
         """SELECT count(*) FROM songci.v_policy_strict_rule_14 v
            JOIN songci.policy p ON p.key_id=v.key_id
            WHERE p.include_scope @> ARRAY['poet_policy']"""),
    ]:
        emit(f"- {label}：**{D.scalar(sql)}**")
    emit()
    emit("### 关键发现：同 ID 存在多个 KeyID（时代专属变体）")
    emit()
    emit("`PolicyConfig` 中同一个 `ID` 会对应多个 `KeyID`，每个 KeyID 属于不同时代。")
    emit("因此按 `ID` 统计与按 `KeyID` 统计结果不同，本项目的政策主键一律取 `KeyID`。")
    emit()
    emit("| ID | 政策名 | 名臣 | KeyID 数 | 含 15 的变体数 | KeyID 列表 |")
    emit("|---|---|---|---|---|---|")
    for row in D.fetch_all("""
        SELECT p.id, p.policy_name, m.name, count(*),
               count(*) FILTER (WHERE p.time_type_list @> ARRAY[15]),
               array_agg(p.key_id ORDER BY p.key_id)
        FROM songci.policy p JOIN songci.minister m ON m.id = p.minister_id
        WHERE p.id IN (3013, 3060, 3090, 3108, 3111, 3165, 3200, 3206, 3230, 3236)
        GROUP BY p.id, p.policy_name, m.name ORDER BY p.id"""):
        emit(f"| {row[0]} | {row[1]} | {row[2]} | {row[3]} | {row[4]} | {row[5]} |")
    emit()
    emit("### 会被第 4 条删掉的 21 条词人政策（全部 `MinisterId=0`）")
    emit()
    emit("| 词人 | 政策 ID | 政策名 | 第 1 条(TT=15)是否满足 |")
    emit("|---|---|---|---|")
    for row in D.fetch_all("""
        SELECT po.name, p.id, p.policy_name, (p.time_type_list @> ARRAY[15])
        FROM songci.poet_policy pp JOIN songci.poet po ON po.id = pp.poet_id
        JOIN songci.policy p ON p.key_id = pp.policy_key_id ORDER BY pp.poet_id"""):
        emit(f"| {row[0]} | {row[1]} | {row[2]} | {'满足' if row[3] else '不满足'} |")

    emit("## 三、关键关联验证")
    emit()
    emit("| 关联 | 结果 |")
    emit("|---|---|")
    n = D.scalar("SELECT count(*) FROM songci.poet_policy WHERE NOT resolved")
    emit(f"| 词人 `RewardPolicyId` → `policy.ID` | 21/21 命中，未命中 {n} |")
    n = D.scalar("SELECT count(*) FROM songci.singer_pool WHERE pool_id IS NULL")
    emit(f"| 唱词人池 ID → `common_effect_pool.ID` | 未命中 {n}（**池表与政策表编号空间不同**）|")
    n = D.scalar("""
        SELECT count(*) FROM (
          SELECT DISTINCT m.id, k FROM songci.minister m, unnest(m.policy_list) AS k
        ) t WHERE t.k NOT IN (SELECT key_id FROM songci.policy)""")
    emit(f"| 名臣 `PolicyList` → `policy.KeyID` | 未命中 {n}（**按 KeyID 关联，非 ID**）|")
    n = D.scalar("SELECT count(*) FROM songci.policy_effect WHERE depth > 0")
    emit(f"| 二级政策引用展开 | 展开出 {n} 行 `depth>0` 效果 |")
    emit()

    emit("## 四、词句库可抽取性")
    emit()
    emit("| 项目 | 数量 |")
    emit("|---|---|")
    for label, sql in [
        ("词句总数", "SELECT count(*) FROM songci.verse"),
        ("豪放词句（Style=1）", "SELECT count(*) FROM songci.verse WHERE style=1"),
        ("婉约词句（Style=2）", "SELECT count(*) FROM songci.verse WHERE style=2"),
        ("无解锁限制（可直接抽取）",
         "SELECT count(DISTINCT verse_id) FROM songci.verse_unlock_requirement WHERE minister_id IS NULL"),
        ("需名臣解锁", "SELECT count(DISTINCT verse_id) FROM songci.verse_unlock_requirement WHERE minister_id IS NOT NULL"),
        ("需特殊名臣(999)解锁", "SELECT count(DISTINCT verse_id) FROM songci.verse_unlock_requirement WHERE is_special"),
    ]:
        emit(f"| {label} | {D.scalar(sql)} |")
    emit()
    emit("需解锁词句按名臣分布：")
    emit()
    emit("| 名臣 ID | 名臣名 | 需其解锁的词句数 |")
    emit("|---|---|---|")
    for row in D.fetch_all("""
        SELECT u.minister_id, coalesce(m.name, '(未收录)'), count(DISTINCT u.verse_id)
        FROM songci.verse_unlock_requirement u
        LEFT JOIN songci.minister m ON m.id = u.minister_id
        WHERE u.minister_id IS NOT NULL
        GROUP BY u.minister_id, m.name ORDER BY 3 DESC"""):
        emit(f"| {row[0]} | {row[1]} | {row[2]} |")
    emit()

    emit("## 五、唱词人偏好与效果")
    emit()
    emit("**权威口径 = `songci.singer.FeatureDesc` 文字描述**"
         "（视图 `songci.v_singer_overview` ＋ `songci.singer_rule`；")
    emit("规则由 `src/ingest.py` 从 `src/effects.py` 的 `SINGER_RULES` 落库，"
         "`SINGER_RULES` 即按 `FeatureDesc` 逐字推导）。")
    emit()
    emit("> 早期版本此表关联的是 `singer_effect`（来自 `TriggerCommonEffectPoolConfigId`），"
         "实测那些 ID 指向**占位军事政策**（451-458/650/651），数值全错，已弃用。")
    emit()
    emit("| ID | 唱词人 | 豪放偏好 | 婉约偏好 | P(抽豪放) | 效果描述（FeatureDesc，权威） | 触发时机 | 效果数值 |")
    emit("|---|---|---|---|---|---|---|---|")
    for row in D.fetch_all("""
        SELECT s.id, s.name, s.bold_favor, s.graceful_favor,
               round(s.bold_favor::numeric / nullif(s.bold_favor + s.graceful_favor, 0), 3),
               s.feature_desc, r.trigger_desc, r.effect_text
        FROM songci.singer s LEFT JOIN songci.singer_rule r ON r.singer_id = s.id
        ORDER BY s.id"""):
        emit(f"| {row[0]} | {row[1]} | {row[2]} | {row[3]} | {row[4]} | {row[5]} "
             f"| {row[6] or ''} | {row[7] or ''} |")
    emit()
    emit("> 唱词人 2「铁衣」：效果池政策 `30001` 的文案是「必定唱出词句，但后续择律收益-10%」，")
    emit("> 与 `FeatureDesc` 冲突 —— **以 `FeatureDesc`（歌板+1 / 次）为准**。")
    emit()

    emit("## 六、词人与名臣效果（`songci.v_reward_overview`）")
    emit()
    emit("| 项 | 值 |")
    emit("|---|---|")
    emit("| 视图 | `v_reward_overview`（词人 + 名臣，取代早期的 `v_poet_reward`） |")
    emit("| 筛选来源 | `songci.model_scope`（`kind='minister_policy'` 等），"
         "清单唯一来源是 `src/effects.py` |")
    emit("| 星级 | 同一政策名只保留最高星级（星级不能并存） |")
    emit("| 重复登记 | 同一效果在源数据里重复登记时合并为**一行**，`重复登记` 列记次数 |")
    emit("| 排除项 | `model_scope.kind='pipeline_effect'`"
         "（2807/881/1030/32850/1039/999/3200/3202） |")
    emit()
    emit("| 类别 | 行数 |")
    emit("|---|---|")
    for row in D.fetch_all("""
        SELECT 类别, count(*) FROM songci.v_reward_overview GROUP BY 类别 ORDER BY 类别"""):
        emit(f"| {row[0]} | {row[1]} |")
    emit()
    n_all = D.scalar("SELECT count(*) FROM songci.v_reward_overview")
    n_dis = D.scalar("""SELECT count(*) FROM (
        SELECT DISTINCT 类别, 归属id, 政策id, 效果类型 FROM songci.v_reward_overview) t""")
    emit(f"去重校验：`count(*) = {n_all}` vs "
         f"`count(distinct 类别,归属id,政策id,效果类型) = {n_dis}` "
         f"→ **{'一致' if n_all == n_dis else '存在重复行'}**")
    emit()

    emit("## 七、东坡食单（`songci.v_dongpo_food`）")
    emit()
    emit("数据源：`DongPoFoodConfig.json`（每道菜一条）+ `CommonEffectPoolConfig.json`（池 `652-660`）。")
    emit("东坡食单由苏轼「老饕」（`3212 ★1` / `3213 ★2`）开启；在模拟器中为**逐个勾选项**。")
    emit("")
    emit("| 菜品 ID | 菜名 | 词元价 | 效果池 | 效果（权威源 EffectDesc） | 池内效果（EffectList/值/add_type） |")
    emit("|---|---|---|---|---|---|")
    for row in D.fetch_all("""
        SELECT f.id, f.name, f.ci_yuan_price, f.pool_id, p.effect_desc
        FROM songci.dongpo_food f
        LEFT JOIN songci.common_effect_pool p ON p.id = f.pool_id
        ORDER BY f.id"""):
        fid, fname, price, pool_id, desc = row
        eff = D.fetch_all("""
            SELECT effect_type, effect_value, add_type FROM songci.common_effect_pool_effect
            WHERE pool_id = %s ORDER BY ordinal""", (pool_id,))
        eff_txt = "，".join(f"{t}({v:g}/add={a})" for t, v, a in eff)
        emit(f"| {fid} | {fname} | {price:g} | {pool_id} | {desc or ''} | {eff_txt} |")
    emit()
    emit("**规则（用户 2026-10-03 确认）**")
    emit()
    emit("- 价格 `CiYuanPrice` **不收取**（「价格不管」）")
    emit("- 未勾选苏轼名臣（328）时勾选项禁用，引擎也不生效")
    emit("- **9 道全收集 且 勾了苏轼** → 老饕 ★2「收集所有食单 豪放词情+20 层」")
    emit("")
    emit("> `CommonEffectPoolConfig` 的 ID 与 `PolicyConfig.ID` **编号空间不同**（池 `451` = "
         "「当前唱词人id为1」，政策 `451` = 「徵兵制」）。")
    emit("> 早期按政策表解析唱词人池 `451-458/650/651`，效果全错，现已改为解析 `CommonEffectPoolConfig`。")
    emit()

    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    D.log("")
    D.log("=" * 70)
    D.log(f"报告已写入 {OUT}")
    D.log("校验结果: " + ("全部通过" if ok else "存在 FAIL"))
    D.log("=" * 70)
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
