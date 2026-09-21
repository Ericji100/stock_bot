param(
    [Parameter(Mandatory = $true)]
    [int]$CoordinatorProcessId
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$runRoot = Join-Path $projectRoot 'reports\course_backtest\2024-02-02\historical_scan_2023h2_formal_ai_v3_daily_scan\hybrid_monitoring_v1'
$newLedger = Join-Path $runRoot 'v3_stock_lifecycle_and_triggers.jsonl'
$newBacktest = Join-Path $runRoot 'v3_backtest.json'

if (Get-Process -Id $CoordinatorProcessId -ErrorAction SilentlyContinue) {
    Wait-Process -Id $CoordinatorProcessId
}

if (-not (Test-Path -LiteralPath $newLedger) -or -not (Test-Path -LiteralPath $newBacktest)) {
    Write-Output 'Formal V3 replay did not produce both required inputs. Behavioral-equivalence audit was not run.'
    exit 2
}

& python (Join-Path $PSScriptRoot 'hybrid_v3_behavioral_equivalence.py')
if ($LASTEXITCODE -ne 0) {
    throw "Behavioral-equivalence audit failed with exit code $LASTEXITCODE"
}

Write-Output 'Behavioral-equivalence audit completed.'
