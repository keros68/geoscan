@echo off
setlocal
rem Build the standalone GUI (one-folder). Run from anywhere; works from repo root.
set "REPO_ROOT=%~dp0.."
pushd "%REPO_ROOT%" >nul

where pyinstaller >nul 2>nul
if errorlevel 1 (
  echo PyInstaller not found. Install it first:  pip install pyinstaller
  popd >nul
  exit /b 1
)

pyinstaller packaging\mapgis_vectorize_gui.spec --noconfirm
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
  echo Build failed with exit code %EXIT_CODE%.
  popd >nul
  exit /b %EXIT_CODE%
)

echo.
echo Build finished: %REPO_ROOT%\dist\mapgis_vectorize_gui\
copy /Y "packaging\mapgis_settings.example.json" "dist\mapgis_vectorize_gui\mapgis_settings.example.json" >nul
rem Copy the operator readme (Chinese filename; wildcard keeps this .cmd pure ASCII).
for %%f in ("packaging\*.txt") do copy /Y "%%f" "dist\mapgis_vectorize_gui\" >nul
if exist "packaging\gdal_bundle\ogr2ogr.exe" (
  echo Copying bundled GDAL so colleague machines do not need QGIS...
  robocopy "packaging\gdal_bundle" "dist\mapgis_vectorize_gui\gdal" /E /NFL /NDL /NJH /NJS /NP >nul
  if errorlevel 8 (
    echo Failed to copy the GDAL bundle.
    popd >nul
    exit /b 1
  )
) else (
  echo NOTE: packaging\gdal_bundle\ not found - the packaged app will need QGIS or MAPGIS_OGR2OGR on target machines.
)
echo Smoke test:
dist\mapgis_vectorize_gui\mapgis_vectorize_gui.exe --check
set "EXIT_CODE=%ERRORLEVEL%"
popd >nul
exit /b %EXIT_CODE%
