# fix: 择律收益加成只放大 7 种效果；并让「词牌/擅长词牌」进入模拟

## 这个 PR 在改什么

三处数值口径 + 一条缺失的抽取规则，另附两个**不依赖 PostgreSQL** 的复核脚本。

| # | 文件 | 改动 | 影响 |
|---|---|---|---|
| 1 | `src/simulator.py` `src/game_rules.py` | 「后续择律收益提升」(32513) **只放大 7 种效果类型** | 高加成下**战斗力虚高约 3 倍**（28.2 → 7.8） |
| 2 | `src/effects.py` | 唱词人 9（苏轸）歌板 **4 → 3** | 与效果池 650 → 政策 40014 的 `EffectValueList` 一致 |
| 3 | `src/effects.py` `src/simulator.py` `src/model.py` | 新增 `VerseRow.ci_pai_name` 并实现**擅长词牌筛池** | 补上 README「未建模」里的 C-2；改变抽取分布 |
| 4 | `src/game_rules.py`（新） | 规则层：触发时机取 `PolicyConfig.TriggerTime`、条件文案解码、解锁条件解析 | 取代"从 FeatureDesc 正则猜" |
| 5 | `tools/*`（新） | 数据搬运脚本、免数据库运行脚本、蒙特卡洛对比 | 解决"仓库没有数据、跑不起来" |

> **没有**改「答错」分支 —— 见文末「刻意不改的东西」。

---

## 1. 收益加成：`1 + 加成` 只作用于 7 种效果

README「数值口径」写的是「**只放大词句自身的收益**」。方向对，但**范围不对**：
真实规则是一张白名单，只有下面 7 种吃倍率，其余原样结算。

| 效果类型 | 属性 | 效果类型 | 属性 |
|---|---|---|---|
| 51 | 民心 | 553 | 威望 |
| 209 | 军心 | 32510 | 歌板 |
| 351 | 腐化 | 32514 | 词元 |
| 552 | 文化点 | | |

合成方式是 **`最终值 = 基础值 × 倍率`**（乘法，倍率初值 1.0），不是累加；
而战斗力、发展年数、乐感、词情、诗意、灵犀等**都不吃**这个倍率。

**受影响最大的就是战斗力**（不在白名单）。用本仓库自己的内核实测
（`--benefit 2.0`，即倍率 3.0，500 局）：

| 属性 | 修正前 | 修正后 |
|---|---|---|
| **战斗力** | **28.19** | **7.82** |
| 词元 / 民心 / 军心 / 威望 / 腐化 / 歌板 | 138.42 / 52.23 / 62.94 / 54.49 / 46.94 / 3.01 | **完全相同** |

后一行是关键对照：改动只命中了该改的效果类型，其余纹丝不动。

代码改动（`src/simulator.py`）：
* 热路径 `verse.attr_effects` 循环加一个属性判断（`REWARD_MULTIPLY_ATTRS`）；
* 慢路径 `_apply_effects()` 增加 `whitelist_only` 参数，按 effect_type 判断
  —— **词情(32515/32516) 走的是慢路径**（它们被 `EFFECT_VALUE_ATTR.pop` 排除了），
  所以这里也必须拦，否则词情仍会被放大。

`config.reward_multiply_whitelist = False` 可一键回到旧口径做对比。

## 2. 唱词人 9（苏轸）：歌板 +4 → +3

`SINGER_RULES[9]` 的 `effect_value` 抄的是 `FeatureDesc` 文案「歌板+4」，
但效果池 650 → 政策 40014 的实际 `EffectValueList` 是 `[3.0, 1.0, 1.0]`
（歌板 +3、豪放/婉约词情各 +1），另有 40036 给词元 +2。**以效果数据为准。**
（`game_rules.singer_effect_value()` 可自动取回该值，避免以后再手抄。）

## 3. 「词牌」此前根本进不了模拟（README C-2 的根因）

`ingest.py` 与 `schema.sql` 都存了 `ci_pai_name`，但**模拟侧的
`SELECT id, ci_name, style FROM verse` 没取它**，`VerseRow` 也没有这个字段 ——
所以「擅长词牌」这条规则**无法实现**，不只是"没用到"。

本 PR 补上字段，并实现筛池规则：

```
正确词句池 = { 已解锁且未收集的词句 v :
      含「全部」 ∨ v.Style == 抽到的律 ∨ v.CiPaiName ∈ 擅长词牌 }
若过滤后少于 OptionCount(3) 条 -> 回退全量
```

注意两点，都和直觉相反：

* 这是 **OR**，所以擅长词牌是**跨风格放宽**（唱词人能在别的律下唱自己擅长的词牌），
  不是"只抽擅长词牌"；
* 唱词人 2「铁衣」的擅长词牌是 `全部` ⇒ **连风格都不筛**。

实测（`--singer 9`，800 局）：军心 53.85 → 54.90、歌板 2.27 → 1.94、平均题数 4.01 → 4.00。
`config.specialty_pool_rule = False` 可关闭。

## 4. 唱词人效果的权威语义（解决 `effects.py` 头部那段自述）

原文写「Trigger/Remove 池指向军事政策，是占位数据（8/9 位唱词人的 FeatureDesc 与
30000-30007 政策描述不匹配）」。实际情况：

* 池不是占位数据：链路是 `SingerConfig.TriggerCommonEffectPoolConfigId` → 效果池 →
  `1030 AddPolicy` → 政策；
* **8/9 位唱词人的 `FeatureDesc` 与「效果池的 `EffectDesc`」逐字一致**（不是与政策描述比）；
* 唯一不一致的是唱词人 2「铁衣」↔ 政策 **30001**：那条 `EffectDesc`
  「必定唱出词句，但后续择律收益-10%」**全表孤例**且没有配套的 32513 负值效果，
  而它的 `TriggerTime=81` + `EffectList=[32510]=1.0` 正是
  「每次择律正确→歌板+1」→ 判定为**策划文案残留，功能正确**；
* 唱词人 6 的 +10% **挂在池 456 上**（政策 30005 的 `EffectList` 为空是对的）。

新增的 `src/game_rules.py` 把这个链路与触发类型
（77 每次 / 78 豪放正确 / 79 婉约正确 / 80 错误 / 81 正确 / 82 解锁新词人 /
101 正确择某词人词）做成纯函数，附 24 条单测（不连库）。

`ConditionLibConfig.ConditionDesc` 是**异或编码**（先 base64，再按 UTF-8 密钥循环异或），
`game_rules.decrypt_condition_desc()` 可直接解出唱词人解锁条件等文案，
配合 `parse_unlock_condition()` 就能做解锁门控与提示文案。

## 5. 让仓库跑得起来（数据搬运 + 免数据库运行）

* `tools/convert_game_configs.py`：把 8 张配置表（`SongCiVerseConfig` /
  `SongCiPoetConfig` / `SongCiSingerConfig` / `PolicyConfig` / `EffectTypeConfig` /
  `MinisterBaseConfig` / `DongPoFoodConfig` / `CommonEffectPoolConfig`）
  按本项目契约（`{"dataList": [...]}`）落到 `游戏数据/`，并校验行数区间。
  **不含任何游戏数据**，只做格式搬运 —— 使用者需自备导出结果。
* `tools/run_without_db.py`：直接构造 `effects.GameData` 调 `simulator.run_once`，
  **不需要 PostgreSQL**，用于复核本次改动（`--legacy` 一键切回旧口径）。
* `tools/mc_compare.py`：纯规则层的蒙特卡洛对比（可选）。

---

## 怎么复核

```sh
# 1) 准备数据
python tools/convert_game_configs.py --raw-dir <配置 JSON 目录> --out 游戏数据

# 2) 规则层单测（不连库、不需要游戏数据）
python -m unittest discover -s tests -v      # 24 passed

# 3) 用本仓库自己的内核跑 before/after（不需要 PostgreSQL）
python tools/run_without_db.py --runs 500 --benefit 2.0            # 修正后
python tools/run_without_db.py --runs 500 --benefit 2.0 --legacy   # 修正前
```

第 3 步的期望输出（差值只出现在战斗力）：

```
口径=修正(fixed)   combat_power 7.82
口径=旧(legacy)    combat_power 28.19
```

另外，第 1 条规则在游戏内也很好验：堆高「后续择律收益提升」后，
**战斗力不会随倍率变化**，而歌板/词元/民心/军心/腐化/威望/文化点会。

---

## 刻意**不改**的东西（避免误伤）

1. **不新增「答错」分支。** 3 个候选里必有正解（答对才结束本次择律）、
   且每首词都有准确答案，所以"每次择律都正确"是**结构性成立**的前提 ——
   本项目的假设没错，不需要动。
   因此 `SINGER_RULES[1]` 的 `module="none"` 也是**对的**（该效果恒不触发），
   本 PR 只补了一段注释说明理由，没有改行为。
   （`game_rules.replay_cost()` / `escalate_ticket()` 仍然保留：玩家**主动重听**
   时要知道花多少歌板，这部分是真实成本。）
2. **不动「每笔向 0 截断」与 `add_type=2` 复利口径** —— 那是既有裁定；
   若之后要复核，建议用 `config` 开关而不是直接改。
3. **不动星级去重与名臣政策关联**（`effects.py:1105-1120` 已注释依据；且本版数据里
   `UnlockNeedMinisterStar` 全为 0、`UnlockNeedPolicyId` 全空，不影响结果）。

## 为什么不顺手改既有注释

`src/config.py`、`src/ingest.py` 里关于数据目录与顶层键的描述保持原样，
避免这个 PR 混入与主题无关的措辞改动。
