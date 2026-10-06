"""运行配置。

优先级：命令行参数 > 环境变量 > R2keys.conf 兜底 > 代码默认值。

必需的环境变量只有 D1 的 API Token（需要 D1 编辑权限）；
Account ID 在 R2keys.conf 里已存在，缺失时才要求用环境变量给出。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# 项目根目录：<root>/src/backendworker/config.py → parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# DESIGN.md §2 中确认的默认值
DEFAULT_DATABASE_NAME = "datacabsite"
DEFAULT_DATABASE_ID = "f42c3293-63b2-4ca3-a3b4-942a1d7fb926"
DEFAULT_D1_API_BASE = "https://api.cloudflare.com/client/v4"
# 数据在 R2 桶里的目录（对象存储的「目录」只是 key 的公共前缀）
DEFAULT_R2_PREFIX = "data_product_files"

_ENV_MAP = {
    "api_token": ("CLOUDFLARE_API_TOKEN", "CF_API_TOKEN", "D1_API_TOKEN"),
    "account_id": ("CLOUDFLARE_ACCOUNT_ID", "CF_ACCOUNT_ID"),
    "database_id": ("CLOUDFLARE_D1_DATABASE_ID", "D1_DATABASE_ID"),
    "data_dir": ("BACKENDWORKER_DATA_DIR",),
    "storage_mode": ("BACKENDWORKER_STORAGE",),
    "r2_bucket": ("R2_BUCKET", "BUCKET_NAME"),
    "r2_endpoint_url": ("R2_ENDPOINT_URL", "ENDPOINT_URL"),
    "r2_access_key_id": ("R2_ACCESS_KEY_ID", "ACCESS_KEY_ID"),
    "r2_secret_access_key": ("R2_SECRET_ACCESS_KEY", "SECRET_ACCESS_KEY"),
    "r2_prefix": ("R2_PREFIX",),
}


def _from_env(key: str) -> str | None:
    for name in _ENV_MAP[key]:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def _conf_values() -> dict[str, str]:
    """读取 R2keys.conf（本地凭据文件，已 gitignore）。

    本地调试时用它兜底 R2 的账号/桶/密钥，省得每次手敲环境变量。
    CI 里没有这个文件，走环境变量。
    """
    conf = PROJECT_ROOT / "R2keys.conf"
    if not conf.exists():
        return {}
    return dict(re.findall(r'([A-Z_]+)\s*=\s*["\']([^"\']+)["\']', conf.read_text(encoding="utf-8")))


def _from_conf(key: str) -> str | None:
    return _conf_values().get(key)


@dataclass(slots=True)
class Config:
    account_id: str
    database_id: str
    api_token: str
    data_dir: Path
    # D1 写入批次：每条 INSERT/UPDATE 是一条语句，多条语句拼在一个 HTTP 请求里
    batch_size: int = 200
    max_payload_bytes: int = 512 * 1024  # 单请求 SQL 文本上限，超过则再切分
    max_retries: int = 5                # 429/5xx 退避重试上限
    retry_base_delay: float = 1.0       # 退避基数（秒）
    batch_delay: float = 0.2            # 批间延迟（秒），避免打满限流
    http_timeout: float = 90.0
    dry_run: bool = False               # True 时只做本地计算，不写 D1、不改名

    # 数据源：local（默认，本地调试） / r2（生产，CI 直接用）
    storage_mode: str = "local"
    r2_bucket: str = ""
    r2_endpoint_url: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_prefix: str = DEFAULT_R2_PREFIX  # 桶内的目录前缀，如 data_product_files

    database_name: str = field(default=DEFAULT_DATABASE_NAME)
    api_base: str = DEFAULT_D1_API_BASE

    @property
    def query_url(self) -> str:
        return (
            f"{self.api_base}/accounts/{self.account_id}"
            f"/d1/database/{self.database_id}/query"
        )

    def require_token(self) -> str:
        if not self.api_token:
            raise SystemExit(
                "缺少 D1 API Token：请设置环境变量 CLOUDFLARE_API_TOKEN"
                "（Cloudflare 控制台 → My Profile → API Tokens，需 D1 编辑权限）。"
            )
        return self.api_token


def load_config(
    *,
    data_dir: str | os.PathLike[str] | None = None,
    batch_size: int | None = None,
    dry_run: bool = False,
    storage_mode: str | None = None,
    require_token: bool = True,
) -> Config:
    """组装配置。dry_run 模式下不强求 token。"""
    mode = (storage_mode or _from_env("storage_mode") or "local").strip().lower()
    if mode not in ("local", "r2"):
        raise SystemExit(f"未知 storage 模式：{mode}（可选 local / r2）")

    account_id = _from_env("account_id") or _from_conf("ACCOUNT_ID") or ""
    database_id = _from_env("database_id") or DEFAULT_DATABASE_ID

    resolved_data_dir = Path(data_dir) if data_dir else Path(_from_env("data_dir") or PROJECT_ROOT / "processeddata")

    def pick(key: str, conf_key: str, default: str = "") -> str:
        return _from_env(key) or _from_conf(conf_key) or default

    cfg = Config(
        account_id=account_id,
        database_id=database_id,
        api_token=_from_env("api_token") or "",
        data_dir=resolved_data_dir,
        batch_size=batch_size or 200,
        dry_run=dry_run,
        storage_mode=mode,
        r2_bucket=pick("r2_bucket", "BUCKET_NAME"),
        r2_endpoint_url=pick("r2_endpoint_url", "ENDPOINT_URL"),
        r2_access_key_id=pick("r2_access_key_id", "ACCESS_KEY_ID"),
        r2_secret_access_key=pick("r2_secret_access_key", "SECRET_ACCESS_KEY"),
        # 允许把 R2_PREFIX 显式设为空串（表示文件就在桶根目录），
        # 所以这里判断的是「有没有设」而不是「设的值真不真」
        r2_prefix=_from_env("r2_prefix") if "R2_PREFIX" in os.environ else DEFAULT_R2_PREFIX,
    )

    if mode == "r2" and not (cfg.r2_bucket and cfg.r2_endpoint_url):
        raise SystemExit(
            "storage 模式为 r2，但缺少桶信息：请设置 R2_BUCKET 与 R2_ENDPOINT_URL 环境变量。"
        )

    if not cfg.account_id:
        raise SystemExit("缺少 Cloudflare Account ID：请设置环境变量 CLOUDFLARE_ACCOUNT_ID。")
    if require_token and not dry_run:
        cfg.require_token()
    return cfg
