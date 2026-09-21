# Register the Windows Task Scheduler watchdog: at logon and every 15 minutes, start the night shift
# if it is not running (nightshift itself exits immediately when another instance holds the lock).
# Usage (Windows PowerShell, no admin needed):  .\scripts\register-task.ps1
param(
    [string]$Distro = "privasheet-dev",
    [string]$TaskName = "PrivaSheet Night Shift"
)
$ErrorActionPreference = "Stop"

# conhost --headless keeps a console window from popping up every 15 minutes.
$action = New-ScheduledTaskAction -Execute "conhost.exe" `
    -Argument "--headless wsl.exe -d $Distro -u agent -- bash -lc `"nightshift run >> ~/nightshift/supervisor.log 2>&1`""

$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$every15 = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
    -RepetitionInterval (New-TimeSpan -Minutes 15)

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($atLogon, $every15) `
    -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "Registered '$TaskName'. Dashboard: http://127.0.0.1:8770"
Write-Host "Stop:    Disable-ScheduledTask -TaskName '$TaskName'; wsl -d $Distro -u agent -- pkill -f '^/opt/nightshift/venv/bin/python -m nightshift run'"
Write-Host "Pause:   wsl -d $Distro -u agent -- nightshift mode off"
