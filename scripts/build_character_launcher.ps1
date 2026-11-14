param([string]$PythonPath, [string]$BuildRoot)
$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if (-not $PythonPath) { $PythonPath = Join-Path $env:USERPROFILE '.conda\envs\gpt_sovits\python.exe' }
if (-not $BuildRoot) { $BuildRoot = Join-Path $ProjectRoot ('temp\character_launcher_' + (Get-Date -Format 'yyyyMMdd_HHmmss')) }
$BuildRoot = [IO.Path]::GetFullPath($BuildRoot)
if (-not $BuildRoot.StartsWith(($ProjectRoot.TrimEnd('\') + '\temp\'), [StringComparison]::OrdinalIgnoreCase)) { throw 'Launcher build must be under project temp.' }
if (Test-Path -LiteralPath $BuildRoot) { throw 'Build directory already exists.' }
& $PythonPath -m PyInstaller --noconfirm --clean --onefile --windowed --name character_launcher --paths $ProjectRoot --runtime-hook (Join-Path $PSScriptRoot 'pyi_rth_tk_runtime.py') --distpath (Join-Path $BuildRoot 'dist') --workpath (Join-Path $BuildRoot 'build') --specpath $BuildRoot (Join-Path $PSScriptRoot 'character_launcher.py')
if ($LASTEXITCODE -ne 0) { throw 'Launcher build failed.' }
Write-Output (Join-Path $BuildRoot 'dist\character_launcher.exe')
