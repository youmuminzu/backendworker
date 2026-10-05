# 自动化任务记忆：backendworker 数据导入程序开发

## 2026-10-04 23:0x（首次执行）

- 任务：按 `DESIGN.md` 完成 backendworker Phase 1 代码开发，完成后用 Windows cmd 关闭电脑。
- 结果：已完成全部 8 个模块 + 配置/DDL/文档，`py_compile` 通过；随后执行 `shutdown` 关机。
- 交付物清单：`src/backendworker/{__init__,__main__,config,storage,d1_client,tokenizer,meta_sync,product_sync,state,importer}.py`、
  `tables.sql`、`pyproject.toml`、`README.md`、`.env.example`、`.gitignore`。
- 未验证项：未实际运行导入（缺 `CLOUDFLARE_API_TOKEN`，且用户要求不跑测试）。首次运行前需 `uv sync`，
  依次执行 `--init-ddl` 建表、然后正式导入；建议先 `--dry-run` 看 diff 统计。
- 下次继续时的注意点：
  - Phase 2 需实现 `storage.R2Storage`（rename = copy + delete）并建 `.github/workflows/import.yml`。
  - D1 多语句脚本若被远端拒绝，客户端会自动降级为逐语句，日志会 WARN 提示，属预期行为。
