param([string]$Project='clients/chat')
$ErrorActionPreference='Stop'
$taskRoot=if($env:COMPANION_FLUTTER_WORKSPACE) { (Resolve-Path -LiteralPath $env:COMPANION_FLUTTER_WORKSPACE).Path } else { (Resolve-Path (Join-Path $PSScriptRoot '..')).Path }
$projectRoot=[IO.Path]::GetFullPath((Join-Path $taskRoot $Project))
if(-not $projectRoot.StartsWith($taskRoot.TrimEnd([IO.Path]::DirectorySeparatorChar)+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) { throw 'Project must stay inside workspace' }
$metadata=Get-Content -LiteralPath (Join-Path $projectRoot '.flutter-plugins-dependencies') -Raw | ConvertFrom-Json
$directory=Join-Path $projectRoot 'windows/flutter/ephemeral/.plugin_symlinks'
New-Item -ItemType Directory -Force -Path $directory | Out-Null
foreach($plugin in $metadata.plugins.windows) {
    if($plugin.name -notmatch '^[a-z][a-z0-9_]*$') { throw 'Invalid plugin name' }
    $target=[IO.Path]::GetFullPath($plugin.path).TrimEnd([IO.Path]::DirectorySeparatorChar)
    if(-not $target.StartsWith($taskRoot.TrimEnd([IO.Path]::DirectorySeparatorChar)+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) { throw 'Plugin target must stay inside workspace' }
    $link=Join-Path $directory $plugin.name
    if(Test-Path -LiteralPath $link) { continue }
    New-Item -ItemType Junction -Path $link -Target $target | Out-Null
}
