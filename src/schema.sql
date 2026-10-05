-- ============================================================
-- 宋词择律模拟 · 数据库结构 (PostgreSQL 18)
-- schema: songci
-- 原则: 原始 JSON 只读; 全量入库 + 分层标记, 不物理删除
-- ============================================================

CREATE SCHEMA IF NOT EXISTS songci;
SET search_path TO songci, public;

-- ------------------------------------------------------------
-- 原始层: 忠实映射 6 份配置, raw_json 保留原文
-- ------------------------------------------------------------

DROP TABLE IF EXISTS songci.policy_condition CASCADE;
DROP TABLE IF EXISTS songci.singer_effect CASCADE;
DROP TABLE IF EXISTS songci.singer_rule CASCADE;
DROP TABLE IF EXISTS songci.model_scope CASCADE;
DROP TABLE IF EXISTS songci.dongpo_food CASCADE;
DROP TABLE IF EXISTS songci.singer_pool CASCADE;
DROP TABLE IF EXISTS songci.common_effect_pool_effect CASCADE;
DROP TABLE IF EXISTS songci.common_effect_pool CASCADE;
DROP TABLE IF EXISTS songci.poet_policy CASCADE;
DROP TABLE IF EXISTS songci.minister_policy CASCADE;
DROP TABLE IF EXISTS songci.policy_effect_ref CASCADE;
DROP TABLE IF EXISTS songci.policy_effect CASCADE;
DROP TABLE IF EXISTS songci.verse_unlock_requirement CASCADE;
DROP TABLE IF EXISTS songci.verse_poet_relation CASCADE;
DROP TABLE IF EXISTS songci.verse_effect CASCADE;
DROP TABLE IF EXISTS songci.verse CASCADE;
DROP TABLE IF EXISTS songci.poet CASCADE;
DROP TABLE IF EXISTS songci.singer CASCADE;
DROP TABLE IF EXISTS songci.policy CASCADE;
DROP TABLE IF EXISTS songci.effect_type CASCADE;
DROP TABLE IF EXISTS songci.minister CASCADE;

-- 词句库 (SongCiVerseConfig.json) 101 行
CREATE TABLE songci.verse (
    id                     INTEGER PRIMARY KEY,
    ci_name                TEXT,
    ci_pai_name            TEXT,
    style                  SMALLINT NOT NULL,          -- 1=豪放 2=婉约
    content                TEXT,
    descr                  TEXT,
    unlock_reward_effect_pool_id INTEGER,
    raw_json               JSONB NOT NULL
);
COMMENT ON COLUMN songci.verse.style IS '1=豪放, 2=婉约';

-- 词人库 (SongCiPoetConfig.json) 21 行
CREATE TABLE songci.poet (
    id                INTEGER PRIMARY KEY,
    name              TEXT NOT NULL,
    reward_policy_id  INTEGER,                          -- -> policy.id
    minister_id       INTEGER,                          -- 0 = 无名臣
    img_id            INTEGER,
    anim_name         TEXT,
    raw_json          JSONB NOT NULL
);

-- 唱词人库 (SongCiSingerConfig.json) 9 行
CREATE TABLE songci.singer (
    id                     INTEGER PRIMARY KEY,
    name                   TEXT NOT NULL,
    bold_favor             INTEGER NOT NULL DEFAULT 0,
    graceful_favor         INTEGER NOT NULL DEFAULT 0,
    feature_desc           TEXT,
    description            TEXT,
    unlock_condition       TEXT,
    trigger_pool_policy_id INTEGER,                     -- TriggerCommonEffectPoolConfigId -> policy.id
    remove_pool_policy_id  INTEGER,                     -- RemoveCommonEffectPoolConfigId  -> policy.id
    specialty_ci_pai_names TEXT[],
    raw_json               JSONB NOT NULL,
    CONSTRAINT singer_favor_positive CHECK (bold_favor >= 0 AND graceful_favor >= 0)
);

-- 政策库 (PolicyConfig.json) 9275 行; ID 有重复, KeyID 唯一 -> 以 KeyID 为主键
CREATE TABLE songci.policy (
    key_id             INTEGER PRIMARY KEY,
    id                 INTEGER NOT NULL,
    policy_name        TEXT,
    time_type_list     INTEGER[] NOT NULL DEFAULT '{}',
    policy_type        INTEGER,
    minister_id        INTEGER,                          -- 0/空 = 非名臣政策
    effect_desc        TEXT,
    effect_up_desc     TEXT,
    effect_down_desc   TEXT,
    descr              TEXT,
    desc_from          TEXT,
    star_cnt           INTEGER,
    cost_time          INTEGER,
    pre_list           INTEGER[] DEFAULT '{}',
    condition_effect   INTEGER,
    condition_values   DOUBLE PRECISION[] DEFAULT '{}',
    include_scope      TEXT[] NOT NULL DEFAULT '{}',     -- 分层标记, 绝不据此删行
    raw_json           JSONB NOT NULL
);
CREATE INDEX idx_policy_id            ON songci.policy (id);
CREATE INDEX idx_policy_minister      ON songci.policy (minister_id);
CREATE INDEX idx_policy_tt            ON songci.policy USING GIN (time_type_list);
CREATE INDEX idx_policy_scope         ON songci.policy USING GIN (include_scope);

-- 效果字典 (EffectTypeConfig.json) 639 行
CREATE TABLE songci.effect_type (
    effect_type   INTEGER PRIMARY KEY,
    effect_name   TEXT,
    add_desc      TEXT,
    reduce_desc   TEXT,
    add_desc_sp   TEXT,
    reduce_desc_sp TEXT,
    effect_desc   TEXT,
    character_type INTEGER,
    is_show       INTEGER,
    min_value     DOUBLE PRECISION,
    max_value     DOUBLE PRECISION,
    raw_json      JSONB NOT NULL
);

-- 名臣库 (MinisterBaseConfig.json) 317 行
CREATE TABLE songci.minister (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    minister_type   INTEGER,
    time_type_list  INTEGER[] NOT NULL DEFAULT '{}',
    policy_list     INTEGER[] NOT NULL DEFAULT '{}',     -- 存的是 policy.KeyID (实测 9016 次命中)
    start_year_list INTEGER[] DEFAULT '{}',
    end_year_list   INTEGER[] DEFAULT '{}',
    raw_json        JSONB NOT NULL
);
CREATE INDEX idx_minister_tt      ON songci.minister USING GIN (time_type_list);
CREATE INDEX idx_minister_policy  ON songci.minister USING GIN (policy_list);

-- ------------------------------------------------------------
-- 展开层: 规范化后的关系
-- ------------------------------------------------------------

-- 词句 -> 效果 (EffectTypeList / EffectParamList / EffectAddTypeList 一一对应)
CREATE TABLE songci.verse_effect (
    verse_id      INTEGER NOT NULL REFERENCES songci.verse(id) ON DELETE CASCADE,
    ordinal       SMALLINT NOT NULL,
    effect_type   INTEGER,          -- 可为 NULL: 该值其实是二级政策引用
    effect_value  DOUBLE PRECISION,
    add_type      SMALLINT,
    effect_name   TEXT,
    PRIMARY KEY (verse_id, ordinal)
);
CREATE INDEX idx_verse_effect_type ON songci.verse_effect (effect_type);

-- 词句 <-> 词人
CREATE TABLE songci.verse_poet_relation (
    verse_id INTEGER NOT NULL REFERENCES songci.verse(id) ON DELETE CASCADE,
    poet_id  INTEGER NOT NULL,
    PRIMARY KEY (verse_id, poet_id)
);

-- 词句解锁条件 (UnlockNeedMinister / UnlockNeedMinisterStar / UnlockNeedPolicyId)
CREATE TABLE songci.verse_unlock_requirement (
    verse_id     INTEGER NOT NULL REFERENCES songci.verse(id) ON DELETE CASCADE,
    ordinal      SMALLINT NOT NULL,
    minister_id  INTEGER,
    star         INTEGER,
    policy_id    INTEGER,
    is_special   BOOLEAN NOT NULL DEFAULT FALSE,   -- minister_id = 999 等特殊值
    PRIMARY KEY (verse_id, ordinal)
);

-- 政策 -> 效果 (已递归展开二级政策引用)
CREATE TABLE songci.policy_effect (
    policy_key_id    INTEGER NOT NULL REFERENCES songci.policy(key_id) ON DELETE CASCADE,
    source_policy_id INTEGER NOT NULL,   -- 效果实际来源政策 ID (可为被引用的子政策)
    effect_type      INTEGER,
    effect_value     DOUBLE PRECISION,
    add_type         SMALLINT,
    effect_name      TEXT,
    depth            SMALLINT NOT NULL DEFAULT 0,
    ordinal          SMALLINT NOT NULL,
    PRIMARY KEY (policy_key_id, source_policy_id, ordinal, depth)
);
CREATE INDEX idx_policy_effect_type ON songci.policy_effect (effect_type);
CREATE INDEX idx_policy_effect_src  ON songci.policy_effect (source_policy_id);

-- 政策 -> 被引用子政策 (审计用)
CREATE TABLE songci.policy_effect_ref (
    policy_key_id    INTEGER NOT NULL REFERENCES songci.policy(key_id) ON DELETE CASCADE,
    child_policy_id  INTEGER NOT NULL,
    depth            SMALLINT NOT NULL DEFAULT 1,
    PRIMARY KEY (policy_key_id, child_policy_id, depth)
);

-- 名臣 -> 政策 (按 KeyID 关联)
CREATE TABLE songci.minister_policy (
    minister_id   INTEGER NOT NULL REFERENCES songci.minister(id) ON DELETE CASCADE,
    policy_key_id INTEGER NOT NULL REFERENCES songci.policy(key_id) ON DELETE CASCADE,
    ordinal       SMALLINT NOT NULL,
    PRIMARY KEY (minister_id, policy_key_id)
);
CREATE INDEX idx_minister_policy_key ON songci.minister_policy (policy_key_id);

-- 词人 -> 效果政策
CREATE TABLE songci.poet_policy (
    poet_id       INTEGER NOT NULL REFERENCES songci.poet(id) ON DELETE CASCADE,
    policy_key_id INTEGER REFERENCES songci.policy(key_id) ON DELETE SET NULL,
    policy_id     INTEGER,
    resolved      BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (poet_id)
);

-- 唱词人 -> 效果池条目 (来自 CommonEffectPoolConfig 451-458/650/651 的效果行)
CREATE TABLE songci.singer_effect (
    singer_id     INTEGER NOT NULL REFERENCES songci.singer(id) ON DELETE CASCADE,
    pool_type     TEXT NOT NULL,        -- 'trigger' | 'remove'
    ordinal       SMALLINT NOT NULL,
    policy_key_id INTEGER REFERENCES songci.policy(key_id) ON DELETE SET NULL,
    policy_id     INTEGER,              -- 池里 1030「添加政策」引用的政策 ID
    effect_type   INTEGER,
    effect_value  DOUBLE PRECISION,
    add_type      SMALLINT,
    PRIMARY KEY (singer_id, pool_type, ordinal)
);

-- 唱词人效果规则 (权威口径 = singer.FeatureDesc 文字描述)。
-- 由 src/ingest.py 从 src/effects.py 的 SINGER_RULES 落库 —— 即「按 FeatureDesc
-- 逐字推导出的确定性规则」, 供 SQL 侧查询 (取代早期用 TriggerCommonEffectPoolConfigId
-- 关联到的占位军事政策)。
CREATE TABLE songci.singer_rule (
    singer_id        INTEGER PRIMARY KEY REFERENCES songci.singer(id) ON DELETE CASCADE,
    module           TEXT NOT NULL,      -- none|every_correct|on_style|on_poet_unlock|passive_bonus|su_shi
    trigger_desc     TEXT NOT NULL,      -- 触发时机 (中文)
    effect_text      TEXT NOT NULL,      -- 效果数值的可读文本
    effect_type      INTEGER,
    effect_value     DOUBLE PRECISION,
    style            SMALLINT,           -- 1 豪放 / 2 婉约 (on_style)
    bonus            DOUBLE PRECISION,   -- passive_bonus 的收益加成 (小数)
    sentiment_bonus  SMALLINT,           -- su_shi 的词情层数
    target_poet_id   INTEGER,            -- su_shi 的目标词人
    note             TEXT
);

-- 通用效果池 (CommonEffectPoolConfig.json, 747 条)
--   唱词人效果池 (451-458/650/651)、东坡食单 (652-660) 等都指向这张表。
--   注意: 这些 ID 与 PolicyConfig.ID **编号空间不同**, 早期按 PolicyConfig 解析
--   会命中同号的军事政策 (451=徵兵制), 得到完全错误的效果。
CREATE TABLE songci.common_effect_pool (
    id               INTEGER PRIMARY KEY,
    name             TEXT,
    condition_lib_id INTEGER,
    condition_desc   TEXT,
    trigger_target   SMALLINT,
    effect_desc      TEXT,
    raw_json         JSONB
);

CREATE TABLE songci.common_effect_pool_effect (
    pool_id      INTEGER NOT NULL REFERENCES songci.common_effect_pool(id) ON DELETE CASCADE,
    ordinal      SMALLINT NOT NULL,
    effect_type  INTEGER,
    effect_value DOUBLE PRECISION,
    add_type     SMALLINT,
    PRIMARY KEY (pool_id, ordinal)
);

-- 唱词人 -> 效果池 (Trigger/RemoveCommonEffectPoolConfigId 指向 common_effect_pool)
CREATE TABLE songci.singer_pool (
    singer_id INTEGER NOT NULL REFERENCES songci.singer(id) ON DELETE CASCADE,
    kind      TEXT NOT NULL,            -- 'trigger' | 'remove'
    pool_id   INTEGER REFERENCES songci.common_effect_pool(id) ON DELETE SET NULL,
    PRIMARY KEY (singer_id, kind)
);

-- 东坡食单 (DongPoFoodConfig.json, 9 道菜; 苏轼「老饕」政策开启)
CREATE TABLE songci.dongpo_food (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    ci_yuan_price   DOUBLE PRECISION,   -- CiYuanPrice = 词元价 (本模拟不收取, 见状态报告)
    pool_id         INTEGER REFERENCES songci.common_effect_pool(id) ON DELETE SET NULL,
    descr           TEXT,
    icon            TEXT,
    raw_json        JSONB
);

-- 本模拟「纳入范围」的登记表 (唯一来源 = src/effects.py), 供 SQL 侧筛选复用,
-- 避免把政策 ID 清单硬编码进视图。
--   kind = minister_policy        -> STARTUP_MINISTER_POLICY_IDS
--   kind = poet_draw_policy       -> POET_DRAW_POLICY_IDS 的取值
--   kind = minister_draw_policy   -> MINISTER_DRAW_POLICY_IDS 的取值
--   kind = singer_draw_policy     -> _SINGER_DRAW_POLICY_IDS 的取值
--   kind = pipeline_effect        -> PIPELINE_EFFECT_IDS (无数值语义, 展示需排除)
CREATE TABLE songci.model_scope (
    kind   TEXT NOT NULL,
    ref_id INTEGER NOT NULL,
    PRIMARY KEY (kind, ref_id)
);

-- 政策条件 (Condition_Effect / Condition_Values)
CREATE TABLE songci.policy_condition (
    policy_key_id     INTEGER NOT NULL REFERENCES songci.policy(key_id) ON DELETE CASCADE,
    condition_effect  INTEGER,
    condition_values  DOUBLE PRECISION[],
    ordinal           SMALLINT NOT NULL,
    PRIMARY KEY (policy_key_id, ordinal)
);
