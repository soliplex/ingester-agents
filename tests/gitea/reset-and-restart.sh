#!/bin/bash
# Script to completely reset and restart Gitea
# This will DELETE ALL DATA and start fresh

echo "⚠️  WARNING: This will delete ALL Gitea data!"
echo "Press Ctrl+C to cancel, or Enter to continue..."
read -r

echo ""
echo "Step 1: Stopping and removing container..."
docker stop gitea 2>/dev/null
docker rm gitea 2>/dev/null

echo "Step 2: Removing Docker volumes and data..."
# Remove Docker volume
docker volume rm gitea_gitea_data 2>/dev/null
echo "✓ Docker volume removed"

# Also remove local directory if it exists (for backward compatibility)
if [ -d "gitea_data" ]; then
    rm -rf gitea_data
    echo "✓ Local data directory removed"
fi

echo "Step 3: Starting container..."
if command -v docker compose &> /dev/null; then
    docker compose up -d
else
    docker-compose up -d
fi

echo "Step 4: Waiting for Gitea to initialize..."
sleep 10

echo ""
echo "✓ Gitea has been reset and restarted!"
echo ""
echo "Next steps:"
echo "  1. Open http://localhost:3000 in your browser"
echo "  2. Complete the installation wizard"
echo "  3. Create admin user during installation (recommended)"
echo "     OR run ./init-admin.sh after installation"
echo ""
echo "See INSTALL_GUIDE.md for detailed instructions"
