$ErrorActionPreference = "Stop"

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$runtimeRoot = Join-Path $projectRoot ".runtime\trade_monitor"
$logPath = Join-Path $runtimeRoot "local_scheduler_process.log"
[System.IO.Directory]::CreateDirectory($runtimeRoot) | Out-Null

Set-Location -LiteralPath $projectRoot
$env:PYTHONUTF8 = "1"
$python = (Get-Command python.exe -ErrorAction Stop).Source

& $python -m trade_monitor.local_scheduler `
    --config "trade_monitor\local_scheduler_config.json" `
    --runtime-root ".runtime\trade_monitor" `
    daemon 2>&1 | Out-File -LiteralPath $logPath -Append -Encoding utf8

exit $LASTEXITCODE
