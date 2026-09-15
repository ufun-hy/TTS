param(
    [string]$Version = "1.0.0"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Stage = Join-Path $Root "build\windows\stage"
$DownloadRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("ai-live-studio-" + [Guid]::NewGuid().ToString("N"))
$HostPython = (Get-Command python.exe -ErrorAction Stop).Source

$PythonUrl = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip"
$PythonSha256 = "009d6bf7e3b2ddca3d784fa09f90fe54336d5b60f0e0f305c37f400bf83cfd3b"
$TorchVersion = "2.6.0+cu124"
$TorchIndex = "https://download.pytorch.org/whl/cu124"

$OllamaUrl = "https://github.com/ollama/ollama/releases/download/v0.34.0/ollama-windows-amd64.zip"
$OllamaSha256 = "a7dd1b174f39d3d1b8a25d4cbc86045d0e190b17187bfdcbe2f2ee3b5a11470e"
$CosyVoiceUrl = "https://github.com/Lourdle/cosyvoice.cpp/releases/download/v0.1.3/cosyvoice-1616b12-windows-x64-ffmpeg-no_icu.zip"
$CosyVoiceSha256 = "24c8589adcc0587932b5a7cc8d7489de8bd4f4b651bd0f3a67d4b42a611ba5c6"
$LlamaCudaUrl = "https://github.com/ggml-org/llama.cpp/releases/download/b10938/llama-b10938-bin-win-cuda-12.4-x64.zip"
$LlamaCudaSha256 = "e44c0135a03ab33cb477ed2f3211fac9efe6ff0106c740c2303ebfae60f9e7b2"
$CudaRuntimeUrl = "https://github.com/ggml-org/llama.cpp/releases/download/b10938/cudart-llama-bin-win-cuda-12.4-x64.zip"
$CudaRuntimeSha256 = "8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6"
$FfmpegUrl = "https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-09-15-13-18/ffmpeg-n8.1.2-53-g1005b294ff-win64-lgpl-shared-8.1.zip"
$FfmpegSha256 = "3b8e88c903043650350bf39adcd96c686e348e35857fd3476603e4d4ce126063"

function Download-Verified([string]$Url, [string]$Sha256, [string]$Destination) {
    Write-Host "Downloading $Url"
    Invoke-WebRequest -Uri $Url -OutFile $Destination
    $actual = (Get-FileHash -Algorithm SHA256 -Path $Destination).Hash.ToLowerInvariant()
    if ($actual -ne $Sha256.ToLowerInvariant()) {
        throw "SHA256 mismatch for ${Url}: expected $Sha256, got $actual"
    }
}

function Copy-ArchiveContents([string]$Archive, [string]$Destination) {
    $extract = Join-Path $DownloadRoot ([Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $extract | Out-Null
    Expand-Archive -Path $Archive -DestinationPath $extract -Force
    Copy-Item -Path (Join-Path $extract "*") -Destination $Destination -Recurse -Force
}

function Normalize-Component([string]$Component, [string[]]$Required) {
    foreach ($name in $Required) {
        $target = Join-Path $Component $name
        if (-not (Test-Path $target)) {
            $found = Get-ChildItem -LiteralPath $Component -Filter $name -File -Recurse | Select-Object -First 1
            if ($found) { Copy-Item -LiteralPath $found.FullName -Destination $target -Force }
        }
    }
    Get-ChildItem -LiteralPath $Component -Filter "*.dll" -File -Recurse |
        Where-Object { $_.DirectoryName -ne $Component } |
        ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $Component -Force }
}

function Remove-DevelopmentFiles([string]$Packages) {
    foreach ($relative in @("torch\include", "torch\share")) {
        $path = Join-Path $Packages $relative
        if (Test-Path $path) { Remove-Item -LiteralPath $path -Recurse -Force }
    }
    Get-ChildItem -LiteralPath $Packages -Directory -Recurse |
        Where-Object { $_.Name -eq "__pycache__" } |
        Sort-Object FullName -Descending |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }
    Get-ChildItem -LiteralPath $Packages -File -Recurse |
        Where-Object { $_.Extension -in @(".lib", ".pyc", ".pyo") } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }
}

Push-Location $Root
try {
    New-Item -ItemType Directory -Force -Path $DownloadRoot | Out-Null
    if (Test-Path $Stage) {
        Remove-Item -LiteralPath $Stage -Recurse -Force
    }
    $bin = Join-Path $Stage "runtime\bin"
    $ollamaBin = Join-Path $bin "ollama"
    $cosyvoiceBin = Join-Path $bin "cosyvoice"
    $ffmpegDir = Join-Path $bin "ffmpeg"
    New-Item -ItemType Directory -Force -Path $ollamaBin, $cosyvoiceBin, $ffmpegDir | Out-Null

    $pythonArchive = Join-Path $DownloadRoot "python-embed.zip"
    Download-Verified $PythonUrl $PythonSha256 $pythonArchive
    $pythonDir = Join-Path $Stage "runtime\python"
    New-Item -ItemType Directory -Force -Path $pythonDir | Out-Null
    Expand-Archive -Path $pythonArchive -DestinationPath $pythonDir -Force
    $pth = Get-ChildItem -LiteralPath $pythonDir -Filter "*._pth" -File | Select-Object -First 1
    if (-not $pth) { throw "Embedded Python _pth file was not found" }
    New-Item -ItemType Directory -Force -Path (Join-Path $pythonDir "Lib\site-packages") | Out-Null
    @(
        "$($pth.BaseName).zip"
        "."
        "..\.."
        "Lib\site-packages"
        "import site"
    ) | Set-Content -LiteralPath $pth.FullName -Encoding ASCII

    $sitePackages = Join-Path $pythonDir "Lib\site-packages"
    & $HostPython -m pip install --disable-pip-version-check --no-cache-dir --upgrade --target $sitePackages --no-deps qwen-asr==0.0.6
    if ($LASTEXITCODE -ne 0) { throw "qwen-asr installation failed" }
    & $HostPython -m pip install --disable-pip-version-check --no-cache-dir --upgrade --target $sitePackages `
        --index-url https://pypi.org/simple --extra-index-url $TorchIndex `
        -r (Join-Path $Root "scripts\requirements-qwen-asr-windows.txt")
    if ($LASTEXITCODE -ne 0) { throw "Windows ASR dependency installation failed" }

    # The wheel ships C++ headers/import libraries for extension development;
    # they are not needed by the bundled inference process and push Inno Setup
    # past its single-file Windows installer limit.
    Remove-DevelopmentFiles $sitePackages

    $ollamaArchive = Join-Path $DownloadRoot "ollama.zip"
    Download-Verified $OllamaUrl $OllamaSha256 $ollamaArchive
    Copy-ArchiveContents $ollamaArchive $ollamaBin

    $cosyvoiceArchive = Join-Path $DownloadRoot "cosyvoice.zip"
    Download-Verified $CosyVoiceUrl $CosyVoiceSha256 $cosyvoiceArchive
    Copy-ArchiveContents $cosyvoiceArchive $cosyvoiceBin

    $llamaArchive = Join-Path $DownloadRoot "llama-cuda.zip"
    Download-Verified $LlamaCudaUrl $LlamaCudaSha256 $llamaArchive
    Copy-ArchiveContents $llamaArchive $cosyvoiceBin

    $cudaRuntimeArchive = Join-Path $DownloadRoot "cuda-runtime.zip"
    Download-Verified $CudaRuntimeUrl $CudaRuntimeSha256 $cudaRuntimeArchive
    Copy-ArchiveContents $cudaRuntimeArchive $cosyvoiceBin

    $ffmpegArchive = Join-Path $DownloadRoot "ffmpeg.zip"
    Download-Verified $FfmpegUrl $FfmpegSha256 $ffmpegArchive
    $ffmpegExtract = Join-Path $DownloadRoot "ffmpeg"
    New-Item -ItemType Directory -Force -Path $ffmpegExtract | Out-Null
    Expand-Archive -Path $ffmpegArchive -DestinationPath $ffmpegExtract -Force
    $ffmpegExecutable = Get-ChildItem -LiteralPath $ffmpegExtract -Filter "ffmpeg.exe" -File -Recurse | Select-Object -First 1
    if (-not $ffmpegExecutable) { throw "FFmpeg archive does not contain ffmpeg.exe" }
    Copy-Item -Path (Join-Path $ffmpegExecutable.DirectoryName "*") -Destination $ffmpegDir -Recurse -Force

    $vcRoots = @(
        (Join-Path $env:ProgramFiles "Microsoft Visual Studio"),
        (Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio")
    )
    $vcRuntime = $null
    foreach ($vcRoot in $vcRoots) {
        if (-not $vcRoot) { continue }
        $vcRuntime = Get-ChildItem -LiteralPath $vcRoot -Filter "Microsoft.VC143.CRT" -Directory -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($vcRuntime) { break }
    }
    if ($vcRuntime) {
        foreach ($component in @($ollamaBin, $cosyvoiceBin, $ffmpegDir)) {
            Copy-Item -Path (Join-Path $vcRuntime.FullName "*.dll") -Destination $component -Force
        }
    } else {
        Write-Warning "MSVC x64 redist directory was not found; relying on Windows/Python runtime DLLs"
    }

    Normalize-Component $ollamaBin @("ollama.exe")
    Normalize-Component $cosyvoiceBin @("cosyvoice-server.exe", "cosyvoice.dll", "onnxruntime.dll")
    Normalize-Component $ffmpegDir @("ffmpeg.exe", "ffprobe.exe")

    $sourceDirs = @("audio_cache", "audio_client", "local_runtime", "recording_transcript", "server", "timeline", "voice_datasets", "web", "config", "scripts")
    foreach ($directory in $sourceDirs) {
        Copy-Item -Path (Join-Path $Root $directory) -Destination $Stage -Recurse -Force
    }
    Copy-Item -Path (Join-Path $Root "windows_client.py") -Destination $Stage -Force
    Copy-Item -Path (Join-Path $Root "voices.json") -Destination $Stage -Force

    $manifest = [ordered]@{
        product = "AI Live Studio"
        version = $Version
        python = "3.11.9-embed-amd64"
        torch = $TorchVersion
        qwen_asr = "0.0.6"
        ollama = "0.34.0"
        cosyvoice = "v0.1.3 (1616b12, no ICU)"
        ggml_cuda = "llama.cpp b10938 CUDA 12.4"
        ffmpeg = "n8.1.2-53-g1005b294ff"
        model_path_default = "D:\\AI-Live-Studio-Models"
    }
    $manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Stage "runtime\runtime-manifest.json") -Encoding UTF8
    & $HostPython -m pip freeze --path $sitePackages | Set-Content -LiteralPath (Join-Path $Stage "runtime\python-lock.txt") -Encoding UTF8

    & $HostPython -m PyInstaller --clean --noconfirm (Join-Path $Root "build\windows\AI-Live-Studio.spec")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller launcher build failed" }

    $bundledPython = Join-Path $pythonDir "python.exe"
    & $bundledPython -c "import torch, qwen_asr; print(torch.__version__); print(qwen_asr.__name__)"
    if ($LASTEXITCODE -ne 0) { throw "Bundled Python import smoke test failed" }
    Remove-DevelopmentFiles $sitePackages

    $iscc = $null
    $isccCommand = Get-Command iscc.exe -ErrorAction SilentlyContinue
    if ($null -ne $isccCommand) { $iscc = $isccCommand.Source }
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
    if ($null -eq $iscc) { throw "Inno Setup is required to create AI-Live-Studio-Windows-Test-Setup.exe" }
    & $iscc "/DMyAppVersion=$Version" (Join-Path $Root "build\windows\AI-Live-Studio.iss")
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup build failed" }
    Write-Host "Installer created in build\windows\output\AI-Live-Studio-Windows-Test-Setup.exe"
}
finally {
    Pop-Location
}
