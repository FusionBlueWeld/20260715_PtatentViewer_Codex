$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$controlFile = Join-Path $projectRoot 'runtime\server-control.json'

if (-not (Test-Path -LiteralPath $controlFile)) {
    Write-Host 'PatentViewer is not running.'
    exit 0
}

try {
    $control = Get-Content -Raw -LiteralPath $controlFile | ConvertFrom-Json
    if ($control.host -ne '127.0.0.1' -or $control.port -lt 1) { throw 'Invalid server control file.' }
    $shutdownUrl = "http://127.0.0.1:$($control.port)/api/admin/shutdown"
    $headers = @{ 'X-PatentViewer-Control-Token' = $control.token }
    Invoke-RestMethod -Method Post -Uri $shutdownUrl -Headers $headers -ContentType 'application/json' -Body '{}' -TimeoutSec 3 | Out-Null
} catch {
    try {
        $healthUrl = "http://127.0.0.1:$($control.port)/api/health"
        Invoke-RestMethod -Uri $healthUrl -TimeoutSec 1 | Out-Null
    } catch {
        Remove-Item -LiteralPath $controlFile -Force -ErrorAction SilentlyContinue
        Write-Host 'PatentViewer was already stopped. Removed stale control data.'
        exit 0
    }
    throw
}

for ($i = 0; $i -lt 50; $i++) {
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:$($control.port)/api/health" -TimeoutSec 1 | Out-Null
    } catch {
        Write-Host 'PatentViewer stopped.'
        exit 0
    }
    Start-Sleep -Milliseconds 200
}
throw 'PatentViewer did not stop within 10 seconds.'
