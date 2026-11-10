[CmdletBinding(PositionalBinding=$false)]
param(
    [ValidateSet('flutter','dart')][string]$Tool='flutter',
    [Parameter(Position=0,ValueFromRemainingArguments=$true)][string[]]$ToolArguments=@()
)
$ErrorActionPreference='Stop'
$taskRoot=if($env:COMPANION_FLUTTER_WORKSPACE) { (Resolve-Path -LiteralPath $env:COMPANION_FLUTTER_WORKSPACE).Path } else { (Resolve-Path (Join-Path $PSScriptRoot '..')).Path }
$toolchain=Join-Path $taskRoot '.local/toolchains/flutter'
$localRoot=Join-Path $taskRoot '.local/flutter-dev'
$overrides=@{
    APPDATA=(Join-Path $localRoot 'appdata')
    LOCALAPPDATA=(Join-Path $localRoot 'localappdata')
    PUB_CACHE=(Join-Path $localRoot 'pub-cache')
    GRADLE_USER_HOME=(Join-Path $localRoot 'gradle')
    ANDROID_USER_HOME=(Join-Path $localRoot 'android-user')
    ANDROID_AVD_HOME=(Join-Path $localRoot 'avd')
    ANDROID_EMULATOR_HOME=(Join-Path $localRoot 'emulator')
    ANDROID_HOME=(Join-Path $taskRoot '.local/android-sdk')
    ANDROID_SDK_ROOT=(Join-Path $taskRoot '.local/android-sdk')
    NUGET_PACKAGES=(Join-Path $localRoot 'nuget')
    DOTNET_CLI_HOME=(Join-Path $localRoot 'dotnet')
    TEMP=(Join-Path $localRoot 'temp')
    TMP=(Join-Path $localRoot 'temp')
    CI='true'
    DASH__SUPPRESS_ANALYTICS='true'
    DART_SUPPRESS_ANALYTICS='true'
    FLUTTER_SUPPRESS_ANALYTICS='true'
    FLUTTER_ROOT=$toolchain
    PATH=((Join-Path $toolchain 'bin')+[IO.Path]::PathSeparator+$env:PATH)
}
foreach($key in @('APPDATA','LOCALAPPDATA','PUB_CACHE','GRADLE_USER_HOME',
    'ANDROID_USER_HOME','ANDROID_AVD_HOME','ANDROID_EMULATOR_HOME','TEMP','NUGET_PACKAGES',
    'DOTNET_CLI_HOME')) { New-Item -ItemType Directory -Force -Path $overrides[$key] | Out-Null }
$previous=@{}
try {
    foreach($key in $overrides.Keys) {
        $previous[$key]=[Environment]::GetEnvironmentVariable($key,'Process')
        [Environment]::SetEnvironmentVariable($key,$overrides[$key],'Process')
    }
    $entry=if($Tool -eq 'dart') { Join-Path $toolchain 'bin/dart.bat' } else {
        Join-Path $toolchain 'bin/flutter.bat'
    }
    if(-not (Test-Path -LiteralPath $entry)) { throw 'Install the pinned Flutter SDK in .local/toolchains/flutter first.' }
    & $entry @ToolArguments
    $result=$LASTEXITCODE
} finally {
    foreach($key in $previous.Keys) {
        [Environment]::SetEnvironmentVariable($key,$previous[$key],'Process')
    }
}
exit $result
