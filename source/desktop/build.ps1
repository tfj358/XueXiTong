param(
    [string]$OutputName = '',
    [string]$ReleaseDir = ''
)
$ErrorActionPreference = 'Stop'
$pythonPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'superstar-checker/.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) { throw "Python runtime not found: $pythonPath" }
$buildArgs = @((Join-Path $PSScriptRoot 'release_tools.py'), 'build')
if ($OutputName) { $buildArgs += @('--output-name', $OutputName) }
if ($ReleaseDir) { $buildArgs += @('--release-dir', $ReleaseDir) }
& $pythonPath @buildArgs
if ($LASTEXITCODE -ne 0) { throw 'Build failed; diagnostics retained in release/.failed' }
