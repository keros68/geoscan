; Inno Setup script for GeoScan.
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
#define AppVersion "0.2.2"
; The only UI: the Tauri console shell. It spawns the frozen Python engine via
; "GeoScan.exe --engine". GeoScan.exe itself has no interface (the classic
; tkinter GUI was removed) — it carries --engine / --check / --batch.
#define ConsoleExe "GeoScanConsole.exe"
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
; Upgrade installs must overwrite GeoScanConsole.exe / GeoScan.exe; ask the
; Restart Manager to close a running instance instead of failing on the lock.
CloseApplications=yes
RestartApplications=no
; SetupIconFile=..\..\packaging\app_icon.ico
UninstallDisplayIcon={app}\{#ConsoleExe}

[Languages]
; ChineseSimplified.isl is an "unofficial" translation Inno does not bundle. For
; full Chinese wizard chrome, drop it into the Inno Languages folder (or vendor
; it next to this .iss) and switch MessagesFile below. Until then the wizard
; buttons are English while all app-specific strings ([Tasks]/[Icons]/[Run]) are
; Chinese.
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[InstallDelete]
; Wipe the previous runtime layer BEFORE copying the new one. An overwrite
; install otherwise leaves orphaned files from older builds inside _internal\
; (e.g. an old loose cv2\__init__.py shadowing the new cv2 extension module ->
; "No module named 'numpy.core.multiarray'" crash on startup). _internal and
; gdal are fully owned by the installer, so deleting them is always safe; the
; user's config lives in %LOCALAPPDATA%\GeoScan and is untouched.
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\gdal"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\GeoScan"; Filename: "{app}\{#ConsoleExe}"
Name: "{group}\卸载 GeoScan"; Filename: "{uninstallexe}"
Name: "{autodesktop}\GeoScan"; Filename: "{app}\{#ConsoleExe}"; Tasks: desktopicon

[Run]
; Headless self-check right after install so a broken bundle fails visibly.
; --check exercises the frozen Python runtime (cv2 conversion + engine modules),
; which is exactly what the console's engine relies on.
Filename: "{app}\{#AppExe}"; Parameters: "--check"; Flags: runhidden waituntilterminated; StatusMsg: "正在自检..."
Filename: "{app}\{#ConsoleExe}"; Description: "启动 GeoScan"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Remove only install-dir leftovers; user config in %LOCALAPPDATA% is preserved
; on purpose so a reinstall keeps their tool paths and key.
Type: filesandordirs; Name: "{app}"

[Code]
// The console shell renders in WebView2. Win11 / recent Win10 ship it with the
// OS or Edge; without it the console — GeoScan's only UI — cannot start, so
// warn with a download pointer. Non-blocking on purpose: the user can install
// WebView2 after GeoScan, before first launch. Evergreen runtime registry
// locations, per-machine then per-user.
function WebView2Installed(): Boolean;
var
  Version: string;
begin
  Result :=
    RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) or
    RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) or
    RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version);
  if Result and ((Version = '') or (Version = '0.0.0.0')) then
    Result := False;
end;

function InitializeSetup(): Boolean;
begin
  if not WebView2Installed() then
    MsgBox('未检测到 Microsoft WebView2 运行库。'#13#10 +
           'GeoScan 的界面必须依赖它才能启动；Windows 11 自带，老系统可到微软官网搜索'#13#10 +
           '“WebView2 Runtime” 免费安装。'#13#10#13#10 +
           '可以继续安装 GeoScan，但首次使用前请先装好 WebView2，否则界面无法打开。',
           mbInformation, MB_OK);
  Result := True;
end;
