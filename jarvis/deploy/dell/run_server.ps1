# run_server.ps1 — run the JARVIS V2 server in the foreground (Ctrl+C stops)
#
#   .\deploy\dell\run_server.ps1 [--host HOST] [--port PORT]
#
# Configuration is read from .env / environment (see server/config.py);
# optional flags override host/port for that run only.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $root

$python = Join-Path $root ".venv-server\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Error ".venv-server is missing - run .\deploy\dell\setup.ps1 first."
}
if (-not (Test-Path ".env")) {
    Write-Warning "No .env found - using defaults (loopback bind, development)."
}

& $python server\run.py @args
