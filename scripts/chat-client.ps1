param(
    [Parameter(Mandatory=$true)][string]$Server,
    [string]$DataDirectory = '.local/native-chat-client'
)
$ErrorActionPreference = 'Stop'
$workspace = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $workspace
& .venv/Scripts/pythonw.exe -m companion_chat_client --server $Server --data-dir $DataDirectory
if ($LASTEXITCODE -ne 0) { throw 'Native chat client exited with an error' }
