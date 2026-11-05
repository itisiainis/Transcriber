# Build the official whisper.cpp sources alongside the existing CPU build.
# Prerequisites: Visual Studio C++ Build Tools (including CMake), Vulkan SDK.
param([string]$Version = "v1.9.5")
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path $PSScriptRoot -Parent
$buildRoot = Join-Path $projectRoot "whisper\vulkan-build"
$destination = Join-Path $projectRoot "whisper\vulkan"

$sdk = $env:VULKAN_SDK
if (-not $sdk) {
    $sdk = Get-ChildItem "C:\VulkanSDK" -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending | Select-Object -First 1 -ExpandProperty FullName
}
if (-not $sdk -or -not (Test-Path (Join-Path $sdk "Bin\glslc.exe"))) {
    throw "Install the Vulkan SDK first: winget install --id KhronosGroup.VulkanSDK --exact --source winget"
}
$env:VULKAN_SDK = $sdk
$vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
$vsRoot = if (Test-Path $vswhere) { & $vswhere -latest -products '*' -property installationPath }
if (-not $vsRoot) {
    # Also works while a VS installation has not yet been registered by vswhere.
    $vsRoot = Get-ChildItem (Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio") -Filter vcvars64.bat -Recurse |
        Select-Object -First 1 | ForEach-Object { $_.Directory.Parent.Parent.Parent.FullName }
}
if (-not $vsRoot) { throw "Install Visual Studio Build Tools with Desktop development with C++." }
$vcvars = Join-Path $vsRoot "VC\Auxiliary\Build\vcvars64.bat"
if (-not (Test-Path $vcvars)) { throw "Missing C++ toolchain: $vcvars" }
$cmake = Join-Path $vsRoot "Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
$ninja = Join-Path $vsRoot "Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\ninja.exe"
if (-not (Test-Path $cmake) -or -not (Test-Path $ninja)) { throw "Install the CMake tools component in Visual Studio Build Tools." }

New-Item -ItemType Directory -Force -Path $buildRoot | Out-Null
$archive = Join-Path $buildRoot "$Version.zip"
$source = Join-Path $buildRoot ("whisper.cpp-" + $Version.TrimStart('v'))
if (-not (Test-Path $source)) {
    Invoke-WebRequest "https://github.com/ggml-org/whisper.cpp/archive/refs/tags/$Version.zip" -OutFile $archive -UseBasicParsing
    Expand-Archive -LiteralPath $archive -DestinationPath $buildRoot -Force
}
$build = Join-Path $buildRoot "build"
# Keep command text in a file: paths with spaces stay quoted end to end.
$batch = Join-Path $buildRoot "build.cmd"
@"
@echo off
call "$vcvars"
if errorlevel 1 exit /b 1
"$cmake" -S "$source" -B "$build" -G Ninja -DCMAKE_MAKE_PROGRAM="$ninja" -DCMAKE_BUILD_TYPE=Release -DGGML_VULKAN=ON -DGGML_NATIVE=OFF -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_SERVER=OFF
if errorlevel 1 exit /b 1
"$cmake" --build "$build" --target whisper-cli --parallel 8
exit /b %errorlevel%
"@ | Set-Content -LiteralPath $batch -Encoding ASCII
& $env:ComSpec /d /c $batch
if ($LASTEXITCODE -ne 0) { throw "Whisper Vulkan build failed ($LASTEXITCODE)." }
New-Item -ItemType Directory -Force -Path $destination | Out-Null
Get-ChildItem (Join-Path $build "bin") -File | Where-Object { $_.Extension -in '.exe', '.dll' } |
    Copy-Item -Destination $destination -Force
Write-Host "Installed $Version GPU build in $destination. Reload the extension, then enable Settings > Transcription > Use GPU (Vulkan)."
