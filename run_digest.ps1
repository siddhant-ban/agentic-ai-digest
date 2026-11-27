# Run the agentic news digest pipeline with uv (for Windows Task Scheduler).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

uv run agentic-emailer run
exit $LASTEXITCODE
