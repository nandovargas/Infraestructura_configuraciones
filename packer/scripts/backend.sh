#!/usr/bin/env bash
# Imagen backend: Python 3.12 + dependencias Django + Prometheus (instalado, aún sin uso).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

cloud-init status --wait || true

apt-get update -y
apt-get upgrade -y
apt-get install -y ca-certificates curl git build-essential \
  python3 python3-venv python3-dev python3-pip libpq-dev

# Entorno virtual con las dependencias del backend
install -d -o ubuntu -g ubuntu /opt/asistencia/backend
sudo -u ubuntu python3 -m venv /opt/asistencia/backend/venv
sudo -u ubuntu /opt/asistencia/backend/venv/bin/pip install --upgrade pip
sudo -u ubuntu /opt/asistencia/backend/venv/bin/pip install -r /tmp/app/requirements.txt

# Prometheus (binario oficial, versión fija)
useradd --system --no-create-home --shell /usr/sbin/nologin prometheus || true
cd /tmp
curl -fsSLO "https://github.com/prometheus/prometheus/releases/download/v${PROMETHEUS_VERSION}/prometheus-${PROMETHEUS_VERSION}.linux-amd64.tar.gz"
tar xzf "prometheus-${PROMETHEUS_VERSION}.linux-amd64.tar.gz"
D="prometheus-${PROMETHEUS_VERSION}.linux-amd64"
install -m 0755 "$D/prometheus" "$D/promtool" /usr/local/bin/
install -d -o prometheus -g prometheus /etc/prometheus /var/lib/prometheus
cp -r "$D/consoles" "$D/console_libraries" /etc/prometheus/ 2>/dev/null || true
install -m 0644 -o prometheus -g prometheus /tmp/app/prometheus.yml /etc/prometheus/prometheus.yml
promtool check config /etc/prometheus/prometheus.yml

cat > /etc/systemd/system/prometheus.service <<'EOF'
[Unit]
Description=Prometheus
After=network-online.target
Wants=network-online.target

[Service]
User=prometheus
Group=prometheus
ExecStart=/usr/local/bin/prometheus \
  --config.file=/etc/prometheus/prometheus.yml \
  --storage.tsdb.path=/var/lib/prometheus \
  --web.listen-address=0.0.0.0:9090
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
# Instalado pero deshabilitado: se activará cuando se vaya a usar.
systemctl disable prometheus || true

python3 --version
/opt/asistencia/backend/venv/bin/python -c "import django; print('Django', django.get_version())"
prometheus --version

rm -rf /tmp/prometheus-* /tmp/app
apt-get clean
rm -rf /var/lib/apt/lists/*
