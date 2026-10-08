"""维护预计算计数表 `stats`（tables.sql 末尾那张表）。

为什么要这张表：列表页要显示「共 N 件」并算总页数。
若每次请求都 `SELECT COUNT(*)`，等于把整张 product 扫一遍——20 万行数据时一次首屏读 20 万行，
而 D1 免费额度是 500 万行读/天，一天只够约 24 次首屏。
改成读这里的预计算值后，首屏只剩几十行读，且不再随商品量增长而变差。

代价：**导入端必须在写完商品后维护它**，否则页面计数会过期。
这里采用「整体重算」而非增量维护——按 tables.sql 里的建议，简单且不易漏。

三类 scope：
    total      stat_key=''           全表商品数
    market     stat_key=market_id    各交易所商品数
    category   stat_key=category_id  各分类商品数（商品可属多个分类，故各类之和 > 总数属正常）

【平台约束·勿改】D1 把 SQLITE_LIMIT_COMPOUND_SELECT 降到了 5
    workerd 在 src/workerd/util/sqlite.c++ 的 setupSecurity() 里按
    sqlite.org/security.html 的「高安全值」下调了一组运行时上限：
        SQLITE_LIMIT_COMPOUND_SELECT = 5   （本地 SQLite 默认 500）
        SQLITE_LIMIT_EXPR_DEPTH      = 100
        SQLITE_LIMIT_VARIABLE_NUMBER = 100
        SQLITE_LIMIT_ATTACHED        = 0
        SQLITE_LIMIT_LIKE_PATTERN_LENGTH = 50
    D1 官方 limits 页面并未列出这一项，只能从 workerd 源码确认。

    因此「把 N 个分类 UNION ALL 串成一条 SELECT」在本地能跑、在 D1 必失败：
        too many terms in compound SELECT（D1 code 7500）
    该限制约束的是**一条复合语句里的 SELECT 项数**，与语句条数无关——
    所以解法是拆成 N 条独立语句一次发出，而不是分批 UNION（分批只是把
    报错阈值从 35 挪到 5，并没有解决问题）。详见 `_category_counts`。
"""

from __future__ import annotations

import logging
from typing import Sequence

from .d1_client import D1Client, D1Error, QuotaExceeded, chunks, quote
from .state import now_str
from .tokenizer import TAG_SEPARATOR

log = logging.getLogger(__name__)

SCOPE_TOTAL = "total"
SCOPE_MARKET = "market"
SCOPE_CATEGORY = "category"
# 重建时覆盖的三个 scope；表里有其他 scope 的话由后续接入方自行维护
ALL_SCOPES = (SCOPE_TOTAL, SCOPE_MARKET, SCOPE_CATEGORY)


def rebuild(client: D1Client, category_ids: Sequence[str]) -> int:
    """整体重算 stats 表，返回写入的行数。

    `category_ids` 传当前 category.json 里的编码列表（即要统计哪些分类）。
    """
    if client.cfg.dry_run:
        log.info("[dry-run] 跳过 stats 重算")
        return 0

    rows: list[tuple[str, str, int]] = []

    # 1) 全表总数
    total = client.query("SELECT COUNT(*) AS cnt FROM product")
    rows.append((SCOPE_TOTAL, "", int(total[0]["cnt"]) if total else 0))

    # 2) 各交易所商品数
    market_rows = client.query(
        "SELECT market_id, COUNT(*) AS cnt FROM product GROUP BY market_id"
    )
    for row in market_rows:
        rows.append((SCOPE_MARKET, str(row["market_id"]), int(row["cnt"])))

    # 3) 各分类商品数 —— 走 product_fts 的 category_id 全文索引
    rows.extend(_category_counts(client, list(category_ids)))

    return _write_rows(client, rows)


def _category_counts(client: D1Client, category_ids: list[str]) -> list[tuple[str, str, int]]:
    """用 FTS5 倒排索引统计每个分类的商品数。

    为什么能用：product_fts 的 category_id 列存的是「按 || 拆开后空格拼接」的编码，
    FTS5 建立了倒排索引，`MATCH` 查某个编码比 `LIKE '%code%'` 全表扫便宜得多。

    **每个分类一条独立 SELECT，合成一个多语句脚本一次发出** ——
    不要改成 `UNION ALL` 把 N 个分类串成一条：那会撞上 D1 的
    SQLITE_LIMIT_COMPOUND_SELECT（见 `_category_counts` 下方说明）。
    N 条语句各自是单 SELECT，命中数在 D1 侧聚合完再回传，
    本地只收到 N 行，且不把命中行拉到 Python 内存里分桶。

    为什么不用「一条 MATCH 带 N 个 OR」：那样确实也能绕开 compound select，
    但 FTS5 命中的是全部商品（每行都带至少一个分类），20 万行会全量回传，
    响应体十几 MB，还得在本地分桶——比逐个 COUNT(*) 贵得多。

    正确性核验（已跑过）：
      - unicode61 会把下划线当分隔符，`ai_service` 被拆成 `ai` `service` 两个 token；
        MATCH 默认是词间 AND，所以查询 `ai_service` 等价于要求两个 token 同时存在。
      - 已确认 35 个编码两两之间**没有**「某编码 token 集合被另一编码覆盖」的情况，
        因此不会出现「查 A 分类却命中了只属于 B 分类的商品」。
    """
    if not category_ids:
        return []

    statements = [
        f"SELECT {quote(code)} AS stat_key, COUNT(*) AS cnt "
        f"FROM product_fts WHERE product_fts MATCH {quote(f'category_id:{code}')}"
        for code in category_ids
    ]

    try:
        rows: list[dict] = []
        for part in chunks(statements, client.cfg.batch_size):
            rows.extend(client.query_script(part))
            client.pace()
    except QuotaExceeded:
        # 配额耗尽不是 FTS 的问题，回退成 LIKE 全扫描只会白烧 35 次读取配额，
        # 而且结果一样拿不到。直接向上抛，让上层整轮中止。
        raise
    except D1Error as exc:
        log.warning("FTS 分类计数失败（%s），回退为 product 表 LIKE 全扫描", exc)
        rows = _category_counts_by_like(client, category_ids)

    return [(SCOPE_CATEGORY, str(row["stat_key"]), int(row["cnt"])) for row in rows]


def _category_counts_by_like(client: D1Client, category_ids: list[str]) -> list[dict]:
    """兜底：直接对 product.category_id 做 LIKE。

    适用于 product_fts 尚未包含 category_id 列的旧库。
    已确认 35 个编码两两之间不存在子串关系，`%code%` 不会误命中别的分类。
    """
    results: list[dict] = []
    for code in category_ids:
        rows = client.query(
            f"SELECT {quote(code)} AS stat_key, COUNT(*) AS cnt "
            f"FROM product WHERE category_id LIKE {quote(f'%{code}%')}"
        )
        results.extend(rows)
    return results


def _write_rows(client: D1Client, rows: list[tuple[str, str, int]]) -> int:
    """先清空再由主键 UPSERT 写回。"""
    if not rows:
        return 0
    scopes = ", ".join(quote(s) for s in ALL_SCOPES)
    client.query(f"DELETE FROM stats WHERE scope IN ({scopes})")
    client.pace()

    updated_at = quote(now_str())
    statements = [
        "INSERT INTO stats (scope, stat_key, cnt, updated_at) "
        f"VALUES ({quote(scope)}, {quote(key)}, {int(cnt)}, {updated_at}) "
        "ON CONFLICT(scope, stat_key) DO UPDATE SET "
        "cnt=excluded.cnt, updated_at=excluded.updated_at"
        for scope, key, cnt in rows
    ]
    for batch in chunks(statements, client.cfg.batch_size):
        client.execute(batch)
        client.pace()

    log.info("stats 重算完成：共 %d 行（其中分类 %d 个）",
             len(rows), sum(1 for s, _, _ in rows if s == SCOPE_CATEGORY))
    return len(rows)
