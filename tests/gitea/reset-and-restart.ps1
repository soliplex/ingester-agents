# PowerShell script to completely reset and restart Gitea
# This will DELETE ALL DATA and start fresh

Write-Host "⚠️  WARNING: This will delete ALL Gitea data!" -ForegroundColor Red
Write-Host "Press Ctrl+C to cancel, or any other key to continue..." -ForegroundColor Yellow
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")

Write-Host ""
Write-Host "Step 1: Stopping and removing container..." -ForegroundColor Cyan
docker stop gitea 2>$null
docker rm gitea 2>$null

Write-Host "Step 2: Removing Docker volumes and data..." -ForegroundColor Cyan
# Remove Docker volume
docker volume rm gitea_gitea_data 2>$null
Write-Host "✓ Docker volume removed" -ForegroundColor Green

# Also remove local directory if it exists (for backward compatibility)
if (Test-Path gitea_data) {
    Remove-Item -Recurse -Force gitea_data
    Write-Host "✓ Local data directory removed" -ForegroundColor Green
}

Write-Host "Step 3: Starting container..." -ForegroundColor Cyan
docker compose up -d
if ($LASTEXITCODE -ne 0) {
    # Try docker-compose with hyphen
    docker-compose up -d
}

Write-Host "Step 4: Waiting for Gitea to initialize..." -ForegroundColor Cyan
Start-Sleep -Seconds 10

Write-Host ""
Write-Host "✓ Gitea has been reset and restarted!" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host "  1. Open http://localhost:3000 in your browser" -ForegroundColor Gray
Write-Host "  2. Complete the installation wizard" -ForegroundColor Gray
Write-Host "  3. Create admin user during installation (recommended)" -ForegroundColor Gray
Write-Host "     OR run .\init-admin.ps1 after installation" -ForegroundColor Gray
Write-Host ""
Write-Host "See INSTALL_GUIDE.md for detailed instructions" -ForegroundColor Cyan
