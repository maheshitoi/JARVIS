# Builds dist\JarvisDevice\JarvisDevice.exe — copy that whole folder to any Windows PC.
# Usage (from this folder):   powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path .venv)) { python -m venv .venv }
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller

# --onedir starts faster than --onefile and is less likely to be flagged by antivirus.
.\.venv\Scripts\pyinstaller.exe --noconfirm --clean --windowed --onedir `
    --name JarvisDevice `
    --collect-all livekit `
    --collect-all sounddevice `
    --collect-all _sounddevice_data `
    jarvis_device.py

Copy-Item device.example.yaml dist\JarvisDevice\device.example.yaml -Force
Write-Host ""
Write-Host "Built: $PSScriptRoot\dist\JarvisDevice\JarvisDevice.exe"
Write-Host "Copy the JarvisDevice folder to the device, add device.yaml next to the exe, run it."
