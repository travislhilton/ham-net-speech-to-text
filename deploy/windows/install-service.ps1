# Run as Administrator on the puck. Installs recorder.py as a Windows service via NSSM.
# choco install nssm   (or download nssm.cc)
$root = if (Test-Path "C:\ham-net-speech-to-text\capture") { "C:\ham-net-speech-to-text\capture" } else { "C:\HAM-Sidekick-Station\capture" }   # PCs installed before the rename keep the old folder
$py   = "$root\.venv\Scripts\python.exe"
nssm install EmcommVoiceCapture $py "$root\recorder.py" "--config" "$root\config.yaml"
nssm set EmcommVoiceCapture AppDirectory $root
nssm set EmcommVoiceCapture AppPriority HIGH_PRIORITY_CLASS
nssm set EmcommVoiceCapture AppEnvironmentExtra EMCOMM_STT_KEY=CHANGE_ME
nssm set EmcommVoiceCapture AppStdout "$root\service.log"
nssm set EmcommVoiceCapture AppStderr "$root\service.err"
nssm set EmcommVoiceCapture Start SERVICE_AUTO_START
nssm start EmcommVoiceCapture
