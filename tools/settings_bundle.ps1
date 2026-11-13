<#
HAM Sidekick Station: keep this PC's settings in ONE zip file, and put them back after a re-install.

  settings_bundle.ps1 -Backup  C:\Users\you\hamsidekick-admin\backups\settings.zip   [-InstallDir C:\HAM-Sidekick-Station]
  settings_bundle.ps1 -Restore C:\Users\you\hamsidekick-admin\backups\settings.zip   [-InstallDir C:\HAM-Sidekick-Station]

What is kept (settings, never the heavy or rebuildable parts): config\station.yaml, local.yaml, secrets.env and the other
hand-edited config; data\*.json (net schedule, radios, Sidekick settings, local names, scan spots, alerts, the operator's
choices); data\knowledge, chat, voice, tts, grok_stt, sidekick_replay; capture\config.yaml; setup\; deploy\windows\ssh-keys.txt.
NOT kept: history.db and recordings (the re-install script moves those back separately), the callbook, the OpenVINO cache,
models, logs, and config\engine.yaml (this PC is measured again; pass -WithEngine to put the old measurement back).
The zip holds secrets.env (API keys): keep it on this PC, never commit it or send it anywhere.
Restore stops nothing: run it while the station is stopped (the re-install script does).
#>
param(
  [string]$Backup = '', [string]$Restore = '', [string]$InstallDir = 'C:\HAM-Sidekick-Station', [switch]$WithEngine
)
$ErrorActionPreference = 'Stop'
$files = @('config\station.yaml', 'config\local.yaml', 'config\secrets.env', 'capture\config.yaml', 'deploy\windows\ssh-keys.txt')
$dataDirs = @('knowledge', 'chat', 'voice', 'tts', 'grok_stt', 'sidekick_replay')
$skipData = @('power_snapshot.json')
if ($Backup) {
  $stage = Join-Path $env:TEMP ("hsbundle-" + [guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Force $stage | Out-Null
  $n = 0
  function Keep($rel) { $src = Join-Path $InstallDir $rel; if (Test-Path $src) { $dst = Join-Path $stage $rel; New-Item -ItemType Directory -Force (Split-Path $dst) | Out-Null; Copy-Item $src $dst -Recurse -Force; $script:n++ } }
  foreach ($f in $files) { Keep $f }
  Keep 'setup'
  if ($WithEngine) { Keep 'config\engine.yaml' }
  Get-ChildItem "$InstallDir\data" -File -Force -ErrorAction SilentlyContinue |
    Where-Object { ($_.Extension -in '.json', '.jsonl') -and $skipData -notcontains $_.Name -and $_.Name -notlike 'history*' } |
    ForEach-Object { Keep "data\$($_.Name)" }
  foreach ($d in $dataDirs) { Keep "data\$d" }
  New-Item -ItemType Directory -Force (Split-Path $Backup) | Out-Null
  if (Test-Path $Backup) { Remove-Item $Backup -Force }
  Compress-Archive -Path "$stage\*" -DestinationPath $Backup -CompressionLevel Optimal
  Remove-Item $stage -Recurse -Force
  $z = Get-Item $Backup
  "backup: $n item(s) -> $($z.FullName) ($([math]::Round($z.Length/1KB)) KB)"
  exit 0
}
if ($Restore) {
  if (-not (Test-Path $Restore)) { throw "no such file: $Restore" }
  $stage = Join-Path $env:TEMP ("hsbundle-" + [guid]::NewGuid().ToString('N'))
  Expand-Archive $Restore $stage -Force
  $n = 0
  foreach ($item in Get-ChildItem $stage -Recurse -File -Force) {
    $rel = $item.FullName.Substring($stage.Length).TrimStart('\', '/')
    if ($rel -eq 'config\engine.yaml' -and -not $WithEngine) { continue }
    $dst = Join-Path $InstallDir $rel
    New-Item -ItemType Directory -Force (Split-Path $dst) | Out-Null
    Copy-Item $item.FullName $dst -Force; $n++
  }
  Remove-Item $stage -Recurse -Force
  "restore: $n file(s) put back under $InstallDir"
  exit 0
}
Write-Host 'usage: settings_bundle.ps1 -Backup <zip> | -Restore <zip> [-InstallDir <dir>] [-WithEngine]'
exit 2
