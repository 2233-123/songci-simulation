-- ============================================================
-- M2 · 筛选口径视图
-- 复现任务书第 1-5 条的筛选规则, 并给出实测对照口径。
-- 原则: 视图只读, 不删任何数据。
-- ============================================================

SET search_path TO songci, public;

-- 列定义发生过变化的视图必须先 DROP 再建 —— `CREATE OR REPLACE VIEW`
-- 不允许改列名/列顺序。
DROP VIEW IF EXISTS songci.v_singer_overview;
DROP VIEW IF EXISTS songci.v_dongpo_food;
DROP VIEW IF EXISTS songci.v_reward_overview;
-- v_poet_reward 已被 v_reward_overview (词人 + 名臣) 取代
DROP VIEW IF EXISTS songci.v_poet_reward;

-- 「宋词择律系统」直接相关效果 ID
--   32510 歌板 / 32513 后续择律收益 / 32514 词元
--   32515 豪放词情 / 32516 婉约词情 / 32517 择律开题消耗
--   2807  系统开关(开启择律系统) / 1030 条件效果
CREATE OR REPLACE VIEW songci.v_songci_effect_ids AS
SELECT * FROM (VALUES
    (32510, '歌板'),
    (32513, '后续择律收益提升'),
    (32514, '词元'),
    (32515, '豪放词情'),
    (32516, '婉约词情'),
    (32517, '择律开题消耗'),
    (2807,  '系统开关(开启择律系统)'),
    (1030,  '条件效果')
) AS t(effect_type, effect_name);

-- ------------------------------------------------------------
-- 任务书第 3 条: 名臣只保留 TimeTypeList 含 11 / 1101 / 1102
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW songci.v_minister_11x AS
SELECT m.id, m.name, m.time_type_list, cardinality(m.policy_list) AS policy_ref_count
FROM songci.minister m
WHERE m.time_type_list && ARRAY[11, 1101, 1102]::integer[];

-- ------------------------------------------------------------
-- 任务书第 1 条: 删掉 TimeTypeList 不为 15 的政策
--   SQL 等价的「保留」口径: TimeTypeList 含 15
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW songci.v_policy_tt15 AS
SELECT p.key_id, p.id, p.policy_name, p.time_type_list, p.minister_id, p.effect_desc
FROM songci.policy p
WHERE p.time_type_list @> ARRAY[15]::integer[];

-- ------------------------------------------------------------
-- 任务书第 2 条: 词人的对应政策单独拿出来
--   关联: SongCiPoetConfig.RewardPolicyId -> PolicyConfig.ID
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW songci.v_policy_poet AS
SELECT po.id AS poet_id, po.name AS poet_name, p.key_id, p.id AS policy_id,
       p.policy_name, p.time_type_list, p.effect_desc
FROM songci.poet_policy pp
JOIN songci.poet po      ON po.id = pp.poet_id
JOIN songci.policy p     ON p.key_id = pp.policy_key_id
WHERE pp.resolved;

-- ------------------------------------------------------------
-- 任务书第 4 条: 只保留 MinisterId 对应到(被裁剪后的)名臣表中的政策
--   名臣已由第 3 条裁剪 -> 等价于 minister_id IN (v_minister_11x)
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW songci.v_policy_minister_match AS
SELECT p.key_id, p.id, p.policy_name, p.minister_id, m.name AS minister_name,
       p.time_type_list, p.effect_desc
FROM songci.policy p
JOIN songci.v_minister_11x m ON m.id = p.minister_id;

-- ------------------------------------------------------------
-- 任务书第 1 + 4 条「字面串联」的结果
--   ⚠️ 实证: 此口径会清空全部词人政策(21 条 MinisterId=0)
--            与全部名臣择律政策(其 TimeTypeList 为 902/905/10/1001, 不含 15)
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW songci.v_policy_strict_rule_14 AS
SELECT p.key_id, p.id, p.policy_name, p.minister_id, m.name AS minister_name,
       p.time_type_list, p.effect_desc
FROM songci.policy p
JOIN songci.v_minister_11x m ON m.id = p.minister_id
WHERE p.time_type_list @> ARRAY[15]::integer[];

-- ------------------------------------------------------------
-- 推荐模拟口径 A: 效果链触达宋词效果系统的政策
--   = policy.include_scope 含 'songci_core'
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW songci.v_songci_core_policy AS
SELECT p.key_id, p.id, p.policy_name, p.minister_id, m.name AS minister_name,
       p.time_type_list, p.include_scope, p.effect_desc
FROM songci.policy p
LEFT JOIN songci.minister m ON m.id = p.minister_id
WHERE p.include_scope @> ARRAY['songci_core']::text[];

-- ------------------------------------------------------------
-- 推荐模拟口径 B: 严格按效果 ID 判定(不含 2807/1030 这些通用效果)
--   用于与 A 对照, 说明口径宽窄差异
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW songci.v_songci_effect_policy AS
SELECT DISTINCT p.key_id, p.id, p.policy_name, p.minister_id, p.time_type_list, p.effect_desc
FROM songci.policy p
JOIN songci.policy_effect pe ON pe.policy_key_id = p.key_id
WHERE pe.effect_type IN (32510, 32513, 32514, 32515, 32516, 32517);

-- ------------------------------------------------------------
-- 模拟实际使用口径: 词人政策 ∪ 唱词人池政策 ∪ 名臣择律政策
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW songci.v_songci_related_policy AS
SELECT p.key_id, p.id, p.policy_name, p.minister_id, p.include_scope, p.effect_desc,
       (p.include_scope @> ARRAY['poet_policy']::text[])   AS is_poet_policy,
       (p.include_scope @> ARRAY['singer_policy']::text[]) AS is_singer_policy,
       (p.include_scope @> ARRAY['minister_11x']::text[])  AS is_minister_policy,
       (p.include_scope @> ARRAY['songci_core']::text[])   AS is_songci_core
FROM songci.policy p
WHERE p.include_scope && ARRAY['poet_policy', 'singer_policy', 'minister_11x', 'songci_core']::text[];

-- ------------------------------------------------------------
-- 词句可抽取性: 结合解锁门控(需拥有对应名臣)
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW songci.v_verse_drawable AS
SELECT v.id, v.ci_name, v.ci_pai_name, v.style,
       (v.style = 1) AS is_bold,
       u.minister_id AS unlock_minister_id,
       u.is_special  AS unlock_is_special,
       (u.minister_id IS NULL) AS drawable_unconditionally,
       v.unlock_reward_effect_pool_id
FROM songci.verse v
LEFT JOIN songci.verse_unlock_requirement u ON u.verse_id = v.id;

-- ------------------------------------------------------------
-- 词句效果汇总(宽表): 便于直接查收益
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW songci.v_verse_reward AS
SELECT v.id, v.ci_name, v.style,
       max(CASE WHEN ve.effect_type = 32514 THEN ve.effect_value END) AS 词元,
       max(CASE WHEN ve.effect_type = 32510 THEN ve.effect_value END) AS 歌板,
       max(CASE WHEN ve.effect_type = 51    THEN ve.effect_value END) AS 民心,
       max(CASE WHEN ve.effect_type = 204   THEN ve.effect_value END) AS 战斗力,
       max(CASE WHEN ve.effect_type = 209   THEN ve.effect_value END) AS 军心,
       max(CASE WHEN ve.effect_type = 351   THEN ve.effect_value END) AS 腐化,
       max(CASE WHEN ve.effect_type = 553   THEN ve.effect_value END) AS 威望,
       max(CASE WHEN ve.effect_type = 757   THEN ve.effect_value END) AS 发展年数
FROM songci.verse v
LEFT JOIN songci.verse_effect ve ON ve.verse_id = v.id
GROUP BY v.id, v.ci_name, v.style;

-- ------------------------------------------------------------
-- 词人 / 名臣 效果总表: 归属 -> 择律政策 -> 效果明细
--   * 名臣侧只列「本模拟纳入的择律政策」(STARTUP_MINISTER_POLICY_IDS), 与 GUI/模拟一致
--   * 同族多星级只保留最高星级 (星级不能并存, 只保留最好的)
--   * DB 里重复登记的同一效果合并为一行, 用 `重复登记` 记次数 (不再是重复行)
--   * 排除 2807/881/1030/32850 等无数值语义的管线效果
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW songci.v_reward_overview AS
WITH pipeline AS (
    SELECT ref_id AS effect_type FROM songci.model_scope WHERE kind = 'pipeline_effect'
), minister_policies AS (
    SELECT DISTINCT ON (p.minister_id, p.policy_name)
           p.minister_id, p.key_id, p.id, p.policy_name, p.star_cnt
    FROM songci.policy p
    JOIN songci.model_scope ms ON ms.kind = 'minister_policy' AND ms.ref_id = p.id
    WHERE p.minister_id IS NOT NULL AND p.minister_id <> 0
    ORDER BY p.minister_id, p.policy_name, p.star_cnt DESC NULLS LAST, p.id DESC
), poet_rows AS (
    SELECT '词人'::text AS 类别, po.id AS 归属id, po.name AS 归属,
           p.id AS 政策id, p.policy_name AS 政策名, p.star_cnt AS 星级,
           pe.effect_type AS 效果类型, pe.effect_value AS 数值, pe.add_type AS 加成方式
    FROM songci.poet po
    JOIN songci.poet_policy pp ON pp.poet_id = po.id
    JOIN songci.policy p ON p.key_id = pp.policy_key_id
    JOIN songci.policy_effect pe ON pe.policy_key_id = p.key_id AND pe.depth = 0
    WHERE po.raw_json->>'_placeholder' IS DISTINCT FROM 'true'
      AND pe.effect_type NOT IN (SELECT effect_type FROM pipeline)
), minister_rows AS (
    SELECT '名臣'::text, m.id, m.name,
           mp.id, mp.policy_name, mp.star_cnt,
           pe.effect_type, pe.effect_value, pe.add_type
    FROM minister_policies mp
    JOIN songci.minister m ON m.id = mp.minister_id
    JOIN songci.policy_effect pe ON pe.policy_key_id = mp.key_id AND pe.depth = 0
    WHERE pe.effect_type NOT IN (SELECT effect_type FROM pipeline)
), all_rows AS (
    SELECT * FROM poet_rows UNION ALL SELECT * FROM minister_rows
)
SELECT 类别, 归属id, 归属, 政策id, 政策名, 星级,
       效果类型,
       coalesce(max(et.effect_name), max(vt.effect_name), '') AS 效果名,
       数值, 加成方式,
       count(*) AS 重复登记
FROM all_rows a
LEFT JOIN songci.effect_type et ON et.effect_type = a.效果类型
LEFT JOIN songci.v_songci_effect_ids vt ON vt.effect_type = a.效果类型
GROUP BY 类别, 归属id, 归属, 政策id, 政策名, 星级, 效果类型, 数值, 加成方式
ORDER BY 类别 DESC, 归属id, 政策id, 效果类型;

-- ------------------------------------------------------------
-- 东坡食单 (DongPoFoodConfig + CommonEffectPoolConfig 652-660)
--   苏轼「老饕」(3212★1/3213★2) 开启; 9 道全收集额外 豪放词情+10/20 层
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW songci.v_dongpo_food AS
SELECT f.id AS ID, f.name AS 菜名, f.ci_yuan_price AS 词元价,
       f.pool_id AS 效果池id,
       coalesce(p.effect_desc, '') AS 效果描述,
       coalesce(string_agg(
           et.effect_name || ' ' || pe.effect_value::text
           || CASE WHEN pe.add_type = 2 THEN '(比例)' ELSE '' END,
           '，' ORDER BY pe.ordinal), '') AS 池内效果,
       f.descr AS 说明
FROM songci.dongpo_food f
LEFT JOIN songci.common_effect_pool p ON p.id = f.pool_id
LEFT JOIN songci.common_effect_pool_effect pe ON pe.pool_id = f.pool_id
LEFT JOIN songci.effect_type et ON et.effect_type = pe.effect_type
GROUP BY f.id, f.name, f.ci_yuan_price, f.pool_id, p.effect_desc, f.descr
ORDER BY f.id;

-- ------------------------------------------------------------
-- 唱词人效果总览 —— **以 FeatureDesc 文字描述为准**
--   `singer_rule` 由 src/ingest.py 从 src/effects.py 的 SINGER_RULES 落库,
--   而 SINGER_RULES 是按 FeatureDesc 逐字推导的确定性规则。
--   `singer_pool` / `common_effect_pool` 提供**游戏原始效果池**对照
--   (TriggerCommonEffectPoolConfigId 指向 CommonEffectPoolConfig, 不是 PolicyConfig)。
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW songci.v_singer_overview AS
SELECT s.id AS ID, s.name AS 唱词人,
       s.bold_favor AS 豪放偏好, s.graceful_favor AS 婉约偏好,
       round(s.bold_favor::numeric / nullif(s.bold_favor + s.graceful_favor, 0), 4) AS 抽到豪放概率,
       s.feature_desc AS 效果描述_权威,
       r.trigger_desc AS 触发时机,
       r.effect_text AS 效果数值,
       sp.pool_id AS 效果池id,
       coalesce(nullif(cp.condition_desc, ''), cp.effect_desc, '') AS 池内文本,
       r.note AS 备注
FROM songci.singer s
LEFT JOIN songci.singer_rule r ON r.singer_id = s.id
LEFT JOIN songci.singer_pool sp ON sp.singer_id = s.id AND sp.kind = 'trigger'
LEFT JOIN songci.common_effect_pool cp ON cp.id = sp.pool_id
ORDER BY s.id;
