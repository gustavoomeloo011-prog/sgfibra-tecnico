param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("0", "1")]
    [string]$Disabled
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$configPath = Join-Path $projectRoot "wrangler.jsonc"
$config = Get-Content -LiteralPath $configPath -Raw
$updated = [regex]::Replace(
    $config,
    '"SYSTEM_DISABLED"\s*:\s*"[01]"',
    "`"SYSTEM_DISABLED`": `"$Disabled`""
)

if ($updated -eq $config -and $config -notmatch '"SYSTEM_DISABLED"\s*:\s*"' + $Disabled + '"') {
    throw "Nao foi possivel localizar SYSTEM_DISABLED no wrangler.jsonc."
}

Set-Content -LiteralPath $configPath -Value $updated -Encoding utf8
Set-Location -LiteralPath $projectRoot

Write-Host ""
if ($Disabled -eq "1") {
    Write-Host "Bloqueando o acesso publico..." -ForegroundColor Yellow
} else {
    Write-Host "Liberando o acesso publico..." -ForegroundColor Green
}

& npx.cmd wrangler deploy
if ($LASTEXITCODE -ne 0) {
    throw "O Cloudflare nao confirmou a alteracao."
}

Write-Host ""
if ($Disabled -eq "1") {
    Write-Host "SISTEMA PUBLICO DESLIGADO." -ForegroundColor Yellow
} else {
    Write-Host "SISTEMA PUBLICO LIGADO." -ForegroundColor Green
}
