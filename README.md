# backendworker

数据导入端（data importer）：把 `data-collector` 采集清洗后的数据同步进 **Cloudflare D1**，
并维护 **FTS5** 全文索引。设计文档见 [`DESIGN.md`](./DESIGN.md)。

```
processeddata/*.csv|*.json  ──►  backendworker  ──►  Cloudflare D1
                                  元数据同步 / diff 导入 / FTS5 / sync_state
```

> 职责边界：本程序只做「文件 → 数据库」的导入链路。搜索 / 列表等查询 API 属于后续独立项目。

---

## 1. 安装

```bash
uv sync
```

依赖：`httpx`（D1 HTTP API）、`jieba`（中文分词）。Python ≥ 3.14。

## 2. 配置

| 环境变量 | 必需 | 说明 |
|---|---|---|
| `CLOUDFLARE_API_TOKEN` | ✅ | Cloudflare API Token，需 **D1 编辑权限**；Phase 2 还需 R2 读写权限 |
| `CLOUDFLARE_ACCOUNT_ID` | 兜底 | 缺省时自动从 `R2keys.conf` 的 `ACCOUNT_ID` 读取 |
| `CLOUDFLARE_D1_DATABASE_ID` | 可选 | 默认 `f42c3293-63b2-4ca3-a3b4-942a1d7fb926`（`datacabsite`） |
| `BACKENDWORKER_DATA_DIR` | 可选 | 默认 `<项目根>/processeddata` |

Windows 临时设置方式（PowerShell）：

```powershell
$env:CLOUDFLARE_API_TOKEN = "xxxxx"
```

## 3. 使用

```bash
# 首次运行：建表（库为空时执行一次即可）
uv run python -m backendworker.importer --init-ddl

# 常规运行：全量/增量导入
uv run python -m backendworker.importer

# 只跑干跑校验：不写库、不改名，仅本地计算 diff 与分词
uv run python -m backendworker.importer --dry-run

# 只处理个别站点
uv run python -m backendworker.importer --only beijing,shanghai

# 只同步 market / category 元数据
uv run python -m backendworker.importer --sync-meta-only
```

常用参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--data-dir` | `processeddata/` | 数据目录 |
| `--batch-size` | `200` | 单个 HTTP 请求携带的 SQL 语句数 |
| `--batch-delay` | `0.2` | 批间延迟（秒），避免触发 D1 限流 |
| `--init-ddl` | off | 执行 `tables.sql` 建表（已存在的表跳过） |
| `--storage` | `local` | 数据源：`local`=本地目录；`r2`=直接读写 Cloudflare R2（生产用） |
| `--dry-run` | off | 只计算不写库 |
| `--rebuild-stats` | off | 强制重算 `stats` 计数表；单独运行时不跑导入 |
| `-v/--verbose` | off | DEBUG 日志 |

## 4. 运行流程

1. 读 `product_files.json`，与 D1 `sync_state` 表比对 `upload_at` → 得出待处理文件列表；**为空则直接退出**（空跑保护）
2. 每次运行都同步 `market.json` / `category.json`（UPSERT；json 中不存在的条目保留不动）
3. 逐站点处理 `{market_id}_new.csv`：
   - 无 `{market_id}_old.csv` → 全部视为新增
   - 否则按 `raw_data_key` diff：新增 INSERT / 变更 UPDATE / 下架 DELETE
   - 与写库同批维护 `product_fts`（`rowid = product.product_id`）
   - **全程零回查**：不做 `(market_id, raw_data_key)` 存在性校验；
     新增时 product 的 INSERT 与 FTS 的 INSERT 相邻放同一批，FTS 用 `last_insert_rowid()` 取 rowid；
     变更/下架用子查询就地取 `product_id`
4. **全部写库成功后**才更新 `sync_state`，再把 `_new.csv` 改名为 `_old.csv`
5. 本轮有商品变动时，整体重算 `stats` 计数表（`total` / `market` / `category` 三类）

崩溃一致性：改名严格在写库成功之后。中途崩溃 → 文件未改名 → 下次重跑按同样 diff 重算。
注意：**重跑会重复插入**（没有唯一约束也不做存在性校验），少量重复需另行脚本清理。

## 5. 模块划分

```
src/backendworker/
├── config.py        # D1 database_id / account_id / token / 路径 / 批次参数
├── storage.py       # Storage 协议 + LocalStorage（Phase 2 增 R2Storage）
├── d1_client.py     # D1 HTTP API：多语句批量执行、字面量转义、退避重试、逐语句降级
├── tokenizer.py     # jieba 分词、词性过滤、tags 拆分
├── meta_sync.py     # market.json / category.json → UPSERT
├── product_sync.py  # diff 计算 + 增/改/删 + FTS5 维护
├── state.py         # sync_state 读写、待处理文件判定
├── stats.py         # 预计算计数表 stats 的整体重算
└── importer.py      # __main__ 编排入口 + 运行摘要
```

表结构 DDL 见 [`tables.sql`](./tables.sql)。

> 注意：`CREATE VIRTUAL TABLE IF NOT EXISTS` 不会修改已存在的表结构。
> 若此前已按旧版（4 列）建过 `product_fts`，需先 `DROP TABLE product_fts;` 再重新执行 `tables.sql`
> 重建索引表，之后重跑一次导入即可补齐索引。

> **`product.category_id` 故意不建外键**：它是 `||` 拼接的多值列，而 SQLite 外键比的是整个字段值，
> `"finance||construction"` 在 `category` 主键里必然找不到，建了会让所有多值商品写入失败
> （实测首次导入 7/9 站点全挂）。`market_id` 是单值，外键保留。

> **product 表不建 UNIQUE 约束、不建 `category_id` 索引**：D1 的「已写入行数」把索引写入也计入，
> 多一个索引就多算一次写入。导入端也**不做存在性校验**，新增一律纯 INSERT
> （少量重复另行脚本清理），写入代价压到最低。唯一保留的索引是 `idx_product_market`——
> 变更/下架按 `(market_id, raw_data_key)` 定位的语句依赖它，去掉会退化成全表扫描。

## 6. 分词约定（导入端与查询端必须一致）

- `title` / `description` / `source`：`jieba.posseg` 分词，仅保留名词 `n*`、动词 `v*`、形容词 `a*`、英文 `eng`；虚词与单字丢弃
- `tags`：按 `||` 拆分，**每段整体作为一个关键词**，同时对段内文本再分词一并写入
  （保证搜「医疗」能命中标签「医疗健康」）
- `category_id`：英文编码，按 `||` 拆开后直接空格拼接，**不分词**
- 分词结果以空格拼接为一条字符串存入 `product_fts`
- 查询侧（Phase 3）对用户输入做同样分词，词间空格连接后 `MATCH`（默认 AND 语义）

## 7. stats 计数表

列表页要显示「共 N 件」并算总页数。若每次请求都 `COUNT(*)` 就是一次全表扫——
20 万行时一次首屏读 20 万行，而 D1 免费额度 500 万行读/天只够约 24 次首屏。
`stats` 表存预计算值，首屏降到几十行读。

| scope | stat_key | 含义 |
|---|---|---|
| `total` | `''` | 全表商品数 |
| `market` | `market_id` | 各交易所商品数 |
| `category` | `category_id` | 各分类商品数（商品可属多个分类，各类之和 > 总数属正常） |

**导入端负责维护**：每轮有商品变动时整体重算一次（`DELETE` 三个 scope + 重新 `UPSERT`，约 45 行写入）；
无变动则跳过。也可 `uv run ... --rebuild-stats` 单独重算。

分类计数走 **`product_fts` 的倒排索引**：

```sql
SELECT 'healthcare' AS stat_key, COUNT(*) FROM product_fts
WHERE product_fts MATCH 'category_id:healthcare'
-- 35 个编码用 UNION ALL 拼成一条语句，一次请求拿全
```

比 `LIKE '%healthcare%'` 全表扫 ×35 便宜得多。正确性已核验：unicode61 会把下划线拆开
（`ai_service` → `ai` `service`，MATCH 默认 AND），35 个编码两两之间**不存在**
「某编码 token 集合被另一编码覆盖」的情况，因此不会串类。
若 `product_fts` 缺 `category_id` 列会自动回退到 `LIKE`（也已核验编码间无子串关系）。

## 8. 定时运行（GitHub Actions）

`.github/workflows/import.yml`，每天**北京时间 03:00** 自动跑导入
（Actions 的 cron 按 UTC 解析：UTC+8 → 前一天 19:00 UTC）。

要点：

- **生产环境 Storage 走 `r2` 模式**（workflow 里 `BACKENDWORKER_STORAGE: r2`）：
  CSV / JSON 由程序直接读写 R2，`_new.csv → _old.csv` 的改名也发生在 R2 里。
  所以 runner **不需要任何本地快照**，没有下载/上传步骤、不需要缓存。
  `LocalStorage` 仅用于本地开发调试（`--storage local`，默认）。
- 手动触发（Actions 页面 → Run workflow）可勾选 `--init-ddl`（建表）或 `--rebuild-stats`（重算计数）。
- 需要配置的 Secrets：`CLOUDFLARE_API_TOKEN`、`CLOUDFLARE_ACCOUNT_ID`、`CLOUDFLARE_D1_DATABASE_ID`、
  `R2_ACCESS_KEY_ID`、`R2_SECRET_ACCESS_KEY`、`R2_BUCKET`、`R2_ENDPOINT_URL`。
- 采集端必须先把新快照传到 R2（更新 `product_files.json` 的 `upload_at`），
  否则 job 会因「无文件更新」空跑退出——这是预期行为。

## 9. 后续

- Storage 已对接 R2（`R2Storage`，rename = copy + delete）；本地调试用 `LocalStorage`
- Phase 3：Workers 查询 API（搜索 / 列表 / 详情，遵循 §6 分词约定）
