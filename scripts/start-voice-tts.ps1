#Requires -Version 7.0
param(
    [Parameter(Mandatory = $true)][string]$RuntimeRoot,
    [ValidateRange(1024, 65535)][int]$Port = 19880,
    [ValidateRange(10, 600)][int]$StartupSeconds = 180
)
$ErrorActionPreference = 'Stop'
$workspace = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $PSScriptRoot 'voice-runtime-env.ps1') -RuntimeRoot $RuntimeRoot -Workspace $workspace
$pins = Get-Content -LiteralPath (Join-Path $voiceRoot 'upstream-pins.json') -Raw | ConvertFrom-Json
if ($pins.gpt_release_commit -notmatch '^[a-f0-9]{40}$') { throw 'Invalid GPT-SoVITS source pin' }
$upstream = Join-Path $voiceRoot ('upstream/GPT-SoVITS-' + $pins.gpt_release_commit)
# Upstream uses absolute imports such as `text`; put its modules before base-Python
# directories so unrelated files in a system Python installation cannot shadow them.
$env:PYTHONPATH = (Join-Path $upstream 'GPT_SoVITS') + ';' + $upstream + ';' + $workspace
$python = Join-Path $voiceRoot 'venv/Scripts/python.exe'
$config = Join-Path $voiceRoot 'tts-infer.yaml'
$record = Join-Path $voiceRoot 'tts-process.json'
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw 'Requested TTS port is already in use'
}
$arguments = @('-B', '-m', 'companion_voice_node.gpt_server',
    '--script', ('"' + (Join-Path $upstream 'api_v2.py') + '"'),
    '--port', $Port, '--config', ('"' + $config + '"'))
$process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $upstream `
    -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $voiceRoot 'logs/tts-stdout.log') `
    -RedirectStandardError (Join-Path $voiceRoot 'logs/tts-stderr.log')
$identity = [ordered]@{ pid=$process.Id; created_utc_ticks=$process.StartTime.ToUniversalTime().Ticks;
    executable=$python; source_commit=$pins.gpt_release_commit; port=$Port; state='STARTING' }
$identity | ConvertTo-Json | Set-Content -LiteralPath $record -Encoding utf8
$deadline = [DateTime]::UtcNow.AddSeconds($StartupSeconds)
while ([DateTime]::UtcNow -lt $deadline) {
    $process.Refresh()
    if ($process.HasExited) {
        $identity.state = 'FAILED'
        $identity | ConvertTo-Json | Set-Content -LiteralPath $record -Encoding utf8
        throw 'TTS startup failed; inspect the local logs directory'
    }
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/openapi.json" -TimeoutSec 2
        if ($response.StatusCode -eq 200) {
            $identity.state = 'READY'
            $identity | ConvertTo-Json | Set-Content -LiteralPath $record -Encoding utf8
            Write-Output ('TTS ready on loopback port ' + $Port + '; process ' + $process.Id)
            return
        }
    } catch { }
    Start-Sleep -Milliseconds 500
}
$process.Refresh()
if (-not $process.HasExited) { $process.Kill($true); $process.WaitForExit() }
$identity.state = 'STARTUP_TIMEOUT'
$identity | ConvertTo-Json | Set-Content -LiteralPath $record -Encoding utf8
throw 'TTS startup deadline exceeded; owned startup process stopped'
