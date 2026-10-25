param(
    [ValidateSet('main', 'review')][string]$Mode = 'main',
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
# A legacy/public launcher does not participate in local locking. Refuse rather
# than guess its database, inspect credentials, or stop it.
if ($Mode -eq 'main') {
    $processes = Get-CimInstance Win32_Process
    if ($processes | Where-Object { $_.Name -match '^python(w)?(.exe)?$' -and -not $_.CommandLine }) {
        throw 'Cannot inspect a Python process safely. Main startup refused.'
    }
    $running = $processes | Where-Object {
        $_.Name -match '^python(w)?(.exe)?$' -and
        $_.CommandLine -match 'knowvia\.web|knowvia\.production|funnel_start\.py|local_start\.py'
    }
    if ($running) { throw 'Another Knowvia process exists. Main startup refused; use the existing HTTPS site or explicitly stop that instance yourself.' }
    $tailscale = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
    if (Test-Path $tailscale) {
        $status = & $tailscale funnel status --json 2>$null
        if ($LASTEXITCODE -ne 0) { throw 'Cannot verify Funnel state. Main startup refused.' }
        $config = ($status -join "`n") | ConvertFrom-Json
        if ($null -ne $config -and @($config.PSObject.Properties).Count -gt 0) {
            throw 'Tailscale serving configuration exists. Local HTTP startup refused to avoid accidental public exposure; do not stop Funnel without separate approval.'
        }
    }
}
$arguments = @('-B', (Join-Path $PSScriptRoot 'local_start.py'), '--mode', $Mode)
if ($CheckOnly) { $arguments += '--check-only' }
& (Join-Path $root '.venv\Scripts\python.exe') @arguments
exit $LASTEXITCODE
