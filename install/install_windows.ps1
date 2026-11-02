# pgm2multicam installer for Windows (best effort, UNTESTED).
#   powershell -ExecutionPolicy Bypass -File install\install_windows.ps1 [-SkipFfmpegCheck] [-Uninstall]
param(
    [switch]$SkipFfmpegCheck,
    [switch]$Uninstall
)
$ErrorActionPreference = "Stop"

$RepoDir    = Split-Path -Parent $PSScriptRoot
$AppDir     = Join-Path $env:APPDATA "pgm2multicam"
$VenvDir    = Join-Path $AppDir "venv"
$EngineDir  = Join-Path $AppDir "engine"
$ScriptName = "PGM to Multicam.py"
$ScriptsDir = Join-Path $env:APPDATA "Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility"

if ($Uninstall) {
    Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $ScriptsDir $ScriptName)
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $AppDir
    Write-Host "pgm2multicam removed (Resolve script and $AppDir)."
    exit 0
}

# --- Python >= 3.8 (py launcher first, then python on PATH)
$Py = $null
foreach ($cand in @(@("py", "-3"), @("python"))) {
    $exe = $cand[0]
    $pre = @($cand | Select-Object -Skip 1)
    if (Get-Command $exe -ErrorAction SilentlyContinue) {
        & $exe @pre -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { $Py = $cand; break }
    }
}
if (-not $Py) {
    Write-Error "Python 3.8+ is required. Install it from https://www.python.org/downloads/windows/ (64-bit, 'Add to PATH')."
    exit 1
}
$PyExe = $Py[0]
$PyPre = @($Py | Select-Object -Skip 1)

# --- ffmpeg / ffprobe
$Ffmpeg  = (Get-Command ffmpeg  -ErrorAction SilentlyContinue).Source
$Ffprobe = (Get-Command ffprobe -ErrorAction SilentlyContinue).Source
if (-not $Ffmpeg -or -not $Ffprobe) {
    if ($SkipFfmpegCheck) {
        Write-Warning "ffmpeg/ffprobe not found; continuing (-SkipFfmpegCheck)."
    } else {
        Write-Host "ffmpeg and ffprobe are required. Install with:  winget install Gyan.FFmpeg"
        Write-Host "then open a new terminal and run this installer again."
        exit 1
    }
}

# --- venv + numpy
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
$VenvPy = Join-Path $VenvDir "Scripts\python.exe"
$pipOk = $false
if (Test-Path $VenvPy) {
    & $VenvPy -m pip --version *> $null
    $pipOk = ($LASTEXITCODE -eq 0)
}
if (-not $pipOk) {
    Write-Host "Creating virtual environment in $VenvDir"
    & $PyExe @PyPre -m venv --clear $VenvDir
}
if ($env:PGM2MC_SKIP_PIP -ne "1") {
    & $VenvPy -m pip install --quiet --upgrade pip
    & $VenvPy -m pip install --quiet --upgrade numpy
}

# --- engine
$Src = Join-Path $RepoDir "engine\pgm2mc"
if (-not (Test-Path $Src)) { Write-Error "Engine package not found at $Src"; exit 1 }
$Dst = Join-Path $EngineDir "pgm2mc"
if (Test-Path $Dst) { Remove-Item -Recurse -Force $Dst }
New-Item -ItemType Directory -Force -Path $EngineDir | Out-Null
Copy-Item -Recurse $Src $EngineDir
Get-ChildItem -Path $EngineDir -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force

# --- config.json
$cfg = [ordered]@{ python = $VenvPy; engine_dir = $EngineDir; ffmpeg = $Ffmpeg; ffprobe = $Ffprobe }
[System.IO.File]::WriteAllText((Join-Path $AppDir "config.json"), ($cfg | ConvertTo-Json), (New-Object System.Text.UTF8Encoding $false))

# --- Resolve script
New-Item -ItemType Directory -Force -Path $ScriptsDir | Out-Null
Copy-Item -Force (Join-Path $RepoDir "resolve\$ScriptName") (Join-Path $ScriptsDir $ScriptName)

Write-Host ""
Write-Host "pgm2multicam is installed."
Write-Host "  1. Restart DaVinci Resolve."
Write-Host "  2. Select the program clip and the ISO clips in the Media Pool."
Write-Host "  3. Workspace > Scripts > PGM to Multicam."
