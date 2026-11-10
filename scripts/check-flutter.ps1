[CmdletBinding()]
param([switch]$WindowsBuild,[switch]$AndroidBuild,[switch]$Wire)
$ErrorActionPreference='Stop'
$taskRoot=if($env:COMPANION_FLUTTER_WORKSPACE) { (Resolve-Path -LiteralPath $env:COMPANION_FLUTTER_WORKSPACE).Path } else { (Resolve-Path (Join-Path $PSScriptRoot '..')).Path }
$project=Join-Path $taskRoot 'clients/chat'
Push-Location -LiteralPath $project
try {
    & "$PSScriptRoot/flutter.ps1" -Tool flutter -ToolArguments @('pub','get','--enforce-lockfile')
    if($LASTEXITCODE) { throw 'Flutter dependencies failed' }
    & "$PSScriptRoot/prepare-flutter-links.ps1"
    & "$PSScriptRoot/flutter.ps1" -Tool dart -ToolArguments @('format','--output=none','--set-exit-if-changed','lib','test')
    if($LASTEXITCODE) { throw 'Dart formatting failed' }
    & "$PSScriptRoot/flutter.ps1" -Tool flutter -ToolArguments @('analyze','--no-pub')
    if($LASTEXITCODE) { throw 'Flutter analysis failed' }
    & "$PSScriptRoot/flutter.ps1" -Tool flutter -ToolArguments @('test','--no-pub')
    if($LASTEXITCODE) { throw 'Flutter tests failed' }
    if($WindowsBuild) {
        & "$PSScriptRoot/flutter.ps1" -Tool flutter -ToolArguments @('build','windows','--release','--no-pub')
        if($LASTEXITCODE) { throw 'Windows build failed' }
    }
    if($AndroidBuild) {
        & "$PSScriptRoot/flutter.ps1" -Tool flutter -ToolArguments @('build','apk','--debug','--no-pub')
        if($LASTEXITCODE) { throw 'Android build failed' }
    }
} finally { Pop-Location }
if($Wire) {
    $previous=$env:COMPANION_FLUTTER_WIRE
    try {
        $env:COMPANION_FLUTTER_WIRE='1'
        & "$PSScriptRoot/check.ps1" -TestFiles @('tests/test_native_chat_wire.py') -Workers 0 -SkipModelPreparation
        if($LASTEXITCODE) { throw 'Flutter/Core wire test failed' }
    } finally { $env:COMPANION_FLUTTER_WIRE=$previous }
}
