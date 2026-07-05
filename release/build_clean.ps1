<#
.SYNOPSIS
  Phase 0 clean build of the MapGIS vectorization GUI (one-folder).

.DESCRIPTION
  Builds in a dedicated clean virtualenv containing ONLY the real runtime deps
  (release/requirements-runtime.txt), so PyInstaller never collects the ~200 MB
  of scientific libs the app doesn't use. Then:
    - drops the non-headless opencv the rapidocr metadata drags in,
    - runs PyInstaller with the (excludes-hardened) spec,
    - mirrors build_gui_exe.cmd's post-copies (settings example, readme, gdal),
    - optionally trims unused GDAL format DLLs (-TrimGdal),
    - prints a size report and runs the --check smoke test.

  Expected result: ~350-380 MB, down from 636 MB. See release/README.md.

.EXAMPLE
  release\build_clean.ps1                 # reuse venv if present
  release\build_clean.ps1 -Recreate       # rebuild the venv from scratch
  release\build_clean.ps1 -TrimGdal       # also delete unused GDAL DLLs (verify DXF after!)
#>
[CmdletBinding()]
param(
    [switch]$Recreate,
    [switch]$TrimGdal,
    [string]$VenvDir
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $VenvDir) { $VenvDir = Join-Path $repoRoot ".venv_build" }
$venvPython = Join-Path $VenvDir "Scripts\python.exe"
$spec = Join-Path $repoRoot "packaging\mapgis_vectorize_gui.spec"
$distDir = Join-Path $repoRoot "dist\mapgis_vectorize_gui"

function Section($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }

# 1. Clean venv -------------------------------------------------------------
if ($Recreate -and (Test-Path $VenvDir)) {
    Section "Removing existing venv $VenvDir"
    Remove-Item -Recurse -Force $VenvDir
}
if (-not (Test-Path $venvPython)) {
    Section "Creating clean venv at $VenvDir"
    py -3.12 -m venv $VenvDir
    if (-not (Test-Path $venvPython)) { python -m venv $VenvDir }
}
if (-not (Test-Path $venvPython)) { throw "Failed to create venv python at $venvPython" }

# 2. Install ONLY runtime deps ---------------------------------------------
Section "Installing runtime dependencies (clean set)"
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r (Join-Path $PSScriptRoot "requirements-runtime.txt")

# rapidocr's metadata requires opencv-python (GUI wheel); we only need headless.
Section "Dropping non-headless opencv (rapidocr only needs cv2)"
& $venvPython -m pip uninstall -y opencv-python 2>$null | Out-Null

# 3. Guardrail: fail loudly if bloat leaked back in -------------------------
Section "Verifying no known bloat libs are installed"
$installed = & $venvPython -m pip list --format=freeze
$bloat = @("scipy", "numba", "llvmlite", "pandas", "pymatting", "scikit-image", "scikit-learn", "seaborn")
$leaked = @()
foreach ($b in $bloat) {
    if ($installed | Select-String -SimpleMatch "$b==") { $leaked += $b }
}
if ($leaked.Count -gt 0) {
    Write-Warning "Bloat libs present in build venv: $($leaked -join ', '). The spec excludes them, but a clean venv is better. Consider -Recreate."
} else {
    Write-Host "OK - clean venv, no scipy/numba/pandas/etc." -ForegroundColor Green
}
if ($installed | Select-String -SimpleMatch "opencv-python==") {
    Write-Warning "Non-headless opencv-python still present; both wheels will be bundled."
}

# 4. PyInstaller (from the clean venv so it sees the clean site-packages) ----
Section "Running PyInstaller"
Push-Location $repoRoot
try {
    & $venvPython -m PyInstaller $spec --noconfirm
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
} finally {
    Pop-Location
}

# 5. Post-copies (mirror packaging/build_gui_exe.cmd) -----------------------
Section "Copying settings example, readme, GDAL bundle"
Copy-Item (Join-Path $repoRoot "packaging\mapgis_settings.example.json") $distDir -Force -ErrorAction SilentlyContinue
Get-ChildItem (Join-Path $repoRoot "packaging\*.txt") -ErrorAction SilentlyContinue |
    ForEach-Object { Copy-Item $_.FullName $distDir -Force }
$gdalSrc = Join-Path $repoRoot "packaging\gdal_bundle"
if (Test-Path (Join-Path $gdalSrc "ogr2ogr.exe")) {
    robocopy $gdalSrc (Join-Path $distDir "gdal") /E /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "GDAL bundle copy failed (robocopy $LASTEXITCODE)" }
} else {
    Write-Warning "packaging\gdal_bundle\ogr2ogr.exe not found - packaged app will need QGIS or MAPGIS_OGR2OGR."
}

# 6. Optional GDAL DLL trim -------------------------------------------------
if ($TrimGdal) {
    Section "Trimming unused GDAL DLLs"
    & (Join-Path $PSScriptRoot "trim_gdal.ps1") -GdalDir (Join-Path $distDir "gdal") -Apply
}

# 7. Size report ------------------------------------------------------------
Section "Size report"
$total = (Get-ChildItem $distDir -Recurse -File | Measure-Object Length -Sum).Sum
"TOTAL dist: {0:N1} MB" -f ($total / 1MB) | Write-Host -ForegroundColor Green
Get-ChildItem $distDir | ForEach-Object {
    $sz = if ($_.PSIsContainer) { (Get-ChildItem $_.FullName -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum } else { $_.Length }
    [PSCustomObject]@{ MB = [math]::Round($sz / 1MB, 1); Name = $_.Name }
} | Sort-Object MB -Descending | Select-Object -First 8 | Format-Table -AutoSize

# 8. Smoke test -------------------------------------------------------------
Section "Smoke test (--check)"
& (Join-Path $distDir "mapgis_vectorize_gui.exe") --check
Write-Host "`nBuild finished: $distDir" -ForegroundColor Green
if ($TrimGdal) {
    Write-Warning "GDAL was trimmed. RUN A REAL DXF EXPORT (line + text) before shipping to confirm no driver DLL is missing."
}
