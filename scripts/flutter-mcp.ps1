$ErrorActionPreference='Stop'
& (Join-Path $PSScriptRoot 'flutter.ps1') -Tool dart -ToolArguments @('mcp-server','--enable','all')
exit $LASTEXITCODE
