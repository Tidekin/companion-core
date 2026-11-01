#Requires -Version 7.0
param(
    [Parameter(Mandatory = $true)][string]$RuntimeRoot,
    [Parameter(Mandatory = $true)][string]$Config,
    [ValidateRange(1024, 65535)][int]$Port = 19880,
    [ValidateRange(10, 600)][int]$StartupSeconds = 240
)
$ErrorActionPreference = 'Stop'
$workspace = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $PSScriptRoot 'voice-runtime-env.ps1') -RuntimeRoot $RuntimeRoot -Workspace $workspace
$configuration = (Resolve-Path -LiteralPath $Config).Path
if (-not $configuration.StartsWith($voiceRoot + [IO.Path]::DirectorySeparatorChar,
        [StringComparison]::OrdinalIgnoreCase)) { throw 'Speech config must stay in RuntimeRoot' }
$settings = Get-Content -LiteralPath $configuration -Raw | ConvertFrom-Json
$applioSource = (Resolve-Path -LiteralPath $settings.applio_source).Path
$applioRuntime = Split-Path -Parent $applioSource
$overlay = (Resolve-Path -LiteralPath (Join-Path $applioRuntime 'overlay')).Path
$python = Join-Path $voiceRoot 'venv/Scripts/python.exe'
$env:PYTHONPATH = $workspace + ';' + (Join-Path $voiceRoot 'venv/Lib/site-packages') + ';' + $overlay
$env:APPLIO_ROOT = $applioSource
$env:APPLIO_CODE_ROOT = $applioSource
$env:APPLIO_CONFIG_DIR = Join-Path $applioRuntime 'user-config'
$env:APPLIO_LOGS_DIR = Join-Path $applioRuntime 'logs'
$env:OMP_NUM_THREADS = '4'
$env:MKL_NUM_THREADS = '4'
$expectedManifest = & $python -B -m companion_voice_node.speech_service --config $configuration --print-manifest
if ($LASTEXITCODE -ne 0) { throw 'Local speech resources are invalid' }
$record = Join-Path $voiceRoot 'speech-process.json'
if (Test-Path -LiteralPath $record) {
    $previous = Get-Content -LiteralPath $record -Raw | ConvertFrom-Json
    $existing = Get-Process -Id $previous.pid -ErrorAction SilentlyContinue
    if ($null -ne $existing -and
        $existing.StartTime.ToUniversalTime().Ticks -eq $previous.created_utc_ticks) {
        throw 'This runtime already has a live speech process; use the identity-checked stop script first'
    }
}
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw 'Requested local speech port is already in use'
}
$arguments = @('-B', '-m', 'companion_voice_node.speech_service',
    '--config', ('"' + $configuration + '"'), '--port', $Port)
$process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $workspace `
    -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $voiceRoot 'logs/speech-stdout.log') `
    -RedirectStandardError (Join-Path $voiceRoot 'logs/speech-stderr.log')
$identity = [ordered]@{ pid=$process.Id; created_utc_ticks=$process.StartTime.ToUniversalTime().Ticks;
    executable=$python; workspace=$workspace; port=$Port; state='STARTING' }
$identity | ConvertTo-Json | Set-Content -LiteralPath $record -Encoding utf8
$deadline = [DateTime]::UtcNow.AddSeconds($StartupSeconds)
while ([DateTime]::UtcNow -lt $deadline) {
    $process.Refresh()
    if ($process.HasExited) {
        $identity.state = 'FAILED'
        $identity | ConvertTo-Json | Set-Content -LiteralPath $record -Encoding utf8
        throw 'Local speech startup failed; inspect local speech logs'
    }
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/healthz" -TimeoutSec 2
        if ($health.status -eq 'ready' -and $health.model -eq $settings.model -and
            $health.manifest_sha256 -eq $expectedManifest.Trim()) {
            $identity.state = 'READY'
            $identity['service_manifest_sha256'] = $health.manifest_sha256
            $identity | ConvertTo-Json | Set-Content -LiteralPath $record -Encoding utf8
            Write-Output ('Local speech ready on loopback port ' + $Port + '; process ' + $process.Id)
            return
        }
    } catch { }
    Start-Sleep -Milliseconds 500
}
$process.Refresh()
if (-not $process.HasExited) { $process.Kill($true); $process.WaitForExit() }
$identity.state = 'STARTUP_TIMEOUT'
$identity | ConvertTo-Json | Set-Content -LiteralPath $record -Encoding utf8
throw 'Local speech startup deadline exceeded; owned process stopped'
