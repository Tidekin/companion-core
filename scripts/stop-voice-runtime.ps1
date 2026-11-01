#Requires -Version 7.0
param(
    [Parameter(Mandatory = $true)][string]$RuntimeRoot,
    [ValidateSet('tts','node','speech')][string]$Service = 'tts'
)
$ErrorActionPreference = 'Stop'
$workspace = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $PSScriptRoot 'voice-runtime-env.ps1') -RuntimeRoot $RuntimeRoot -Workspace $workspace
$record = Join-Path $voiceRoot ($Service + '-process.json')
$identity = Get-Content -LiteralPath $record -Raw | ConvertFrom-Json
$process = Get-Process -Id $identity.pid -ErrorAction SilentlyContinue
if ($null -ne $process) {
    if ($process.StartTime.ToUniversalTime().Ticks -ne $identity.created_utc_ticks -or
        $process.Path -ne (Join-Path $voiceRoot 'venv/Scripts/python.exe')) {
        throw 'Saved process identity no longer matches; refusing to stop another process'
    }
    # Windows venv Python launches the base interpreter as a child. Keep native
    # process handles so PID reuse cannot make verification target another process.
    $inventory = Get-CimInstance Win32_Process
    $pending = @($process.Id)
    $descendants = @()
    while ($pending.Count) {
        $next = @($inventory | Where-Object { $_.ParentProcessId -in $pending })
        $descendants += @($next | ForEach-Object {
            Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
        })
        $pending = @($next | ForEach-Object { $_.ProcessId })
    }
    $process.Kill($true)
    $process.WaitForExit()
    foreach ($child in $descendants) {
        if (-not $child.WaitForExit(5000)) { throw 'A service child has not confirmed exit' }
    }
}
$identity.state = 'STOPPED'
$identity | ConvertTo-Json | Set-Content -LiteralPath $record -Encoding utf8
Write-Output ($Service + ' process stopped; node quarantine and job records were preserved')
