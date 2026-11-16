# Inicia la plataforma: worker en segundo plano + interfaz Streamlit.
# Requisitos: Python 3.12+, Terraform >= 1.10, Docker Desktop abierto, AMIs de Packer creadas
# y credenciales de AWS en el entorno (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY).
Set-Location $PSScriptRoot
if (-not (Test-Path ".venv")) {
    python -m venv .venv
    .\.venv\Scripts\pip install -r requirements.txt
}
if (-not $env:AWS_ACCESS_KEY_ID) { Write-Warning "AWS_ACCESS_KEY_ID no está definido: las operaciones fallarán." }

$worker = Start-Process -FilePath ".\.venv\Scripts\python.exe" -ArgumentList "worker.py" -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput "datos\logs\worker.out" -RedirectStandardError "datos\logs\worker.err"
Write-Host "Worker iniciado (PID $($worker.Id)). Registro: datos\logs\worker.log"
try {
    .\.venv\Scripts\streamlit.exe run app.py
} finally {
    Stop-Process -Id $worker.Id -ErrorAction SilentlyContinue
    Write-Host "Worker detenido."
}
