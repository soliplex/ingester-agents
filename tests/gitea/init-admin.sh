#!/bin/bash
# Script to create initial admin user for Gitea test instance
# Usage: ./init-admin.sh

echo "Waiting for Gitea to start..."
sleep 5

echo "Creating admin user..."
docker exec -it gitea gitea admin user create \
  --username admin \
  --password admin123 \
  --email admin@example.com \
  --admin \
  --must-change-password=false

if [ $? -eq 0 ]; then
    echo "✓ Admin user created successfully"
    echo "Username: admin"
    echo "Password: admin123"
    echo "Email: admin@example.com"

    # Create API token
    echo ""
    echo "Creating API token..."
    docker exec -it gitea gitea admin user generate-access-token \
      --username admin \
      --token-name "test-token" \
      --scopes "write:repository,write:issue,write:user"

    echo ""
    echo "Save this token for GITEA_TOKEN environment variable"
else
    echo "✗ Failed to create admin user (may already exist)"
    echo "Try logging in with: admin / admin123"
fi
