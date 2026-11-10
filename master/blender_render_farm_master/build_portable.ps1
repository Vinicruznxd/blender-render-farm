$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$distRoot = Join-Path $projectRoot "dist"
$bundleRoot = Join-Path $distRoot "BlenderRenderFarmMaster"
$legacyPortableRoot = Join-Path $distRoot "BlenderRenderFarmMasterPortable"
$buildInfoPath = Join-Path $bundleRoot "build_info.json"

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Virtual environment not found at .venv. Create it first with 'python -m venv .venv'."
}

Push-Location $projectRoot
try {
    & $venvPython -m pip install -r requirements.txt
    & $venvPython -m pip install -r requirements-build.txt
    & $venvPython -m PyInstaller --noconfirm --clean "BlenderRenderFarmMaster.spec"

    if (-not (Test-Path -LiteralPath $bundleRoot)) {
        throw "PyInstaller did not generate the expected folder: $bundleRoot"
    }

    if (Test-Path -LiteralPath $legacyPortableRoot) {
        Remove-Item -LiteralPath $legacyPortableRoot -Recurse -Force
    }

    $configSource = Join-Path $projectRoot "config.json"
    $configTarget = Join-Path $bundleRoot "config.json"
    $portableConfig = Get-Content -LiteralPath $configSource -Raw -Encoding UTF8 | ConvertFrom-Json
    $portableConfig.coordinator_url = ""
    $portableConfig.master_id = ""
    $portableConfig.master_name = ""
    $portableConfig.workers = @()
    $portableConfig | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $configTarget -Encoding UTF8

    $imgTarget = Join-Path $bundleRoot "img"
    if (Test-Path -LiteralPath $imgTarget) {
        Remove-Item -LiteralPath $imgTarget -Recurse -Force
    }
    Copy-Item -LiteralPath (Join-Path $projectRoot "img") -Destination $imgTarget -Recurse -Force

    $jobsTarget = Join-Path $bundleRoot "app\data\jobs"
    $logsTarget = Join-Path $bundleRoot "app\data\logs"
    if (-not (Test-Path -LiteralPath $jobsTarget)) {
        New-Item -ItemType Directory -Path $jobsTarget -Force | Out-Null
    }
    if (-not (Test-Path -LiteralPath $logsTarget)) {
        New-Item -ItemType Directory -Path $logsTarget -Force | Out-Null
    }

    $exePath = Join-Path $bundleRoot "BlenderRenderFarmMaster.exe"
    $builtAtUtc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    $bundleHash = (Get-FileHash -LiteralPath $exePath -Algorithm SHA256).Hash.ToLower()
    $buildInfo = @{
        buildId = "master-" + $builtAtUtc.Replace(":", "").Replace("-", "")
        buildTimestampUtc = $builtAtUtc
        bundleHash = $bundleHash
        appVersion = "1.2.2"
        protocolVersion = 2
        requirementsHash = (Get-FileHash -LiteralPath (Join-Path $projectRoot "requirements.txt") -Algorithm SHA256).Hash.ToLower()
    }
    $buildInfo | ConvertTo-Json | Set-Content -LiteralPath $buildInfoPath -Encoding UTF8

    $portableReadme = @"
Blender Render Farm Master Portable

Contents:
- BlenderRenderFarmMaster.exe
- _internal\
- build_info.json
- config.json
- img\
- app\data\jobs\
- app\data\logs\

Run:
1. Edit config.json if needed.
2. Start BlenderRenderFarmMaster.exe

Notes:
- This is a portable onedir build.
- Keep the entire folder together. Do not copy only the .exe without _internal.
- Replace the whole folder during upgrades. Do not overlay a new exe on top of an old _internal.
- config.json, jobs, and logs stay writable next to the executable.
- master_id and master_name are generated automatically on first start if config.json leaves them empty.
- The portable bundle is generated without saved worker entries from the build machine.
- In coordinator mode, leave coordinator_url empty to use automatic LAN discovery, or set it manually only when you want to force a specific Coordinator.
"@
    Set-Content -LiteralPath (Join-Path $bundleRoot "PORTABLE.txt") -Value $portableReadme -Encoding UTF8

    $previousQtPlatform = $env:QT_QPA_PLATFORM
    try {
        $env:QT_QPA_PLATFORM = "offscreen"
        $smokeProcess = Start-Process -FilePath $exePath -ArgumentList "--smoke-test" -WorkingDirectory $bundleRoot -WindowStyle Hidden -Wait -PassThru
        if ($smokeProcess.ExitCode -ne 0) {
            throw "Qt runtime smoke test failed with exit code $($smokeProcess.ExitCode)"
        }
    }
    finally {
        $env:QT_QPA_PLATFORM = $previousQtPlatform
    }

    Write-Host ""
    Write-Host "Portable build ready at:" -ForegroundColor Green
    Write-Host $bundleRoot -ForegroundColor Green
}
finally {
    Pop-Location
}
