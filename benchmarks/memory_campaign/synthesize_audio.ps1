# Synthesize bank WAVs listed in <round>/audio-to-synthesize.jsonl with Windows TTS.
# Usage: powershell -File synthesize_audio.ps1 -Round <round dir>
param([Parameter(Mandatory = $true)][string]$Round)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$config = Get-Content -LiteralPath (Join-Path $Round 'round.json') -Encoding UTF8 | ConvertFrom-Json
$bankName = if ($config.bank) { $config.bank } else { 'bank' }
$bankRoot = Join-Path (Join-Path $Round $bankName) 'suite-v1'
$items = Get-Content -LiteralPath (Join-Path $Round 'audio-to-synthesize.jsonl') -Encoding UTF8 | ForEach-Object { $_ | ConvertFrom-Json }
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
$voices = @($speaker.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.Name -like 'zh-*' })
if ($voices.Count -eq 0) { throw 'No installed Chinese voice' }
$speaker.SelectVoice($voices[0].VoiceInfo.Name)
$speaker.Rate = -1
foreach ($item in $items) {
    $target = [IO.Path]::GetFullPath((Join-Path $bankRoot $item.asset))
    if (-not $target.StartsWith([IO.Path]::GetFullPath($bankRoot), [StringComparison]::OrdinalIgnoreCase)) { throw 'Asset outside bank' }
    if (Test-Path -LiteralPath $target) { throw "Existing audio cannot be overwritten: $($item.source_id)" }
    $speaker.SetOutputToWaveFile($target)
    $speaker.Speak($item.text)
    $speaker.SetOutputToNull()
    Write-Output $item.source_id
}
$speaker.Dispose()
