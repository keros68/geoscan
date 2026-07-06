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

## 打包（Phase 5，已整合）

发布形态是 **PyInstaller-first 混合**：`release\build_clean.ps1` 构建冻结
Python 应用后，追加 `npm run tauri build -- --no-bundle` 产出
`geoscan-console.exe`，拷入 `dist/GeoScan/GeoScanConsole.exe`，随 Inno 安装包
一起分发。安装布局下控制台 spawn 同目录的 `GeoScan.exe --engine`（见上方
解析顺序），因此终端机器**不需要 Python/Node/Rust**，只需要 WebView2
（Win11 自带）。

- 轻量更新：engine zip 换 `_internal/engine/` 里的 geoscan 源码 →
  控制台只重启引擎进程，窗口不中断（`apply_engine_update` 命令）。
- 整包更新：控制台下载安装器、启动后自动关窗（`download_installer_update`）。
- 经典 tkinter 界面与 `GeoScan.exe --batch` 保持可用（开始菜单“经典界面”）。

详见 `release/README.md` 的“0.2.0 起的双界面布局”。
