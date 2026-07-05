; Inno Setup script for the MapGIS semi-auto vectorization GUI.
; Build the app first (release\build_clean.ps1), then compile this with the Inno
; Setup Compiler (ISCC.exe installer.iss) to produce dist\installer\GeoScanSetup.exe
;
; Design notes (see release/README.md):
;  - Installs to Program Files (ASCII path -> ogr2ogr GDAL_DATA is happy).
;  - User config lives in %LOCALAPPDATA%\GeoScan\config (written by the
;    app, NOT by this installer), so upgrade/uninstall never touches settings or
;    the DPAPI-encrypted API key. This is what makes in-place auto-update safe:
;    the updater re-runs this installer and the user's tool paths + key survive.

#define AppName "GeoScan"
; Keep this in step with src/geoscan/__init__.py __version__ and the release tag.
#define AppVersion "0.1.1"
#define AppExe "GeoScan.exe"
; dist folder relative to this .iss (release\installer\ -> repo\dist\...)
#define DistDir "..\..\dist\GeoScan"

[Setup]
AppId={{7B3F2A10-9C4D-4E58-8F1A-MAPGISVEC001}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=MapGIS Vectorize
DefaultDirName={autopf}\GeoScan
DefaultGroupName=GeoScan
DisableProgramGroupPage=yes
OutputDir=..\..\dist\installer
OutputBaseFilename=GeoScanSetup
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
; Per-user install: no admin prompt, lands in a user-writable location
; ({localappdata}\Programs\GeoScan by default). The wizard's dir page stays on,
; so the user can still browse to any folder they can write to.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
WizardStyle=modern
; SetupIconFile=..\..\packaging\app_icon.ico
UninstallDisplayIcon={app}\{#AppExe}

[Languages]
; ChineseSimplified.isl is an "unofficial" translation Inno does not bundle. For
; full Chinese wizard chrome, drop it into the Inno Languages folder (or vendor
; it next to this .iss) and switch MessagesFile below. Until then the wizard
; buttons are English while all app-specific strings ([Tasks]/[Icons]/[Run]) are
; Chinese.
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\GeoScan"; Filename: "{app}\{#AppExe}"
Name: "{group}\卸载 GeoScan"; Filename: "{uninstallexe}"
Name: "{autodesktop}\GeoScan"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
; Headless self-check right after install so a broken bundle fails visibly.
Filename: "{app}\{#AppExe}"; Parameters: "--check"; Flags: runhidden waituntilterminated; StatusMsg: "正在自检..."
Filename: "{app}\{#AppExe}"; Description: "启动 GeoScan"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Remove only install-dir leftovers; user config in %LOCALAPPDATA% is preserved
; on purpose so a reinstall keeps their tool paths and key.
Type: filesandordirs; Name: "{app}"
