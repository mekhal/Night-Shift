# Publish the night shift's local develop branch to GitHub.
# The WSL distro has no GitHub credentials; this script runs on Windows with the maintainer's git credentials.
# Pushes only develop, only fast-forward, and only when every new commit uses an allowed author/committer email.
# Usage: .\scripts\sync-github.ps1   (register-sync-task.ps1 runs it every 30 minutes)
param(
    [string]$Distro = "privasheet-dev",
    [string]$Repo = "C:\Users\mekha\OneDrive\Documents\AI\Udemy\PrivaSheet\PrivaSheet",
    [string[]]$AllowedEmails = @("mekha.l@outlook.com", "nightshift@localhost", "noreply@github.com")
)
$ErrorActionPreference = "Continue"
$env:GIT_TERMINAL_PROMPT = "0"

$logDir = Join-Path $env:LOCALAPPDATA "PrivaSheet-nightshift"
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir "sync.log"
function Write-Log([string]$message) {
    $line = "{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $message
    Add-Content -Path $log -Value $line -Encoding utf8
    Write-Host $line
}

Set-Location $Repo
$bundle = Join-Path $env:TEMP "privasheet-develop.bundle"

# cmd.exe pipes bytes unchanged; PowerShell 5.1 redirection would re-encode the binary bundle.
& cmd.exe /c "wsl.exe -d $Distro -u agent -- git -C /home/agent/work/PrivaSheet bundle create - develop > `"$bundle`" 2>nul"
if ($LASTEXITCODE -ne 0) { Write-Log "ERROR bundle failed ($LASTEXITCODE)"; exit 1 }

git fetch --quiet $bundle "+develop:refs/remotes/nightshift/develop" *> $null
if ($LASTEXITCODE -ne 0) { Write-Log "ERROR fetch from bundle failed"; exit 1 }
git fetch --quiet origin develop *> $null
if ($LASTEXITCODE -ne 0) { Write-Log "ERROR fetch origin failed"; exit 1 }

$local = git rev-parse nightshift/develop
$remote = git rev-parse origin/develop
if ($local -eq $remote) { Write-Log "up to date ($($local.Substring(0, 7)))"; exit 0 }

git merge-base --is-ancestor origin/develop nightshift/develop
if ($LASTEXITCODE -ne 0) {
    Write-Log "SKIPPED origin/develop has commits the night shift does not have (not a fast-forward)"
    exit 2
}

$emails = git log "origin/develop..nightshift/develop" --format="%ae%n%ce" | Sort-Object -Unique
$blocked = @($emails | Where-Object { $_ -and ($AllowedEmails -notcontains $_) })
if ($blocked.Count -gt 0) {
    Write-Log "SKIPPED commits use emails not allowed on the public repo: $($blocked -join ', ')"
    exit 3
}

$count = (git rev-list --count "origin/develop..nightshift/develop")
git push --quiet origin "refs/remotes/nightshift/develop:refs/heads/develop" *> $null
if ($LASTEXITCODE -ne 0) { Write-Log "ERROR push failed ($LASTEXITCODE)"; exit 1 }
Write-Log "pushed $count commit(s) to develop ($($remote.Substring(0, 7)) -> $($local.Substring(0, 7)))"
