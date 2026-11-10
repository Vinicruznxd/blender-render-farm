$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$exePath = Join-Path $projectRoot "dist\BlenderRenderFarmMaster.exe"

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Virtual environment not found at .venv. Create it first with 'python -m venv .venv'."
}

Push-Location $projectRoot
try {
    & $venvPython -m pip install -r requirements.txt
    & $venvPython -m pip install -r requirements-build.txt
    & $venvPython -m PyInstaller --noconfirm --clean "BlenderRenderFarmMasterStandalone.spec"

    if (-not (Test-Path -LiteralPath $exePath)) {
        throw "PyInstaller did not generate the expected executable: $exePath"
    }

    $previousQtPlatform = $env:QT_QPA_PLATFORM
    try {
        $env:QT_QPA_PLATFORM = "offscreen"
        $smokeProcess = Start-Process -FilePath $exePath -ArgumentList "--smoke-test" -WorkingDirectory (Split-Path -Parent $exePath) -WindowStyle Hidden -Wait -PassThru
        if ($smokeProcess.ExitCode -ne 0) {
            throw "Standalone Qt runtime smoke test failed with exit code $($smokeProcess.ExitCode)"
        }
    }
    finally {
        $env:QT_QPA_PLATFORM = $previousQtPlatform
    }

    Write-Host ""
    Write-Host "Standalone build ready at:" -ForegroundColor Green
    Write-Host $exePath -ForegroundColor Green
}
finally {
    Pop-Location
}
