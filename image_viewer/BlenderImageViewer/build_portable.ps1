$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$distRoot = Join-Path $projectRoot "dist"
$bundleRoot = Join-Path $distRoot "BlenderImageViewer"
$buildInfoPath = Join-Path $bundleRoot "build_info.json"

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Virtual environment not found at .venv. Create it first with 'python -m venv .venv'."
}

Push-Location $projectRoot
try {
    & $venvPython -m pip install -r requirements.txt
    & $venvPython -m pip install -r requirements-build.txt
    & $venvPython -c "from pathlib import Path; from PIL import Image; root = Path.cwd(); png = root / 'img' / 'icon.png'; ico = root / 'img' / 'icon.ico'; image = Image.open(png).convert('RGBA'); image.save(ico, format='ICO', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)]); print(ico)"
    & $venvPython -m PyInstaller --noconfirm --clean "BlenderImageViewer.spec"

    if (-not (Test-Path -LiteralPath $bundleRoot)) {
        throw "PyInstaller did not generate the expected folder: $bundleRoot"
    }

    $configSource = Join-Path $projectRoot "config.json"
    $configTarget = Join-Path $bundleRoot "config.json"
    $portableConfig = Get-Content -LiteralPath $configSource -Raw -Encoding UTF8 | ConvertFrom-Json
    $portableConfig.last_image_folder = ""
    $portableConfig.last_blend_file = ""
    $portableConfig.last_output_folder = ""
    $portableConfig | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $configTarget -Encoding UTF8

    $imgTarget = Join-Path $bundleRoot "img"
    if (Test-Path -LiteralPath $imgTarget) {
        Remove-Item -LiteralPath $imgTarget -Recurse -Force
    }
    Copy-Item -LiteralPath (Join-Path $projectRoot "img") -Destination $imgTarget -Recurse -Force

    $logsTarget = Join-Path $bundleRoot "app\data\logs"
    if (-not (Test-Path -LiteralPath $logsTarget)) {
        New-Item -ItemType Directory -Path $logsTarget -Force | Out-Null
    }

    $exePath = Join-Path $bundleRoot "BlenderImageViewer.exe"
    $builtAtUtc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    $bundleHash = (Get-FileHash -LiteralPath $exePath -Algorithm SHA256).Hash.ToLower()
    $buildInfo = @{
        buildId = "image-viewer-" + $builtAtUtc.Replace(":", "").Replace("-", "")
        buildTimestampUtc = $builtAtUtc
        bundleHash = $bundleHash
        appVersion = "1.2.2"
        requirementsHash = (Get-FileHash -LiteralPath (Join-Path $projectRoot "requirements.txt") -Algorithm SHA256).Hash.ToLower()
    }
    $buildInfo | ConvertTo-Json | Set-Content -LiteralPath $buildInfoPath -Encoding UTF8

    $portableReadme = @"
BlenderImageViewer Portable

Contents:
- BlenderImageViewer.exe
- _internal\
- build_info.json
- config.json
- img\
- app\data\logs\

Run:
1. Edit config.json if needed.
2. Start BlenderImageViewer.exe

Notes:
- This is a portable onedir build.
- Keep the entire folder together. Do not copy only the .exe without _internal.
- Replace the whole folder during upgrades. Do not overlay a new exe on top of an old _internal.
- The coordinator is detected automatically by the application.
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
