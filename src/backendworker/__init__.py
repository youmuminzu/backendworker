"""backendworker —— 数据导入端（data importer）。

把 data-collector 采集清洗后的数据（CSV / JSON）导入 Cloudflare D1，
并维护 FTS5 全文索引。详见 DESIGN.md。
"""

__version__ = "0.1.0"
