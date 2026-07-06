# GeoScan

GeoScan 是面向扫描地质剖面图的半自动矢量化工具：把 TIFF/JPG 扫描图转换成可人工复核的线、文字和区候选，并按需输出 DXF、QGIS 对齐文件（`.tfw` + 毫米坐标 GeoJSON）或 MapGIS 6.7 `.WL` / `.WT` 文件。MapGIS 只是可选转换目标，不是运行 GeoScan 的必需环境。

它只产出**待核查**候选，不发明任何地质内容。每条线、每个文字候选都保持 `checked=no`，最终以人工复核为准。工具不猜地质界线、不猜标注、不猜地层代码——它做的是"把扫描图转成可编辑草稿"，不是"自动读懂这张地质图"。

作者：keros68
项目仓库：[keros68/geoscan](https://github.com/keros68/geoscan)
邮箱：keros68@gmal.com

> 中文为主，English summary below.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB.svg)](https://www.python.org/)
[![tests](https://github.com/keros68/geoscan/actions/workflows/tests.yml/badge.svg)](https://github.com/keros68/geoscan/actions/workflows/tests.yml)

## 适用场景

- 手里有老的扫描地质剖面图（纸图扫描件、TIFF/JPG），想转成可编辑矢量再手工整饰。
- 需要把扫描图里的线要素、文字标注提取成 DXF / GeoJSON，导入 QGIS、MapGIS 或任意 GIS/CAD 编辑器。
- 需要 QGIS 直接叠加检查：输出源 dpi 底图、同名 `.tfw` world file 和毫米坐标 GeoJSON。
- 使用 MapGIS 6.7 时，可把候选进一步转成原生 `.WL` / `.WT`，在 SECTION 里做叠加检查和手工编辑。
- 要批量处理一整个文件夹的扫描图，一张一张跑、可断点续跑。

## 它做什么

- **输入调平**（可选）：纠偏、把扫描件转成干净的 RGB TIFF；默认关闭，已处理过的图不会被二次处理。
- **线提取**：Hough 直线（默认）或中心线追踪（直线 + 曲线 + 闭合环）。
- **文字候选**：OCR（rapidocr）或内置兜底区域检测器。
- **输出类别可选**：运行前可选择 MapGIS、DXF、QGIS 输出；不勾选 MapGIS 时不会跑 MapGIS 环境/加密狗检测。
- **DXF 导出**：经 GDAL/ogr2ogr 出 DXF，QGIS、MapGIS 或任意支持 DXF 的 GIS/CAD 都能装入。
- **QGIS 对齐文件**：交付目录写出源 dpi 底图、同名 `.tfw` world file、毫米坐标 GeoJSON，QGIS 直接叠加查看。
- **MapGIS 6.7 桥接**（可选，需 Windows + MapGIS）：驱动 SECTION / W60_Conv 把 DXF 转成原生 `.WL` / `.WT`。
- **人工看图增强底图**（锐化/对比度，几何不变）：只用于人工编辑时看得更清，从不参与 OCR/矢量化。
- **Tauri 控制台 + 批量运行**：单张或整个文件夹，图形界面或命令行皆可。
- **就地自动更新**：界面里点「检查更新」即可从 GitHub Releases 拉新版；优先轻量更新引擎，必要时回退整包安装，配置不丢。

## 不做什么

- **不发明地质内容**：不猜边界、不猜标注、不猜地层代码；所有候选一律 `checked=no`。
- **AI 只做复审**：可以分类、提示、给建议，永远不写坐标、地质解释或 `checked=yes`。
- **不把候选当成成果**：测试通过只证明代码路径正常，矢量化质量最终以人工复核为准。
- **不在本仓库存放任何地图数据**：不含地图影像、扫描件、标准文档、符号库或运行输出——这些是用户自己的资料，另存于独立数据工作区。
- **不联网偷跑**：API Key 由用户自己配置，DPAPI 当前用户加密存储，永不进源码、设置文件或日志。

## 工作流程

```text
扫描地质图 (TIFF / JPG)
  ↓  00 输入冻结 + 可选调平（保留源图 dpi；内部另写像素单位预览图）
  ↓  04 线候选：Hough 直线 / 中心线追踪
  ↓  05 文字候选：OCR 或兜底检测器
  ↓  输出类别选择：DXF / QGIS / MapGIS
  ↓  DXF / GeoJSON 导出（坐标缩放到图幅毫米单位，经 ogr2ogr，可选）
  ↓  QGIS 对齐文件（源 dpi TIFF + 同名 .tfw + 毫米坐标 GeoJSON，可选）
  ↓  08 SECTION/W60 桥接（可选，需 MapGIS）→ 校验过的 .WL / .WT
  ↓
交付文件夹  ——  源 dpi 底图 + 可选 .tfw / 毫米 GeoJSON / DXF / .WL / .WT
```

每次运行都从原始图像重新生成全部结果，绝不回喂旧候选。

## 快速开始

### 普通用户（Windows 安装包）

到 [**Releases**](https://github.com/keros68/geoscan/releases/latest) 页下载最新的
`GeoScanSetup.exe` 运行。安装**无需管理员权限**，默认装到当前用户目录
（`%LOCALAPPDATA%\Programs\GeoScan`），也可在安装向导里自选任意可写文件夹——
装到哪里不受限制。你的设置存在 `%LOCALAPPDATA%\GeoScan\config\`，升级/卸载都不动它。

打开程序 → 点 **检查更新**：有新版会自动下载、校验并更新。普通代码更新通常只下载轻量
engine 包；运行时变化时才会下载完整安装包。你的工具路径和 API Key 都保留。

### 开发者

```bash
pip install -e ".[dev,ocr]"
pytest
```

```bash
# 处理单张图（把 --source-raster 指向数据工作区里的一张图）
python -m geoscan.production_program run --source-raster <path>\t01_XXXX.tif --map-id T01_XXXX --conversion-mode cli

# 批量处理一个文件夹（可续跑）
python -m geoscan.batch_runner run --project-root <workdir> --source-dir <tiff-folder> --conversion-mode cli

# 图形界面（Tauri 控制台，开发模式；需要 Node + Rust/MSVC）
cd ui && npm run tauri dev
```

维护者出安装包 + 发布新版（喂给自动更新）的完整流程见 [`release/README.md`](release/README.md)。

## 环境依赖

- Python 3.12+，`numpy`、`opencv-python-headless`、`pillow`。
- 可选：`rapidocr` + `onnxruntime`（OCR）、`pywin32`（MapGIS 桥接，Windows）。
- DXF 导出需要 GDAL/ogr2ogr；安装包内置轻量 GDAL，源码运行可使用本机 QGIS/GDAL 或环境变量指定。
- MapGIS 桥接额外需要本机 MapGIS 6.7（含 SECTION）及其硬件加密狗——仅 DXF → `.WL`/`.WT` 这一步用到。

## License

MIT，见 [LICENSE](LICENSE)。本仓库只含程序本身，不含任何地图影像、扫描数据、标准文档、符号库或运行输出。

---

## English summary

**GeoScan** semi-automatically vectorizes scanned geologic section maps into
reviewable vector candidates. From a TIFF/JPG scan, it can produce DXF,
QGIS-aligned files (`.tfw` + millimetre-unit GeoJSON), and optionally MapGIS 6.7
`.WL` / `.WT` files. MapGIS is an optional output target, not a required runtime
environment.

Every candidate stays **checked=no** until a human reviews it. The tool never
invents geological content (no guessed boundaries, labels, or formation codes),
and any AI stage is review-only: it may classify or suggest, never write
coordinates or mark anything as verified. This repository ships **only the
program** — no map images, scan data, standards, symbol libraries, or run
outputs.

- **Line extraction**: Hough straight lines or centerline tracing.
- **Text candidates**: OCR (rapidocr) or a fallback region detector.
- **Selectable outputs**: DXF, QGIS alignment files, and optional MapGIS 6.7
  bridge output.
- **DXF export** via GDAL/ogr2ogr; the Windows installer bundles a lightweight
  GDAL runtime.
- **Tauri/React console + batch runner**, and **in-app auto-update** from GitHub
  Releases.

Windows users: download `GeoScanSetup.exe` from
[Releases](https://github.com/keros68/geoscan/releases/latest). Developers:
`pip install -e ".[dev,ocr]"` then `pytest`. License: MIT.
