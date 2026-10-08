"""编排入口：`python -m backendworker.importer`

DESIGN.md §5 流程：

    1. 初始化配置 / Storage / D1 客户端 / jieba
    2. 读 product_files.json，与 sync_state 比对 → 待处理文件列表（空则退出）
    3. 同步 market / category 元数据
    4. 逐站点：读 CSV → 与 _old.csv diff → 写库 + 维护 FTS → 更新 sync_state → 改名
    5. 输出运行摘要
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import PROJECT_ROOT, Config, load_config
from .d1_client import D1Client, D1Error, QuotaExceeded
from .meta_sync import sync_metadata
from .product_sync import ProductSyncer, SyncStats
from .state import (
    compute_pending,
    fetch_sync_state,
    load_product_files,
    now_str,
    upsert_sync_state,
)
from .storage import create_storage
from .stats import rebuild as rebuild_stats
from .tokenizer import initialize

log = logging.getLogger("backendworker")


# ------------------------------------------------------------------ CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backendworker.importer",
        description="把 processeddata/ 下的采集结果导入 Cloudflare D1 并维护 FTS5 索引",
    )
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="本地数据目录（storage=local 时用），默认 <项目根>/processeddata")
    parser.add_argument("--storage", choices=("local", "r2"), default=None,
                        help="数据源：local=本地目录（默认，本地调试用）；r2=直接从 Cloudflare R2 读写（生产用）")
    parser.add_argument("--batch-size", type=int, default=None, help="每批 SQL 语句数，默认 200")
    parser.add_argument("--batch-delay", type=float, default=None, help="批间延迟秒数，默认 0.2")
    parser.add_argument("--only", default="", help="只处理指定站点，逗号分隔，如 beijing,shanghai")
    parser.add_argument("--init-ddl", action="store_true", help="先执行 tables.sql 建表（首次运行用，已存在的表跳过）")
    parser.add_argument("--sync-meta-only", action="store_true", help="只同步 market/category 元数据后退出")
    parser.add_argument("--rebuild-stats", action="store_true",
                        help="不管本轮有没有商品变动，都强制重算 stats 计数表（也可单独运行）")
    parser.add_argument("--dry-run", action="store_true", help="只做本地计算，不写库、不改名")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出 DEBUG 日志")
    return parser


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


# ------------------------------------------------------------------ 建表


_DDL_PREFIXES = ("CREATE", "DROP", "ALTER", "PRAGMA")


def run_ddl(client: D1Client, cfg: Config, ddl_path: Path | None = None) -> None:
    path = ddl_path or PROJECT_ROOT / "tables.sql"
    script = path.read_text(encoding="utf-8")
    statements = _split_ddl(script)
    log.info("执行建表脚本：%s（%d 条语句）", path.name, len(statements))
    if cfg.dry_run:
        log.info("[dry-run] 跳过 DDL 执行")
        return
    for index, stmt in enumerate(statements, start=1):
        head = stmt.splitlines()[0][:70]
        log.info("  DDL [%d/%d] %s", index, len(statements), head)
        try:
            client.query(stmt)
        except D1Error as exc:
            raise D1Error(f"DDL 第 {index} 条执行失败 → {head}\n{exc}") from exc
        client.pace()


def _split_ddl(script: str) -> list[str]:
    """按 `;` 拆分 DDL，丢掉整行注释。

    `--` 与 `#` 开头的行都要丢：`#` 不是 SQL 注释，
    漏掉它会把文件头说明拼进第一条 CREATE 语句，导致 D1 返回 400。
    tables.sql 中不存在字符串字面量内的分号，直接按分号切分是安全的。
    """
    kept: list[str] = []
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--") or stripped.startswith("#"):
            continue
        kept.append(line)

    statements: list[str] = []
    for chunk in "\n".join(kept).split(";"):
        stmt = chunk.strip()
        if not stmt:
            continue
        head = stmt.split(None, 1)[0].upper()
        if head not in _DDL_PREFIXES:
            log.warning("忽略非 DDL 片段：%s…", stmt[:60].replace("\n", " "))
            continue
        statements.append(stmt)
    return statements


# ------------------------------------------------------------------ 主流程


def run(args: argparse.Namespace) -> int:
    cfg = load_config(
        data_dir=args.data_dir,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
        storage_mode=args.storage,
    )
    if args.batch_delay is not None:
        cfg.batch_delay = args.batch_delay
    if not cfg.dry_run:
        cfg.require_token()

    log.info(
        "backendworker 启动：data_dir=%s database=%s dry_run=%s",
        cfg.data_dir, cfg.database_id, cfg.dry_run,
    )
    if cfg.storage_mode == "local" and not cfg.data_dir.is_dir():
        log.error("数据目录不存在：%s", cfg.data_dir)
        return 2

    if cfg.storage_mode == "r2":
        log.info("数据源 storage=r2（s3://%s/%s）", cfg.r2_bucket, cfg.r2_prefix or "<桶根目录>")
    else:
        log.info("数据源 storage=local（%s）", cfg.data_dir)

    storage = create_storage(cfg)
    initialize(verbose=args.verbose)

    client = D1Client(cfg)
    try:
        if args.init_ddl:
            run_ddl(client, cfg)

        if args.sync_meta_only:
            catalog = sync_metadata(client, storage)
            log.info("仅元数据模式结束：market %d / category %d",
                     len(catalog.markets), len(catalog.categories))
            return 0

        # 单独重算 stats（不跑导入，也不受「有无待处理文件」影响）
        if args.rebuild_stats:
            catalog = sync_metadata(client, storage)
            rows = rebuild_stats(client, sorted(catalog.categories))
            log.info("仅重算 stats 完成：%d 行", rows)
            return 0

        entries = load_product_files(storage)
        if args.only:
            wanted = {s.strip().lower() for s in args.only.split(",") if s.strip()}
            entries = [e for e in entries if e.market_id.lower() in wanted]
        if not entries:
            log.warning("没有可处理的文件清单，退出")
            return 0

        # 步骤 2：触发判断
        # 配额错误在这里就必须拦住：它一旦被当成「sync_state 表不存在」，
        # 后面所有站点都会被判为「首次出现」而当成新增重跑，正好撞上配额上限。
        try:
            sync_state = {} if cfg.dry_run else fetch_sync_state(client)
        except QuotaExceeded as exc:
            log.error("D1 配额耗尽，无法读取 sync_state，本轮中止（未改动任何数据）")
            log.error("%s", exc)
            return 3

        pending = compute_pending(entries, sync_state)
        if not pending:
            log.info("所有文件内容均无变化（CSV 未重新清洗），空跑保护退出")
            return 0
        log.info("待处理文件 %d 个：%s", len(pending), ", ".join(e.file_name for e in pending))

        # 步骤 3：元数据同步（每次都做，代价可忽略）
        try:
            catalog = sync_metadata(client, storage)
        except QuotaExceeded as exc:
            log.error("D1 配额耗尽于元数据同步，本轮中止（未改动任何商品数据）")
            log.error("%s", exc)
            return 3

        # 步骤 4：逐站点处理
        syncer = ProductSyncer(client)
        summaries: list[SyncStats] = []
        failed: list[str] = []

        for entry in pending:
            try:
                stats = syncer.process_market(storage=storage, entry=entry, catalog=catalog)
                summaries.append(stats)
                # process_market 内部判定为异常状态（如 _new.csv 为空但 _old.csv 有数据）时
                # 不抛异常，改用 errors 标记，这里同样按失败处理：不改名、不更新状态
                if stats.errors:
                    failed.append(entry.file_name)
                    log.error("%s 未通过前置检查，跳过（不改名，下次会重算）", entry.file_name)
                    continue
            except QuotaExceeded as exc:
                # 账号级配额耗尽：后面的站点同样写不进去，
                # 继续跑只会把剩余站点全标失败并浪费一整轮退避重试。
                # 已提交的批次保持原样（写入幂等，重跑安全），下次整轮重来即可。
                log.error("D1 配额耗尽，中止本轮导入（剩余 %d 个站点未处理）", len(pending) - len(summaries) - len(failed))
                log.error("%s", exc)
                print_summary(summaries, failed, client, cfg, aborted_by_quota=True)
                return 3
            except Exception as exc:
                failed.append(entry.file_name)
                log.exception("%s 处理失败：%s（不改名，下次会重算）", entry.file_name, exc)
                continue

            if cfg.dry_run:
                log.info("[dry-run] %s 跳过 sync_state 更新与文件改名", entry.file_name)
                continue

            try:
                upsert_sync_state(client, entry)  # 先记状态
                storage.rename(entry.file_name, entry.old_name)  # 成功后再改名
            except QuotaExceeded as exc:
                # 商品已入库但状态没记、文件没改名 —— 下次会整站重跑。
                # 这是安全的：新增走 UPSERT，重跑收敛为「更新为最新值」，不会插出重复行。
                log.error("D1 配额耗尽于 %s 的收尾阶段：商品已入库，但 sync_state 未记录、文件未改名", entry.file_name)
                log.error("%s", exc)
                log.error("下次运行会整站重跑该站点（新增走 UPSERT，不会产生重复数据）")
                print_summary(summaries, failed, client, cfg, aborted_by_quota=True)
                return 3
            except Exception as exc:
                failed.append(entry.file_name)
                log.exception("%s 收尾失败（状态/改名）：%s", entry.file_name, exc)

        # 步骤 5：商品有变动就整体重算 stats（列表页的计数来源）
        wrote = any(s.added or s.changed or s.removed for s in summaries)
        stats_rows = 0
        if args.rebuild_stats or wrote:
            try:
                stats_rows = rebuild_stats(client, sorted(catalog.categories))
            except QuotaExceeded as exc:
                log.error("D1 配额耗尽于 stats 重算：%s", exc)
                failed.append("stats")
            except Exception as exc:
                failed.append("stats")
                log.exception("重算 stats 失败：%s", exc)
        else:
            log.info("本轮无商品变动，跳过 stats 重算")

        print_summary(summaries, failed, client, cfg, stats_rows=stats_rows)
        return 1 if failed else 0
    finally:
        client.close()


# ------------------------------------------------------------------ 摘要


def print_summary(
    summaries: list[SyncStats],
    failed: list[str],
    client: D1Client,
    cfg: Config,
    stats_rows: int = 0,
    aborted_by_quota: bool = False,
) -> None:
    total = SyncStats(market_id="合计")
    lines = []
    header = f"{'站点':<12}{'新增':>8}{'变更':>8}{'下架':>8}{'未变':>8}{'跳过':>8}{'告警':>8}"
    lines.append("")
    lines.append("=" * len(header))
    lines.append("运行摘要")
    lines.append("=" * len(header))
    lines.append(header)
    lines.append("-" * len(header))
    for s in summaries:
        lines.append(
            f"{s.market_id:<12}{s.added:>8}{s.changed:>8}{s.removed:>8}"
            f"{s.unchanged:>8}{s.skipped:>8}{s.warnings:>8}"
        )
        total.added += s.added
        total.changed += s.changed
        total.removed += s.removed
        total.unchanged += s.unchanged
        total.skipped += s.skipped
        total.warnings += s.warnings
    if summaries:
        lines.append("-" * len(header))
        lines.append(
            f"{'合计':<12}{total.added:>8}{total.changed:>8}{total.removed:>8}"
            f"{total.unchanged:>8}{total.skipped:>8}{total.warnings:>8}"
        )
    lines.append("=" * len(header))
    if aborted_by_quota:
        lines.append("本轮因 D1 每日行数配额耗尽而中止")
        lines.append("已提交的站点已改名并记录状态，不会重复导入")
        lines.append("未完成的站点下次整站重跑（新增走 UPSERT，不会产生重复数据）")
        lines.append("配额于次日 00:00 UTC（北京时间 08:00）重置")
    elif failed:
        lines.append(f"失败站点 {len(failed)} 个（未改名，下次仍会重跑）：{', '.join(failed)}")
    else:
        lines.append("全部站点处理成功")
    if stats_rows:
        lines.append(f"stats 计数表已整体重算，写入 {stats_rows} 行")
    if not cfg.dry_run:
        lines.append(
            f"D1 请求数 {client.stat_requests}（重试 {client.stat_retries} 次），"
            f"回查行数 {client.stat_rows_returned}"
        )
    lines.append(f"完成时间 {now_str()}")
    print("\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        return run(args)
    except KeyboardInterrupt:
        log.warning("用户中断")
        return 130
    except SystemExit as exc:  # argparse / 配置校验
        code = exc.code if isinstance(exc.code, int) else 1
        if isinstance(exc.code, str):
            log.error("%s", exc.code)
        return code
    except Exception:
        log.exception("运行异常终止")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
