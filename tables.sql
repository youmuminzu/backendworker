-- Cloudflare D1 表结构（DESIGN.md §4 版本）
--
-- Phase 1：本地运行 `uv run python -m backendworker.importer --init-ddl`
--          或手动 `wrangler d1 execute datacabsite --file=tables.sql` 执行一次。
--
-- 说明：库首次初始化时全部按本文件建表。market / category 无历史数据，
--      product 直接按含 UNIQUE(market_id, raw_data_key) 的新结构创建，无需迁移。
-- 注意：注释一律用 `--`，不要用 `#`（`#` 不是 SQL 注释，会让整个脚本报语法错误）。

CREATE TABLE IF NOT EXISTS market (
    market_id   TEXT PRIMARY KEY,       -- 交易所英文短名，如 beijing
    name        TEXT NOT NULL,          -- 交易所全称
    short_name  TEXT NOT NULL           -- 简称/展示名
);

CREATE TABLE IF NOT EXISTS category (
    category_id TEXT PRIMARY KEY,       -- 分类编码，如 ai_service
    name        TEXT NOT NULL,          -- 分类中文名
    sort_order  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS product (
    product_id  INTEGER PRIMARY KEY AUTOINCREMENT,                  -- 自增主键
    raw_data_key TEXT NOT NULL,                                     -- 交易所网站里的主键
    title       TEXT NOT NULL,                                      -- 商品名称/标题
    description TEXT,                                               -- 商品描述
    tags        TEXT,                                               -- 卡片展示 tag，多个用 || 隔开
    market_id   TEXT NOT NULL REFERENCES market(market_id),          -- 所属交易所（单值，外键有效）
    -- 分类，多个用 || 隔开。**故意不加外键**：这是反范式拼接列，
    -- SQLite 外键比的是整个字段值，"finance||construction" 在 category 主键里必然找不到，
    -- 加了外键会让所有多值商品写入失败（实测 7/9 站点全挂）。
    category_id TEXT NOT NULL,
    source      TEXT,                                               -- 数源机构
    detail_url  TEXT,                                               -- 详情页地址
    update_at   TEXT                                                -- 本行最后一次插入/更新的时间（导入端写入，YYYY-MM-DD HH:MM:SS）
    --   去重改由导入端在写入前先查 (market_id, raw_data_key) 是否存在来保证（见 product_sync）。
);

-- 只保留 market_id 索引：导入端所有定位语句都是 WHERE market_id=? AND raw_data_key IN (...)，
-- 没有它会退化成全表扫描。
CREATE INDEX IF NOT EXISTS idx_product_market ON product(market_id);

-- 不建 category_id 索引：该列是 || 拼接的多值串，B-tree 索引对多值前缀匹配无效；
-- 分类检索走 product_fts 的 category_id 全文索引。

-- 全文索引：独立 FTS5 表（非 external content），列中存「分词后空格拼接」的文本。
-- 约定：rowid = product.product_id
-- category_id 为英文编码，按 || 拆分后直接空格拼接，不再走 jieba 分词。
-- 注意：列已存在时 CREATE VIRTUAL TABLE IF NOT EXISTS 不会修改表结构，
--      若此前已按旧版建过表，请先 DROP TABLE product_fts 再执行本文件重建。
CREATE VIRTUAL TABLE IF NOT EXISTS product_fts USING fts5(
    title,
    description,
    source,
    tags,
    category_id
);

-- 运行状态：记录每个已处理文件的 upload_at，使无状态环境（GitHub Actions）能判断是否重跑
CREATE TABLE IF NOT EXISTS sync_state (
    file_name    TEXT PRIMARY KEY,       -- 如 beijing_new.csv
    upload_at    TEXT NOT NULL,          -- product_files.json 中记录的上传时间
    processed_at TEXT NOT NULL           -- 本程序处理完成时间
);

-- ==========================================================
-- stats —— 预计算的计数表，让列表页免掉 count(*) 的全表扫
--
-- 为什么需要它：分页要显示「共 N 件」并算总页数。若每次请求都跑 count(*)，
-- 就等于把整张 product 表扫一遍 —— 20 万行数据时一次首屏读 20 万行，
-- 而 D1 免费额度是 500 万行读/天，一天只够约 24 次首屏。
-- 改读这里的预计算值后，首屏只剩几十行读，且不再随商品增长而变差。
--
--    导入脚本**必须在写入商品后维护本表**，否则页面计数会过期。
--    推荐直接整体重算（即下面三条语句），比增量维护简单且不易漏。
--
-- scope 取值：
--   'total'     stat_key = ''            全表商品数（首页那种无筛选的列表）
--   'market'    stat_key = market_id     各交易所商品数
--   'category'  stat_key = category_id   各类商品数
-- ==========================================================
CREATE TABLE stats (
  scope      TEXT    NOT NULL,
  stat_key   TEXT    NOT NULL,
  cnt        INTEGER NOT NULL,
  updated_at TEXT,
  PRIMARY KEY (scope, stat_key)
);