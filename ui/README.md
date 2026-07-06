# GeoScan Control Console (Tauri + React shell)

现代化桌面外壳，按 `docs/superpowers/specs/2026-07-05-geoscan-ui-architecture-design.md`
实现：React 前端 + Tauri 壳 + Python JSONL 引擎宿主（`geoscan.engine_host`）。
所有领域逻辑仍在 Python；本目录不含任何私有模块，也不 import 它们。

## 架构

```
React (src/)  ──invoke──▶  Tauri (src-tauri/)  ──stdio JSONL──▶  python -m geoscan.engine_host
     ▲                          │                                      │
     └── engine-message 事件 ◀──┴── stdout 每行原样转发                └── run_production_program / batch_runner
```

- 请求：`{"id": 1, "cmd": "run_single", "args": {...}}` → 响应 `{"id": 1, "ok": true, "data": {...}}`
- 事件（无 id）：`log` / `stage` / `status` / `batch_row` / `result` / `hello`
- 阶段轨道的状态来自输出目录轮询 + 最终 PROGRAM_RUN_REPORT.json 映射——不造假进度。

## 开发

```powershell
cd ui
npm install
npm run tauri dev     # 需要 Rust 工具链；会自动 spawn 本仓库的 Python 引擎
```

环境变量：

- `GEOSCAN_PYTHON` — 指定 Python 解释器（默认 PATH 里的 `python`）
- `GEOSCAN_REPO` — 指定仓库根（默认按 dev 目录布局 `ui/../..` 推导）

引擎协议的测试在 `tests/test_engine_host.py`（`pytest` 直接跑，无需前端）。

## 打包（后续阶段）

设计文档 Phase 5：打包整合尚未做——当前 Tkinter GUI 仍是发布形态，
本外壳先在 dev 模式验收。打包时需决定 PyInstaller-first 还是 Tauri-first，
并保持私有模块排除与两层更新语义。
