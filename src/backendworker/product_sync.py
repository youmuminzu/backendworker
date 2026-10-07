"""product 表 diff 导入 + FTS5 索引维护。

DESIGN.md §5 步骤 4 / §6.3 / §7：

    _new.csv ──┐
              ├─ diff ─→ 新增(INSERT) / 变更(UPDATE) / 下架(DELETE)
    _old.csv ──┘              │
                              └→ 同步维护 product_fts（rowid = product_id）

要点：
- 无 _old.csv 时全部视为新增（首次导入）
- 变更检测：逐列比较除 raw_data_key 外的 7 个字段，任一不同即视为变更
- 自增 id 获取：新增时用 `last_insert_rowid()` 就地写入 FTS；变更/下架用子查询取（见 ProductSyncer）
- 新增走 UPSERT（依赖 product 表的 UNIQUE(market_id, raw_data_key)），
  批次中途失败后重跑收敛为「更新为最新值」，不会插出重复行
- 导入端维护 `update_at` 列：INSERT / UPDATE 时写入当前时间。
  它不来自 CSV，因此不参与 diff 比较（COMPARE_FIELDS 只覆盖 CSV 列）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import NamedTuple, Sequence

from .d1_client import D1Client, chunks, quote, quote_or_null
from .meta_sync import MetaCatalog
from .state import now_str
from .storage import Storage
from .tokenizer import build_fts_row

log = logging.getLogger(__name__)

FIELDS = (
    "raw_data_key",
    "title",
    "description",
    "tags",
    "market_id",
    "category_id",
    "source",
    "detail_url",
)
# 变更检测比较除主键外的所有字段（DESIGN §5）
COMPARE_FIELDS = FIELDS[1:]
REQUIRED_FIELDS = ("raw_data_key", "title", "market_id", "category_id")
TAG_SEPARATOR = "||"
# 导入端维护的列：不在 CSV 里，也不参与 diff 比较
DERIVED_COLUMN = "update_at"


class ProductRow(NamedTuple):
    """一条商品记录，字段顺序与 CSV 表头一致。"""

    raw_data_key: str
    title: str
    description: str
    tags: str
    market_id: str
    category_id: str
    source: str
    detail_url: str

    @property
    def comparable(self) -> tuple[str, ...]:
        return tuple(self.__getattribute__(f) for f in COMPARE_FIELDS)


@dataclass(slots=True)
class SyncStats:
    """单个站点的运行统计。"""

    market_id: str = ""
    total_rows: int = 0
    added: int = 0
    changed: int = 0
    removed: int = 0
    unchanged: int = 0
    skipped: int = 0
    warnings: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.added + self.changed + self.removed + self.unchanged


def read_rows(storage: Storage, file_name: str, stats: SyncStats) -> dict[str, ProductRow]:
    """读取 CSV → {raw_data_key: ProductRow}，同时完成去重与校验前的清洗。"""
    raw = storage.read_csv(file_name)
    if not raw:
        log.info("%s 为空文件（0 行数据）", file_name)
        return {}

    rows: dict[str, ProductRow] = {}
    for index, item in enumerate(raw, start=2):  # 第 1 行是表头
        values = {name: str(item.get(name) or "").strip() for name in FIELDS}
        key = values["raw_data_key"]
        if not key:
            stats.skipped += 1
            stats.warnings += 1
            log.warning("%s 第 %d 行缺少 raw_data_key，跳过", file_name, index)
            continue
        if key in rows:
            stats.skipped += 1
            stats.warnings += 1
            log.warning("%s 第 %d 行 raw_data_key 重复（%s），取最后一条", file_name, index, key)
        rows[key] = ProductRow(**values)
    return rows


def validate_rows(
    rows: dict[str, ProductRow],
    catalog: MetaCatalog,
    file_name: str,
    stats: SyncStats,
) -> dict[str, ProductRow]:
    """行级校验（DESIGN §7），返回有效行集合。"""
    valid: dict[str, ProductRow] = {}
    unknown_category_reported: set[str] = set()

    for key, row in rows.items():
        missing = [f for f in REQUIRED_FIELDS if not getattr(row, f)]
        if missing:
            stats.skipped += 1
            stats.warnings += 1
            log.warning("%s：%s 缺少必填字段 %s，跳过该行", file_name, key, "、".join(missing))
            continue

        if row.market_id not in catalog.market_ids:
            stats.skipped += 1
            stats.warnings += 1
            log.warning("%s：%s 的 market_id=%s 不在 market.json 中，跳过该行",
                        file_name, key, row.market_id)
            continue

        for code in row.category_id.split(TAG_SEPARATOR):
            code = code.strip()
            if code and code not in catalog.category_ids and code not in unknown_category_reported:
                unknown_category_reported.add(code)
                stats.warnings += 1
                log.warning("%s：%s 含未知分类编码 %s（不阻断，D1 外键不校验拼接串内部）",
                            file_name, key, code)

        valid[key] = row
    return valid


def diff_rows(new_rows: dict[str, ProductRow], old_rows: dict[str, ProductRow]):
    """_new vs _old → (added, changed, removed_keys, unchanged_count)。"""
    if not old_rows:
        return list(new_rows.values()), [], [], 0

    added = [row for key, row in new_rows.items() if key not in old_rows]
    removed = [old_rows[key] for key in old_rows.keys() - new_rows.keys()]
    changed = [
        row
        for key, row in new_rows.items()
        if key in old_rows and old_rows[key].comparable != row.comparable
    ]
    unchanged = sum(1 for key, row in new_rows.items() if key in old_rows and old_rows[key].comparable == row.comparable)
    return added, changed, removed, unchanged


# ------------------------------------------------------------------ 写库


class ProductSyncer:
    """负责把单个站点的 diff 结果落到 D1（product + product_fts）。

    关键设计：**全程零回查**。不做 (market_id, raw_data_key) 存在性校验，
    新增直接 UPSERT —— 靠 product 表的 UNIQUE(market_id, raw_data_key) 判重，
    冲突时覆盖为最新值。这让「批次中途失败 → 次日整站重跑」天然幂等：
    已提交的行被更新，未提交的行被插入，最终状态与一次成功写入完全一致。

    FTS 的 rowid 怎么拿到 product_id：
        新增   → product 的 UPSERT 与对应 FTS 的 INSERT 相邻放在同一个批处理脚本里，
                  FTS 里写 `last_insert_rowid()`，SQLite 顺序执行，取到的就是刚写入那行的 id。
        变更/下架 → `INSERT INTO product_fts(rowid, ...) SELECT product_id, ... FROM product WHERE ...`
                  / `DELETE FROM product_fts WHERE rowid IN (SELECT product_id FROM product WHERE ...)`
    """

    def __init__(self, client: D1Client) -> None:
        self.client = client
        self.cfg = client.cfg

    # ---------- 主入口

    def process_market(
        self,
        *,
        storage: Storage,
        entry,  # state.FileEntry
        catalog: MetaCatalog,
        dry_run: bool = False,
    ) -> SyncStats:
        market_id = entry.market_id
        stats = SyncStats(market_id=market_id)
        log.info("── 开始处理站点 %s（%s）", market_id, entry.file_name)

        new_rows = validate_rows(read_rows(storage, entry.file_name, stats), catalog, entry.file_name, stats)
        stats.total_rows = len(new_rows)

        old_rows: dict[str, ProductRow] = {}
        if storage.exists(entry.old_name):
            old_rows = validate_rows(read_rows(storage, entry.old_name, stats), catalog, entry.old_name, stats)
            log.info("%s：new %d 行 / old %d 行", market_id, len(new_rows), len(old_rows))
        else:
            log.info("%s：无 %s，全部按新增处理", market_id, entry.old_name)

        # 空跑保护：_new.csv 缺失/无有效行，而 _old.csv 有数据 ——
        # 这是「采集端还没上传新一轮快照」的异常状态，不是「商品全部下架」。
        # 若照 diff 逻辑执行，会把整个站点的商品全删掉，必须拦下。
        if not new_rows and old_rows:
            msg = (
                f"{market_id}：{entry.file_name} 无有效数据行，但 {entry.old_name} 有 "
                f"{len(old_rows)} 行 —— 按异常状态处理，不执行删除，不改名"
            )
            log.error(msg)
            stats.errors.append(msg)
            return stats

        added, changed, removed, unchanged = diff_rows(new_rows, old_rows)
        stats.added, stats.changed, stats.removed, stats.unchanged = len(added), len(changed), len(removed), unchanged

        if dry_run or self.cfg.dry_run:
            log.info("[dry-run] %s：新增 %d / 变更 %d / 下架 %d / 未变 %d / 跳过 %d",
                     market_id, stats.added, stats.changed, stats.removed, stats.unchanged, stats.skipped)
            return stats

        try:
            self._apply_added(added, market_id)
            self._apply_changed(changed, market_id)
            self._apply_removed(removed, market_id)
        except Exception as exc:  # 任一环节失败 → 抛给上层，阻止改名
            stats.errors.append(str(exc))
            log.error("%s 写库失败：%s", market_id, exc)
            raise

        log.info("%s 写库完成：新增 %d / 变更 %d / 下架 %d / 未变 %d / 跳过 %d",
                 market_id, stats.added, stats.changed, stats.removed, stats.unchanged, stats.skipped)
        return stats

    # ---------- 新增

    def _apply_added(self, rows: Sequence[ProductRow], market_id: str) -> None:
        """UPSERT + 重建 FTS。

        FTS 那行用子查询按 (market_id, raw_data_key) 取 product_id，
        而不是 `last_insert_rowid()` —— 走 UPSERT 的 DO UPDATE 分支时
        last_insert_rowid() 不会更新（它返回的是上一条 INSERT 的 rowid），
        重跑时会把 FTS 写到别的行上。

        **每行写入前必须先删掉对应的 FTS 记录**：FTS5 对同 rowid 重复 INSERT
        会报 constraint failed。首次导入时 product 行是新的、删不到东西（无害）；
        但配额中断后重跑时 product 行已存在，FTS 里也已有它，不先删就会失败。
        这与 `_apply_changed` 用同一套「先删后插」逻辑。

        附带好处：product 的 UPSERT 与 FTS 的写入不再必须相邻同批，
        逐语句降级模式下也能正确工作，所以这里不再需要
        `multi_statement_enabled` 的前置检查。

        `market_id` 仅为与 `_apply_changed` / `_apply_removed` 签名一致而保留，
        实际每行的 market_id 都取自 CSV 自身。
        """
        if not rows:
            return
        statements: list[str] = []
        for row in rows:
            statements.append(self._insert_sql(row))
            statements.append(self._fts_delete_sql(row.market_id, [row.raw_data_key]))
            statements.append(self._fts_insert_select_sql(row))
        for part in self.client.chunk_by_payload(statements):
            self.client.execute(part)
            self.client.pace()

    @staticmethod
    def _insert_sql(row: ProductRow) -> str:
        """UPSERT：依赖 product 表的 UNIQUE(market_id, raw_data_key)。

        为什么新增也走 UPSERT 而不是纯 INSERT：
        批次中途失败时，站点不改名、不写 sync_state，次日会拿同一份 _new.csv
        整站重跑，而 CSV diff 仍会把这些行判为「新增」。纯 INSERT 会把上一轮
        已落库的行再插一遍，重复数据每失败一轮就累积一批。UPSERT 让重跑收敛。

        冲突时覆盖除 raw_data_key / market_id 外的全部 CSV 列 + update_at，
        语义等价于「这行以本轮 CSV 为准」。
        """
        assignments = ", ".join(
            f"{name}=excluded.{name}" for name in COMPARE_FIELDS
        )
        return (
            "INSERT INTO product (raw_data_key, title, description, tags, market_id, "
            f"category_id, source, detail_url, {DERIVED_COLUMN}) VALUES ("
            f"{quote(row.raw_data_key)}, {quote(row.title)}, {quote_or_null(row.description)}, "
            f"{quote_or_null(row.tags)}, {quote(row.market_id)}, {quote(row.category_id)}, "
            f"{quote_or_null(row.source)}, {quote_or_null(row.detail_url)}, "
            f"{quote(now_str())}) "
            "ON CONFLICT(market_id, raw_data_key) DO UPDATE SET "
            f"{assignments}, {DERIVED_COLUMN}=excluded.{DERIVED_COLUMN}"
        )

    @staticmethod
    def _update_sql(row: ProductRow) -> str:
        """UPDATE 时刷新 `update_at`；它不来自 CSV，所以不在 COMPARE_FIELDS 里参与 diff 比较。"""
        assignments = ", ".join(
            f"{name}={quote_or_null(getattr(row, name))}" for name in COMPARE_FIELDS
        )
        return (
            f"UPDATE product SET {assignments}, {DERIVED_COLUMN}={quote(now_str())} "
            f"WHERE market_id={quote(row.market_id)} AND raw_data_key={quote(row.raw_data_key)}"
        )

    # ---------- 变更

    def _apply_changed(self, rows: Sequence[ProductRow], market_id: str) -> None:
        if not rows:
            return
        for batch in chunks(rows, self.cfg.batch_size):
            statements = [self._update_sql(row) for row in batch]
            # 整批先清一次旧 FTS 记录，再逐行用子查询取 product_id 写回新分词
            statements.append(self._fts_delete_sql(market_id, [row.raw_data_key for row in batch]))
            statements.extend(self._fts_insert_select_sql(row) for row in batch)
            for part in self.client.chunk_by_payload(statements):
                self.client.execute(part)
                self.client.pace()

    # ---------- 下架

    def _apply_removed(self, rows: Sequence[ProductRow], market_id: str) -> None:
        if not rows:
            return
        for batch in chunks(rows, self.cfg.batch_size):
            keys = [row.raw_data_key for row in batch]
            # 先删 FTS（此时 product 行还在，能子查询到 product_id），再删 product
            self.client.execute([
                self._fts_delete_sql(market_id, keys),
                self._product_delete_sql(market_id, keys),
            ])
            self.client.pace()

    # ---------- FTS 语句构造

    def _fts_values(self, row: ProductRow) -> str:
        """该行四个字段的分词结果，作为 FTS 列值。"""
        fts = build_fts_row(
            title=row.title,
            description=row.description,
            source=row.source,
            tags=row.tags,
            category_id=row.category_id,
        )
        return (
            f"{quote_or_null(fts['title'])}, {quote_or_null(fts['description'])}, "
            f"{quote_or_null(fts['source'])}, {quote_or_null(fts['tags'])}, "
            f"{quote_or_null(fts['category_id'])}"
        )

    def _fts_insert_select_sql(self, row: ProductRow) -> str:
        """rowid 用子查询就地取，无需Python 侧先查 id。

        product 的写入与本语句在同一个批处理脚本里顺序执行，
        所以查到的就是刚写入（或刚更新）的那一行。
        """
        return (
            "INSERT INTO product_fts (rowid, title, description, source, tags, category_id) "
            f"SELECT product_id, {self._fts_values(row)} "
            f"FROM product WHERE market_id={quote(row.market_id)} "
            f"AND raw_data_key={quote(row.raw_data_key)}"
        )

    def _fts_delete_sql(self, market_id: str, keys: Sequence[str]) -> str:
        keys_sql = ", ".join(quote(key) for key in keys)
        return (
            "DELETE FROM product_fts WHERE rowid IN "
            f"(SELECT product_id FROM product WHERE market_id={quote(market_id)} "
            f"AND raw_data_key IN ({keys_sql}))"
        )

    @staticmethod
    def _product_delete_sql(market_id: str, keys: Sequence[str]) -> str:
        keys_sql = ", ".join(quote(key) for key in keys)
        return (
            f"DELETE FROM product WHERE market_id={quote(market_id)} "
            f"AND raw_data_key IN ({keys_sql})"
        )
