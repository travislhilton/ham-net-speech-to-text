<#
ham-net-speech-to-text: install or update the standalone Windows station. Idempotent: run it again
after every update. Needs administrator rights and internet the first time (~2.5 GB of downloads:
Python packages, two Whisper models, the FCC license database).

  install.cmd                              (double-click; elevates and runs this)
  install.ps1 [-InstallDir C:\ham-net-speech-to-text] [-Callsign W9XYZ] [-SkipModels] [-SkipCallbook] [-SkipQrz]
#>
param(
  [string]$InstallDir = 'C:\ham-net-speech-to-text',
  [string]$Callsign = '',
  [switch]$SkipModels,
  [switch]$SkipCallbook,
  [switch]$SkipQrz
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

Step "Checking this PC"
if (-not [Environment]::Is64BitOperatingSystem) { throw 'This station needs 64-bit Windows 10 or 11.' }
$os = Get-CimInstance Win32_OperatingSystem
Write-Host "  $($os.Caption) $($os.Version), $([math]::Round($os.TotalVisibleMemorySize/1MB)) GB memory"
$drive = (Split-Path -Qualifier $InstallDir)
$freeGB = [math]::Round((Get-PSDrive ($drive.TrimEnd(':'))).Free / 1GB, 1)
if (-not (Test-Path "$InstallDir\.venv") -and $freeGB -lt 8) {
  throw "Only $freeGB GB free on $drive; the first install needs about 8 GB (Python packages, Whisper models, FCC database)."
}
Write-Host "  $freeGB GB free on $drive"
$online = $false
try { $online = (Invoke-WebRequest -UseBasicParsing -Method Head -TimeoutSec 15 'https://pypi.org/simple/pip/').StatusCode -eq 200 } catch {}
if (-not $online -and -not (Test-Path "$InstallDir\.venv")) {
  throw 'No internet: the first install downloads about 2.5 GB. Connect to the internet and run it again (after that the station works offline).'
}
Write-Host "  internet: $(if ($online) { 'yes' } else { 'no (update skipped where possible)' })"
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
function Download($url, $out) { Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $out -TimeoutSec 600 }

Step "Microsoft Visual C++ runtime (OpenVINO, PyTorch and faster-whisper need it)"
$vc = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64' -ErrorAction SilentlyContinue
if ($vc -and $vc.Installed -eq 1 -and $vc.Major -ge 14 -and $vc.Minor -ge 40) {
  Write-Host "  installed ($($vc.Version))"
} else {
  $exe = "$env:TEMP\vc_redist.x64.exe"
  Download 'https://aka.ms/vs/17/release/vc_redist.x64.exe' $exe
  $p = Start-Process $exe -ArgumentList '/install', '/quiet', '/norestart' -Wait -PassThru
  if ($p.ExitCode -notin 0, 1638, 3010) { throw "Visual C++ runtime install failed (exit $($p.ExitCode))" }
  Write-Host "  installed$(if ($p.ExitCode -eq 3010) { ' (Windows wants a restart; the station works before it)' })"
}

Step "Python 3.12"
$py = 'C:\Program Files\Python312\python.exe'
if (-not (Test-Path $py)) {
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    Run { & winget install --id Python.Python.3.12 -e --source winget --scope machine --silent `
      --accept-source-agreements --accept-package-agreements --disable-interactivity }
  }
  if (-not (Test-Path $py)) {                        # no winget (some fresh or LTSC installs): python.org directly
    $exe = "$env:TEMP\python-3.12.10-amd64.exe"
    Download 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' $exe
    $p = Start-Process $exe -ArgumentList '/quiet', 'InstallAllUsers=1', 'PrependPath=0', 'Include_test=0', `
      'TargetDir="C:\Program Files\Python312"' -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "Python install failed (exit $($p.ExitCode))" }
  }
  if (-not (Test-Path $py)) { throw "Python did not install to $py" }
}
Run { & $py --version }

Step "Program files -> $InstallDir"
New-Item -ItemType Directory -Force $InstallDir | Out-Null
if ($Src -ne (Resolve-Path $InstallDir).Path) {
  foreach ($d in 'capture','hamkit','station','transcribe','config','tools','install','docs') {
    if (Test-Path "$Src\$d") { robocopy "$Src\$d" "$InstallDir\$d" /MIR /XD __pycache__ .venv /XF local.yaml station.yaml engine.yaml secrets.env config.yaml ssh-keys.txt /NFL /NDL /NJH /NJS /NP | Out-Null }
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
  Step "Whisper models (OpenVINO, Intel GPU/NPU/CPU; first run ~3 GB)"
  # live: turbo (final text), small.en (fallback), base.en (words while talking); accurate pass on this PC: large-v3
  foreach ($m in 'whisper-large-v3-turbo-int8-ov', 'whisper-small.en-int8-ov', 'whisper-base.en-int8-ov',
                 'whisper-large-v3-int8-ov') {
    Run { & $vpy -c "from huggingface_hub import snapshot_download as s; s('OpenVINO/$m', local_dir=r'$InstallDir\models\$m')" }
    Write-Host "  $m ok"
  }
  Run { & $vpy -c "import openvino as o; print('  OpenVINO devices:', o.Core().available_devices)" }
  if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    Write-Host '  NVIDIA GPU found: adding CUDA libraries for faster-whisper'
    Run { & $vpy -m pip install -q nvidia-cublas-cu12 nvidia-cudnn-cu12 }
    Run { & $vpy -c "from huggingface_hub import snapshot_download as s; s('Systran/faster-whisper-large-v3', local_dir=r'$InstallDir\models\faster-whisper-large-v3')" }
    Write-Host '  faster-whisper-large-v3 ok (accurate pass on the NVIDIA card)'
  }
  Step "Choosing the transcription engine for this PC (benchmark, ~1-3 min)"
  Run { & $vpy "$InstallDir\tools\pick_engine.py" --models "$InstallDir\models" --out "$InstallDir\config\engine.yaml" }
  # The accurate second pass on this PC's spare compute (when the home server is not there): what can it
  # sustain? Both stages / the final text after each net only / none. Written to engine.yaml `accurate:`.
  Step "Measuring the accurate pass on this PC (benchmark, ~2-6 min)"
  Run { & $vpy "$InstallDir\tools\pick_accurate.py" --models "$InstallDir\models" --out "$InstallDir\config\engine.yaml" }
}

if (-not $SkipCallbook) {
  Step "Callbook: FCC license database + country file + ZIP centres (first run ~200 MB)"
  Push-Location $InstallDir
  Run { & $vpy -m hamkit.fcc update }
  Run { & $vpy -m hamkit.dxcc update }
  Run { & $vpy -m hamkit.zipgeo update }    # ZIP centres (1 MB, once): VHF/UHF callsign distances
  Pop-Location
}

if (-not $SkipQrz) {
  Step "QRZ.com lookups (optional)"
  Push-Location $InstallDir
  $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
  & $vpy -m hamkit.qrz has-login 2>$null | Out-Null
  if ($LASTEXITCODE -eq 0) {
    Write-Host '  a QRZ.com login is already stored (change it: python -m hamkit.qrz set-login, remove: forget-login)'
  } else {
    Write-Host '  QRZ.com adds grid squares, photos and non-US callsigns to the callsign cards. Full records need a'
    Write-Host '  QRZ "XML Logbook Data" subscription; without one QRZ gives only names and countries. US callsigns'
    Write-Host '  are fully covered by the FCC database either way. The password is kept in Windows Credential Manager.'
    $ans = Read-Host '  Set up a QRZ.com login now? (y/N)'
    if ($ans -match '^[yY]') { & $vpy -m hamkit.qrz set-login } else { Write-Host '  skipped (add it later: python -m hamkit.qrz set-login)' }
  }
  $ErrorActionPreference = $old
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

Step "Station service (dashboard http://localhost:8091/)"
# Port 8091 from this PC's home network and Tailscale only (the dashboard shows callbook addresses)
if (-not (Get-NetFirewallRule -Name 'HamNet-Station-8091' -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -Name 'HamNet-Station-8091' -DisplayName 'HamNet Station dashboard' -Direction Inbound `
    -Protocol TCP -LocalPort 8091 -Action Allow | Out-Null
}
Set-NetFirewallRule -Name 'HamNet-Station-8091' -Enabled True -Profile Any `
  -RemoteAddress @('LocalSubnet', '100.64.0.0/10', '127.0.0.1')
# Start at logon in the operator's own session: audio inputs and the QRZ password (Credential Manager)
# belong to that session. pythonw = no console window; output goes to data\station.log.
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name   # e.g. STATIONPC\operator (USERDOMAIN is WORKGROUP over SSH)
$sAct = New-ScheduledTaskAction -Execute "$venv\Scripts\pythonw.exe" -Argument '-m station --log data\station.log' `
  -WorkingDirectory $InstallDir
$sTrg = New-ScheduledTaskTrigger -AtLogOn -User $user
# Watchdog: Task Scheduler's restart-on-failure covers only a failed launch, not a crash later (11-04: pythonw died
# in fastprox.dll, Windows' WMI client, 1 s after a restart and stayed down). Starting the task every 2 minutes
# does nothing while it runs (IgnoreNew) and brings it back within 2 minutes of a crash.
$sTrg2 = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 2)
$sSet = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0 `
  -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$sPrn = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'HamNet Station' -Action $sAct -Trigger $sTrg, $sTrg2 -Settings $sSet -Principal $sPrn `
  -Description 'Live voice-net transcription and dashboard (ham-net-speech-to-text)' -Force | Out-Null
Set-Content -Path "$env:PUBLIC\Desktop\HamNet Station.url" -Value "[InternetShortcut]`r`nURL=http://localhost:8091/" -Encoding ascii
Stop-ScheduledTask -TaskName 'HamNet Station' -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*-m station*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-ScheduledTask -TaskName 'HamNet Station'
Write-Host "  started; it also starts at every logon for $user. Open the 'HamNet Station' shortcut on the desktop."

Step "Done"
$gpu = (Get-CimInstance Win32_VideoController | ForEach-Object { "$($_.Name) (driver $($_.DriverVersion))" }) -join '; '
Write-Host "  Graphics: $gpu"
Write-Host '  (Intel or NVIDIA graphics are used only with a current driver: update it from Windows Update or the maker if'
Write-Host '   config\engine.yaml chose the CPU; then run: .\.venv\Scripts\python.exe tools\pick_engine.py)'
Write-Host "Installed in $InstallDir. Audio inputs this station can record from:"
Run { & $vpy "$InstallDir\capture\recorder.py" --list-devices }
