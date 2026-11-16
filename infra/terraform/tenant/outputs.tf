# URL del tenant con su identificador como subdominio: http://<tenant_id>.<IP>[.<dominio_base>]
output "frontend_url" {
  value = "http://${var.tenant_id}.${aws_eip.frontend.public_ip}${var.dominio_base != "" ? ".${var.dominio_base}" : ""}"
}

output "frontend_ip_url" {
  value = "http://${aws_eip.frontend.public_ip}"
}

output "backend_url" {
  value = "http://${aws_eip.backend.public_ip}:8000/api"
}

output "prometheus_url" {
  value = "http://${aws_eip.backend.public_ip}:9090"
}

# Datos para el inventario de Ansible (los genera la plataforma en un directorio temporal)
output "hosts" {
  value = {
    frontend = { publica = aws_eip.frontend.public_ip, privada = aws_instance.frontend.private_ip }
    backend  = { publica = aws_eip.backend.public_ip, privada = aws_instance.backend.private_ip }
    bd       = { publica = aws_instance.bd.public_ip, privada = aws_instance.bd.private_ip }
  }
}

output "instancias" {
  value = {
    frontend = aws_instance.frontend.id
    backend  = aws_instance.backend.id
    bd       = aws_instance.bd.id
  }
}

output "bd_volumen_id" {
  value = aws_instance.bd.root_block_device[0].volume_id
}

output "amis" {
  value = {
    frontend = aws_instance.frontend.ami
    backend  = aws_instance.backend.ami
    bd       = aws_instance.bd.ami
  }
}

output "monitoreo_conectado" {
  value = length(data.aws_security_groups.monitoreo.ids) > 0
}

# ---------- Secretos: nunca se guardan en la base de la plataforma ----------
output "secretos" {
  sensitive = true
  value = {
    db_name        = var.db_name
    db_user        = var.db_user
    db_password    = random_password.db.result
    django_secret  = random_password.django_secret.result
    admin_password = random_password.admin_app.result
    ssh_privada    = tls_private_key.ansible.private_key_openssh
  }
}
