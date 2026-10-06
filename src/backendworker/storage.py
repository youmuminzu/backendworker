"""Storage 抽象：屏蔽「文件从哪来」。

DESIGN.md §3.2
    Storage 协议: list_files() / read_csv(name) / read_json(name) / rename(src, dst) / delete(name)
    ├── LocalStorage   # Phase 1：本地目录
    └── R2Storage      # Phase 2：rename 语义 = copy + delete
"""

from __future__ import annotations

import csv
import io
import json
import shutil
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Storage(Protocol):
    """数据源读写协议。业务逻辑只依赖本协议，切换本地/R2 不影响上层。"""

    def list_files(self) -> list[str]:
        """列出当前可用文件名（不含子目录）。"""
        ...

    def read_csv(self, name: str) -> list[dict[str, str]]:
        """读取 CSV 并返回 dict 列表。CSV 一律按 utf-8-sig 读取以消除 BOM。"""
        ...

    def read_json(self, name: str) -> Any:
        """读取 JSON（不带 BOM）。"""
        ...

    def rename(self, src: str, dst: str) -> None:
        """重命名/移动；目标存在时覆盖。"""
        ...

    def delete(self, name: str) -> None:
        """删除文件；不存在时静默返回。"""
        ...

    def exists(self, name: str) -> bool:
        """文件是否存在。"""
        ...


class LocalStorage:
    """Phase 1：本地目录实现，直接落在 processeddata/ 上。"""

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)

    def _path(self, name: str) -> Path:
        # 防目录穿越：只取文件名部分
        return self.base_dir / Path(name).name

    def list_files(self) -> list[str]:
        if not self.base_dir.is_dir():
            return []
        return sorted(p.name for p in self.base_dir.iterdir() if p.is_file())

    def read_csv(self, name: str) -> list[dict[str, str]]:
        path = self._path(name)
        if not path.exists():
            return []
        # utf-8-sig：消除 BOM，否则首列名会带 \ufeff 导致字段对不上（DESIGN §7）
        with path.open("r", encoding="utf-8-sig", newline="") as fp:
            return [
                {k: (v if v is not None else "") for k, v in row.items()}
                for row in csv.DictReader(fp)
            ]

    def read_json(self, name: str) -> Any:
        path = self._path(name)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as fp:
            return json.load(fp)

    def rename(self, src: str, dst: str) -> None:
        src_path, dst_path = self._path(src), self._path(dst)
        if not src_path.exists():
            return
        src_path.replace(dst_path)  # 原子覆盖

    def delete(self, name: str) -> None:
        path = self._path(name)
        if path.exists():
            path.unlink()

    def exists(self, name: str) -> bool:
        return self._path(name).exists()

    def write_text(self, name: str, text: str) -> None:
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def copy_to(self, name: str, dst_dir: str | Path) -> None:
        Path(dst_dir).mkdir(parents=True, exist_ok=True)
        shutil.copy2(self._path(name), Path(dst_dir) / Path(name).name)


class R2Storage:
    """Phase 2：走 Cloudflare R2（S3 兼容 API）。

    与 LocalStorage 完全同构，**业务代码不需要感知底层在哪**。
    唯一语义差异：

    - `rename()`：对象存储没有原子改名，实现为 CopyObject + DeleteObject。
      ⚠️ 因此它不是原子的——但导入链路的崩溃安全性不依赖它：
      改名严格发生在「该站点全部写库成功」之后，中途崩溃时文件仍叫 `_new.csv`，
      下次重跑会按同样的 diff 重算。
    - `list_files()`：返回去除前缀后的文件名（不含子目录）。

    关于「目录」：对象存储里的目录只是 key 的公共前缀，没有真实目录。
    本类用一个 `prefix` 表达「数据在桶里的哪个目录下」（如 `data_product_files`），
    所有读写都自动拼在它下面。当前约定该目录下是**扁平**的文件列表。
    """

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        prefix: str = "",
    ) -> None:
        if not bucket or not endpoint_url:
            raise ValueError("R2Storage 需要 bucket 与 endpoint_url")
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover
            raise ImportError("使用 R2Storage 需要安装 boto3：uv add boto3") from exc

        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._s3 = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",
        )

    # ---- key 处理

    def _key(self, name: str) -> str:
        """只取文件名部分（防路径穿越），再拼上统一前缀。

        传 `"data_product_files/beijing_new.csv"` 和传 `"beijing_new.csv"` 等价——
        都会落到 `<prefix>/beijing_new.csv`。
        """
        base = Path(name).name
        return f"{self.prefix}/{base}" if self.prefix else base

    def _iter_keys(self):
        paginator = self._s3.get_paginator("list_objects_v2")
        kwargs = {"Bucket": self.bucket}
        if self.prefix:
            kwargs["Prefix"] = self.prefix + "/"
        for page in paginator.paginate(**kwargs):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                # 跳过目录占位对象：控制台建「文件夹」会生成一个 0 字节、
                # 以 / 结尾的 key，它不是文件
                if key.endswith("/"):
                    continue
                yield key

    # ---- 协议实现

    def list_files(self) -> list[str]:
        names = (key.rsplit("/", 1)[-1] for key in self._iter_keys())
        return sorted(name for name in names if name)

    def read_csv(self, name: str) -> list[dict[str, str]]:
        text = self._read_text(name, encoding="utf-8-sig")  # 同样要消 BOM
        if text is None:
            return []
        return [
            {k: (v if v is not None else "") for k, v in row.items()}
            for row in csv.DictReader(io.StringIO(text))
        ]

    def read_json(self, name: str) -> Any:
        text = self._read_text(name, encoding="utf-8")
        return json.loads(text) if text else None

    def rename(self, src: str, dst: str) -> None:
        src_key, dst_key = self._key(src), self._key(dst)
        if not self.exists(src):
            return
        self._s3.copy_object(
            Bucket=self.bucket,
            Key=dst_key,
            CopySource={"Bucket": self.bucket, "Key": src_key},
        )
        self._s3.delete_object(Bucket=self.bucket, Key=src_key)

    def delete(self, name: str) -> None:
        try:
            self._s3.delete_object(Bucket=self.bucket, Key=self._key(name))
        except Exception:  # 不存在即成功
            pass

    def exists(self, name: str) -> bool:
        try:
            self._s3.head_object(Bucket=self.bucket, Key=self._key(name))
            return True
        except Exception:
            return False

    # ---- 内部

    def _read_text(self, name: str, *, encoding: str) -> str | None:
        try:
            resp = self._s3.get_object(Bucket=self.bucket, Key=self._key(name))
        except Exception:
            return None
        return resp["Body"].read().decode(encoding)


def create_storage(cfg) -> Storage:
    """按配置选择 Storage 实现。

    local（默认）：本地目录，方便开发调试；
    r2          ：生产环境，CSV/JSON 全部直接从 R2 读写，`_new.csv → _old.csv`
                  的改名也发生在 R2 里，因此 runner/CI **不需要任何本地快照**。
    """
    if cfg.storage_mode == "r2":
        return R2Storage(
            bucket=cfg.r2_bucket,
            endpoint_url=cfg.r2_endpoint_url,
            access_key_id=cfg.r2_access_key_id,
            secret_access_key=cfg.r2_secret_access_key,
            prefix=cfg.r2_prefix,
        )
    return LocalStorage(cfg.data_dir)
