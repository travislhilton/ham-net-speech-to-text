# Enable OpenSSH Server on the puck (W9XYZ-5) so the Macs can manage it. Idempotent.
# Run ONCE in an elevated PowerShell (Start → type "PowerShell" → Run as administrator):
#   paste this whole file, or:  powershell -ExecutionPolicy Bypass -File enable-ssh.ps1
# Key-only login from the home server and dev laptop; port 22 open only to Tailscale + home LAN.
$ErrorActionPreference = 'Stop'
$keys = @(
  'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEKEYEXAMPLEKEYEXAMPLEKEYEXAMPLEKEY you@your-computer',
  'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEKEYEXAMPLEKEYEXAMPLEKEYEXAMPLEKEY you@your-computer (dev laptop)'
)
$allowFrom = @('100.64.0.0/10', '192.168.1.0/24')   # Tailscale CGNAT range, home LAN

$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run this in an ADMINISTRATOR PowerShell.' }

# 1. The OpenSSH Server feature (built into Windows 11; downloads a few MB the first time)
$cap = Get-WindowsCapability -Online | Where-Object Name -like 'OpenSSH.Server*'
if ($cap.State -ne 'Installed') { Add-WindowsCapability -Online -Name $cap.Name | Out-Null }

# 2. Start now and on every boot
Set-Service sshd -StartupType Automatic
Start-Service sshd

# 3. Land in PowerShell, not cmd.exe
New-Item -Path 'HKLM:\SOFTWARE\OpenSSH' -Force | Out-Null
New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell -PropertyType String -Force `
  -Value "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" | Out-Null

# 4. Keys. An administrator's keys are read ONLY from administrators_authorized_keys, and sshd
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

# 5. Firewall: port 22 from Tailscale and the home LAN only, on every network profile
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
