# flowlite installer - installs deps, pulls model, creates Windows Startup shortcut.
# Run from this folder:  powershell -ExecutionPolicy Bypass -File .\install.ps1

$ErrorActionPreference = "Stop"

Write-Host "[flowlite] installing Python dependencies..." -ForegroundColor Cyan
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

Write-Host ""
Write-Host "[flowlite] pulling Ollama model gemma3:1b..." -ForegroundColor Cyan
ollama pull gemma3:1b

Write-Host ""
Write-Host "[flowlite] pre-downloading Whisper large-v3-turbo (may take a minute)..." -ForegroundColor Cyan
python -c "from faster_whisper import WhisperModel; WhisperModel('deepdml/faster-whisper-large-v3-turbo-ct2', device='cuda', compute_type='int8_float16'); print('model cached')"

Write-Host ""
Write-Host "[flowlite] generating app icon..." -ForegroundColor Cyan
python make_icon.py

Write-Host ""
Write-Host "[flowlite] creating shortcuts..." -ForegroundColor Cyan

$AppDir   = $PSScriptRoot
$Daemon   = Join-Path $AppDir "daemon.py"
$IconPath = Join-Path $AppDir "icons\flowlite.ico"
$Pythonw  = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $Pythonw) {
    $PyPath = Split-Path (Get-Command python.exe).Source -Parent
    $Pythonw = Join-Path $PyPath "pythonw.exe"
}
if (-not (Test-Path $Pythonw)) {
    throw "pythonw.exe not found. Install Python with the launcher or adjust PATH."
}

$IconArg = "$Pythonw,0"
if (Test-Path $IconPath) { $IconArg = $IconPath }

function New-FlowliteShortcut($LnkPath) {
    $Shell = New-Object -ComObject WScript.Shell
    $Shortcut = $Shell.CreateShortcut($LnkPath)
    $Shortcut.TargetPath       = $Pythonw
    $Shortcut.Arguments        = "`"$Daemon`""
    $Shortcut.WorkingDirectory = $AppDir
    $Shortcut.WindowStyle      = 7
    $Shortcut.Description       = "flowlite - local speech-to-text"
    $Shortcut.IconLocation      = $IconArg
    $Shortcut.Save()
}

# 1) Startup (auto-launch on login)
$Startup  = [Environment]::GetFolderPath("Startup")
$StartupLnk = Join-Path $Startup "flowlite.lnk"
New-FlowliteShortcut $StartupLnk

# 2) Desktop (double-click to open the app)
$Desktop  = [Environment]::GetFolderPath("Desktop")
$DesktopLnk = Join-Path $Desktop "flowlite.lnk"
New-FlowliteShortcut $DesktopLnk

# 3) Start Menu (search "flowlite" to launch)
$StartMenu = Join-Path ([Environment]::GetFolderPath("ApplicationData")) "Microsoft\Windows\Start Menu\Programs"
$StartMenuLnk = Join-Path $StartMenu "flowlite.lnk"
New-FlowliteShortcut $StartMenuLnk

Write-Host ""
Write-Host "[flowlite] installed."
Write-Host "  Startup:    $StartupLnk"
Write-Host "  Desktop:    $DesktopLnk"
Write-Host "  Start Menu: $StartMenuLnk"
Write-Host ("  Start now:  {0} {1}" -f $Pythonw, $Daemon)
Write-Host "  Uninstall:  Remove-Item the three .lnk files above"
