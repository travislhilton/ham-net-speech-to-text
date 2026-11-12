<#
ham-net-speech-to-text: install or update the standalone Windows station. Idempotent: run it again
after every update. Needs administrator rights and internet the first time (~4 GB of downloads:
Python packages, four Whisper models, the FCC license database; more with an NVIDIA card).

  install.cmd                              (double-click; elevates and runs this)
  install.ps1 [-InstallDir C:\ham-net-speech-to-text] [-Callsign W9XYZ] [-SkipModels] [-SkipCallbook] [-SkipQrz] [-SkipVoice]
              [-AreaPack <file or https address>]   (else an area-pack.json beside install.cmd is imported)
              [-KeepAwake traffic|listening|always|off]   (answers the power-settings question)
              [-Remeasure]                         (measure this PC again on an update)
#>
param(
  [string]$InstallDir = 'C:\ham-net-speech-to-text',
  [string]$Callsign = '',
  [switch]$SkipModels,
  [switch]$SkipCallbook,
  [switch]$SkipQrz,
  [switch]$SkipVoice,
  [string]$AreaPack = '',
  [ValidateSet('', 'traffic', 'listening', 'always', 'off')][string]$KeepAwake = '',
  [switch]$Remeasure
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
  throw 'No internet: the first install downloads about 4 GB. Connect to the internet and run it again (after that the station works offline).'
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
  # No byte-order mark: PowerShell 5's `-Encoding utf8` writes one (the station reads utf-8-sig anyway)
  [IO.File]::WriteAllText($station, @"
# This station's settings (kept across updates). Radios are added here as they are connected.
callsign: $($Callsign.ToUpper())
radios: []
"@, (New-Object Text.UTF8Encoding $false))
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
}

if (-not $SkipVoice) {
  # Who?'s "sounds like K7ABC" suggestion (station/voicehint.py). Small and separate from the Whisper models, so a
  # station installed with -SkipModels gets it too. Missing = no suggestions; everything else works.
  Step "Voice model for Who? suggestions (26 MB, once)"
  $vm = "$InstallDir\models\voiceprint\voxceleb_resnet34_LM.onnx"
  if (Test-Path $vm) { Write-Host '  already there' }
  elseif (-not $online) { Write-Host '  no internet: skipped (Who? works without voice suggestions)' }
  else {
    New-Item -ItemType Directory -Force (Split-Path $vm) | Out-Null
    try { Download 'https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM/resolve/main/voxceleb_resnet34_LM.onnx' "$vm.part"
          Move-Item -Force "$vm.part" $vm; Write-Host '  ok' }
    catch { Remove-Item -Force "$vm.part" -ErrorAction SilentlyContinue
            Write-Host "  could not download it ($($_.Exception.Message)); Who? works without voice suggestions" }
  }
}

# Local names for the area (station/localnames.py): an area pack someone in the area published. -AreaPack, else an
# area-pack.json beside install.cmd or in the install folder. Merged: names already on the station are updated, none
# removed, so re-running the installer is harmless.
Step "Local names (area pack)"
$pack = $AreaPack
if (-not $pack) {
  foreach ($c in @("$PSScriptRoot\area-pack.json", "$PSScriptRoot\..\..\area-pack.json", "$InstallDir\area-pack.json")) {
    if (Test-Path $c) { $pack = (Resolve-Path $c).Path; break }
  }
}
if (-not $pack) { Write-Host '  none (Settings > Local names can import one later)' }
elseif ($pack -match '^https?://' -and -not $online) { Write-Host "  no internet: $pack not imported (Settings > Local names later)" }
else {
  Push-Location $InstallDir
  try { Run { & $vpy -m station.localnames import $pack --data "$InstallDir\data" } }
  catch { Write-Host "  could not import $pack ($($_.Exception.Message)); Settings > Local names can import it" }
  finally { Pop-Location }
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

Step "Power plan 'HamNet Station' (the station switches to it only while transcribing)"
# Laptops slow themselves right down when the screen goes dark (README "On a laptop"). While a radio listens or the
# accurate pass runs, the station saves the operator's own plan + power mode, switches to this one (and Best
# performance), and puts them back afterwards (station/awake.py). Same GUID as station/awake.py HAMNET_PLAN.
$hn = '6f1c2d4e-8a3b-4c5d-9e7f-48616d4e6574'
$old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
powercfg /query $hn 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { powercfg /duplicatescheme 381b4222-f694-41f0-9685-ff5bb260df2e $hn | Out-Null }   # from Balanced
powercfg /changename $hn 'HamNet Station' 'Full speed while HamNet Station transcribes; switched on and off by the station' | Out-Null
$hnSet = @(   # subgroup, setting, value (on mains AND battery: it is active only while transcribing)
  @('SUB_SLEEP', 'STANDBYIDLE', 0), @('SUB_SLEEP', 'HIBERNATEIDLE', 0), @('SUB_SLEEP', 'UNATTENDSLEEP', 0),
  @('SUB_VIDEO', 'VIDEOIDLE', 0), @('SUB_VIDEO', 'VIDEOCONLOCK', 0), @('SUB_BUTTONS', 'LIDACTION', 0),
  @('SUB_PROCESSOR', 'PERFEPP', 0), @('SUB_PROCESSOR', 'PERFEPP1', 0),
  @('2a737441-1930-4402-8d77-b2bebba308a3', '48e6b7a6-50f5-4782-a5d4-53bb8f07e226', 0),   # USB selective suspend off
  @('19cbb8fa-5279-450e-9fac-8a3d5fedd0c1', '12bbebe6-58d6-4636-95bb-3217ef867c1a', 0),   # Wi-Fi: max performance
  @('501a4d13-42af-4429-9fd1-a8218c268e20', 'ee12f906-d277-404b-b6da-e5fa1a576df5', 0),   # PCIe link power off
  @('44f3beca-a7c0-460e-9df2-bb8b99e0cba6', '3619c3f2-afb2-4afc-b0e9-e7fef372de36', 2)    # Intel graphics: max
)
foreach ($v in $hnSet) {
  powercfg /setacvalueindex $hn $v[0] $v[1] $v[2] 2>$null | Out-Null
  powercfg /setdcvalueindex $hn $v[0] $v[1] $v[2] 2>$null | Out-Null    # a setting this PC lacks is skipped
}
$ErrorActionPreference = $old
Write-Host '  ready (your own power plan stays active; set it for battery life as you like)'

if (-not $SkipModels) {
  # Both benchmarks with the screen and system held awake on the HamNet plan (station/pccheck.py): a laptop measured
  # with its screen off runs at a quarter of its speed and would get a slower engine than it can run (2026-11-12).
  # An update keeps the earlier measurement unless -Remeasure or there is none yet.
  $eng = "$InstallDir\config\engine.yaml"
  if ($Remeasure -or -not (Test-Path $eng) -or -not (Select-String -Path $eng -Pattern '^accurate:' -Quiet)) {
    Step "Measuring this PC at full speed (screen held on; ~3-8 min, leave the PC plugged in)"
    # The station shares the graphics and switches power plans: off for the measurement. Its task is DISABLED, not
    # only stopped: the 2-minute watchdog trigger started it again mid-measurement (2026-11-12, a test install).
    $task = Get-ScheduledTask -TaskName 'HamNet Station' -ErrorAction SilentlyContinue
    if ($task) { Disable-ScheduledTask -TaskName 'HamNet Station' | Out-Null; Stop-ScheduledTask -TaskName 'HamNet Station' -ErrorAction SilentlyContinue }
    Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*-m station*' -and $_.CommandLine -notlike '*pccheck*' } |
      ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    try { Run { & $vpy -m station.pccheck measure --data "$InstallDir\data" --models "$InstallDir\models" --out $eng } }
    finally { if ($task) { Enable-ScheduledTask -TaskName 'HamNet Station' | Out-Null } }   # re-registered + started below
  } else {
    Step "This PC was measured before (kept; -Remeasure or Settings -> This PC measures again)"
  }
}

Step "What the station changes on this PC while it transcribes"
Push-Location $InstallDir
$asked = $false
& $vpy -m station.pccheck asked --data "$InstallDir\data" 2>$null; if ($LASTEXITCODE -eq 0) { $asked = $true }
$mode = $KeepAwake
if (-not $mode -and -not $asked) {
  Write-Host '  Laptops run several times slower once the screen goes dark, so while the station transcribes it can keep'
  Write-Host '  the screen on and switch to its own "HamNet Station" power plan at Best performance, then put your own'
  Write-Host '  plan back. When should it do that?'
  Write-Host '    1  During nets and traffic (recommended)'
  Write-Host '    2  The whole time a radio listens'
  Write-Host '    3  Always, while the station runs'
  Write-Host '    4  Never (my power settings only; transcription may be slow with the screen off)'
  $ans = ''
  if ([Environment]::UserInteractive -and -not [Console]::IsInputRedirected) { $ans = Read-Host '  Choose 1-4 [1]' }
  $mode = @{ '2' = 'listening'; '3' = 'always'; '4' = 'off' }[$ans.Trim()]
  if (-not $mode) { $mode = 'traffic' }
}
if ($mode) { Run { & $vpy -m station.pccheck consent --data "$InstallDir\data" --mode $mode } }
& $vpy -m station.pccheck explain --data "$InstallDir\data" | ForEach-Object { Write-Host "  $_" }
Pop-Location

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
Step "This PC (power settings that slow transcription; Settings -> This PC shows the same)"
Push-Location $InstallDir; & $vpy -m station.pccheck check --data "$InstallDir\data"; Pop-Location
Write-Host "Installed in $InstallDir. Audio inputs this station can record from:"
Run { & $vpy "$InstallDir\capture\recorder.py" --list-devices }
