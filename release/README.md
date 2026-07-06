# release/ — 打包 / 安装 / 发布 / 自动升级

本目录是发布工具链，**与 `packaging/` 并存**：`packaging/` 是 PyInstaller spec +
GDAL bundle 的所在，本目录提供「干净构建 → 瘦身 → 出安装包 → 发布到 GitHub
Releases → 客户端自动升级」的外层流程。

## 组成

| 文件 | 作用 | 状态 |
|---|---|---|
| `requirements-runtime.txt` | 仅列真实运行时依赖（干净 venv 用） | ✅ 可用 |
| `build_clean.ps1` | 干净 venv 构建 + Tauri 控制台 + 瘦身 + 冒烟 | ✅ 可用 |
| `trim_gdal.ps1` | GDAL 裁剪（默认 dry-run，`-Apply` 才删） | ✅ 可用 |
| `installer/installer.iss` | Inno Setup 出 `GeoScanSetup.exe` | ✅ 可用 |

客户端自动升级的**程序侧**逻辑不在本目录，而在 `src/geoscan/updater.py`
（对 GitHub Releases 的 `releases/latest` 查询 / 下载 / 校验 / 换装），由
新控制台（engine_host 的 `check_update`/`apply_engine_update` 命令）和经典
GUI 的「检查更新」按钮共同复用。测试见 `tests/test_updater.py`。

## 0.2.0 起的双界面布局

安装目录内并存两个界面，共享同一个冻结 Python 运行时与两层更新：

```
GeoScanConsole.exe   主界面（Tauri/React 控制台；开始菜单/桌面快捷方式指向它）
GeoScan.exe          经典 tkinter 界面（保留一个版本线作后备入口）
GeoScan.exe --engine 控制台后台引擎（JSONL over stdio；控制台自动拉起，用户不用管）
_internal\           冻结运行时 + engine\ 松散 geoscan 代码（引擎 zip 更新的目标）
gdal\                自带 ogr2ogr
```

控制台的引擎解析顺序在 `ui/src-tauri/src/main.rs`：`GEOSCAN_PYTHON` 环境变量
（开发覆盖）→ 同目录 `GeoScan.exe --engine`（安装布局）→ 仓库内 `python -m
geoscan.engine_host`（`tauri dev` 回退）。**构建机需要 Node + Rust(MSVC)**，
`build_clean.ps1` 会自动 `npm install` 并 `tauri build --no-bundle`。

轻量引擎更新对控制台同样生效：`apply_engine_update` 换掉 `_internal/engine/`
后控制台只重启引擎进程，窗口不动；整包更新则由控制台下载安装器、自动关窗交给
Inno（`CloseApplications=yes` 兜底）。终端用户机器需要 WebView2 运行库
（Win11 自带；安装器检测缺失时提示，不阻塞——经典界面不依赖它）。

## 构建安装包（Phase 0）

```powershell
# 1. 干净构建（首次建 .venv_build 只装真实依赖）
release\build_clean.ps1                 # 复用已有 venv
release\build_clean.ps1 -Recreate       # 从零重建 venv（换依赖版本时）

# 2.（可选）GDAL 裁剪
release\trim_gdal.ps1                    # 闭包安全，只删导入闭包外的 DLL

# 3. 出安装包（需先装 Inno Setup 6，用 ISCC 编译）
ISCC release\installer\installer.iss     # 产出 dist\installer\MapGISVectorizeSetup.exe
```

安装后：程序装在 `C:\Program Files\MapGISVectorize\`（纯 ASCII，GDAL 友好），
用户配置写在 `%LOCALAPPDATA%\MapGISVectorize\config\`，**升级/卸载都不动它**。
这正是就地自动升级安全的原因：升级器重跑安装包，用户的工具路径 + API Key 都保留。

## 两层更新模型

程序装成两层：**运行时层**（冻结的 cv2/onnx/numpy/rapidocr，~100MB，极少变）+
**引擎层**（松散的 `geoscan` 代码，在 `_internal/engine/`，~180KB，每版都变）。
所以代码更新只发一个 `engine-<版本>-rt<N>.zip`（~180KB），客户端覆盖松散文件夹即可；
只有运行时变了（换 numpy/cv2 大版本）才回退整包安装器。

- 客户端怎么知道自己的运行时版本：读 `_internal/runtime_version.txt`（内容如 `1`）。
- 引擎包命名 `engine-<引擎版本>-rt<运行时版本>.zip`，客户端只接受 `rt` 号与本机一致的引擎包。
- **换了重型依赖时**：把 `packaging/runtime_version.txt` 的数字 +1，那一版起客户端会走整包安装器。

## 内置 GDAL（离线 DXF 导出）

DXF 导出要 `ogr2ogr`。为让**没装 QGIS** 的机器也能导出，程序在自己文件夹里带一个
`gdal/`（`ogr2ogr.exe` + 其 DLL 依赖闭包 + `gdal-data`，约 120MB；不含 800MB 的 PROJ
网格，像素单位导出用不上）。`bundled_gdal_dir()` 会在 `<exe>/gdal/ogr2ogr.exe` 找到它。

`packaging/gdal_bundle/` 是 git-ignored 的第三方二进制，需要时从一份 QGIS 现装重建：

```powershell
python release\build_gdal_bundle.py "D:\Qgis"   # 用 objdump 算 ogr2ogr 的 DLL 闭包
```

GDAL 属于**运行时层**（稳定）。加/换 GDAL 时把 `packaging/runtime_version.txt` +1，
让老用户走整包安装器拿到它。

## 发布一个新版本（GitHub Releases）

```powershell
# 0. bump 版本号（三处必须一致）：src/geoscan/__init__.py 的 __version__、
#    installer.iss 的 AppVersion、以及下面的 Release tag v<版本>。提交并推送。
#    （若换了运行时依赖，另把 packaging/runtime_version.txt +1）

# 1. 构建冻结程序，并把 GDAL 包复制到 dist\GeoScan\gdal（顶层，与 exe 同级）
python -m PyInstaller packaging\GeoScan.spec --noconfirm     # 或 release\build_clean.ps1（会自动复制 gdal）
Copy-Item -Recurse packaging\gdal_bundle dist\GeoScan\gdal   # 直接 PyInstaller 时手动复制

# 2. 构建两个资产
python release\build_engine_zip.py             # -> dist\engine-<版本>-rt<N>.zip（轻量）
ISCC release\installer\installer.iss           # -> dist\installer\GeoScanSetup.exe（整包，含 gdal）

# 3. 发布，同时上传两个资产
gh release create v<版本> `
  dist\installer\GeoScanSetup.exe `
  dist\engine-<版本>-rt<N>.zip `
  --title "GeoScan v<版本>" --notes "本次更新内容：..."
```

> 0.2.0 特例：runtime line 2→3（安装布局加入了 GeoScanConsole.exe），老客户端
> 不会匹配 rt3 引擎包，自动回退整包安装器——这是有意的，engine zip 无法送达
> 控制台 exe。

发布后：已在两层版（≥0.1.3）上的用户点「检查更新」→ 只下 ~180KB 引擎包 → 自动重启生效；
运行时号变了或还在旧单层版的用户 → 回退整包 `GeoScanSetup.exe`。

> **版本号三处必须一致**：`__version__`、`installer.iss` AppVersion、Release tag `v<版本>`。
> 客户端比较 Release tag 对本机 `__version__`；对不上就不会提示更新。

## 安全边界（不可破坏）

- **公开仓库 = 零凭据下载**：Release 资产任何人可下载，`updater.py` 不含任何密钥。
- **校验**：Release 资产若带 sha256 digest（GitHub 现会记录），下载后逐字节校验；
  传输本身走 HTTPS。
- **配置不入包**：`mapgis_settings.json` / DPAPI 加密的 `mapgis_ai_key.dat` 只在
  `%LOCALAPPDATA%`，永不进安装包、不进仓库、不进日志。

## 为什么这么设计（要点）

- **体积**：636MB 的一半是 app 从没 import 的科学计算库（PyMatting→numba/llvmlite、
  scikit→scipy、seaborn→pandas）。干净 venv 直接不装它们；spec excludes 兜底。
- **配置不丢**：旧版把配置写在 exe 同目录，装进 Program Files 会需要管理员、且升级被
  覆盖。迁到 `%LOCALAPPDATA%` 后天然隔离；旧配置首启动自动迁入。
- **GDAL 与中文路径**：`gdal`（GDAL_DATA/PROJ_LIB）永远随程序装在 ASCII 的 Program
  Files，规避 ogr2ogr 的非 ASCII 路径 bug。
- **整包升级而非增量**：开源分发下，GitHub Releases 免费托管整包安装器，客户端整包
  覆盖升级最简单、最稳。若将来更新频繁到整包令人痛，再考虑分层增量。
