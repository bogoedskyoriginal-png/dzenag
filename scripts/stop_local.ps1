$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$listener = Get-NetTCPConnection -LocalPort 8501 -State Listen -ErrorAction SilentlyContinue
if (-not $listener) { Write-Output 'The app is already stopped.'; exit 0 }
$process = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener[0].OwningProcess)"
if ($process.CommandLine.Contains($projectRoot) -and $process.CommandLine.Contains('streamlit')) {
    Stop-Process -Id $process.ProcessId
    Write-Output 'Streamlit stopped. Scheduled/background work is controlled by the stop switch in the app.'
} else {
    throw 'Port 8501 belongs to another app; it was not stopped.'
}
