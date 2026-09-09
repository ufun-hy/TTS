$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Spec = Join-Path $Root "build\windows\AI-Audio-Client.spec"
$Installer = Join-Path $Root "build\windows\AI-Audio-Client.iss"

python -m PyInstaller --clean --noconfirm $Spec

$iscc = $null
$isccCommand = Get-Command iscc.exe -ErrorAction SilentlyContinue
if ($null -ne $isccCommand) {
    $iscc = $isccCommand.Source
}

if ($null -eq $iscc) {
    $candidates = @(
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) {
            $iscc = $candidate
            break
        }
    }
}

if ($null -eq $iscc) {
    throw "Inno Setup is required to create AI-Audio-Client-Setup.exe"
}

& $iscc $Installer
Write-Host "Installer created in build\windows\output"
