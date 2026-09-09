$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Spec = Join-Path $Root "build\windows\AI-Audio-Client.spec"
$Installer = Join-Path $Root "build\windows\AI-Audio-Client.iss"

python -m PyInstaller --clean --noconfirm $Spec

$iscc = Get-Command iscc.exe -ErrorAction SilentlyContinue
if ($null -eq $iscc) {
    throw "Inno Setup is required to create AI-Audio-Client-Setup.exe"
}

& $iscc.Source $Installer
Write-Host "Installer created in build\windows\output"
