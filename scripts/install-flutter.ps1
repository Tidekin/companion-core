[CmdletBinding()]
param()
$ErrorActionPreference='Stop'
$taskRoot=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$destination=Join-Path $taskRoot '.local/toolchains'
New-Item -ItemType Directory -Force -Path $destination | Out-Null
$archive=Join-Path $destination 'flutter-sdk.zip'
$expected='d8e2b5977e09082c6440f89dc1aa887b94c8fbcc3b39222797046295a3f68a9e'
if(-not (Test-Path -LiteralPath $archive)) {
    Invoke-WebRequest -Uri 'https://storage.googleapis.com/flutter_infra_release/releases/stable/windows/flutter_windows_3.47.9-stable.zip' -OutFile $archive
}
if((Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToLowerInvariant() -ne $expected) {
    throw 'Pinned Flutter SDK checksum mismatch'
}
if(-not (Test-Path (Join-Path $destination 'flutter/bin/flutter.bat'))) {
    Expand-Archive -LiteralPath $archive -DestinationPath $destination
}
& (Join-Path $PSScriptRoot 'flutter.ps1') -Tool flutter -ToolArguments @('--version')
exit $LASTEXITCODE
