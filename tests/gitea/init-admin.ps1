# PowerShell script to create initial admin user for Gitea test instance
# Usage: .\init-admin.ps1

Write-Host "Waiting for Gitea to start..." -ForegroundColor Cyan
Start-Sleep -Seconds 5

Write-Host "Creating admin user..." -ForegroundColor Cyan
docker exec gitea gitea admin user create `
  --username admin `
  --password admin123 `
  --email admin@example.com `
  --admin `
  --must-change-password=false

if ($LASTEXITCODE -eq 0) {
    Write-Host "Admin user created successfully" -ForegroundColor Green
    Write-Host "Username: admin" -ForegroundColor Yellow
    Write-Host "Password: admin123" -ForegroundColor Yellow
    Write-Host "Email: admin@example.com" -ForegroundColor Yellow

    # Create API token
    Write-Host ""
    Write-Host "Creating API token..." -ForegroundColor Cyan
    $token = docker exec -u git  gitea gitea admin user generate-access-token `
      --username admin `
      --token-name "test-token" `
      --scopes "write:repository,write:issue,write:user"

    Write-Host ""
    Write-Host "API Token generated:" -ForegroundColor Green
    Write-Host $token -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Set this in your environment:" -ForegroundColor Cyan
    Write-Host "  `$env:GITEA_TOKEN = `"$token`"" -ForegroundColor Gray
} else {
    Write-Host "Failed to create admin user (may already exist)" -ForegroundColor Red
    Write-Host "Try logging in with admin and password admin123" -ForegroundColor Yellow
}
