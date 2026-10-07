# 宋词择律模拟 · SongCi Draw Simulator

《无悔华夏》宋词择律系统的**蒙特卡洛模拟器**：把游戏里"选题 → 择律 → 结算"的黑箱拆成
可复现的概率模型，跑成千上万局，输出统计报告与图表，用来比较不同开局配置的期望收益。

> **免责声明**：本项目是个人研究性质的概率模拟工具，**不包含任何游戏资源文件**
> （词句文本、政策表、数值表等一律需使用者自备，见"数据准备"）。
> 《无悔华夏》及其全部游戏数据的著作权归其开发/发行方所有；本项目与其无隶属关系，
> 代码仅用于学习与数值研究，请勿用于商业用途。

---

## 目录

- [它能做什么](#它能做什么)
- [环境依赖](#环境依赖)
- [数据准备（必读）](#数据准备必读)
- [建库与导入](#建库与导入)
- [运行](#运行)
- [数值口径](#数值口径)
- [目录结构](#目录结构)
- [已建模 / 未建模](#已建模--未建模)
- [License](#license)

---

## 它能做什么

| 模块 | 说明 |
|---|---|
| **开局配置** | 选唱词人、开局词人/名臣、东坡食单、初始歌板（可为负）/词元/词情/收益加成 |
| **筛选查询** | 词句、词人、名臣、唱词人、政策、效果全部规范化入库（21 张表 + 14 个视图），查询走 SQL |
| **模拟运行** | 逐次择律：加权抽取风格 → 抽词句（按风格各自不放回，抽干转放回）→ 结算词句/词人/名臣/唱词人效果 → 词情与触发判定 |
| **结果查看** | 每局独立目录：`summary.md`（9 个小节）+ `raw/*.csv`（单局明细、择律事件流）+ `charts/*.png` + `批量汇总.csv` + `配置快照.json` |

一次 10000 局的完整批跑（本机）约 1~2 分钟，输出可直接复现（同一 `--seed`）。

## 环境依赖

* **Python 3.9+**
* **PostgreSQL 13+**（开发环境：PostgreSQL 18）
* Python 包：见 [`requirements.txt`](requirements.txt)

  ```bash
  pip install -r requirements.txt
  ```

* GUI 需要 `tkinter`：Windows/macOS 自带；Debian/Ubuntu 需 `sudo apt install python3-tk`
* 图表中文：代码会自动探测 Windows / macOS / Linux 常见中文字体
  （SimHei、Microsoft YaHei、PingFang SC、Noto Sans CJK SC、文泉驿…）；
  若一个都没有，图仍会生成，但中文会显示为方框 —— 可 `apt install fonts-noto-cjk`

## 数据准备（必读）

模拟**不读游戏原始文件**，但建库（`src/ingest.py`）需要 8 个游戏配置 JSON，
它们**不在本仓库中**，需自行从游戏资源中导出，并保持顶层为 `{"dataList": [...]}`：

| 文件 | 必需内容 |
|---|---|
| `SongCiVerseConfig.json` | 词句库（词句、风格、效果、归属词人、解锁条件） |
| `SongCiPoetConfig.json` | 词人表 |
| `SongCiSingerConfig.json` | 唱词人表 |
| `DongPoFoodConfig.json` | 东坡食单 |
| `CommonEffectPoolConfig.json` | 通用效果池（唱词人效果、东坡食单、解锁奖励） |
| `PolicyConfig.json` | 政策表（含词人/名臣效果、政策星级） |
| `EffectTypeConfig.json` | 效果类型表（数值量纲、上限） |
| `MinisterBaseConfig.json` | 名臣表 |

放置位置（任选其一，代码会依次查找）：

1. `<项目根>/游戏数据/` —— 推荐，8 个文件放一起；
   或用环境变量指定：`SONGCI_GAME_DATA_DIR=D:\game_data`
2. `<项目根>/` —— 也可以直接把 JSON 摊在项目根目录

> 若只想跑模拟而**不需要重建数据库**（例如已有一份导入好的库），则完全不需要这些 JSON。
> 找得到 / 找不到都可以用 `python -c "import sys;sys.path.insert(0,'src');import config as C;print(C.VERSE_JSON)"` 查看实际解析到的路径。

## 建库与导入

1. 建库建用户（示例）：

   ```sql
   CREATE ROLE songci LOGIN PASSWORD 'your_password';
   CREATE DATABASE songci OWNER songci;
   ```

2. 配置连接（全部可用环境变量覆盖，仓库内**不含任何口令**）：

   | 变量 | 缺省 |
   |---|---|
   | `SONGCI_DB_HOST` | `127.0.0.1` |
   | `SONGCI_DB_PORT` | `5432` |
   | `SONGCI_DB_NAME` | `songci` |
   | `SONGCI_DB_USER` | `songci` |
   | `SONGCI_DB_PASSWORD` | 空 |
   | `SONGCI_PROJECT_ROOT` | `src/` 的上一级 |
   | `SONGCI_GAME_DATA_DIR` | `<项目根>/游戏数据` |

   本机开发可把口令写进 `<项目根>/.db_password`（已在 `.gitignore` 中）。

3. 建表 + 导入 + 校验（可重复执行，幂等）：

   ```bash
   python src/ingest.py            # 建 schema/表 → 全量导入 → 校验
   python src/ingest.py --verify   # 只校验
   ```

   导入会建出 `songci` schema、21 张表；随后可执行视图自检：

   ```bash
   python src/views_check.py       # 建 14 个视图并输出口径对照
   ```

## 运行

**GUI（推荐）**

```bash
python src/gui.py
```

四个标签页：开局配置 / 筛选查询 / 模拟运行 / 结果查看。
勾选名臣会同时列出其效果链；负的初始歌板表示"开局倒欠，靠收益填坑"。

**命令行**

```bash
# 用配置文件（GUI 里可导出，含全部选项）
python src/run_simulation.py --config 配置_示例.json

# 或直接给参数
python src/run_simulation.py --singer 8 --poets 1 3 10 21 --ministers 300 314 327 331 \
       --board -20 --runs 10000 --seed 20261005 --out 输出结果
```

常用参数：`--runs`（默认 1000）、`--seed`、`--out`、
`--board`（初始歌板，可为负）、`--verse-point`、`--benefit-bonus`（百分比）、
`--dongpo`（东坡食单 1-9，需同时选名臣 328）、`--bold-sentiment`、`--graceful-sentiment`、
`--cost`、`--threshold`、`--sentiment-cap`、`--unlock-all`。完整列表见 `--help`。

**输出结构**

```
输出结果/<时间戳>_<配置名>/
├── summary.md            # 9 个小节: 开局配置/择律次数/各属性/词情与触发/风格/终止原因/分支触发率/图表/异常
├── raw/单局明细.csv       # 每局一行（属性列为"波动"口径）
├── raw/择律事件流.csv     # 随机 10 局的完整逐次流程（可用配置种子复现）
│                        #   末列「歌板变动来源」逐笔标注每点歌板是谁给的,
│                        #   如 `词句67(关山魂梦长)+2; 名臣柳永·恋情词3+4; 婉约词情触发+2`
├── charts/*.png          # 择律次数分布、各属性分布
├── 批量汇总.csv           # 指标级汇总（均值/中位/分位数/极值）
└── 配置快照.json          # 完整配置 + 轻量统计，可再次作为 --config 输入
```

## 数值口径

理解报告前务必看这几条（都是与游戏实测/作者裁定对齐的口径，不是笔误）：

* **属性"波动"**：`波动 = 每局终值 − 该局开局一次性政策结算后（第一次择律前）的值`，
  即**纯择律带来的变化**。**例外：歌板报终值**（它是择律的资源与终止条件）。
* **初始属性**：民心 / 威望 / 军心 / 腐化 默认 **50**，设定值域 **0~100**；
  但词句对这四项的增减益**不受值域约束** —— 结算中可突破（民心能到几千、腐化能为负）。
* **词情**：抽到**本局首次**出现的词条时，对应风格词情 +1（重复抽取与放回阶段不再叠加）；
  触发概率 = `floor(层数 / 10) × 10%`，上限由 `--sentiment-cap`（默认 100）控制。
* **择律收益加成**：`1 + 加成` **只放大词句自身的收益**（实机口径）；
  来自唱词人 / 词人 / 名臣的政策规则收益**不吃加成**。只放大正向；
  **所有由择律产生的绝对值（歌板 / 词元 / 民心 / 威望 / 军心 / 腐化 …）一律「取绝对值向下取整」
  并保留符号**（如 `2 歌板 × 3.95 = 7.9 → 7`）。因此事件流里的每次变动都是整数。
  百分比效果（`add_type=2`，如战斗力+8%）不属于"绝对值"，走乘算通道，但乘算结果同样取整。
* **抽取**：按风格各自"不放回"；某风格词句抽干后转为"放回"抽取，此时词情不再叠加。
* **事件流**：只记录**随机 10 局**的完整流程（由配置种子派生，可复现），不设条数上限。

## 目录结构

```
.
├── src/
│   ├── config.py          # 路径 / 数据库 / 效果 ID 语义表
│   ├── db.py              # 数据库访问层（线程内复用连接）
│   ├── model.py           # 数据类与配置（SimulationConfig / TurnEvent / SimulationResult）
│   ├── effects.py         # 从库装配效果：政策规则、词人/名臣/唱词人 mods、词句池
│   ├── simulator.py       # 模拟内核（run_once / run_batch）
│   ├── stats.py           # 统计口径（分位数、波动、分支触发率、异常检测）
│   ├── charts.py          # 图表（matplotlib，缺失数据优雅跳过）
│   ├── report.py          # summary.md / CSV / 配置快照
│   ├── display.py         # 数值展示单位（发展年数等）
│   ├── ingest.py          # 原始 JSON → PostgreSQL 全量导入 + 校验
│   ├── schema.sql         # 21 张表
│   ├── views.sql          # 14 个只读视图（筛选口径）
│   ├── views_check.py     # 视图自检
│   ├── run_simulation.py  # 命令行入口
│   └── gui.py             # tkinter GUI
├── requirements.txt
├── LICENSE
└── README.md
```

## 已建模 / 未建模

**已建模**：唱词人风格权重与逐次效果；词人开局效果与局中解锁（解锁当轮立即结算词情）；
名臣开局政策链；东坡食单（9/9 满汉全席额外效果）；词句按风格不放回抽取与放回降级；
词情层数与触发；择律收益加成（示儿累加 / 声声慢阶梯）；政策"同名只保留最高星级"；
"每次择律"型政策的触发门控（豪放/婉约/词元阈值/一次性上限）；歌板消耗与阈值终止。

**明确未建模**（数据不足或属其它系统，勿据此推演）：

* 军团规模、苏轼「文书双绝 解锁苏轸」等跨系统效果；
* 张孝祥的词人效果条件、欧阳修「会盟/科举」；
* 岳飞「铁马冰河」、辛弃疾「美芹十论」等未收录政策；
* 需要"特殊名臣 999"解锁的词句（导入时已剔除）；
* 「择律错误」分支（本模拟默认每次择律都是正确选择）。

> 开发用的测试套件、文档与一键工具不随本仓库发布，本仓库只含 `src/`。

## License

[MIT](LICENSE)。游戏数据与游戏内容的一切权利归原权利方所有。
