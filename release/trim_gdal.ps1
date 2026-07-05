<#
.SYNOPSIS
  Remove GDAL DLLs that are NOT in ogr2ogr's PE import closure. Safe by design.

.DESCRIPTION
  Computes the transitive import closure of ogr2ogr.exe + gdal*.dll using pefile,
  then only ever deletes bundle DLLs that are unreachable (true lazy plugins).
  DLLs in the import table are hard dependencies of gdal310.dll and are KEPT --
  deleting one stops gdal from loading at all.

  FINDING (2026-07-04): the QGIS-derived bundle in packaging/gdal_bundle links
  EVERY format driver (poppler/netcdf/hdf5/spatialite/xerces/jxl/...) directly
  into gdal310.dll's import table -> closure = 53 DLLs, 0 removable. So on this
  build this script correctly reports "nothing to trim". Meaningfully shrinking
  GDAL would require a custom minimal GDAL build (drivers disabled at compile
  time) -- out of scope. Kept here so a future/leaner GDAL is handled safely.

  DEFAULT = dry run. -Apply deletes only the verified-unreachable DLLs.

.EXAMPLE
  release\trim_gdal.ps1
  release\trim_gdal.ps1 -Apply
#>
[CmdletBinding()]
param(
    [string]$GdalDir,
    [string]$Python,
    [switch]$Apply
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $GdalDir) { $GdalDir = Join-Path $repoRoot "dist\GeoScan\gdal" }
if (-not (Test-Path $GdalDir)) { throw "GDAL dir not found: $GdalDir (build first)" }
if (-not $Python) {
    $venvPy = Join-Path $repoRoot ".venv_build\Scripts\python.exe"
    $Python = if (Test-Path $venvPy) { $venvPy } else { "python" }
}

# Import-closure analysis (needs pefile; it's a PyInstaller dep, so the build
# venv always has it).
$py = @'
import pefile, os, sys, json
gdal = sys.argv[1]
files = {f.lower(): f for f in os.listdir(gdal) if f.lower().endswith((".dll", ".exe"))}
def imports(path):
    out = set()
    try:
        pe = pefile.PE(path, fast_load=True)
        pe.parse_data_directories(directories=[
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"]])
        for attr in ("DIRECTORY_ENTRY_IMPORT", "DIRECTORY_ENTRY_DELAY_IMPORT"):
            for e in getattr(pe, attr, []) or []:
                out.add(e.dll.decode().lower())
        pe.close()
    except Exception:
        pass
    return out
seen = set()
queue = [f for f in files if f == "ogr2ogr.exe" or f.startswith("gdal")]
while queue:
    cur = queue.pop()
    if cur in seen or cur not in files:
        continue
    seen.add(cur)
    for dep in imports(os.path.join(gdal, files[cur])):
        if dep in files and dep not in seen:
            queue.append(dep)
removable = sorted(f for f in files if f not in seen and f.endswith(".dll"))
print(json.dumps({"reachable": len(seen), "removable": removable}))
'@
$tmp = Join-Path $env:TEMP "mapgis_gdal_closure.py"
Set-Content -Path $tmp -Value $py -Encoding utf8
$out = & $Python $tmp $GdalDir
Remove-Item $tmp -Force -ErrorAction SilentlyContinue
$info = $out | ConvertFrom-Json

Write-Host ("Import closure: {0} reachable DLLs in {1}" -f $info.reachable, $GdalDir)
$removable = @($info.removable)
if ($removable.Count -eq 0) {
    Write-Host "Nothing removable -- every bundled DLL is a hard import of gdal. (Expected for the QGIS build.)" -ForegroundColor Yellow
    return
}

$items = $removable | ForEach-Object { Get-Item (Join-Path $GdalDir $_) }
$reclaim = ($items | Measure-Object Length -Sum).Sum
$items | ForEach-Object { "  {0,8:N1} KB  {1}" -f ($_.Length / 1KB), $_.Name } | Write-Host
Write-Host ("Removable (unreachable): {0:N1} MB across {1} files" -f ($reclaim / 1MB), $items.Count) -ForegroundColor Cyan

if (-not $Apply) { Write-Host "`nDRY RUN. -Apply to delete these verified-unreachable DLLs, then re-run a DXF export." -ForegroundColor Yellow; return }
foreach ($f in $items) { Remove-Item $f.FullName -Force }
Write-Host ("`nDeleted {0} unreachable DLLs, reclaimed {1:N1} MB." -f $items.Count, ($reclaim / 1MB)) -ForegroundColor Green
Write-Warning "Even though these were outside the import closure, run one DXF export to be safe (GDAL can LoadLibrary plugins at runtime)."
