# Enable OpenSSH Server on the station PC so the Macs can manage it. Idempotent.
# Run ONCE in an elevated PowerShell (Start → type "PowerShell" → Run as administrator):
#   paste this whole file, or:  powershell -ExecutionPolicy Bypass -File enable-ssh.ps1
# Key-only login from the computers in ssh-keys.txt; port 22 open only to Tailscale + this PC's own network.
param(
  # One public key per line (from the computers that will manage this PC). Kept out of the repo.
  [string]$KeysFile = "$PSScriptRoot\ssh-keys.txt"
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path $KeysFile)) { throw "Put the managing computers' SSH public keys, one per line, in $KeysFile (not committed)." }
$keys = @(Get-Content $KeysFile | Where-Object { $_ -match '^ssh-' })
if (-not $keys) { throw "No ssh- public keys found in $KeysFile" }
$allowFrom = @('100.64.0.0/10', 'LocalSubnet')   # Tailscale and this PC's own network only

$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run this in an ADMINISTRATOR PowerShell.' }

# 1. The OpenSSH Server feature (built into Windows 11; downloads a few MB the first time)
$cap = Get-WindowsCapability -Online | Where-Object Name -like 'OpenSSH.Server*' | Select-Object -First 1
if (-not $cap) { throw 'Windows offers no OpenSSH.Server feature on this PC. Send this output to Claude.' }
Write-Host "OpenSSH.Server feature: $($cap.Name) = $($cap.State)"
if ($cap.State -ne 'Installed') {
  $r = Add-WindowsCapability -Online -Name $cap.Name
  $cap = Get-WindowsCapability -Online -Name $cap.Name
  Write-Host "after install: $($cap.State), restart needed: $($r.RestartNeeded)"
}

# 2. The sshd service. Seen 2026-11-02 on the test PC: the feature install returned without error and no
#    service existed. If sshd.exe is on disk, register the service ourselves; if not, Windows still
#    has the install pending and only a restart finishes it.
$sshdExe = "$env:SystemRoot\System32\OpenSSH\sshd.exe"
if (-not (Get-Service sshd -ErrorAction SilentlyContinue)) {
  if (Test-Path $sshdExe) {
    Write-Host 'sshd.exe is installed but its service is not registered; registering it.'
    New-Service -Name sshd -DisplayName 'OpenSSH SSH Server' -BinaryPathName "`"$sshdExe`"" `
      -Description 'SSH protocol based service to provide secure encrypted communications.' -StartupType Automatic | Out-Null
    sc.exe failure sshd reset= 86400 actions= restart/5000/restart/60000/restart/60000 | Out-Null
  } else {
    Write-Host ''
    Write-Host "The OpenSSH feature reports '$($cap.State)' but $sshdExe is not on disk yet."
    Write-Host 'RESTART this PC (Start > Power > Restart), then run this same script again.'
    exit 1
  }
}

# 3. Start now and on every boot
Set-Service sshd -StartupType Automatic
Start-Service sshd

# 4. Land in PowerShell, not cmd.exe
New-Item -Path 'HKLM:\SOFTWARE\OpenSSH' -Force | Out-Null
New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell -PropertyType String -Force `
  -Value "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" | Out-Null

# 5. Keys. An administrator's keys are read ONLY from administrators_authorized_keys, and sshd
#    ignores that file unless just Administrators + SYSTEM can read it.
$adminKeys = "$env:ProgramData\ssh\administrators_authorized_keys"
$existing = if (Test-Path $adminKeys) { @(Get-Content $adminKeys) } else { @() }
$merged = @($existing + $keys | Where-Object { $_ } | Select-Object -Unique)
Set-Content -Path $adminKeys -Value $merged -Encoding ascii
icacls $adminKeys /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F' | Out-Null
$userKeys = "$env:USERPROFILE\.ssh\authorized_keys"                  # used if the account is not an admin
New-Item -ItemType Directory -Force (Split-Path $userKeys) | Out-Null
$existing = if (Test-Path $userKeys) { @(Get-Content $userKeys) } else { @() }
Set-Content -Path $userKeys -Value (@($existing + $keys | Where-Object { $_ } | Select-Object -Unique)) -Encoding ascii

# 6. Firewall: port 22 from Tailscale and the home LAN only, on every network profile
$rule = Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
if (-not $rule) {
  New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH Server (sshd)' -Direction Inbound `
    -Protocol TCP -LocalPort 22 -Action Allow | Out-Null
}
Set-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -Enabled True -Profile Any -RemoteAddress $allowFrom

Restart-Service sshd
Write-Host ''
Write-Host "sshd: $((Get-Service sshd).Status), starts $((Get-Service sshd).StartType)"
Write-Host "Log in from a Mac as:  ssh $env:USERNAME@$($env:COMPUTERNAME.ToLower())"
Write-Host "whoami = $(whoami)   (tell Claude the part after the backslash)"
