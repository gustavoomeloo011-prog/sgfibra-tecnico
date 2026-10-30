$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

$protectedNames = @(
    "EBF_DATABASE_URL",
    "EBF_REDIS_URL",
    "SESSION_SECRET",
    "NGROK_AUTHTOKEN",
    "EBF_NGROK_DOMAIN"
)

foreach ($name in $protectedNames) {
    $value = [Environment]::GetEnvironmentVariable($name, "User")
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "Variavel protegida ausente: $name"
    }
    Set-Item -Path "Env:$name" -Value $value
}

$env:DATABASE_URL = $env:EBF_DATABASE_URL
$env:REDIS_URL = $env:EBF_REDIS_URL
$env:EBF_SECURE_COOKIE = "1"
$env:PORT = "8000"

$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    & py -3 -m venv (Join-Path $projectRoot ".venv")
}

& $python -m pip install -q -r (Join-Path $projectRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Nao foi possivel instalar as dependencias."
}

$ngrok = Get-Command ngrok -ErrorAction SilentlyContinue
if ($ngrok) {
    $ngrokPath = $ngrok.Source
} else {
    $ngrokPath = Get-ChildItem `
        -Path "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" `
        -Recurse -Filter ngrok.exe -ErrorAction Stop |
        Select-Object -First 1 -ExpandProperty FullName
}

Start-Process `
    -FilePath $python `
    -ArgumentList @(
        "-m", "uvicorn", "app_fastapi.main:app",
        "--host", "127.0.0.1",
        "--port", "8000",
        "--workers", "2",
        "--proxy-headers"
    ) `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden

Start-Sleep -Seconds 3

Start-Process `
    -FilePath $ngrokPath `
    -ArgumentList @(
        "http",
        "--url=$($env:EBF_NGROK_DOMAIN)",
        "8000"
    ) `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden

Write-Host "EBF Tecnico iniciado com sucesso." -ForegroundColor Green
Write-Host "Local:   http://127.0.0.1:8000"
Write-Host "Publico: https://$($env:EBF_NGROK_DOMAIN)"
Write-Host "Gateway: https://ebf-tecnico-gateway.testefoxfibra.workers.dev"
