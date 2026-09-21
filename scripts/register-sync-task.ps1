# Register a Task Scheduler job that publishes the night shift's develop branch to GitHub every 30 minutes.
# Usage (Windows PowerShell, no admin needed):  .\scripts\register-sync-task.ps1
param(
    [string]$TaskName = "PrivaSheet Night Shift GitHub Sync"
)
$ErrorActionPreference = "Stop"

$script = Join-Path $PSScriptRoot "sync-github.ps1"
$action = New-ScheduledTaskAction -Execute "conhost.exe" `
    -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 30)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Force | Out-Null
Write-Host "Registered '$TaskName' (every 30 minutes). Log: $env:LOCALAPPDATA\PrivaSheet-nightshift\sync.log"
