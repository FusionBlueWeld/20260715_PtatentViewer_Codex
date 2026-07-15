$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$candidates = @(
    (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe')
)
$pythonPath = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $pythonPath) {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) { $pythonPath = $python.Source }
}
if (-not $pythonPath) { throw 'Python 3.10 or newer was not found.' }
Set-Location -LiteralPath $projectRoot
& $pythonPath -X utf8 tools\debug_check.py
exit $LASTEXITCODE
