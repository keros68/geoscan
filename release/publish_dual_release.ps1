[CmdletBinding()]
param(
    [string]$Version = "",
    [string]$Runtime = "",
    [string]$Notes = "",
    [string]$MirrorBaseUrl = "https://aidraw.cv/geoscan-updates",
    [string]$MirrorUploadTarget = "aidraw-vps:/var/www/geoscan-updates/",
    [string]$IsccPath = "",
    [switch]$SkipBuild,
    [switch]$SkipGitRelease,
    [switch]$SkipMirrorUpload,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

function Read-EngineVersion {
    $init = Get-Content -Raw -LiteralPath (Join-Path $RepoRoot "src/geoscan/__init__.py")
    if ($init -notmatch "__version__\s*=\s*['""]([^'""]+)['""]") {
        throw "Cannot read __version__ from src/geoscan/__init__.py"
    }
    return $Matches[1]
}

function Read-RuntimeVersion {
    return (Get-Content -Raw -LiteralPath (Join-Path $RepoRoot "packaging/runtime_version.txt")).Trim()
}

function Resolve-Iscc {
    param([string]$Configured)
    if ($Configured) {
        return $Configured
    }
    if ($env:GEOSCAN_ISCC) {
        return $env:GEOSCAN_ISCC
    }
    $known = Join-Path $env:LOCALAPPDATA "Programs/Inno Setup 6/ISCC.exe"
    if (Test-Path -LiteralPath $known) {
        return $known
    }
    return "ISCC"
}

function Format-CommandLine {
    param([string]$File, [string[]]$Arguments)
    $parts = @($File) + $Arguments
    return ($parts | ForEach-Object {
        $value = [string]$_
        if ($value -match '\s') {
            '"' + $value.Replace('"', '\"') + '"'
        } else {
            $value
        }
    }) -join " "
}

function Invoke-External {
    param([string]$File, [string[]]$Arguments)
    $line = Format-CommandLine $File $Arguments
    if ($DryRun) {
        Write-Host "DRYRUN: $line"
        return
    }
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed ($LASTEXITCODE): $line"
    }
}

function Assert-File {
    param([string]$Path, [string]$Description)
    if ($DryRun) {
        Write-Host "DRYRUN: require $Description at $Path"
        return
    }
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Missing $Description`: $Path"
    }
}

function Write-Step {
    param([string]$Name)
    Write-Host ""
    Write-Host "==> $Name"
}

if (-not $Version) {
    $Version = Read-EngineVersion
}
if (-not $Runtime) {
    $Runtime = Read-RuntimeVersion
}
if (-not $Notes) {
    $Notes = "GeoScan v$Version"
}

$Tag = "v$Version"
$MirrorBase = $MirrorBaseUrl.TrimEnd("/")
$InstallerAsset = Join-Path $RepoRoot "dist/installer/GeoScanSetup.exe"
$EngineAsset = Join-Path $RepoRoot "dist/engine-$Version-rt$Runtime.zip"
$MirrorDir = Join-Path $RepoRoot "dist/update_mirror"
$LatestJson = Join-Path $MirrorDir "latest.json"
$MirrorInstaller = Join-Path $MirrorDir "GeoScanSetup.exe"
$MirrorReleases = Join-Path $MirrorDir "releases"
$FixedInstallerUrl = "$MirrorBase/GeoScanSetup.exe"
$Iscc = Resolve-Iscc $IsccPath

Write-Host "GeoScan dual release"
Write-Host "  version: $Version"
Write-Host "  runtime: $Runtime"
Write-Host "  tag: $Tag"
Write-Host "  GitHub release: https://github.com/keros68/geoscan/releases/tag/$Tag"
Write-Host "  mirror latest.json: $MirrorBase/latest.json"
Write-Host "  fixed installer: $FixedInstallerUrl"

if (-not $SkipBuild) {
    Write-Step "Build release assets"
    Invoke-External "powershell" @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "release/build_clean.ps1")
    Invoke-External "python" @("release/build_engine_zip.py")
    Invoke-External $Iscc @("release/installer/installer.iss")
} else {
    Write-Step "Build release assets"
    Write-Host "Skipped by -SkipBuild"
}

Assert-File $InstallerAsset "installer asset"
Assert-File $EngineAsset "engine update asset"

if (-not $SkipGitRelease) {
    Write-Step "Publish GitHub Release"
    if ($DryRun) {
        Invoke-External "gh" @(
            "release", "create", $Tag,
            "dist/installer/GeoScanSetup.exe",
            "dist/engine-$Version-rt$Runtime.zip",
            "--title", "GeoScan $Tag",
            "--notes", $Notes
        )
    } else {
        # A missing release is the normal create path, but gh's expected
        # non-zero probe becomes a terminating NativeCommandError under the
        # script-wide "Stop" policy on Windows PowerShell 5.1.
        $previousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            & gh release view $Tag 2>$null | Out-Null
            $releaseExists = ($LASTEXITCODE -eq 0)
        } finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
        if ($releaseExists) {
            Invoke-External "gh" @(
                "release", "upload", $Tag,
                "dist/installer/GeoScanSetup.exe",
                "dist/engine-$Version-rt$Runtime.zip",
                "--clobber"
            )
        } else {
            Invoke-External "gh" @(
                "release", "create", $Tag,
                "dist/installer/GeoScanSetup.exe",
                "dist/engine-$Version-rt$Runtime.zip",
                "--title", "GeoScan $Tag",
                "--notes", $Notes
            )
        }
    }
} else {
    Write-Step "Publish GitHub Release"
    Write-Host "Skipped by -SkipGitRelease"
}

Write-Step "Build mirror manifest"
Invoke-External "python" @(
    "release/build_update_manifest.py",
    "--version", $Version,
    "--runtime", $Runtime,
    "--base-url", $MirrorBase,
    "--notes", $Notes,
    "--installer", $InstallerAsset,
    "--engine", $EngineAsset,
    "--out-dir", $MirrorDir
)

Assert-File $LatestJson "mirror latest.json"
Assert-File $MirrorInstaller "fixed mirror installer"

if (-not $SkipMirrorUpload) {
    Write-Step "Upload mirror assets before latest.json"
    Invoke-External "scp" @("-r", $MirrorInstaller, $MirrorReleases, $MirrorUploadTarget)

    Write-Step "Upload mirror latest.json last"
    Invoke-External "scp" @($LatestJson, $MirrorUploadTarget)

    Write-Step "Verify public mirror"
    Invoke-External "curl.exe" @("-I", "--max-time", "20", $FixedInstallerUrl)
    Invoke-External "curl.exe" @("-fsSL", "--max-time", "20", "$MirrorBase/latest.json")
} else {
    Write-Step "Upload mirror assets before latest.json"
    Write-Host "Skipped by -SkipMirrorUpload"
    Write-Step "Upload mirror latest.json last"
    Write-Host "Skipped by -SkipMirrorUpload"
}

Write-Host ""
Write-Host "Dual release workflow finished."
