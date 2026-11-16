#!/usr/bin/env bash
# Imagen frontend: Node.js LTS + nginx (para servir el build de Vite) + dependencias npm.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

cloud-init status --wait || true

apt-get update -y
apt-get upgrade -y
apt-get install -y ca-certificates curl gnupg nginx build-essential git

# Node.js (NodeSource)
curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -
apt-get install -y nodejs

# Dependencias del frontend preinstaladas
install -d -o ubuntu -g ubuntu /opt/asistencia/frontend
cp /tmp/app/package.json /tmp/app/package-lock.json /opt/asistencia/frontend/
chown ubuntu:ubuntu /opt/asistencia/frontend/package*.json
sudo -u ubuntu bash -c 'cd /opt/asistencia/frontend && npm ci'

# nginx como servidor de la SPA (fallback a index.html para react-router)
cat > /etc/nginx/sites-available/asistencia <<'EOF'
server {
    listen 80 default_server;
    server_name _;
    root /var/www/asistencia;
    index index.html;
    location / {
        try_files $uri /index.html;
    }
}
EOF
install -d /var/www/asistencia
ln -sf /etc/nginx/sites-available/asistencia /etc/nginx/sites-enabled/asistencia
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable nginx

node --version
npm --version
nginx -v

apt-get clean
rm -rf /var/lib/apt/lists/* /tmp/app
