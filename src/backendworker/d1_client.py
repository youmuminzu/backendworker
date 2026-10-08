"""Cloudflare D1 HTTP API 客户端。

DESIGN.md §6.4：
- 端点 `POST /accounts/{account_id}/d1/database/{database_id}/query`，Bearer Token 认证
- 批量：单次请求携带多条 SQL 语句（严格转义字符串字面量），每批 200~500 行
- 幂等由上层 UPSERT 保证；本层负责 429/5xx 指数退避重试（上限 5 次）与批间延迟
- 降级：若远端不接受多语句脚本，自动切为「逐语句」模式继续跑
- **配额豁免**：账号级每日行读/行写上限耗尽（`QuotaExceeded`）不进重试分支，
  也不触发多语句降级 —— 它要到午夜 UTC 才重试，重试或降级都只是白烧时间
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

# Cloudflare 账号级每日行数配额耗尽的报错文案（见 D1 error list）。
# 命中即视为 QuotaExceeded：午夜 UTC 才重置，任何重试/降级都无意义。
QUOTA_MARKERS = (
    "daily row write limit",
    "daily row read limit",
    "maximum account storage limit",
)


class D1Error(RuntimeError):
    """D1 返回业务错误（success=false 或单条语句执行失败）。"""

    def __init__(self, message: str, *, errors: list[Any] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


class QuotaExceeded(D1Error):
    """账号级 D1 每日行读/行写配额已耗尽。

    与 D1Error 的区别：这是**账号级、跨库、不可重试**的错误，
    一直重试到 00:00 UTC 也不会成功。上层应当立即中止整轮导入，
    而不是跳过单个站点继续跑（后面的站点同样会失败，只是白等退避）。
    """


def is_quota_error(value: Any) -> bool:
    """按报错文案判定是否为配额耗尽。

    D1 把这类错误放在 HTTP 400 的 `errors[].message`，或 HTTP 200 的
    `result[].error` 里，两处都要认，所以只匹配文案不依赖 HTTP 状态码。
    """
    text = str(value).lower()
    return any(marker in text for marker in QUOTA_MARKERS)


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

            # 2) 配额耗尽优先于一切状态码判断。
            #    D1 常规把它放在 400 的 errors[].message 里，但保底也要防它
            #    裹在 429/5xx 里（那样会被下面的退避重试白等 5 次）。
            quota = self._detect_quota(resp)
            if quota is not None:
                raise quota

            # 3) 限流 / 服务端错误：可重试
            if resp.status_code in RETRYABLE_STATUS:
                last_error = httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}", request=resp.request, response=resp
                )
                if attempt >= self.cfg.max_retries:
                    break
                self._backoff(f"HTTP {resp.status_code}", delay, attempt)
                delay *= 2
                continue

            # 4) 其余 4xx（400/401/403…）：请求本身有问题，重试无意义，
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
                # 单条语句级失败也可能就是配额问题（放在 result[].error 里）
                quota = self._quota_from_body(body)
                if quota is not None:
                    raise quota
                raise D1Error(
                    f"D1 返回失败：{_brief(body.get('errors'))}", errors=body.get("errors") or []
                )
            return body.get("result") or []

        raise D1Error(f"D1 请求重试 {self.cfg.max_retries} 次仍失败：{last_error}")

    # ---- 配额识别

    @staticmethod
    def _quota_message(raw: Any) -> str | None:
        """从一段报错文案里提取可读信息，命中配额关键词才返回。"""
        for item in (raw if isinstance(raw, list) else [raw]):
            message = item.get("message") if isinstance(item, dict) else item
            if message and is_quota_error(message):
                return str(message)
        return None

    def _quota_from_body(self, body: dict[str, Any]) -> QuotaExceeded | None:
        """检查 HTTP 200 响应体里的 errors / result[].error。"""
        for key in ("errors", "messages"):
            message = self._quota_message(body.get(key))
            if message:
                return _quota_error(message)

        for item in body.get("result") or []:
            if isinstance(item, dict) and not item.get("success", True):
                message = self._quota_message(item.get("error"))
                if message:
                    return _quota_error(message)
        return None

    def _detect_quota(self, resp: httpx.Response) -> QuotaExceeded | None:
        """不限状态码地检查响应体是否在说「配额耗尽」。"""
        if resp.status_code < 400:
            return None  # 2xx 的配额错误由 _quota_from_body 处理
        try:
            body = resp.json()
        except ValueError:
            message = self._quota_message(resp.text)
            return _quota_error(message) if message else None
        return self._quota_from_body(body)

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
                error = item.get("error")
                if is_quota_error(error):
                    raise _quota_error(error)
                raise D1Error(f"SQL 执行失败：{_brief(error)}\n---\n{sql[:300]}")
            rows.extend(item.get("results") or [])
        self.stat_rows_returned += len(rows)
        return rows

    def query_script(self, statements: Sequence[str]) -> list[dict[str, Any]]:
        """把多条只读 SELECT 拼成一个脚本发出去，返回合并后的结果行。

        存在的理由：D1（workerd）把 SQLITE_LIMIT_COMPOUND_SELECT 降到 5，
        所以「N 个分类 UNION ALL 成一条」会被解析阶段直接拒绝。但 N 条
        **独立**语句不受该限制约束——它是复合 SELECT 的项数上限，不是
        语句条数上限。于是「每分类一条 COUNT(*)」既能并行发一次请求拿到结果，
        又完全绕开该限制。

        与 `execute` 的区别：execute 面向写入且丢弃返回值；本方法面向读取，
        会把每条语句的 results 平铺返回。多语句响应里每个语句各有一项 result，
        因此调用方应让每条语句自带区分列（本处是 `stat_key` 别名），
        这样即便顺序被打乱也不会串行。

        多语句脚本若被拒，会自动降级为逐条查询（语义相同，都是只读），
        而不是让调用方掉进更贵的兜底路径。

        语句里**不要**写任何写操作：这里没有 execute 的幂等重跑保障，
        中途失败重发会有重复写风险。
        """
        if not statements:
            return []
        try:
            results = self._post({"sql": build_script(statements)})
        except QuotaExceeded:
            raise
        except D1Error as exc:
            log.warning("多语句只读脚本失败（%s），改用逐条查询", _brief(exc))
            rows = []
            for stmt in statements:
                rows.extend(self.query(stmt))
            return rows
        rows: list[dict[str, Any]] = []
        for item in results:
            if not item.get("success", True):
                error = item.get("error")
                if is_quota_error(error):
                    raise _quota_error(error)
                raise D1Error(f"SQL 执行失败：{_brief(error)}")
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

        `QuotaExceeded` 直接向上抛，不进降级路径：配额耗尽与「多语句支不支持」
        毫无关系，逐语句重跑只会把同一个配额错误再撞一遍，并把日志误导成
        「多语句脚本不被支持」。
        """
        if not statements:
            return
        if self._multi_statement:
            try:
                self._post({"sql": build_script(statements)})
                return
            except QuotaExceeded:
                raise
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


def _quota_error(message: str) -> QuotaExceeded:
    """构造带处置建议的 QuotaExceeded。

    日志里必须写清三件事：D1 不会丢数据、什么时候恢复、为什么重试没用——
    否则看到报错的人会以为数据库坏了，或以为重跑几轮就能补上。
    """
    return QuotaExceeded(
        f"D1 每日行数配额已耗尽：{_brief(message)}\n"
        "已写入的数据不会丢失，但本轮剩余写入都会被拒绝。\n"
        "配额于次日 00:00 UTC（北京时间 08:00）重置；"
        "本轮已提交的批次不会重放（写入均幂等，重跑安全）。"
    )


def _http_body(resp: "httpx.Response", limit: int = 800) -> str:
    """把 HTTP 响应体压成单行，便于在日志/异常里看到 D1 的真实报错。"""
    text = " ".join((resp.text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"
