# setup.ps1 — one-time Dell bootstrap for the JARVIS V2 always-on server
#
#   powershell -ExecutionPolicy Bypass -File .\deploy\dell\setup.ps1
#
# Creates .venv-server, installs requirements-server.txt, creates .env from
# the template, makes the logs/ + data/ dirs, then runs the smoke suite.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $root

Write-Host "== JARVIS V2 - Dell server setup ==" -ForegroundColor Cyan
Write-Host "Repo root: $root"

# 1) Python discovery (3.10+)
$py = $null
if (Get-Command python -ErrorAction SilentlyContinue) { $py = "python" }
elseif (Get-Command py -ErrorAction SilentlyContinue) { $py = "py" }
else { Write-Error "Python 3.10+ is required (python.org download, tick 'Add python.exe to PATH')." }

& $py --version
if ($LASTEXITCODE -ne 0) { Write-Error "python --version failed." }

# 2) Virtual environment
if (-not (Test-Path ".venv-server")) {
    Write-Host "Creating .venv-server ..."
    & $py -m venv .venv-server
}
$python = Join-Path $root ".venv-server\Scripts\python.exe"

# 3) Dependencies
Write-Host "Installing server dependencies (requirements-server.txt) ..."
& $python -m pip install --upgrade pip | Out-Null
& $python -m pip install -r requirements-server.txt
if ($LASTEXITCODE -ne 0) { Write-Error "pip install failed - check network access to PyPI." }

# 4) .env from template (never overwrite an existing one)
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example - EDIT IT now (at least set JARVIS_SECRET_KEY)." -ForegroundColor Yellow
}

# 5) Working dirs
New-Item -ItemType Directory -Force -Path "logs", "data" | Out-Null

# 6) Self-test
Write-Host "Running the server test suite ..."
& $python server\smoke.py
if ($LASTEXITCODE -ne 0) { Write-Error "Smoke suite failed - see messages above." }

Write-Host ""
Write-Host "Done. Next steps:" -ForegroundColor Green
Write-Host "  1. Edit .env           (JARVIS_SECRET_KEY, node name, bind host)"
Write-Host "  2. .\deploy\dell\run_server.ps1        # foreground run"
Write-Host "  3. .\deploy\dell\install_service.ps1   # auto-start (logon or boot)"
Write-Host "  Health check:          http://<host>:<port>/healthz"
