param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$appPath = Join-Path $projectRoot 'dzen_agent\ui.py'
$appUrl = 'http://localhost:8501'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Missing .venv. See README.md.' }
$listener = Get-NetTCPConnection -LocalPort 8501 -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener[0].OwningProcess)"
    if (-not $process.CommandLine.Contains($projectRoot) -or -not $process.CommandLine.Contains('streamlit')) {
        throw 'Port 8501 is occupied by another application.'
    }
} else {
    $logDir = Join-Path $projectRoot 'logs'
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $arguments = '-m streamlit run "' + $appPath + '" --server.address=127.0.0.1 --server.port=8501 --server.headless=true --browser.gatherUsageStats=false'
    Start-Process -FilePath $pythonPath -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDir 'streamlit.log') -RedirectStandardError (Join-Path $logDir 'streamlit-error.log') | Out-Null
}
$ready = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    try {
        $health = Invoke-WebRequest -UseBasicParsing -Uri ($appUrl + '/_stcore/health') -TimeoutSec 2
        if ($health.StatusCode -eq 200) { $ready = $true; break }
    } catch {}
    Start-Sleep -Seconds 1
}
if (-not $ready) { throw 'The app did not start. See logs/streamlit-error.log.' }
Write-Output $appUrl
if (-not $NoBrowser) { Start-Process $appUrl }
