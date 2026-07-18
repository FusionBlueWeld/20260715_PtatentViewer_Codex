param(
    [int]$Port = 8765,
    [double]$IdleTimeoutMinutes = 30,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$runtime = Join-Path $projectRoot 'runtime'
$controlFile = Join-Path $runtime 'server-control.json'
$stdout = Join-Path $runtime 'server.stdout.log'
$stderr = Join-Path $runtime 'server.stderr.log'
$url = "http://127.0.0.1:$Port/"

function Open-PatentViewerBrowser([string]$TargetUrl) {
    $edgeCandidates = @(
        (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'),
        (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\Edge\Application\msedge.exe')
    )
    $chromeCandidates = @(
        (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe'),
        (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe')
    )
    $browserPath = $edgeCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
    $browserName = 'Edge'
    if (-not $browserPath) {
        $browserPath = $chromeCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
        $browserName = 'Chrome'
    }
    if ($browserPath) {
        # App mode creates a dedicated PatentViewer window without a tab strip.
        # This also avoids Edge/Chrome restoring a startup "New tab" beside the app.
        Start-Process -FilePath $browserPath -ArgumentList @(
            "--app=$TargetUrl",
            '--window-size=1440,900',
            '--window-position=40,40',
            '--no-first-run'
        )
        Set-Content -LiteralPath (Join-Path $runtime 'browser-launch.log') -Value "$browserName app-mode launch: $TargetUrl" -Encoding UTF8
        return
    }
    Start-Process $TargetUrl
    Set-Content -LiteralPath (Join-Path $runtime 'browser-launch.log') -Value "Default browser fallback: $TargetUrl" -Encoding UTF8
}

try {
    $health = Invoke-RestMethod -Uri ($url + 'api/health') -TimeoutSec 2
    if ($health.ok) {
        Write-Host 'PatentViewer is already running.'
        if (-not $NoBrowser) {
            Open-PatentViewerBrowser $url
            Write-Host "PatentViewer opened: $url"
        }
        exit 0
    }
} catch {}

$candidates = @(
    (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
    (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe')
)
$python = Get-Command python -ErrorAction SilentlyContinue
if ($python) { $candidates += $python.Source }
$candidates = $candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique

function Test-Python([string]$Candidate, [switch]$RequirePackages) {
    $probe = if ($RequirePackages) { 'import sys; import pypdf; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' } else { 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' }
    try {
        & $Candidate -X utf8 -c $probe *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

# Do not blindly use the first Python installation: it may exist without the
# packages needed by PatentViewer. Prefer an already-ready interpreter.
$pythonPath = $candidates | Where-Object { Test-Python $_ -RequirePackages } | Select-Object -First 1
if (-not $pythonPath) {
    $pythonPath = $candidates | Where-Object { Test-Python $_ } | Select-Object -First 1
    if (-not $pythonPath) { throw 'Python 3.10 or newer was not found.' }

    Write-Host 'Installing PatentViewer Python dependencies...'
    & $pythonPath -X utf8 -m pip install --disable-pip-version-check -r (Join-Path $projectRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0 -or -not (Test-Python $pythonPath -RequirePackages)) {
        throw 'PatentViewer dependencies could not be installed. Run: python -m pip install -r requirements.txt'
    }
}

New-Item -ItemType Directory -Path $runtime -Force | Out-Null
if (Test-Path -LiteralPath $controlFile) { Remove-Item -LiteralPath $controlFile -Force }
$idleSeconds = [math]::Max(1, [math]::Round($IdleTimeoutMinutes * 60))
$arguments = @('-X','utf8','run.py','--port',"$Port",'--root',$projectRoot,'--idle-timeout',"$idleSeconds",'--control-file',$controlFile,'--quiet')
$process = Start-Process -FilePath $pythonPath -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru

$started = $false
for ($i = 0; $i -lt 60; $i++) {
    if ($process.HasExited) { break }
    try {
        $health = Invoke-RestMethod -Uri ($url + 'api/health') -TimeoutSec 1
        if ($health.ok) { $started = $true; break }
    } catch {}
    Start-Sleep -Milliseconds 200
}
if (-not $started) {
    if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force }
    $detail = if (Test-Path -LiteralPath $stderr) { Get-Content -Raw -LiteralPath $stderr } else { '' }
    throw ('PatentViewer startup failed. ' + $detail)
}

Write-Host "PatentViewer started: $url"
Write-Host "Automatic shutdown: $IdleTimeoutMinutes minute(s) of inactivity"
if (-not $NoBrowser) {
    Open-PatentViewerBrowser $url
}
