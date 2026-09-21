param(
    [Parameter(Mandatory = $true)]
    [int]$Run1ProcessId
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path

if (Get-Process -Id $Run1ProcessId -ErrorAction SilentlyContinue) {
    Wait-Process -Id $Run1ProcessId
}

foreach ($runNumber in 2, 3) {
    & python (Join-Path $PSScriptRoot 'hybrid_v3_ai_review.py') --mode consistency --run $runNumber --batch-size 3 --timeout 900
    if ($LASTEXITCODE -ne 0) {
        throw "Consistency run $runNumber failed with exit code $LASTEXITCODE"
    }
}

& python (Join-Path $PSScriptRoot 'hybrid_v3_consistency.py')
if ($LASTEXITCODE -ne 0) {
    throw "Consistency evaluation failed with exit code $LASTEXITCODE"
}

$runRoot = Join-Path $projectRoot 'reports\course_backtest\2024-02-02\historical_scan_2023h2_formal_ai_v3_daily_scan\hybrid_monitoring_v1'
$consistencyResult = Get-Content -LiteralPath (Join-Path $runRoot 'consistency\consistency_result.json') -Raw | ConvertFrom-Json
if (-not $consistencyResult.passed) {
    Write-Output 'Consistency thresholds were not met. Formal performance replay was not started.'
    exit 2
}

& python (Join-Path $PSScriptRoot 'hybrid_v3_ai_review.py') --mode full --batch-size 3 --timeout 900
if ($LASTEXITCODE -ne 0) {
    throw "Formal full semantic review failed with exit code $LASTEXITCODE"
}

& python (Join-Path $PSScriptRoot 'hybrid_v3_replay.py')
if ($LASTEXITCODE -ne 0) {
    throw "Formal V3 performance replay failed with exit code $LASTEXITCODE"
}
