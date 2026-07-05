# release/ — 打包 / 安装 / 发布 / 自动升级

本目录是发布工具链，**与 `packaging/` 并存**：`packaging/` 是 PyInstaller spec +
GDAL bundle 的所在，本目录提供「干净构建 → 瘦身 → 出安装包 → 发布到 GitHub
Releases → 客户端自动升级」的外层流程。

## 组成

| 文件 | 作用 | 状态 |
|---|---|---|
| `requirements-runtime.txt` | 仅列真实运行时依赖（干净 venv 用） | ✅ 可用 |
| `build_clean.ps1` | 干净 venv 构建 + 瘦身 + 冒烟 | ✅ 可用 |
| `trim_gdal.ps1` | GDAL 裁剪（默认 dry-run，`-Apply` 才删） | ✅ 可用 |
| `installer/installer.iss` | Inno Setup 出 `MapGISVectorizeSetup.exe` | ✅ 可用 |

客户端自动升级的**程序侧**逻辑不在本目录，而在 `src/geoscan/updater.py`
（对 GitHub Releases 的 `releases/latest` 查询 / 下载 / 校验 / 换装），并由
`production_gui.py` 的「检查更新」按钮触发。测试见 `tests/test_updater.py`。

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

## 发布一个新版本（GitHub Releases）

```powershell
# 0. bump 版本号（三处必须一致）：src/geoscan/__init__.py 的 __version__、
#    installer.iss 的 AppVersion、以及下面的 Release tag v<版本>。提交并推送。

# 1. 构建冻结程序（当前环境直接打包；-Recreate 走干净 venv 减体积）
python -m PyInstaller packaging\GeoScan.spec --noconfirm     # 或 release\build_clean.ps1

# 2. 构建两个资产
python release\build_engine_zip.py             # -> dist\engine-<版本>-rt<N>.zip（轻量）
ISCC release\installer\installer.iss           # -> dist\installer\GeoScanSetup.exe（整包）

# 3. 发布，同时上传两个资产
gh release create v0.2.0 `
  dist\installer\GeoScanSetup.exe `
  dist\engine-0.2.0-rt1.zip `
  --title "GeoScan v0.2.0" --notes "本次更新内容：..."
```

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
