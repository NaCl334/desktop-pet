param(
    [string]$BuildRoot,
    [string]$OutputPath,
    [string]$PythonPath,
    [switch]$UpdateFormalExe,
    [switch]$WithoutLive2D
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ExeName = -join @([char]0x6211, [char]0x7684, [char]0x684C, [char]0x5BA0)
$AcceptanceLabel = -join @([char]0x9A8C, [char]0x6536)
$BuildName = "desktop_pet"
$PythonCandidates = @(
    $PythonPath,
    (Join-Path $env:USERPROFILE ".conda\envs\gpt_sovits\python.exe"),
    (Join-Path $ProjectRoot "engines\gpt_sovits\venv\Scripts\python.exe")
)

function Test-TkPython([string]$PythonPath) {
    if (-not $PythonPath) {
        return $false
    }
    if (-not (Test-Path -LiteralPath $PythonPath)) {
        return $false
    }
    $environmentRoot = Split-Path -Parent $PythonPath
    $hasTkLayout = (
        (Test-Path -LiteralPath (Join-Path $environmentRoot "Library\lib\tcl8.6")) -and
        (Test-Path -LiteralPath (Join-Path $environmentRoot "Library\lib\tk8.6")) -and
        (Test-Path -LiteralPath (Join-Path $environmentRoot "Library\bin\tcl86t.dll")) -and
        (Test-Path -LiteralPath (Join-Path $environmentRoot "Library\bin\tk86t.dll")) -and
        (Test-Path -LiteralPath (Join-Path $environmentRoot "DLLs\_tkinter.pyd"))
    )
    if (-not $hasTkLayout) {
        return $false
    }
    & $PythonPath -c "import tkinter" 2>$null
    return $LASTEXITCODE -eq 0
}

$Python = $PythonCandidates |
    Where-Object { Test-TkPython $_ } |
    Select-Object -First 1
if (-not $Python) {
    throw "No Python with a complete Tcl/Tk runtime was found."
}

$EnvironmentRoot = Split-Path -Parent $Python
$TkRoot = Join-Path $EnvironmentRoot "Library"
$RuntimeHook = Join-Path $PSScriptRoot "pyi_rth_tk_runtime.py"
$Live2DRuntime = Join-Path $ProjectRoot "runtime\live2d"
$Icon = Join-Path $ProjectRoot "assets\branding\desktop_pet.ico"
$Main = Join-Path $ProjectRoot "main.py"
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$TempRoot = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot "temp"))
if (-not $BuildRoot) {
    $BuildRoot = Join-Path $TempRoot "desktop_pet_build_portrait_alpha_$Timestamp"
}
$BuildRoot = [System.IO.Path]::GetFullPath($BuildRoot)
$DistRoot = Join-Path $BuildRoot "dist"
$WorkRoot = Join-Path $BuildRoot "build"
$SpecRoot = $BuildRoot
$BundledLive2DRuntime = Join-Path $BuildRoot "bundle_data\runtime\live2d"
$OutputExe = Join-Path $DistRoot "$BuildName.exe"
$TargetExe = Join-Path $ProjectRoot "$ExeName.exe"
if (-not $OutputPath) {
    $OutputPath = Join-Path $ProjectRoot "$($ExeName)_$($AcceptanceLabel)_$Timestamp.exe"
}
$OutputPath = [System.IO.Path]::GetFullPath($OutputPath)

if (-not $BuildRoot.StartsWith(($TempRoot.TrimEnd('\') + '\'), [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "BuildRoot must be isolated under the project temp directory: $BuildRoot"
}
if (Test-Path -LiteralPath $BuildRoot) {
    throw "BuildRoot already exists; choose a new isolated build directory: $BuildRoot"
}
if (-not [string]::Equals(
    (Split-Path -Parent $OutputPath).TrimEnd('\'),
    $ProjectRoot.TrimEnd('\'),
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw "OutputPath must be a distinct EXE in the project root so runtime assets resolve: $OutputPath"
}
if ([string]::Equals($OutputPath, $TargetExe, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "OutputPath must not overwrite the formal desktop pet EXE: $OutputPath"
}
if (Test-Path -LiteralPath $OutputPath) {
    throw "OutputPath already exists; choose a new acceptance EXE path: $OutputPath"
}
if (-not (Test-Path -LiteralPath $Icon)) {
    throw "Missing application icon: $Icon"
}
if (-not (Test-Path -LiteralPath $RuntimeHook)) {
    throw "Missing Tk runtime hook: $RuntimeHook"
}
if (-not $WithoutLive2D -and -not (Test-Path -LiteralPath (Join-Path $Live2DRuntime "live2d\_live2d.pyd"))) {
    throw "Missing Live2D runtime: $Live2DRuntime"
}

New-Item -ItemType Directory -Path $BuildRoot -Force | Out-Null
if (-not $WithoutLive2D) {
New-Item -ItemType Directory -Path $BundledLive2DRuntime -Force | Out-Null
& robocopy.exe $Live2DRuntime $BundledLive2DRuntime /E /XD __pycache__ /XF *.pyc /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) {
    throw "Failed to stage the Live2D runtime (robocopy exit code $LASTEXITCODE)."
}
}

$PyInstallerArguments = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    "--windowed",
    "--name", $BuildName,
    "--icon", $Icon,
    "--runtime-hook", $RuntimeHook,
    "--hidden-import", "tkinter",
    "--hidden-import", "tkinter.ttk",
    "--hidden-import", "tkinter.filedialog",
    "--hidden-import", "tkinter.messagebox",
    "--exclude-module", "torch",
    "--exclude-module", "torchaudio",
    "--exclude-module", "torchvision",
    "--exclude-module", "transformers",
    "--exclude-module", "onnxruntime",
    "--exclude-module", "scipy",
    "--exclude-module", "numba",
    "--exclude-module", "llvmlite",
    "--exclude-module", "av",
    "--exclude-module", "pandas",
    "--exclude-module", "matplotlib",
    "--collect-data", "pyopenjtalk",
    "--add-data", "$(Join-Path $ProjectRoot 'assets\setup');assets/setup",
    "--add-binary", "$(Join-Path $EnvironmentRoot 'DLLs\_tkinter.pyd');.",
    "--add-binary", "$(Join-Path $TkRoot 'bin\tcl86t.dll');.",
    "--add-binary", "$(Join-Path $TkRoot 'bin\tk86t.dll');.",
    "--add-data", "$(Join-Path $TkRoot 'lib\tcl8.6');tcl/tcl8.6",
    "--add-data", "$(Join-Path $TkRoot 'lib\tk8.6');tk/tk8.6",
    "--distpath", $DistRoot,
    "--workpath", $WorkRoot,
    "--specpath", $SpecRoot
)
if ($WithoutLive2D) {
    $PyInstallerArguments += @('--exclude-module', 'live2d', '--exclude-module', 'glfw', '--exclude-module', 'OpenGL')
} else {
    $PyInstallerArguments += @('--paths', $Live2DRuntime, '--collect-all', 'live2d', '--collect-all', 'glfw', '--collect-all', 'OpenGL', '--add-data', "$BundledLive2DRuntime;runtime/live2d")
}
$PyInstallerArguments += $Main

$env:TCL_LIBRARY = Join-Path $TkRoot "lib\tcl8.6"
$env:TK_LIBRARY = Join-Path $TkRoot "lib\tk8.6"
$env:PATH = (Join-Path $TkRoot "bin") + ";" + $env:PATH

Write-Output "Using Python: $Python"
& $Python @PyInstallerArguments
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed with exit code $LASTEXITCODE."
}
if (-not (Test-Path -LiteralPath $OutputExe)) {
    throw "Build completed but the EXE was not found: $OutputExe"
}

New-Item -ItemType Directory -Path (Split-Path -Parent $OutputPath) -Force | Out-Null
Copy-Item -LiteralPath $OutputExe -Destination $OutputPath
Write-Output "Independent acceptance EXE created: $OutputPath"
Get-Item -LiteralPath $OutputPath | Select-Object FullName,Length,LastWriteTime
if ($UpdateFormalExe) {
    Copy-Item -LiteralPath $OutputPath -Destination $TargetExe -Force
    Write-Output "Formal desktop pet EXE updated: $TargetExe"
    Get-Item -LiteralPath $TargetExe | Select-Object FullName,Length,LastWriteTime
}
