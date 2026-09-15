param(
    [ValidateSet("start", "stop", "status", "import-llm")]
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
& python @Arguments
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
