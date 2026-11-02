<#
ham-net-speech-to-text: install or update the standalone Windows station. Idempotent: run it again
after every update. Needs administrator rights and internet the first time (~2.5 GB of downloads:
Python packages, two Whisper models, the FCC license database).

  install.cmd                              (double-click; elevates and runs this)
  install.ps1 [-InstallDir C:\ham-net-speech-to-text] [-Callsign W9XYZ] [-SkipModels] [-SkipCallbook]
#>
param(
  [string]$InstallDir = 'C:\ham-net-speech-to-text',
  [string]$Callsign = '',
  [switch]$SkipModels,
  [switch]$SkipCallbook
)
$ErrorActionPreference = 'Stop'
$Src = (Resolve-Path "$PSScriptRoot\..\..").Path
function Step($m) { Write-Host "`n== $m" -ForegroundColor Cyan }
# Windows PowerShell 5.1 turns a native program's stderr (progress bars, pip warnings) into a
# terminating error under $ErrorActionPreference='Stop'. Run externals here; judge by exit code.
function Run([scriptblock]$cmd) {
  $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
  try { & $cmd 2>&1 | ForEach-Object { "$_" } | Where-Object { $_ -notmatch 'Fetching \d|it/s\]|^\s*$' } | Out-Host }
  finally { $ErrorActionPreference = $old }
  if ($LASTEXITCODE -ne 0) { throw "failed (exit $LASTEXITCODE): $cmd" }
}
$env:HF_HUB_DISABLE_PROGRESS_BARS = '1'; $env:PIP_DISABLE_PIP_VERSION_CHECK = '1'

$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run as administrator (use install.cmd).' }

Step "Python 3.12"
$py = 'C:\Program Files\Python312\python.exe'
if (-not (Test-Path $py)) {
  Run { & winget install --id Python.Python.3.12 -e --source winget --scope machine --silent `
    --accept-source-agreements --accept-package-agreements --disable-interactivity }
  if (-not (Test-Path $py)) { throw "Python did not install to $py" }
}
Run { & $py --version }

Step "Program files -> $InstallDir"
New-Item -ItemType Directory -Force $InstallDir | Out-Null
if ($Src -ne (Resolve-Path $InstallDir).Path) {
  foreach ($d in 'capture','hamkit','config','tools','install','docs') {
    if (Test-Path "$Src\$d") { robocopy "$Src\$d" "$InstallDir\$d" /MIR /XD __pycache__ .venv /XF local.yaml station.yaml engine.yaml /NFL /NDL /NJH /NJS /NP | Out-Null }
  }
  Copy-Item "$Src\README.md" $InstallDir -Force
}
$ver = if (Test-Path "$Src\.git") { (git -C $Src rev-parse --short HEAD 2>$null) } else { 'zip' }
Set-Content "$InstallDir\VERSION.txt" "installed $(Get-Date -Format s) from $Src ($ver)"

Step "Python environment (first run downloads ~1 GB)"
$venv = "$InstallDir\.venv"
if (-not (Test-Path "$venv\Scripts\python.exe")) { Run { & $py -m venv $venv } }
$vpy = "$venv\Scripts\python.exe"
Run { & $vpy -m pip install -q --upgrade pip }
Run { & $vpy -m pip install -q torch --index-url https://download.pytorch.org/whl/cpu }
Run { & $vpy -m pip install -q -r "$InstallDir\install\windows\requirements-windows.txt" }
# Let `python -m hamkit...` work from any folder (not only when PowerShell is in the install dir)
Set-Content -Path "$venv\Lib\site-packages\ham-net-speech-to-text.pth" -Value $InstallDir -Encoding ascii

Step "Machine settings"
$data = "$InstallDir\data"
New-Item -ItemType Directory -Force "$data\callbook", "$data\recordings", "$InstallDir\models" | Out-Null
[Environment]::SetEnvironmentVariable('HAMNET_HOME', $InstallDir, 'Machine')
[Environment]::SetEnvironmentVariable('CALLBOOK_DIR', "$data\callbook", 'Machine')
$env:HAMNET_HOME = $InstallDir; $env:CALLBOOK_DIR = "$data\callbook"
$station = "$InstallDir\config\station.yaml"
if (-not (Test-Path $station)) {
  if (-not $Callsign) { $Callsign = Read-Host 'Your callsign (for hints and IDs)' }
  @"
# This station's settings (kept across updates). Radios are added here as they are connected.
callsign: $($Callsign.ToUpper())
radios: []
"@ | Set-Content $station -Encoding utf8
}

if (-not $SkipModels) {
  Step "Whisper models (OpenVINO, Intel GPU/CPU)"
  foreach ($m in 'whisper-large-v3-turbo-int8-ov', 'whisper-small.en-int8-ov', 'whisper-base.en-int8-ov') {
    Run { & $vpy -c "from huggingface_hub import snapshot_download as s; s('OpenVINO/$m', local_dir=r'$InstallDir\models\$m')" }
    Write-Host "  $m ok"
  }
  Run { & $vpy -c "import openvino as o; print('  OpenVINO devices:', o.Core().available_devices)" }
  if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    Write-Host '  NVIDIA GPU found: adding CUDA libraries for faster-whisper'
    Run { & $vpy -m pip install -q nvidia-cublas-cu12 nvidia-cudnn-cu12 }
  }
  Step "Choosing the transcription engine for this PC (benchmark, ~1-3 min)"
  Run { & $vpy "$InstallDir\tools\pick_engine.py" --models "$InstallDir\models" --out "$InstallDir\config\engine.yaml" }
}

if (-not $SkipCallbook) {
  Step "Callbook: FCC license database + country file (first run ~200 MB)"
  Push-Location $InstallDir
  Run { & $vpy -m hamkit.fcc update }
  Run { & $vpy -m hamkit.dxcc update }
  Pop-Location
}

Step "Daily callbook update (scheduled task, 04:15)"
$act = New-ScheduledTaskAction -Execute $vpy -Argument '-m hamkit.fcc update' -WorkingDirectory $InstallDir
$act2 = New-ScheduledTaskAction -Execute $vpy -Argument '-m hamkit.dxcc update' -WorkingDirectory $InstallDir
$trg = New-ScheduledTaskTrigger -Daily -At 4:15am
$set = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName 'HamNet Callbook Update' -Action $act, $act2 -Trigger $trg -Settings $set `
  -User 'SYSTEM' -RunLevel Highest -Force | Out-Null
Write-Host '  registered (runs when online; catches up after a missed day)'

Step "Done"
Write-Host "Installed in $InstallDir. Audio inputs this station can record from:"
Run { & $vpy "$InstallDir\capture\recorder.py" --list-devices }
