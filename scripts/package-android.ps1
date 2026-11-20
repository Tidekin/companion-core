[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$PreviousApk,
    [Parameter(Mandatory)][string]$OutputApk,
    [ValidateRange(1,2100000000)][int]$BuildNumber
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$project = Join-Path $root 'clients/chat'
$sdkRoot = if ($env:COMPANION_FLUTTER_WORKSPACE) { $env:COMPANION_FLUTTER_WORKSPACE } else { $root }
$tools = Join-Path $sdkRoot '.local/android-sdk/build-tools/36.0.0'
$applicationId = 'dev.tidekin.companion_chat'
$signer = 'cbe6c16f3ce36b7b66e5c8e330f8fed04ee5862a9544bd996d90c60d2a9700e1'

function Get-ReleaseCode([string]$Apk) {
    $certs = & "$tools/apksigner.bat" verify --print-certs $Apk
    if ($LASTEXITCODE) { throw "APK signature verification failed for $Apk" }
    $digests = @($certs | Select-String '^Signer #\d+ certificate SHA-256 digest: ([0-9a-fA-F]{64})$')
    if ($digests.Count -ne 1 -or $digests[0].Matches[0].Groups[1].Value -ne $signer) {
        throw "APK has a different distribution certificate: $Apk"
    }
    $manifest = & "$tools/aapt.exe" dump badging $Apk
    if ($LASTEXITCODE -or $manifest[0] -notmatch "^package: name='([^']+)' versionCode='([0-9]+)'") {
        throw "Could not read APK identity: $Apk"
    }
    if ($Matches[1] -ne $applicationId) { throw "APK has a different application ID: $Apk" }
    return [int]$Matches[2]
}

$previous = (Resolve-Path -LiteralPath $PreviousApk).Path
$output = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputApk)
$built = Join-Path $project 'build/app/outputs/flutter-apk/app-release.apk'
if ((Test-Path -LiteralPath $output) -or [IO.Path]::GetExtension($output) -ne '.apk') {
    throw 'Choose a new output filename ending in .apk.'
}
if ($previous -eq $built -or $output -eq $built) {
    throw 'Keep the previous and published APKs outside the Flutter build output.'
}
$previousCode = Get-ReleaseCode $previous
if (-not $PSBoundParameters.ContainsKey('BuildNumber')) {
    $BuildNumber = $previousCode + 1
}
if ($BuildNumber -le $previousCode) {
    throw "Build number $BuildNumber must exceed the previous APK's versionCode $previousCode."
}
Push-Location -LiteralPath $project
try {
    & "$PSScriptRoot/flutter.ps1" -Tool flutter -ToolArguments @('build','apk','--release',"--build-number=$BuildNumber")
    if ($LASTEXITCODE) { throw 'Flutter Android release build failed.' }
} finally { Pop-Location }

$directory = [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($output))
$temp = Join-Path $directory.FullName ([guid]::NewGuid().ToString() + '.apk')
try {
    [IO.File]::Copy($built, $temp)
    if ((Get-ReleaseCode $temp) -ne $BuildNumber) { throw 'Built APK does not carry the requested versionCode.' }
    [IO.File]::Move($temp, $output)
} finally {
    if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp }
}
Get-FileHash -LiteralPath $output -Algorithm SHA256
