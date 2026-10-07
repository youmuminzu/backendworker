"""sync_state 读写与「待处理文件」判定。

DESIGN.md §4.3 / §6.5：
- 状态存在 D1 而不是本地文件 —— GitHub Actions 这类无状态环境没有可靠的本地状态
- 待处理判定 = product_files.json 里的 upload_at ≠ sync_state 里记的值
- 首次运行（sync_state 为空）→ 全部待处理 → 即首次全量导入
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from .d1_client import D1Client, QuotaExceeded, quote
from .storage import Storage

log = logging.getLogger(__name__)

NEW_SUFFIX = "_new.csv"
OLD_SUFFIX = "_old.csv"


@dataclass(frozen=True, slots=True)
class FileEntry:
    """product_files.json 中的一条记录。"""

    file_name: str
    updated_at: str = ""
    upload_at: str = ""

    @property
    def market_id(self) -> str:
        """beijing_new.csv → beijing"""
        name = self.file_name
        return name[: -len(NEW_SUFFIX)] if name.endswith(NEW_SUFFIX) else name.rsplit(".", 1)[0]

    @property
    def old_name(self) -> str:
        """对应的上一轮快照名：beijing_old.csv"""
        name = self.file_name
        if name.endswith(NEW_SUFFIX):
            return name[: -len(NEW_SUFFIX)] + OLD_SUFFIX
        return name.rsplit(".", 1)[0] + OLD_SUFFIX


def load_product_files(storage: Storage) -> list[FileEntry]:
    """读取 product_files.json（触发信号清单）。"""
    payload = storage.read_json("product_files.json")
    if not payload:
        log.warning("product_files.json 缺失或为空，无文件可处理")
        return []

    raw = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        log.warning("product_files.json 结构异常（期望数组），跳过")
        return []

    entries: list[FileEntry] = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("file_name"):
            log.warning("product_files.json 存在无效条目：%r", item)
            continue
        entry = FileEntry(
            file_name=str(item["file_name"]).strip(),
            updated_at=str(item.get("updated_at") or ""),
            upload_at=str(item.get("upload_at") or ""),
        )
        entries.append(entry)
    return entries


def fetch_sync_state(client: D1Client) -> dict[str, str]:
    """读取 D1 中的 sync_state：{file_name: upload_at}。"""
    try:
        rows = client.query("SELECT file_name, upload_at FROM sync_state")
    except QuotaExceeded:
        # 配额错误绝不能被当成「表不存在」：那会让整轮按首次全量导入重跑，
        # 把所有站点当成新增，白白烧掉本就不够的写入配额。
        raise
    except Exception as exc:  # 表不存在等
        log.warning("读取 sync_state 失败（%s），按首次全量导入处理", exc)
        return {}
    return {str(row["file_name"]): str(row.get("upload_at") or "") for row in rows}


def upsert_sync_state(client: D1Client, entry: FileEntry, processed_at: str | None = None) -> None:
    """某站点全部写库成功后调用（改名之前），记录本轮处理状态。"""
    processed = processed_at or now_str()
    sql = (
        "INSERT INTO sync_state (file_name, upload_at, processed_at) "
        f"VALUES ({quote(entry.file_name)}, {quote(entry.upload_at)}, {quote(processed)}) "
        "ON CONFLICT(file_name) DO UPDATE SET "
        "upload_at=excluded.upload_at, processed_at=excluded.processed_at"
    )
    client.query(sql)


def compute_pending(entries: list[FileEntry], sync_state: dict[str, str]) -> list[FileEntry]:
    """upload_at 变化（或首次出现）的文件即为待处理。"""
    pending: list[FileEntry] = []
    for entry in entries:
        previous = sync_state.get(entry.file_name)
        if previous == entry.upload_at:
            log.debug("%s 未变化（upload_at=%s），跳过", entry.file_name, entry.upload_at)
            continue
        if previous is None:
            log.info("%s 首次出现，待处理", entry.file_name)
        else:
            log.info("%s 有更新：%s → %s，待处理", entry.file_name, previous, entry.upload_at)
        pending.append(entry)
    return pending


def now_str() -> str:
    """与数据中时间戳保持一致的格式：YYYY-MM-DD HH:MM:SS（本地时区）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
