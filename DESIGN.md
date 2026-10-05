# backendworker 数据导入程序设计

> 版本：v1（设计评审稿）　日期：2026-10-04
> 本文档为开发前设计确认稿，确认后按此实现。

---

## 1. 定位与总体架构

本程序是**数据导入端**（data importer）：把 data-collector 采集清洗后的数据（当前在本地 `processeddata/` 目录，后期迁移 Cloudflare R2）同步进 Cloudflare D1 数据库，并维护全文索引。

```
data-collector 产出（csv / json）
        │
        ▼
┌─────────────────────────────┐
│  本程序（本地 Python / uv）   │   ◄── 后期由 GitHub Actions 定时触发
│  1. 同步 market/category     │
│  2. 逐站点 diff 导入 product │
│  3. 维护 FTS5 全文索引       │
└─────────────────────────────┘
        │  D1 HTTP API（远程写入）
        ▼
┌─────────────────────────────┐
│  Cloudflare D1 (SQLite)     │
│  market / category / product│
│  product_fts (FTS5)         │
│  sync_state                 │
└─────────────────────────────┘
```

**职责边界**：本程序只负责"文件 → 数据库"的导入链路；搜索/列表等查询 API 属于后续独立项目。

## 2. 运行环境与技术选型

| 项 | 选择 | 说明 |
|---|---|---|
| 语言/运行时 | Python ≥ 3.14，uv 管理依赖 | 沿用现有 `pyproject.toml` |
| 新增依赖 | `httpx`（D1 HTTP API）、`jieba`（中文分词） | 均为纯 Python，无平台问题 |
| 数据库 | Cloudflare D1，`database_name="datacabsite"`，`database_id="f42c3293-63b2-4ca3-a3b4-942a1d7fb926"` | 通过 **HTTP API** 远程写入，无需本地 SQLite |
| 文件存储 | Phase 1：本地 `processeddata/`；Phase 2：R2 | 代码抽象为 Storage 接口，切换不改业务逻辑 |
| 定时运行 | Phase 1：手动；Phase 2：GitHub Actions | 见第 8 节 |

## 3. 数据源与文件约定

### 3.1 文件清单与编码（已实测核验）

- 9 个站点 CSV：`{market_id}_new.csv`，共约 42,500 行（广东已达 2 万上限）
- `market.json`、`category.json`：元数据，结构为 `{updated_at, upload_at, data: [...]}`
- `product_files.json`：各 CSV 的上传时间戳清单，作为**触发信号**
- 编码：全部合法 UTF-8；**CSV 带 BOM**（读取用 `utf-8-sig`），JSON 不带 BOM
- CSV 列名与 product 表字段一一对应：`raw_data_key, title, description, tags, market_id, category_id, source, detail_url`
- `tags`、`category_id` 为多值列，`||` 分隔；`category_id` 存编码值（如 `ai_service`）

### 3.2 Storage 抽象

```text
Storage 协议: list_files() / read_csv(name) / read_json(name) / rename(src, dst) / delete(name)
├── LocalStorage   # Phase 1：本地目录，直接文件操作
└── R2Storage      # Phase 2：R2 binding 或 S3 API；rename 语义 = copy + delete
```

## 4. 数据库设计（对 tables.sql 的变更）

### 4.1 product 表：不建唯一约束、不建多余索引

```sql
CREATE TABLE product (
  product_id  INTEGER PRIMARY KEY AUTOINCREMENT,
  raw_data_key TEXT NOT NULL,
  title       TEXT NOT NULL,
  description TEXT,
  tags        TEXT,
  market_id   TEXT NOT NULL REFERENCES market(market_id),
  category_id TEXT NOT NULL,                -- ★ 不加外键，见下方说明
  source      TEXT,
  detail_url  TEXT,
  update_at   TEXT                          -- ★ 本行最后一次插入/更新的时间，导入端写入
  -- ★ 没有 UNIQUE(market_id, raw_data_key)，也没有 category_id 索引
);

-- 唯一保留的索引：导入端所有定位语句都是 WHERE market_id=? AND raw_data_key IN (...)
CREATE INDEX IF NOT EXISTS idx_product_market ON product(market_id);
```

**为什么要去掉（用户在 D1 控制台实测后提出）**：D1「使用情况」里的**已写入行数包含索引写入**，
每多一个索引，同一行数据的写入量就被重复计一次。因此：

| 去掉的 | 原因 |
|---|---|
| `UNIQUE (market_id, raw_data_key)` | 唯一约束会隐式建索引；去重改由导入端在写库前查存在性保证（§6.3） |
| `idx_product_category` | `category_id` 是 `\|\|` 多值串，B-tree 索引对多值匹配无效；分类检索走 `product_fts.category_id` 全文索引 |

保留 `idx_product_market`：导入端每个批次的回查（`WHERE market_id=? AND raw_data_key IN (...)`）
都依赖它，没有会退化成全表扫描。

> **更正（实测后修订）**：`category_id` 是 `||` 拼接的多值列（实测 7/9 站点存在多值行，
> 最多 12 个编码拼在一起），而 SQLite 外键比的是**整个字段值**，
> `"finance||construction"` 在 `category` 主键里必然找不到 → 所有多值商品写入全部失败
> （实测首次导入 7 个站点直接 FOREIGN KEY constraint failed）。
> 因此该列**不建外键**；`market_id` 是单值，外键保留。

market、category 表结构不变。

### 4.2 全文索引表：product_fts（FTS5）

```sql
CREATE VIRTUAL TABLE product_fts USING fts5(
  title, description, source, tags, category_id
);
-- 约定：rowid = product.product_id
```

- `category_id` 同样进索引：该列是英文编码（可能 `||` 分隔多个值），
  **只需按 `||` 拆开后空格拼接，不再调用分词**

> 变更提示：`CREATE VIRTUAL TABLE IF NOT EXISTS` 不会修改已存在的表结构。
> 若此前已按旧版（4 列）建过 `product_fts`，需先 `DROP TABLE product_fts;` 再执行本文件重建。

- 独立 FTS5 表（非 external content），列中存**分词后空格拼接的文本**，而非原文
- 存储代价约为原文本 1.5~2 倍，当前数据量（21MB）可忽略
- 用独立表而非触发器的理由：触发器只能复制原文，而我们需要写入"分词后"的文本
- 删除按 rowid 直接 `DELETE`，无需旧值，维护简单

### 4.3 运行状态表：sync_state（新增）

```sql
CREATE TABLE sync_state (
  file_name    TEXT PRIMARY KEY,   -- 如 "beijing_new.csv"
  upload_at    TEXT NOT NULL,      -- product_files.json 中记录的上传时间
  processed_at TEXT NOT NULL       -- 本程序处理完成时间
);
```

作用：GitHub Actions / 任何无状态环境重跑时，靠它判断哪些文件自上次处理后有更新（比对 `upload_at`）。状态存在 D1 而不是本地文件，是刻意的——CI 环境没有可靠的本地状态。

### 4.4 计数表：stats（新增，用户补的表 + 本程序负责维护）

```sql
CREATE TABLE stats (
  scope      TEXT    NOT NULL,      -- 'total' / 'market' / 'category'
  stat_key   TEXT    NOT NULL,      -- '' / market_id / category_id
  cnt        INTEGER NOT NULL,
  updated_at TEXT,
  PRIMARY KEY (scope, stat_key)
);
```

**为什么需要**：列表页要显示「共 N 件」并算总页数。若每次请求都 `COUNT(*)`，
等于把整张 product 扫一遍——20 万行时一次首屏读 20 万行，
而 D1 免费额度 500 万行读/天，一天只够约 24 次首屏。读预计算值后首屏只剩几十行读。

**代价**：导入端必须在写完商品后维护它，否则计数会过期。
采用**整体重算**（`DELETE` 三个 scope + 重新 UPSERT 约 45 行），比增量维护简单且不易漏。

**分类计数走 product_fts 倒排索引**（`category_id` 是多值拼接列，B-tree 索引帮不上忙）：

```sql
SELECT 'healthcare' AS stat_key, COUNT(*) FROM product_fts
WHERE product_fts MATCH 'category_id:healthcare'
-- 35 个编码用 UNION ALL 拼成一条语句，一次请求全部取回
```

比 `LIKE '%healthcare%'` × 35 次全表扫便宜得多。正确性核验（已跑）：
- unicode61 会把 `_` 当分隔符，`ai_service` → `ai` `service`，MATCH 默认 AND 语义；
- 35 个编码两两之间**不存在**「某编码 token 集合被另一编码覆盖」的情况 → 不会串类；
- `product_fts` 缺 `category_id` 列时自动回退 `LIKE`（也已核验编码间无子串关系）。

## 5. 处理流程（pipeline）

```
main()
 ├─ 1. 初始化：加载配置、Storage、D1 客户端、jieba
 ├─ 2. 读 product_files.json，与 sync_state 比对 upload_at
 │      → 得出"待处理文件列表"；为空则直接退出（空跑保护）
 ├─ 3. 同步元数据（每次运行都做，仅几十行，代价可忽略）
 │      ├─ market.json  → UPSERT market 表（按 market_id）
 │      └─ category.json → UPSERT category 表（按 category_id）
 │      规则：json 中存在 → 插入/更新；json 中不存在 → 保留不动（product 外键保护）
 ├─ 4. 逐站点处理（每个待处理 csv）：
 │      a. 读 {market_id}_new.csv（utf-8-sig），行级校验（见 §7）
 │      b. 若无 {market_id}_old.csv → 全部视为新增
 │         否则与 _old.csv 按 raw_data_key diff：
 │           · new 有、old 无        → 新增（INSERT）
 │           · 两者都有但字段有变化  → 变更（UPDATE，保持 product_id 稳定）
 │           · old 有、new 无        → 下架（DELETE）
 │      c. 执行写库 + 维护 product_fts（见 §6.3）
 │      d. **全部成功后**：更新 sync_state，再把 _new.csv 改名为 _old.csv
 ├─ 5. 若本轮有商品变动 → 整体重算 stats 计数表（见 §4.4）
 └─ 6. 输出运行摘要（每站点：新增/变更/下架/跳过条数 + stats 写入行数）
```

**变更检测**：对交集内的行逐列比较除 `raw_data_key` 外的 7 个字段，任一不同即视为变更。不做哈希压缩，行数不大，直接字段比较更直白、无哈希碰撞顾虑。

**崩溃一致性**：文件改名严格放在"该站点全部写库成功"之后。中途崩溃 → 文件未改名 → 下次重跑按同样 diff 重新计算；配合「先查存在性再写」的幂等写（§6.4），重跑无副作用。

## 6. 关键设计细节

### 6.1 分词规则（jieba，本地执行）

| 字段 | 处理方式 |
|---|---|
| title / description / source | `jieba.posseg` 分词，**词性过滤**：仅保留名词（n/nz/ng/ns/nt）、动词（v/vn）、形容词（a/ad/an）等实词；"的、了、与、和"等虚词及单字停用词丢弃（jieba 自带能力，不引入外部停用词表） |
| tags | 按 `\|\|` 拆分为独立标签段；**每段整体作为一个关键词**，同时对段内文本再分词一并写入。理由：用户搜"医疗"应能命中标签"医疗健康"，只有整段 token 会漏召回 |
| category_id | 按 `\|\|` 拆开后**直接空格拼接，不分词**。理由：分类编码本身已是英文单词/词组（`ai_service`），是最小语义单位，再分词只会把它拆碎；unicode61 会把下划线当分隔符（`ai_service` → `ai` `service`），查询侧同样处理，两边一致即可命中 |

分词结果以空格拼接为一条字符串，作为 FTS5 列的存储内容。

### 6.2 查询侧约定（供后续查询 API 遵循）

查询时对用户输入做同样分词，词间以空格连接后 `MATCH`（默认即词间 AND 语义）。此约定写入文档以便查询 API 与导入端保持一致。

### 6.3 FTS5 维护（与 product 写入同批提交）

| 操作 | product 表 | product_fts 表 |
|---|---|---|
| 新增 | 纯 `INSERT`（不做存在性校验） | 与 product 的 INSERT **相邻放同一批**，用 `last_insert_rowid()` 当 rowid |
| 新增对应的 FTS | `INSERT INTO product_fts (rowid, ...) VALUES (last_insert_rowid(), <分词>)` | — |
| 变更 | `UPDATE product SET ... WHERE market_id=? AND raw_data_key=?` | 整批 `DELETE FROM product_fts WHERE rowid IN (SELECT product_id FROM product WHERE ...)`，再逐行 `INSERT INTO product_fts (rowid, ...) SELECT product_id, <分词> FROM product WHERE ...` |
| 下架 | `DELETE FROM product WHERE market_id=? AND raw_data_key=?` | `DELETE FROM product_fts WHERE rowid IN (SELECT product_id FROM product WHERE ...)`（先删 FTS，此时 product 行还在） |

> **没有任何 (market_id, raw_data_key) 存在性校验**（用户确认：少量重复另行写脚本清理）。
> 没有 UNIQUE 约束也不能写 `ON CONFLICT(market_id, raw_data_key)` 冲突目标
> （SQLite 会报 "does not match any PRIMARY KEY or UNIQUE constraint"）。

**product_id 的获取：零回查。**

- 新增：product 的 INSERT 与对应 FTS 的 INSERT 相邻放在**同一个批处理脚本**里，
  SQLite 顺序执行，FTS 那行的 `last_insert_rowid()` 就是刚插入的 `product_id`。
  ⚠️ 这一对语句**不能被拆到两个 HTTP 请求**（连接变了取值就错），
  因此 `product_sync._chunk_pairs()` 按「语句对」切批，并且一旦 D1 降级成逐语句模式，
  `_apply_added` 直接抛错中止而不是写坏索引。
- 变更 / 下架：rowid 用子查询 `SELECT product_id FROM product WHERE ...` 就地取，
  不产生额外的 HTTP 往返（SQLite 内部会读，变更/下架是小集合，可接受）。

对比此前「每批回查 id」的方案：广东 2 万行 × 100 批 ≈ 200 万行读取 → 现在新增路径**零读取**。

### 6.4 D1 写入策略

- 端点：`POST /accounts/{account_id}/d1/database/{database_id}/query`，Bearer Token 认证
- **批量**：单次 HTTP 请求携带多条 SQL 语句（严格转义字符串字面量），每批约 200~500 行；参数化单语句 + 100 绑定参数上限的路径太慢，不用。实现时先以小批量实测多语句支持度，若有问题降级为 `wrangler d1 execute --file` 子进程方案（备选，同样可靠）
- **幂等**：新增走「先查存在性再 INSERT/UPDATE」；FTS 写入前先按 rowid 删除。任何一批失败重试或整体重跑均不产生脏数据
- **限流**：429/5xx 指数退避重试（上限 5 次），批间小延迟

### 6.5 触发判断

待处理文件的判定 = `product_files.json` 中该文件的 `upload_at` ≠ `sync_state` 中记录值。首次运行（sync_state 为空）视为全部待处理，即完成首次全量导入。

## 7. 数据校验与防御

| 场景 | 处理 |
|---|---|
| CSV 用 utf-8-sig 读取 | 消除 BOM，否则首列名带 `\ufeff` 对不上 |
| 行内必填字段缺失（raw_data_key / title / market_id / category_id） | 跳过该行，记 warning 日志，计入"跳过"统计 |
| market_id 不在 market.json 中 | 跳过并告警（外键也会拦，提前拦给出更友好的日志） |
| category_id 多值中含未知编码 | 记 warning，不阻断（该列**不建外键**，多值拼接串不会触发约束） |
| _new.csv 无有效行而 _old.csv 有数据 | 视为异常状态（采集端未上传新快照），**跳过整个站点不写库**，避免 diff 把整站商品判成「下架」全删 |
| _new.csv 内部 raw_data_key 重复 | 取最后一条，记 warning |
| 空文件 / 全空 data 数组 | 正常跳过 |

## 8. 部署与定时运行

### Phase 1（本次开发）：本地手动

```bash
uv run python -m backendworker.importer
```

功能全量（含 FTS、diff、sync_state），通过 D1 HTTP API 写远端真实库——本地运行≠本地库。

### Phase 2：GitHub Actions 定时（已落地）

配置文件：`.github/workflows/import.yml`，每天**北京时间 03:00** 运行
（Actions 的 cron 按 UTC 解析，UTC+8 → 前一天 19:00 UTC，故 `cron: "0 19 * * *"`）。

```yaml
# 要点
on:
  schedule: [{ cron: "0 19 * * *" }]   # 北京时间每天 03:00
  workflow_dispatch:                    # 手动触发，可勾选 --init-ddl / --rebuild-stats
concurrency:
  group: data-import
  cancel-in-progress: false             # 排队而非打断，避免并发写库
steps:
  - checkout / setup-uv / uv sync
  - uv run python -m backendworker.importer 2>&1 | tee import.log
  - import.log 写入 $GITHUB_STEP_SUMMARY + 上传 artifact
```

**Storage 在生产环境走 `r2` 模式**（`BACKENDWORKER_STORAGE=r2`）：

CSV / JSON 全部由程序通过 `R2Storage` 直接读写 R2，`_new.csv → _old.csv` 的**改名也发生在 R2 里**
（`rename()` 实现为 CopyObject + DeleteObject）。因此 runner 不需要任何本地快照，
**没有下载/上传步骤，也不需要缓存**——这是 Storage 抽象存在的意义：
业务流程完全不感知文件在哪，切换 `local` / `r2` 只影响 `--storage` 参数和环境变量。
`LocalStorage` 仅用于本地开发调试。

> `rename()` 在对象存储上并非原子操作，但这不影响崩溃安全性：
> 改名严格发生在「该站点全部写库成功」之后，中途崩溃时文件仍叫 `_new.csv`，
> 下次重跑会按同样的 diff 重新计算（`sync_state` 在 D1 里，天然跨轮保留）。

注意事项：

- Actions 的 `schedule` 实际触发常有几分钟级延迟，属正常现象，不影响正确性
  （触发判断基于 `upload_at` 比对，不依赖精确时刻）
- **数据采集端必须在本任务之前把新快照传到 R2**，否则 job 跑完无事可做
  （`upload_at` 未变化 → 空跑保护退出）
- 需要的 Secrets：`CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ACCOUNT_ID` /
  `CLOUDFLARE_D1_DATABASE_ID` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` /
  `R2_BUCKET` / `R2_ENDPOINT_URL`

## 9. 模块划分

```
src/backendworker/
├── config.py        # D1 database_id、account_id、token（环境变量）、路径等
├── storage.py       # Storage 协议 + LocalStorage；Phase 2 增 R2Storage
├── d1_client.py     # D1 HTTP API 客户端：多语句批量执行、转义、退避重试
├── tokenizer.py     # jieba 分词、词性过滤、tags 拆分
├── meta_sync.py     # market.json / category.json → UPSERT
├── product_sync.py  # diff 计算 + 增/改/删 + FTS5 维护
├── state.py         # sync_state 读写、待处理文件判定
├── stats.py         # 统计计数表 stats 的整体重算（含 FTS 分类计数）
└── importer.py      # __main__ 编排入口 + 运行摘要
```

表结构 DDL 集中放在 `tables.sql`（更新为 §4 版本），初始化时由程序执行或手动经 wrangler 执行一次。

## 10. 实施阶段与边界

| 阶段 | 内容 | 不包含 |
|---|---|---|
| Phase 1（本次） | 本地运行完整导入链路：元数据同步、diff 导入、FTS5、sync_state、校验与日志 | 查询 API、R2、Actions |
| Phase 2 | Storage 切 R2 + GitHub Actions 定时部署 | — |
| Phase 3（独立项目） | Workers 查询 API（搜索/列表/详情，遵循 §6.2 分词约定） | — |

## 11. 本设计中已确认的决策记录

1. 只做导入端，查询 API 后续另行设计（用户已确认）
2. 全文索引用 D1 FTS5，放弃 Workers KV 倒排方案（用户已确认）
3. diff 保留 `_new.csv` vs `_old.csv` 文件比对，改名在写库成功后进行（用户已确认）
4. 库当前为空，product 表直接按新结构建表；**不建 UNIQUE 约束、不建 category_id 索引**
   （用户依据 D1 控制台「已写入行数含索引写入」的实测结论确认，去重改由导入端查存在性保证）
5. 定时运行采用 GitHub Actions 方案（用户已确认）
6. `market.json` 此前疑似乱码经二进制校验为**误报**（head -c 字节截断所致），全部数据文件为合法 UTF-8，无需修复
