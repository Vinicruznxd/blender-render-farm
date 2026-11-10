$ErrorActionPreference = "Stop"

# Compatibility entry point. The canonical build definition lives at the
# project root so both commands always produce the same portable package.
$projectRoot = Split-Path -Parent $PSScriptRoot
& (Join-Path $projectRoot "build_portable.ps1")
