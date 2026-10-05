"""元数据同步：market.json / category.json → UPSERT。

DESIGN.md §5 步骤 3：
- 每次运行都做（仅几十行，代价可忽略）
- json 中存在 → 插入/更新；json 中不存在 → 保留不动（product 外键保护，不做删除）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .d1_client import D1Client, chunks, quote
from .storage import Storage

log = logging.getLogger(__name__)

MARKET_FILE = "market.json"
CATEGORY_FILE = "category.json"


@dataclass(slots=True)
class MetaCatalog:
    """本次运行使用的元数据字典，同时给 product 校验用。"""

    markets: dict[str, str] = field(default_factory=dict)      # market_id → name
    categories: dict[str, str] = field(default_factory=dict)   # category_id → name

    @property
    def market_ids(self) -> set[str]:
        return set(self.markets)

    @property
    def category_ids(self) -> set[str]:
        return set(self.categories)


def _load_items(storage: Storage, name: str) -> list[dict]:
    payload = storage.read_json(name)
    if not payload:
        log.warning("%s 缺失或为空，跳过元数据同步", name)
        return []
    raw = payload.get("data") if isinstance(payload, dict) else payload
    return [item for item in (raw or []) if isinstance(item, dict)]


def sync_markets(client: D1Client, storage: Storage, catalog: MetaCatalog) -> int:
    """UPSERT market 表，按 market_id 冲突更新。"""
    items = _load_items(storage, MARKET_FILE)
    statements: list[str] = []
    for item in items:
        market_id = str(item.get("market_id") or "").strip()
        name = str(item.get("name") or "").strip()
        short_name = str(item.get("short_name") or "").strip() or name
        if not market_id or not name:
            log.warning("%s 条目缺 market_id/name，跳过：%r", MARKET_FILE, item)
            continue
        catalog.markets[market_id] = name
        statements.append(
            "INSERT INTO market (market_id, name, short_name) "
            f"VALUES ({quote(market_id)}, {quote(name)}, {quote(short_name)}) "
            "ON CONFLICT(market_id) DO UPDATE SET "
            "name=excluded.name, short_name=excluded.short_name"
        )

    _run(client, statements, "market", len(items))
    return len(statements)


def sync_categories(client: D1Client, storage: Storage, catalog: MetaCatalog) -> int:
    """UPSERT category 表，按 category_id 冲突更新。

    category.json 不带 sort_order，用数组下标充当展示顺序（默认稳定且与文件一致）。
    """
    items = _load_items(storage, CATEGORY_FILE)
    statements: list[str] = []
    for sort_order, item in enumerate(items):
        category_id = str(item.get("category_id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not category_id or not name:
            log.warning("%s 条目缺 category_id/name，跳过：%r", CATEGORY_FILE, item)
            continue
        catalog.categories[category_id] = name
        statements.append(
            "INSERT INTO category (category_id, name, sort_order) "
            f"VALUES ({quote(category_id)}, {quote(name)}, {sort_order}) "
            "ON CONFLICT(category_id) DO UPDATE SET "
            "name=excluded.name, sort_order=excluded.sort_order"
        )

    _run(client, statements, "category", len(items))
    return len(statements)


def sync_metadata(client: D1Client, storage: Storage) -> MetaCatalog:
    """同步两类元数据，返回本地字典供后续行校验使用。"""
    catalog = MetaCatalog()
    markets = sync_markets(client, storage, catalog)
    categories = sync_categories(client, storage, catalog)
    log.info("元数据同步完成：market %d 条，category %d 条", markets, categories)
    return catalog


def _run(client: D1Client, statements: list[str], table: str, total: int) -> None:
    if not statements:
        log.warning("%s 无有效条目（原始 %d 条），未写入", table, total)
        return
    if client.cfg.dry_run:
        log.info("[dry-run] %s 待 UPSERT %d 条", table, len(statements))
        return
    for batch in chunks(statements, client.cfg.batch_size):
        client.execute(batch)
        client.pace()
