$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$bundledPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$scriptPath = Join-Path $repoRoot "tools\visual_library_report.py"

if (Test-Path $bundledPython) {
    & $bundledPython $scriptPath @args
    exit $LASTEXITCODE
}

$pythonOnPath = Get-Command python -ErrorAction SilentlyContinue
if ($pythonOnPath) {
    & $pythonOnPath.Source $scriptPath @args
    exit $LASTEXITCODE
}

Write-Error "Python was not found. Expected bundled Python at $bundledPython or python on PATH."
exit 1
