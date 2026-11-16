#!/usr/bin/env bash
# Imagen base de datos: PostgreSQL 16 (repositorio de Ubuntu 24.04).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

cloud-init status --wait || true

apt-get update -y
apt-get upgrade -y
apt-get install -y postgresql postgresql-contrib

systemctl enable postgresql

# Zona horaria usada por la aplicación
timedatectl set-timezone America/La_Paz || true

psql --version

# La base 'asistencia_db', el usuario y la contraseña se crean al lanzar la instancia
# (user-data), para no dejar credenciales dentro de la imagen.

apt-get clean
rm -rf /var/lib/apt/lists/*
