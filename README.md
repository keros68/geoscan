# GeoScan

把扫描的旧地质剖面图半自动矢量化成可编辑矢量候选（DXF / GeoJSON）的工具，并可选桥接到 MapGIS 6.7 的 `.WL` / `.WT` 文件。它读入一张扫描地质图，跑线提取、文字识别和格式转换，产出一批可以在 GIS 编辑器里继续核对修改的线/文字候选。

它只产出**待核查**候选，不发明任何地质内容。每条线、每个文字候选都标记为 `unchecked`，需要人工在 MapGIS/QGIS 里逐个核对、修正、确认。工具不猜地质界线、不猜标注、不猜地层代码——它做的是"把扫描图转成可编辑草稿"，不是"自动读懂这张地质图"。

> 中文为主，English summary below.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB.svg)](https://www.python.org/)
[![tests](https://github.com/keros68/geoscan/actions/workflows/tests.yml/badge.svg)](https://github.com/keros68/geoscan/actions/workflows/tests.yml)

## 适用场景

- 手里有老的扫描地质剖面图（纸图扫描件、300dpi TIFF/JPG），想转成可编辑矢量再手工整饰。
- 需要把扫描图里的线要素、文字标注提取成 DXF / GeoJSON，导入 QGIS、MapGIS 或任意 GIS 编辑器。
- 使用 MapGIS 6.7，想把候选进一步转成原生 `.WL` / `.WT`，在 SECTION 里做叠加检查和手工编辑。
- 要批量处理一整个文件夹的扫描图，一张一张跑、可断点续跑。

## 它做什么

- **输入调平**（可选）：纠偏、把扫描件转成干净的 RGB TIFF；默认关闭，已处理过的图不会被二次处理。
- **线提取**：Hough 直线（默认）或中心线追踪（直线 + 曲线 + 闭合环）。
- **文字候选**：OCR（rapidocr）或内置兜底区域检测器。
- **DXF 导出**：经 GDAL/ogr2ogr 出 DXF，QGIS、MapGIS 或任意支持 DXF 的 GIS 都能装入。
- **MapGIS 6.7 桥接**（需 Windows + MapGIS）：驱动 SECTION / W60_Conv 把 DXF 转成原生 `.WL` / `.WT`。
- **人工看图增强底图**（锐化/对比度，几何不变）：只用于人工编辑时看得更清，从不参与 OCR/矢量化。
- **GUI + 批量运行**：单张或整个文件夹，图形界面或命令行皆可。
- **就地自动更新**：GUI 里点「检查更新」即可从 GitHub Releases 拉新版覆盖安装，配置不丢。

## 不做什么

- **不发明地质内容**：不猜边界、不猜标注、不猜地层代码；所有候选一律 `unchecked`。
- **AI 只做复审**：可以分类、提示、给建议，永远不写坐标、地质解释或 `checked=yes`。
- **不把候选当成成果**：测试通过只证明代码路径正常，矢量化质量仍需人工在 MapGIS 里验收。
- **不在本仓库存放任何地图数据**：不含地图影像、扫描件、标准文档、符号库或运行输出——这些是用户自己的资料，另存于独立数据工作区。
- **不联网偷跑**：API Key 由用户自己配置，DPAPI 当前用户加密存储，永不进源码、设置文件或日志。

## 工作流程

```text
扫描地质图 (TIFF / JPG)
  ↓  00 输入冻结 + 可选调平（写成 1px=1 MapGIS 单位的像素图）
  ↓  04 线候选：Hough 直线 / 中心线追踪
  ↓  05 文字候选：OCR 或兜底检测器
  ↓  DXF 导出（GeoJSON → DXF，经 ogr2ogr）
  ↓  08 SECTION/W60 桥接（可选，需 MapGIS）→ 校验过的 .WL / .WT
  ↓
MAPGIS_LOAD_READY  ——  单一交付文件夹：像素底图 + .WL/.WT + 线/文字 DXF
```

每次运行都从原始图像重新生成全部结果，绝不回喂旧候选。

## 快速开始

### 普通用户（Windows 安装包）

到 [**Releases**](https://github.com/keros68/geoscan/releases/latest) 页下载最新的
`GeoScanSetup.exe` 运行。安装**无需管理员权限**，默认装到当前用户目录
（`%LOCALAPPDATA%\Programs\GeoScan`），也可在安装向导里自选任意可写文件夹——
装到哪里不受限制。你的设置存在 `%LOCALAPPDATA%\GeoScan\config\`，升级/卸载都不动它。

打开程序 → **设置** 页 → **检查更新**：有新版会自动下载安装包、校验后就地覆盖，
你的工具路径和 API Key 都保留。

### 开发者

```bash
pip install -e ".[dev,ocr,gui]"
pytest
```

```bash
# 处理单张图（把 --source-raster 指向数据工作区里的一张图）
python -m geoscan.production_program run --source-raster <path>\t01_XXXX.tif --map-id T01_XXXX --conversion-mode cli

# 批量处理一个文件夹（可续跑）
python -m geoscan.batch_runner run --project-root <workdir> --source-dir <tiff-folder> --conversion-mode cli

# 图形界面
python -m geoscan.production_gui
```

维护者出安装包 + 发布新版（喂给自动更新）的完整流程见 [`release/README.md`](release/README.md)。

## 环境依赖

- Python 3.12+，`numpy`、`opencv-python-headless`、`pillow`。
- 可选：`rapidocr` + `onnxruntime`（OCR）、`pywin32`（MapGIS 桥接，Windows）、`sv-ttk`（GUI 主题）。
- DXF 导出需要 GDAL/ogr2ogr（构建时打包，见 `release/`）。
- MapGIS 桥接额外需要本机 MapGIS 6.7（含 SECTION）及其硬件加密狗——仅 DXF → `.WL`/`.WT` 这一步用到。

## License

MIT，见 [LICENSE](LICENSE)。本仓库只含程序本身，不含任何地图影像、扫描数据、标准文档、符号库或运行输出。

---

## English summary

**GeoScan** semi-automatically vectorizes scanned geologic section maps into
editable vector candidates (DXF / GeoJSON), with an optional bridge to MapGIS 6.7
`.WL` / `.WT` files. It runs line extraction, text/OCR, and format conversion to
produce line/text candidates a human then corrects in a GIS editor.

Every candidate is marked **unchecked** — the tool never invents geological
content (no guessed boundaries, labels, or formation codes), and any AI stage is
review-only: it may classify or suggest, never write coordinates or mark
anything as verified. This repository ships **only the program** — no map images,
scan data, standards, symbol libraries, or run outputs.

- **Line extraction**: Hough straight lines or centerline tracing.
- **Text candidates**: OCR (rapidocr) or a fallback region detector.
- **DXF export** via GDAL/ogr2ogr; **MapGIS 6.7 bridge** (Windows + MapGIS) drives
  SECTION / W60_Conv to native `.WL` / `.WT`.
- **GUI + batch runner**, and **in-app auto-update** from GitHub Releases
  (Settings → 检查更新).

Windows users: download `GeoScanSetup.exe` from
[Releases](https://github.com/keros68/geoscan/releases/latest). Developers:
`pip install -e ".[dev,ocr,gui]"` then `pytest`. License: MIT.
