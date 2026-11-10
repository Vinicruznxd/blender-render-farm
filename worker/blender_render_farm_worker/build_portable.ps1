$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$distRoot = Join-Path $projectRoot "dist"
$bundleRoot = Join-Path $distRoot "BlenderRenderFarmWorker"
$buildInfoPath = Join-Path $bundleRoot "build_info.json"

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Virtual environment not found at .venv. Create it first with 'python -m venv .venv'."
}

Push-Location $projectRoot
try {
    & $venvPython -m pip install -r requirements.txt
    & $venvPython -m pip install --upgrade pyinstaller
    & $venvPython -m PyInstaller --noconfirm --clean "BlenderRenderFarmWorker.spec"

    if (-not (Test-Path -LiteralPath $bundleRoot)) {
        throw "PyInstaller did not generate the expected folder: $bundleRoot"
    }

    Copy-Item -LiteralPath (Join-Path $projectRoot "config.json") -Destination (Join-Path $bundleRoot "config.json") -Force

    $imgTarget = Join-Path $bundleRoot "img"
    if (Test-Path -LiteralPath $imgTarget) {
        Remove-Item -LiteralPath $imgTarget -Recurse -Force
    }
    Copy-Item -LiteralPath (Join-Path $projectRoot "img") -Destination $imgTarget -Recurse -Force

    $logsTarget = Join-Path $bundleRoot "logs"
    if (-not (Test-Path -LiteralPath $logsTarget)) {
        New-Item -ItemType Directory -Path $logsTarget | Out-Null
    }

    $exePath = Join-Path $bundleRoot "BlenderRenderFarmWorker.exe"
    $builtAtUtc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    $bundleHash = (Get-FileHash -LiteralPath $exePath -Algorithm SHA256).Hash.ToLower()
    $buildInfo = @{
        buildId = "worker-" + $builtAtUtc.Replace(":", "").Replace("-", "")
        buildTimestampUtc = $builtAtUtc
        bundleHash = $bundleHash
        appVersion = "1.2.2"
        protocolVersion = 2
        requirementsHash = (Get-FileHash -LiteralPath (Join-Path $projectRoot "requirements.txt") -Algorithm SHA256).Hash.ToLower()
    }
    $buildInfo | ConvertTo-Json | Set-Content -LiteralPath $buildInfoPath -Encoding UTF8

    $portableReadme = @"
Blender Render Farm Worker Portable

Contents:
- BlenderRenderFarmWorker.exe
- _internal\
- build_info.json
- config.json
- img\
- logs\

Run:
1. Edit config.json if needed.
2. Start BlenderRenderFarmWorker.exe

Notes:
- This is a portable onedir build.
- Keep the entire folder together. Do not copy only the .exe without _internal.
- Replace the whole folder during upgrades. Do not overlay a new exe on top of an old _internal.
- config.json, img, and logs stay writable next to the executable.
- Preserve each machine's config.json during upgrades.
- gpuEnableAll=true activates every compatible Cycles GPU in one Blender process.
- gpuBackend=AUTO prefers OptiX/CUDA on NVIDIA and tries the compatible Blender backends.
- gpuDeviceIds can restrict rendering to explicit Cycles device IDs or names.
- persistCpuFallback=false keeps a temporary CPU recovery from changing the permanent GPU preference.
- If showRenderDeviceWindow=true, closing the window closes the application.
"@
    Set-Content -LiteralPath (Join-Path $bundleRoot "PORTABLE.txt") -Value $portableReadme -Encoding UTF8

    Write-Host ""
    Write-Host "Portable build ready at:" -ForegroundColor Green
    Write-Host $bundleRoot -ForegroundColor Green
}
finally {
    Pop-Location
}
