# GeoScan

GeoScan 用来处理扫描地质剖面图。输入 TIFF/JPG，程序提取线、文字和区候选，再按你的选择导出 DXF、QGIS 对齐文件，或继续转成 MapGIS 6.7 的 `.WL` / `.WT`。

它不是自动判图软件。所有候选默认 `checked=no`，AI 也只做诊断建议；最终以人工复核为准。

作者：keros68
项目仓库：[keros68/geoscan](https://github.com/keros68/geoscan)
邮箱：keros68@gmal.com

> 中文为主，English summary below.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB.svg)](https://www.python.org/)
[![tests](https://github.com/keros68/geoscan/actions/workflows/tests.yml/badge.svg)](https://github.com/keros68/geoscan/actions/workflows/tests.yml)

## 适用场景

- 扫描图需要先变成可编辑的线和文字候选。
- 只想要 DXF / QGIS 文件，不想安装或检测 MapGIS。
- 已经有 MapGIS 6.7，需要继续转成 `.WL` / `.WT`。
- 一个文件夹里有多张图，要一张一张批量跑。

## 主要功能

- 可选输入调平；默认不动已经处理好的图。
- 提取线候选和文字候选。OCR 不可用时，会退回到内置区域检测。
- 输出类别可选：DXF、QGIS、MapGIS。没勾 MapGIS，就不会检测 MapGIS 环境和加密狗。
- QGIS 输出包含源 dpi 底图、同名 `.tfw`、毫米坐标 GeoJSON。
- MapGIS 输出经 SECTION / W60_Conv 转成 `.WL` / `.WT`，需要本机 MapGIS 6.7。
- 增强底图只方便人工看图，不参与识别。
- 支持单张、批量、自动更新。

## 不做什么

- 不判断地质含义，不猜边界、标注或地层代码。
- 不把 AI 建议写成最终成果，不写坐标，不写 `checked=yes`。
- 不把测试通过等同于图件合格。图件是否可用，仍看人工复核。
- 仓库不放地图、扫描件、标准文档、符号库或运行输出。
- API Key 只保存在本机，DPAPI 当前用户加密，不进源码、设置文件或日志。

## 工作流程

```text
打开扫描图
  ↓
确认 Map ID、输出目录和输出类别（DXF / QGIS / MapGIS）
  ↓
生成线候选、文字候选和预览底图
  ↓
按勾选项导出
  ├─ DXF：线/文字交换文件
  ├─ QGIS：源 dpi 底图 + .tfw + 毫米 GeoJSON
  └─ MapGIS：经 SECTION/W60 转成 .WL / .WT
  ↓
在 QGIS、MapGIS 或 CAD 里人工复核
```

每次运行都从原始图像重新生成结果，不回喂旧候选。

## 快速开始

### 普通用户（Windows 安装包）

到 [**Releases**](https://github.com/keros68/geoscan/releases/latest) 下载
`GeoScanSetup.exe`。安装不需要管理员权限，默认位置是
`%LOCALAPPDATA%\Programs\GeoScan`，也可以改到其他可写目录。设置保存在
`%LOCALAPPDATA%\GeoScan\config\`，升级和卸载不会删除。

点 **检查更新** 可以就地升级。普通更新只下载轻量 engine 包；运行时变了才下载完整安装包。

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

维护者打包和发布流程见 [`release/README.md`](release/README.md)。

## 环境依赖

- Python 3.12+，`numpy`、`opencv-python-headless`、`pillow`。
- 可选：`rapidocr` + `onnxruntime`（OCR）、`pywin32`（MapGIS 桥接，Windows）。
- DXF 导出需要 GDAL/ogr2ogr；安装包内置轻量 GDAL，源码运行可使用本机 QGIS/GDAL 或环境变量指定。
- MapGIS 桥接额外需要本机 MapGIS 6.7（含 SECTION）及其硬件加密狗——仅 DXF → `.WL`/`.WT` 这一步用到。

## License

MIT，见 [LICENSE](LICENSE)。本仓库只含程序本身，不含任何地图影像、扫描数据、标准文档、符号库或运行输出。

---

## English summary

**GeoScan** turns scanned geologic section maps into editable draft files. From a
TIFF/JPG scan, it can export DXF, QGIS alignment files (`.tfw` + millimetre-unit
GeoJSON), or MapGIS 6.7 `.WL` / `.WT` files. MapGIS is optional.

Every candidate stays **checked=no** until a person reviews it. GeoScan does not
guess boundaries, labels, or formation codes. AI features are diagnostic only:
they can point out likely issues, but they do not write final coordinates or mark
anything as verified. This repository contains only the program, not map data or
run outputs.

- Line and text candidate extraction.
- Selectable outputs: DXF, QGIS files, MapGIS files.
- Bundled GDAL in the Windows installer for DXF export.
- Tauri/React console, batch runner, and in-app updates from GitHub Releases.

Windows users: download `GeoScanSetup.exe` from
[Releases](https://github.com/keros68/geoscan/releases/latest). Developers:
`pip install -e ".[dev,ocr]"` then `pytest`. License: MIT.
