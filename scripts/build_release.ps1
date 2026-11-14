param(
    [string]$OutputPath,
    [switch]$Zip,
    [switch]$WithoutLive2D,
    [string]$Executable
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$PythonCandidates = @(
    (Join-Path $env:USERPROFILE '.conda\envs\gpt_sovits\python.exe'),
    (Join-Path $ProjectRoot 'engines\gpt_sovits\venv\Scripts\python.exe')
)
$Python = $PythonCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $Python) { throw 'Python runtime not found for release packaging.' }
$ReleaseArguments = @('-B', (Join-Path $PSScriptRoot 'build_clean_release.py'))
if ($OutputPath) { $ReleaseArguments += @('--output', $OutputPath) }
if ($Zip) { $ReleaseArguments += '--zip' }
if ($WithoutLive2D) { $ReleaseArguments += '--without-live2d' }
if ($Executable) { $ReleaseArguments += @('--executable', $Executable) }
& $Python @ReleaseArguments
if ($LASTEXITCODE -ne 0) { throw 'Release packaging failed.' }
