# Copy this folder into the privasheet-dev distribution and install it (root-owned).
# Usage (Windows PowerShell):  .\scripts\deploy.ps1
# The distribution has interop disabled, so \\wsl.localhost is unavailable; files are streamed with tar
# through cmd.exe (binary-safe pipe).
param(
    [string]$Distro = "privasheet-dev"
)
$ErrorActionPreference = "Stop"

$src = Split-Path -Parent $PSScriptRoot
$excludes = "--exclude=__pycache__ --exclude=.pytest_cache --exclude=*.egg-info --exclude=build"
$extract = "rm -rf /tmp/nightshift-src && mkdir -p /tmp/nightshift-src && tar -xf - -C /tmp/nightshift-src && sed -i 's/\r$//' /tmp/nightshift-src/scripts/*.sh"

& cmd.exe /c "tar -cf - $excludes -C `"$src`" . | wsl.exe -d $Distro -u root -- bash -c `"$extract`""
if ($LASTEXITCODE -ne 0) { throw "copy failed ($LASTEXITCODE)" }

& wsl.exe -d $Distro -u root -- bash /tmp/nightshift-src/scripts/install-in-wsl.sh /tmp/nightshift-src
if ($LASTEXITCODE -ne 0) { throw "install failed ($LASTEXITCODE)" }
Write-Host "Installed. A running night shift keeps the old code until restarted:"
Write-Host "  wsl -d $Distro -u agent -- pkill -f '^/opt/nightshift/venv/bin/python -m nightshift run'   (the scheduled task starts it again)"
