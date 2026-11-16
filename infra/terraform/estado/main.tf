# Bucket S3 donde se guarda el estado de Terraform de cada tenant y del monitoreo.
# Se ejecuta UNA vez (botón "Preparar almacenamiento del estado" de la plataforma).
# Su propio estado es pequeño, no tiene secretos y queda local en plataforma/datos/estado/.
terraform {
  required_version = ">= 1.10.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Plataforma = "asistencia-saas"
      Rol        = "estado-terraform"
      Gestion    = "terraform"
    }
  }
}

resource "random_id" "sufijo" {
  byte_length = 4
}

resource "aws_s3_bucket" "estado" {
  bucket = "asist-saas-tfstate-${random_id.sufijo.hex}"
  # El estado es la única fuente de los secretos de cada tenant: no se puede destruir por accidente
  lifecycle { prevent_destroy = true }
}

# Historial de versiones: se puede recuperar un estado anterior si algo lo corrompe
resource "aws_s3_bucket_versioning" "estado" {
  bucket = aws_s3_bucket.estado.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Cifrado en reposo con KMS
resource "aws_s3_bucket_server_side_encryption_configuration" "estado" {
  bucket = aws_s3_bucket.estado.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "estado" {
  bucket                  = aws_s3_bucket.estado.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "estado" {
  bucket = aws_s3_bucket.estado.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Solo conexiones cifradas (TLS)
resource "aws_s3_bucket_policy" "solo_tls" {
  bucket     = aws_s3_bucket.estado.id
  depends_on = [aws_s3_bucket_public_access_block.estado]
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "SoloTLS"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.estado.arn, "${aws_s3_bucket.estado.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

# Las versiones antiguas del estado se borran a los 90 días
resource "aws_s3_bucket_lifecycle_configuration" "estado" {
  bucket     = aws_s3_bucket.estado.id
  depends_on = [aws_s3_bucket_versioning.estado]
  rule {
    id     = "versiones-antiguas"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }
}

output "bucket" {
  value = aws_s3_bucket.estado.bucket
}

output "region" {
  value = var.region
}
