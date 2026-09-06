# install_service.ps1 — register the JARVIS server as a Windows auto-start task
#
#   .\deploy\dell\install_service.ps1                # runs at YOUR logon
#   .\deploy\dell\install_service.ps1 -AtStartup     # runs at boot as SYSTEM
#
# -AtStartup requires an elevated shell and a repo path SYSTEM can read
# (i.e. NOT under C:\Users\...\Downloads).  With plain logon mode the server
# stops when you log off; -AtStartup keeps it running 24/7.
#
# Remove with .\deploy\dell\uninstall_service.ps1

param(
    [switch]$AtStartup,
    [string]$LogonUser = "$env:USERDOMAIN\$env:USERNAME"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$python = Join-Path $root ".venv-server\Scripts\python.exe"
$runner = Join-Path $root "server\run.py"

if (-not (Test-Path $python)) {
    Write-Error ".venv-server is missing - run .\deploy\dell\setup.ps1 first."
}

$taskName = "JarvisServer"
$cmd = "`"$python`" `"$runner`""

if ($AtStartup) {
    Write-Host "Registering '$taskName' to run at boot as SYSTEM ..."
    schtasks /Create /F /TN $taskName /TR $cmd /SC ONSTART /RU SYSTEM /RL HIGHEST
} else {
    Write-Host "Registering '$taskName' to run at logon of $LogonUser ..."
    schtasks /Create /F /TN $taskName /TR $cmd /SC ONLOGON /RU $LogonUser /RL LIMITED
}
if ($LASTEXITCODE -ne 0) { Write-Error "schtasks /Create failed." }

Write-Host "Registered. Try it:"
Write-Host "  schtasks /Run /TN $taskName"
Write-Host "  schtasks /Query /TN $taskName"
