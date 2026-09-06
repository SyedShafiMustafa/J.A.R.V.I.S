# uninstall_service.ps1 — stop auto-starting the JARVIS server
#
#   .\deploy\dell\uninstall_service.ps1

$taskName = "JarvisServer"
Write-Host "Removing scheduled task '$taskName' ..."
schtasks /Delete /F /TN $taskName
if ($LASTEXITCODE -eq 0) {
    Write-Host "Removed."
} else {
    Write-Host "Task not found or removal failed (exit $LASTEXITCODE)."
}
