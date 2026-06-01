# Run the AI news digest agent with uv (for Windows Task Scheduler).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

uv run python (Join-Path $PSScriptRoot "aggregator.py")
exit $LASTEXITCODE
