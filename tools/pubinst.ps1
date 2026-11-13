# Install from the PUBLIC GitHub ZIP, exactly as a new user would (models/callbook/QRZ already present).
$ErrorActionPreference = 'Stop'
$dl = "$env:USERPROFILE\hamsidekick-admin"; New-Item -ItemType Directory -Force $dl | Out-Null
$zip = "$dl\hamsidekick-master.zip"; $dir = "$dl\hamsidekick-public"
Remove-Item -Force $zip -ErrorAction SilentlyContinue; Remove-Item -Recurse -Force $dir -ErrorAction SilentlyContinue
[Net.ServicePointManager]::SecurityProtocol = 'Tls12'
Invoke-WebRequest -UseBasicParsing 'https://github.com/hamsidekick/hamsidekick/archive/refs/heads/master.zip' -OutFile $zip
"zip bytes: $((Get-Item $zip).Length)"
Expand-Archive $zip $dir
$src = Get-ChildItem $dir -Directory | Select-Object -First 1
"source: $($src.FullName)"
Get-Content "$($src.FullName)\README.md" -TotalCount 1
$ErrorActionPreference = 'Continue'
& powershell -NoProfile -ExecutionPolicy Bypass -File "$($src.FullName)\install\windows\install.ps1" -SkipModels -SkipCallbook -SkipQrz 2>&1 | Out-String -Width 220
"install exit: $LASTEXITCODE"
