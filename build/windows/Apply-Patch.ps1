param(
    [string]$InstallDir = (Join-Path $env:ProgramFiles "AI Live Studio"),
    [string]$DataDir = $(if ($env:AI_LIVE_STUDIO_DATA) { $env:AI_LIVE_STUDIO_DATA } else { Join-Path $env:LOCALAPPDATA "AI-Live-Studio" }),
    [string]$Backup = "",
    [string]$ExpectedUserSid = ""
)
$ErrorActionPreference = "Stop"
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if ($ExpectedUserSid -and $ExpectedUserSid -ne $identity.User.Value) {
    throw "Use the same Windows account as AI Live Studio (DPAPI/user data cannot be moved to another account)."
}
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $PSCommandPath),
        '-InstallDir', ('"{0}"' -f $InstallDir), '-DataDir', ('"{0}"' -f $DataDir),
        '-ExpectedUserSid', $identity.User.Value)
    if ($Backup) { $arguments += @('-Backup', ('"{0}"' -f $Backup)) }
    $process = Start-Process powershell.exe -Verb RunAs -ArgumentList $arguments -Wait -PassThru
    exit $process.ExitCode
}
try {
    $python = Join-Path $InstallDir "runtime\python\python.exe"
    if (-not (Test-Path -LiteralPath $python)) { throw "Bundled Python missing. Install the full AI Live Studio package first." }
    $tool = Join-Path $PSScriptRoot "tools\offline_patch.py"
    $env:PYTHONIOENCODING = "utf-8"
    if ($Backup) {
        & $python $tool rollback --app $InstallDir --data $DataDir --backup $Backup
    } else {
        & $python $tool apply --app $InstallDir --data $DataDir --package (Join-Path $PSScriptRoot "payload")
    }
    $result = $LASTEXITCODE
} catch {
    Write-Host $_ -ForegroundColor Red
    $result = 1
}
Read-Host "Press Enter to close"
exit $result
