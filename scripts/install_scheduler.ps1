param(
    [Parameter(Mandatory=$true)][string]$TaskName,
    [ValidateRange(0,6)][int]$Day = 6,
    [ValidatePattern('^([01]\d|2[0-3]):[0-5]\d$')][string]$At = '18:00'
)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Python environment not found.' }
$days = @('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday')
# Build an absolute Moscow start boundary; Task Scheduler converts it to the Windows timezone.
$moscow = [System.TimeZoneInfo]::FindSystemTimeZoneById('Russian Standard Time')
$nowMoscow = [System.TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow, $moscow)
$mondayIndex = ([int]$nowMoscow.DayOfWeek + 6) % 7
$offset = ($Day - $mondayIndex + 7) % 7
$target = $nowMoscow.Date.AddDays($offset).Add([TimeSpan]::Parse($At))
if ($target -le $nowMoscow) { $target = $target.AddDays(7) }
$localTarget = [System.TimeZoneInfo]::ConvertTime($target, $moscow, [System.TimeZoneInfo]::Local)
$action = New-ScheduledTaskAction -Execute $pythonPath -Argument '-m dzen_agent.pipeline.run --scheduled' -WorkingDirectory $projectRoot
# The named weekday is converted too for machines outside Moscow.
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $localTarget.DayOfWeek -At $localTarget
$taskSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 6) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $taskSettings -Principal $principal -Force | Out-Null
Write-Output 'Installed. The user must be logged in to Windows.'
