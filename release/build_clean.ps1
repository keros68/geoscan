<#
.SYNOPSIS
  Phase 0 clean build of GeoScan (frozen Python engine + Tauri console).

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
$spec = Join-Path $repoRoot "packaging\GeoScan.spec"
$distDir = Join-Path $repoRoot "dist\GeoScan"

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

# Both opencv wheels install into the SAME cv2/ package dir, so the uninstall
# above also deletes the files the headless wheel owns (cv2.pyd, __init__.py,
# ...), leaving a gutted cv2 that PyInstaller then collects as data-only —
# `import cv2` still "works" (empty namespace package) but every real call
# fails. The v0.1.9 installer shipped without cv2.pyd exactly this way.
# Force-reinstall headless to restore its files, then PROVE cv2 works.
Section "Restoring opencv-python-headless (shared cv2/ dir uninstall damage)"
$cvPin = (Get-Content (Join-Path $PSScriptRoot "requirements-runtime.txt") |
    Select-String "^opencv-python-headless==").Line.Trim()
if (-not $cvPin) { throw "opencv-python-headless pin not found in requirements-runtime.txt" }
& $venvPython -m pip install --force-reinstall --no-deps $cvPin
& $venvPython -c "import cv2, numpy; v = cv2.cvtColor(numpy.zeros((4,4,3), dtype=numpy.uint8), cv2.COLOR_BGR2GRAY); print('cv2', cv2.__version__, '+ numpy', numpy.__version__, 'OK')"
if ($LASTEXITCODE -ne 0) { throw "cv2 is broken in the build venv - aborting before PyInstaller collects a gutted bundle" }

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

# 5.5 Tauri console shell (primary UI since 0.2.0) ---------------------------
# Needs Node + Rust(MSVC) on the build machine. `tauri build` runs the
# frontend build (beforeBuildCommand) itself; bundling is off in tauri.conf,
# we only want the release exe, which then ships inside the Inno installer.
Section "Building Tauri console (GeoScanConsole.exe)"
$uiDir = Join-Path $repoRoot "ui"
if (-not (Test-Path (Join-Path $uiDir "node_modules"))) {
    Push-Location $uiDir
    try { npm install; if ($LASTEXITCODE -ne 0) { throw "npm install failed" } }
    finally { Pop-Location }
}
Push-Location $uiDir
try {
    npm run tauri build -- --no-bundle
    if ($LASTEXITCODE -ne 0) { throw "tauri build failed with exit code $LASTEXITCODE" }
} finally {
    Pop-Location
}
$consoleExe = Join-Path $uiDir "src-tauri\target\release\geoscan-console.exe"
if (-not (Test-Path $consoleExe)) { throw "console exe not found: $consoleExe" }
Copy-Item $consoleExe (Join-Path $distDir "GeoScanConsole.exe") -Force
Write-Host "GeoScanConsole.exe copied into dist" -ForegroundColor Green

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
# GeoScan.exe is a GUI-subsystem binary: `&` does NOT wait for it, so the old
# form reported success no matter what --check did. Start-Process -Wait blocks
# until it exits and surfaces the real exit code. (On a crash the PyInstaller
# error dialog holds the process open — close it to let the build fail.)
Section "Smoke test (--check)"
$smoke = Start-Process -FilePath (Join-Path $distDir "GeoScan.exe") -ArgumentList "--check" -Wait -PassThru
if ($smoke.ExitCode -ne 0) { throw "--check smoke test failed (exit $($smoke.ExitCode))" }
Write-Host "--check passed" -ForegroundColor Green

# 8.5 Engine-host smoke: the console talks to `GeoScan.exe --engine` over
# stdio JSONL; a one-shot ping proves the frozen engine path end to end.
# Start-Process file redirection gives the WINDOWED exe real std handles —
# a PowerShell pipeline does not, and the invalid handle only explodes on
# flush ([Errno 22]) inside the engine.
Section "Smoke test (--engine ping)"
$pingReq = Join-Path $env:TEMP "geoscan_engine_ping_req.jsonl"
$pingRes = Join-Path $env:TEMP "geoscan_engine_ping_res.jsonl"
'{"id":1,"cmd":"ping"}' | Set-Content $pingReq -Encoding ascii
$pingProc = Start-Process -FilePath (Join-Path $distDir "GeoScan.exe") -ArgumentList "--engine" `
    -RedirectStandardInput $pingReq -RedirectStandardOutput $pingRes -Wait -PassThru
$pingOut = Get-Content $pingRes -ErrorAction SilentlyContinue
$pingOk = $pingOut | Where-Object { $_ -match '"id":\s*1' -and $_ -match '"ok":\s*true' }
if (-not $pingOk) { throw "--engine ping smoke failed (exit $($pingProc.ExitCode)); output: $($pingOut -join ' | ')" }
Write-Host "--engine ping passed" -ForegroundColor Green

# 8.6 Private-tier guard: the dist becomes a PUBLIC release asset.
Section "Private-module leak check"
$privatePattern = 'native_direct|wl_from_scratch|wt_w60|wt_from_seed|mapgis_binary|mapgis_wl|mapgis_wt|mapgis_wp|native_format_lab|wt_native_diag|mapgis67_diagnostics|seed_templates'
$leaks = Get-ChildItem $distDir -Recurse -File | Where-Object { $_.Name -match $privatePattern }
if ($leaks) { throw "PRIVATE MODULE LEAKED INTO DIST: $($leaks.FullName -join '; ')" }
Write-Host "no private modules in dist" -ForegroundColor Green
Write-Host "`nBuild finished: $distDir" -ForegroundColor Green
if ($TrimGdal) {
    Write-Warning "GDAL was trimmed. RUN A REAL DXF EXPORT (line + text) before shipping to confirm no driver DLL is missing."
}
