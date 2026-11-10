$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$distRoot = Join-Path $projectRoot "dist"
$bundleRoot = Join-Path $distRoot "BlenderRenderFarmCoordinator"
$buildInfoPath = Join-Path $bundleRoot "build_info.json"

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Virtual environment not found at .venv. Create it first with 'python -m venv .venv'."
}

Push-Location $projectRoot
try {
    & $venvPython -m pip install -r requirements.txt
    & $venvPython -m pip install --upgrade pyinstaller
    & $venvPython -m PyInstaller --noconfirm --clean "BlenderRenderFarmCoordinator.spec"

    if (-not (Test-Path -LiteralPath $bundleRoot)) {
        throw "PyInstaller did not generate the expected folder: $bundleRoot"
    }

    $configSource = Join-Path $projectRoot "config.json"
    $configTarget = Join-Path $bundleRoot "config.json"
    $portableConfig = Get-Content -LiteralPath $configSource -Raw -Encoding UTF8 | ConvertFrom-Json
    $portableConfig.coordinatorId = ""
    $portableConfig | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $configTarget -Encoding UTF8

    $imgTarget = Join-Path $bundleRoot "img"
    if (Test-Path -LiteralPath $imgTarget) {
        Remove-Item -LiteralPath $imgTarget -Recurse -Force
    }
    Copy-Item -LiteralPath (Join-Path $projectRoot "img") -Destination $imgTarget -Recurse -Force

    $dataTarget = Join-Path $bundleRoot "app\data"
    $logsTarget = Join-Path $bundleRoot "app\data\logs"
    if (-not (Test-Path -LiteralPath $dataTarget)) {
        New-Item -ItemType Directory -Path $dataTarget -Force | Out-Null
    }
    if (-not (Test-Path -LiteralPath $logsTarget)) {
        New-Item -ItemType Directory -Path $logsTarget -Force | Out-Null
    }

    $exePath = Join-Path $bundleRoot "BlenderRenderFarmCoordinator.exe"
    $builtAtUtc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    $bundleHash = (Get-FileHash -LiteralPath $exePath -Algorithm SHA256).Hash.ToLower()
    $buildInfo = @{
        buildId = "coordinator-" + $builtAtUtc.Replace(":", "").Replace("-", "")
        buildTimestampUtc = $builtAtUtc
        bundleHash = $bundleHash
        appVersion = "1.2.2"
        protocolVersion = 2
        databaseSchemaVersion = 2
        requirementsHash = (Get-FileHash -LiteralPath (Join-Path $projectRoot "requirements.txt") -Algorithm SHA256).Hash.ToLower()
    }
    $buildInfo | ConvertTo-Json | Set-Content -LiteralPath $buildInfoPath -Encoding UTF8

    $portableReadme = @"
Blender Render Farm Coordinator Portable

Contents:
- BlenderRenderFarmCoordinator.exe
- _internal\
- build_info.json
- config.json
- img\
- app\data\
- app\data\logs\

Run:
1. Edit config.json if needed.
2. Start BlenderRenderFarmCoordinator.exe

Notes:
- This is a portable onedir build.
- Keep the entire folder together. Do not copy only the .exe without _internal.
- Replace the whole folder during upgrades. Do not overlay a new exe on top of an old _internal.
- config.json, database, and logs stay writable next to the executable.
- coordinatorId is generated automatically on first start if config.json leaves it empty.
- The SQLite database is created at first start in app\data\coordinator.db.
"@
    Set-Content -LiteralPath (Join-Path $bundleRoot "PORTABLE.txt") -Value $portableReadme -Encoding UTF8

    Write-Host ""
    Write-Host "Portable build ready at:" -ForegroundColor Green
    Write-Host $bundleRoot -ForegroundColor Green
}
finally {
    Pop-Location
}
