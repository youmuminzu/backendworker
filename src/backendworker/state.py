"""sync_state 读写与「待处理文件」判定。

DESIGN.md §4.3 / §6.5：
- 状态存在 D1 而不是本地文件 —— GitHub Actions 这类无状态环境没有可靠的本地状态
- 待处理判定 = product_files.json 里的 **updated_at** ≠ sync_state 里记的值
- 首次运行（sync_state 为空）→ 全部待处理 → 即首次全量导入

为什么用 updated_at 而不是 upload_at：采集端 `build_products.py` 重新生成 CSV 时
写updated_at 并清空 upload_at，而 `transtoR2.py` 只在上传成功后才回写 upload_at。
清洗完但没上传时 upload_at 不可靠，updated_at 才是「内容确实变了」的信号。
详见 `load_product_files` 的注释。
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
    """product_files.json 中的一条记录。

    注意 `upload_at` 字段的语义：它装的是**内容变更基准时间**，
    不一定等于采集端 JSON 里原始的 upload_at。理由见 load_product_files。
    """

    file_name: str
    updated_at: str = ""   # 采集端原始字段：CSV 内容最后一次重新生成的时间
    upload_at: str = ""    # 实际用作变更基准的值（见 load_product_files）

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
    """读取 product_files.json（触发信号清单）。

    **变更基准取 `updated_at`，不用 `upload_at`。**

    采集端这两个字段由不同代码路径、在不同时间写入：
      - `build_products.py` 重新生成 CSV 时写 `updated_at`，同时把 `upload_at` 清空成 ""
      - `transtoR2.py` 的 `stamp_index()` 只在上传成功后才回写 `upload_at`

    所以只要重新清洗了 CSV 而没跟上一次传（上传失败、手动跳过、只跑清洗），
    `upload_at` 就空着或停在旧值，本端会漏掉这次更新 —— 而内容其实已经变了。
    `updated_at` 才是「内容确实变了」的可靠信号。

    统一在入口处赋值（而不是散在调用处），是为了保证「判定用的值」与
    「`upsert_sync_state` 写进 D1 的值」严格同源。两者不一致会导致每轮
    都判定为「有更新」，变成每晚全量重跑。
    """
    payload = storage.read_json("product_files.json")
    if not payload:
        log.warning("product_files.json 缺失或为空，无文件可处理")
        return []

    raw = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        log.warning("product_files.json 结构异常（期望数组），跳过")
        return []

    entries: list[FileEntry] = []
    missing_stamp = 0
    for item in raw:
        if not isinstance(item, dict) or not item.get("file_name"):
            log.warning("product_files.json 存在无效条目：%r", item)
            continue
        updated = str(item.get("updated_at") or "").strip()
        uploaded = str(item.get("upload_at") or "").strip()
        if updated:
            basis = updated
        else:
            # 没有 updated_at 就无法判断内容是否变过，退回 upload_at；
            # 两者皆空则为空串，compute_pending 会判为「首次出现」。
            missing_stamp += 1
            basis = uploaded
        entries.append(
            FileEntry(
                file_name=str(item["file_name"]).strip(),
                updated_at=updated,
                upload_at=basis,
            )
        )

    if missing_stamp:
        log.warning(
            "product_files.json 有 %d 个条目缺少 updated_at，已退回按 upload_at 判定", missing_stamp
        )
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
    """变更基准时间与上次记录不一致（或首次出现）的文件即为待处理。

    比对的是 `entry.upload_at`——它已被 `load_product_files` 赋成
    `updated_at`（内容变更时间），所以这里的语义是「内容是否重新清洗过」。
    """
    pending: list[FileEntry] = []
    for entry in entries:
        previous = sync_state.get(entry.file_name)
        if previous == entry.upload_at and entry.upload_at:
            log.debug("%s 内容未变化（时间戳 %s），跳过", entry.file_name, entry.upload_at)
            continue
        if previous is None:
            log.info("%s 首次出现，待处理", entry.file_name)
        else:
            log.info(
                "%s 内容有更新：%s → %s，待处理",
                entry.file_name, previous, entry.upload_at or "(空)",
            )
        pending.append(entry)
    return pending


def now_str() -> str:
    """与数据中时间戳保持一致的格式：YYYY-MM-DD HH:MM:SS（本地时区）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
