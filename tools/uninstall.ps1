<#
HAM Sidekick Station: remove the program from this PC (run as administrator). Settings and history are NOT lost by default:

  uninstall.ps1 [-InstallDir C:\HAM-Sidekick-Station] [-MoveAside] [-Delete]

Always: stops the station, removes its scheduled tasks ("HAM Sidekick Station", "HAM Sidekick Callbook Update"), the
firewall rule, the desktop shortcut and the machine-wide HAMSIDEKICK_HOME / CALLBOOK_DIR variables.
The folder itself:  -MoveAside renames it to <folder>.pre-reinstall-<date> (everything stays on disk: use this before a
re-install);  -Delete removes it (data, recordings and history go with it);  neither = leave it where it is.
Not touched: Python, the Visual C++ runtime, the 'HAM Sidekick Station' power plan, QRZ login (Windows Credential Manager).
#>
param([string]$InstallDir = '', [switch]$MoveAside, [switch]$Delete)
$ErrorActionPreference = 'Stop'
if (-not $InstallDir) { $InstallDir = if (Test-Path 'C:\HAM-Sidekick-Station') { 'C:\HAM-Sidekick-Station' } else { 'C:\ham-net-speech-to-text' } }
foreach ($t in 'HAM Sidekick Station', 'HAM Sidekick Callbook Update', 'HamNet Station', 'HamNet Callbook Update') {
  if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $t -Confirm:$false; "removed task: $t"
  }
}
Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like "$InstallDir\*" -or $_.CommandLine -like "*$InstallDir*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep 3
foreach ($r in 'HAM-Sidekick-Station-8091', 'HamNet-Station-8091') { Remove-NetFirewallRule -Name $r -ErrorAction SilentlyContinue }
foreach ($s in 'HAM Sidekick Station.url', 'HamNet Station.url') { Remove-Item "$env:PUBLIC\Desktop\$s" -ErrorAction SilentlyContinue }
foreach ($v in 'HAMSIDEKICK_HOME', 'CALLBOOK_DIR', 'HAMNET_HOME') { [Environment]::SetEnvironmentVariable($v, $null, 'Machine') }
"removed: firewall rule, desktop shortcut, machine variables"
if ($MoveAside -and (Test-Path $InstallDir)) {
  $to = "$InstallDir.pre-reinstall-$(Get-Date -Format yyyyMMdd-HHmmss)"
  Rename-Item $InstallDir (Split-Path $to -Leaf); "folder moved aside: $to"
} elseif ($Delete -and (Test-Path $InstallDir)) {
  Remove-Item $InstallDir -Recurse -Force; "folder deleted: $InstallDir"
} else { "folder left in place: $InstallDir" }
