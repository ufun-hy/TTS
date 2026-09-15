param(
    [ValidateSet("check", "start", "stop", "status", "import-llm")]
    [string]$Command = "start",
    [string]$Data = "",
    [string]$Models = "",
    [string]$Python = "",
    [string]$BinDir = ""
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Arguments = @((Join-Path $Root "scripts\windows-runtime.py"), $Command)
if ($Data) { $Arguments += @("--data", $Data) }
if ($Models) { $Arguments += @("--models", $Models) }
if ($Python) { $Arguments += @("--python", $Python) }
if ($BinDir) { $Arguments += @("--bin-dir", $BinDir) }
$BundledPython = Join-Path $Root "runtime\python\python.exe"
$PythonCommand = if (Test-Path $BundledPython) { $BundledPython } else { (Get-Command python.exe -ErrorAction Stop).Source }
& $PythonCommand @Arguments
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
