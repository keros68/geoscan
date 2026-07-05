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

## 发布一个新版本（GitHub Releases）

自动升级依赖「发布版本 = 一个带安装包资产的 GitHub Release」。发布步骤：

```powershell
# 0. 先把 src/geoscan/__init__.py 的 __version__ 和 installer.iss 的 AppVersion
#    一起 bump 到新版本号（例如 0.2.0），提交并推送。

# 1. 干净构建 + 出安装包（见上）
release\build_clean.ps1
ISCC release\installer\installer.iss

# 2. 打 tag 并发布，把安装包作为 Release 资产上传
#    tag 名用 v<版本>，客户端会 strip 掉前导 v 再和 __version__ 比对。
gh release create v0.2.0 `
  dist\installer\MapGISVectorizeSetup.exe `
  --title "GeoScan v0.2.0" `
  --notes "本次更新内容：..."
```

发布后，任何已安装的旧版点「检查更新」就会看到 v0.2.0、下载安装包、就地升级。

> **版本号三处必须一致**：`src/geoscan/__init__.py` 的 `__version__`、
> `installer.iss` 的 `AppVersion`、GitHub Release 的 tag（`v<版本>`）。
> 客户端比较的是 Release tag 对 `__version__`；对不上就不会提示更新。

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
