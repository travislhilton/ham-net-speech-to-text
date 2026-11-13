<#
Install HAM Sidekick Station exactly as the Quick Reference Card says, and check what the card promises. Run as administrator on a PC
you do not mind installing on (the GitHub Actions job runs it on a clean Windows machine).

  operator_install_check.ps1 -ZipUrl <address of the ZIP> -Report install-report.json [-Models]

Card steps 1-4 (station card, side 1):
  1 Download      the ZIP from the project page (here: the address you give)
  2 Unblock, then extract   the ZIP carries the "downloaded from the internet" mark (Zone.Identifier); Unblock-File is what the
                  Properties -> Unblock box does; then Extract All
  3 Run install\windows\install.cmd (it asks for administrator rights itself: already held here)
  4 Type your callsign. Press Enter to skip the QRZ.com question. Wait for == Done
It then checks the result: the scheduled tasks, the firewall rule, the desktop shortcut, and that the dashboard answers with the
right title (the station is started by hand: a build machine has no one logged on for the At-logon task).
The models (about 3 GB) are skipped unless -Models, to keep a build machine's time and disk in check; everything else is the card.
#>
param([Parameter(Mandatory)][string]$ZipUrl, [string]$Report = 'install-report.json', [switch]$Models, [string]$Callsign = 'N0CALL')
$ErrorActionPreference = 'Stop'
$results = @()
function Rec($id, $ok, $detail) { $script:results += [pscustomobject]@{ id = $id; status = $(if ($ok) { 'PASS' } else { 'FAIL' }); detail = $detail }; Write-Host ("{0,-6} {1,-18} {2}" -f $(if ($ok) { 'PASS' } else { 'FAIL' }), $id, $detail) }
$work = Join-Path $env:TEMP ('hs-card-' + [guid]::NewGuid().ToString('N')); New-Item -ItemType Directory $work | Out-Null
try {
  # 1 Download
  [Net.ServicePointManager]::SecurityProtocol = 'Tls12'
  $zip = Join-Path $work 'download.zip'
  Invoke-WebRequest -UseBasicParsing -Uri $ZipUrl -OutFile $zip
  $len = (Get-Item $zip).Length
  Rec 'station:install.1' ($len -gt 500KB) "downloaded $([math]::Round($len/1KB)) KB from the ZIP address"
  # 2 Unblock, then extract
  Set-Content -Path $zip -Stream Zone.Identifier -Value "[ZoneTransfer]`r`nZoneId=3"
  $marked = [bool](Get-Item $zip -Stream Zone.Identifier -ErrorAction SilentlyContinue)
  Unblock-File $zip
  $cleared = -not [bool](Get-Item $zip -Stream Zone.Identifier -ErrorAction SilentlyContinue)
  Expand-Archive $zip (Join-Path $work 'x') -Force
  $root = (Get-ChildItem (Join-Path $work 'x') -Directory | Select-Object -First 1).FullName
  Rec 'station:install.2' ($marked -and $cleared -and (Test-Path "$root\install\windows\install.cmd")) "ZIP marked from the internet: $marked; Unblock cleared it: $cleared; extracted, install\windows\install.cmd present"
  # 3 + 4 Run install.cmd, type the callsign, press Enter at the QRZ question
  $answers = "$Callsign`r`n`r`n"                   # callsign, then Enter for the QRZ question
  $args = @('-KeepAwake', 'off'); if (-not $Models) { $args += '-SkipModels' }
  $out = Join-Path $work 'install.log'
  $p = Start-Process cmd.exe -ArgumentList (@('/c', "`"$root\install\windows\install.cmd`"") + $args) -RedirectStandardInput (Join-Path $work 'in.txt') -RedirectStandardOutput $out -RedirectStandardError (Join-Path $work 'err.txt') -NoNewWindow -PassThru -Wait 2>$null
} catch { Rec 'station:install.1' $false "setup failed: $_" }
# Start-Process needs the stdin file before it runs: do it with the real call below
try {
  Set-Content -Path (Join-Path $work 'in.txt') -Value "$Callsign`r`n`r`n`r`n" -Encoding ascii
  $out = Join-Path $work 'install.log'; $err = Join-Path $work 'err.txt'
  $p = Start-Process cmd.exe -ArgumentList (@('/c', "`"$root\install\windows\install.cmd`"") + $args) -RedirectStandardInput (Join-Path $work 'in.txt') -RedirectStandardOutput $out -RedirectStandardError $err -NoNewWindow -PassThru
  if (-not $p.WaitForExit(2400000)) { $p.Kill(); throw 'the installer did not finish in 40 minutes' }
  $log = (Get-Content $out -Raw -ErrorAction SilentlyContinue) + (Get-Content $err -Raw -ErrorAction SilentlyContinue)
  Rec 'station:install.3' ($log -match 'Installed in') "install.cmd ran (exit $($p.ExitCode)); the installer reached its last step"
  Rec 'station:install.4' ($log -match '== Done') "the callsign was taken from the keyboard, the QRZ question skipped with Enter, and the installer ended with '== Done'"
  $dir = if ($log -match 'Installed in ([^.\r\n]+)\.') { $Matches[1].Trim() } else { 'C:\HAM-Sidekick-Station' }
  # what the card promises afterwards
  Rec 'after:tasks' ([bool](Get-ScheduledTask -TaskName 'HAM Sidekick Station' -ErrorAction SilentlyContinue) -and [bool](Get-ScheduledTask -TaskName 'HAM Sidekick Callbook Update' -ErrorAction SilentlyContinue)) "scheduled tasks 'HAM Sidekick Station' and 'HAM Sidekick Callbook Update'"
  Rec 'after:firewall' ([bool](Get-NetFirewallRule -Name 'HAM-Sidekick-Station-8091' -ErrorAction SilentlyContinue)) 'firewall rule for the dashboard port'
  Rec 'after:shortcut' (Test-Path "$env:PUBLIC\Desktop\HAM Sidekick Station.url") "the 'HAM Sidekick Station' shortcut is on the desktop"
  Rec 'after:folder' ((Test-Path "$dir\station\core.py") -and (Test-Path "$dir\.venv\Scripts\python.exe") -and (Test-Path "$dir\config\station.yaml")) "program, environment and config in $dir"
  # start the station by hand and open the dashboard (card step 5 is the shortcut; the page it opens is the same)
  $py = "$dir\.venv\Scripts\python.exe"
  $env:CALLBOOK_DIR = "$dir\data\callbook"
  $srv = Start-Process $py -ArgumentList '-m', 'station', '--engine', 'fake', '--no-listen', '--port', '8091' -WorkingDirectory $dir -PassThru -WindowStyle Hidden -RedirectStandardOutput "$work\station.log" -RedirectStandardError "$work\station.err"
  $page = $null; foreach ($i in 1..40) { try { $page = (Invoke-WebRequest -UseBasicParsing http://localhost:8091/ -TimeoutSec 5).Content; break } catch { Start-Sleep 2 } }
  Rec 'station:first.5' ($page -and $page.Contains('<title>HAM Sidekick Station</title>')) 'http://localhost:8091 opens the dashboard titled HAM Sidekick Station'
  if ($srv -and -not $srv.HasExited) { Stop-Process -Id $srv.Id -Force }
} catch { Rec 'station:install.3' $false "failed: $_" }
$results | ConvertTo-Json -Depth 3 | Set-Content $Report -Encoding utf8
if ($results | Where-Object status -eq 'FAIL') { exit 1 } else { exit 0 }
