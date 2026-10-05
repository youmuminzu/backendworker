"""Cloudflare D1 HTTP API 客户端。

DESIGN.md §6.4：
- 端点 `POST /accounts/{account_id}/d1/database/{database_id}/query`，Bearer Token 认证
- 批量：单次请求携带多条 SQL 语句（严格转义字符串字面量），每批 200~500 行
- 幂等由上层 UPSERT 保证；本层负责 429/5xx 指数退避重试（上限 5 次）与批间延迟
- 降级：若远端不接受多语句脚本，自动切为「逐语句」模式继续跑
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Iterable, Iterator, Sequence

import httpx

from .config import Config

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class D1Error(RuntimeError):
    """D1 返回业务错误（success=false 或单条语句执行失败）。"""

    def __init__(self, message: str, *, errors: list[Any] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


# ------------------------------------------------------------------ 字面量转义


def quote(value: Any) -> str:
    """把 Python 值转成 SQLite 字符串字面量。

    SQLite 不认反斜杠转义，唯一的转义方式是单引号写两遍；
    NUL 字符会导致 D1/SQLite 截断，直接剔除。
    """
    if value is None:
        return "NULL"
    text = value if isinstance(value, str) else str(value)
    text = text.replace("\x00", "")
    return "'" + text.replace("'", "''") + "'"


def quote_or_null(value: Any) -> str:
    """空串按 NULL 存，避免把「无值」写成「空字符串」污染索引。"""
    if value is None or (isinstance(value, str) and value == ""):
        return "NULL"
    return quote(value)


def build_script(statements: Sequence[str]) -> str:
    """多条语句拼成一个脚本，末尾补分号。"""
    return ";\n".join(s.rstrip().rstrip(";") for s in statements) + ";"


# ------------------------------------------------------------------ 客户端


class D1Client:
    def __init__(self, config: Config, *, http_client: httpx.Client | None = None) -> None:
        self.cfg = config
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(timeout=config.http_timeout)
        self._multi_statement = True  # 远端是否接受多语句脚本
        self.stat_requests = 0
        self.stat_retries = 0
        self.stat_rows_returned = 0

    # ---- 生命周期

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> "D1Client":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---- 底层 POST（带退避重试）

    def _post(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        url = self.cfg.query_url
        headers = {
            "Authorization": f"Bearer {self.cfg.api_token}",
            "Content-Type": "application/json",
        }
        delay = self.cfg.retry_base_delay
        last_error: Exception | None = None

        for attempt in range(1, self.cfg.max_retries + 1):
            # 1) 网络层异常：可重试
            try:
                self.stat_requests += 1
                resp = self._http.post(url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt >= self.cfg.max_retries:
                    break
                self._backoff(exc, delay, attempt)
                delay *= 2
                continue

            # 2) 限流 / 服务端错误：可重试
            if resp.status_code in RETRYABLE_STATUS:
                last_error = httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}", request=resp.request, response=resp
                )
                if attempt >= self.cfg.max_retries:
                    break
                self._backoff(f"HTTP {resp.status_code}", delay, attempt)
                delay *= 2
                continue

            # 3) 其余 4xx（400/401/403…）：请求本身有问题，重试无意义，
            #    必须把 D1 返回的 body 抛出来，否则只能看到一串无信息量的 400
            if resp.status_code >= 400:
                raise D1Error(
                    f"D1 返回 HTTP {resp.status_code}（不重试）：{_http_body(resp)}"
                )

            try:
                body = resp.json()
            except ValueError:
                raise D1Error(f"D1 返回非 JSON：{_http_body(resp)}")

            if not body.get("success"):
                raise D1Error(
                    f"D1 返回失败：{_brief(body.get('errors'))}", errors=body.get("errors") or []
                )
            return body.get("result") or []

        raise D1Error(f"D1 请求重试 {self.cfg.max_retries} 次仍失败：{last_error}")

    def _backoff(self, reason: Any, delay: float, attempt: int) -> None:
        sleep = delay * (2 ** (attempt - 1)) + random.uniform(0, 0.3)
        self.stat_retries += 1
        log.warning(
            "D1 请求失败（%s），%.1fs 后重试 [%d/%d]",
            reason, sleep, attempt, self.cfg.max_retries,
        )
        time.sleep(sleep)

    @property
    def multi_statement_enabled(self) -> bool:
        """远端是否仍接受多语句脚本（False 表示已降级为逐语句模式）。"""
        return self._multi_statement

    # ---- 查询（返回结果行）

    def query(self, sql: str) -> list[dict[str, Any]]:
        results = self._post({"sql": sql})
        rows: list[dict[str, Any]] = []
        for item in results:
            if not item.get("success", True):
                raise D1Error(f"SQL 执行失败：{_brief(item.get('error'))}\n---\n{sql[:300]}")
            rows.extend(item.get("results") or [])
        self.stat_rows_returned += len(rows)
        return rows

    # ---- 批量执行（无返回值）

    def execute(self, statements: Sequence[str]) -> None:
        """执行一批写语句。优先走多语句脚本，被拒时自动降级为逐语句。

        降级判定：先原样逐语句重跑一遍。
        - 逐语句全过 → 说明是多语句模式本身不被支持，永久降级；
        - 逐语句同样失败 → 说明是某条 SQL 有问题，原样抛错并保持批量模式，
          避免一次偶发错误让后续所有批次退化成逐条请求。
        重复执行是安全的：上层所有写入均为 UPSERT / 先删后插的幂等形式。
        """
        if not statements:
            return
        if self._multi_statement:
            try:
                self._post({"sql": build_script(statements)})
                return
            except D1Error as exc:
                log.warning("多语句脚本失败（%s），改用逐语句重试以定位原因", _brief(exc))
                for stmt in statements:
                    self.query(stmt)
                self._multi_statement = False
                log.warning("逐语句重跑成功 → 判定为不支持多语句脚本，后续批次降级执行")
                return
        for stmt in statements:
            self.query(stmt)

    # ---- 工具

    def chunk_by_payload(self, statements: Sequence[str]) -> Iterator[list[str]]:
        """按 batch_size 与单请求字节上限切分，避免请求体过大被拒。"""
        batch: list[str] = []
        size = 0
        for stmt in statements:
            stmt_size = len(stmt.encode("utf-8")) + 2
            if batch and (len(batch) >= self.cfg.batch_size or size + stmt_size > self.cfg.max_payload_bytes):
                yield batch
                batch, size = [], 0
            batch.append(stmt)
            size += stmt_size
        if batch:
            yield batch

    def pace(self) -> None:
        """批间小延迟，避免打满 D1 限流。"""
        if self.cfg.batch_delay > 0:
            time.sleep(self.cfg.batch_delay)


def chunks(items: Iterable[Any], size: int) -> Iterator[list[Any]]:
    """通用定长切分。"""
    batch: list[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _brief(value: Any, limit: int = 300) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _http_body(resp: "httpx.Response", limit: int = 800) -> str:
    """把 HTTP 响应体压成单行，便于在日志/异常里看到 D1 的真实报错。"""
    text = " ".join((resp.text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"
