"""Validate disjoint TDCC holding buckets without counting adjustment/total rows."""
from __future__ import annotations

import math
from typing import Any


def validate_tdcc_snapshot(snapshot):
    if not isinstance(snapshot, dict):
        return snapshot
    fields = ('large_holder_pct', 'big_holder_pct', 'retail_holder_pct')
    invalid = [key for key in fields if snapshot.get(key) is not None and
               (not isinstance(snapshot[key], (int, float)) or not math.isfinite(snapshot[key]) or not 0 <= snapshot[key] <= 100)]
    if not invalid:
        return snapshot
    return {**snapshot, 'raw_invalid_snapshot': snapshot, 'status':'invalid',
            **{key: None for key in fields if key in snapshot}, 'concentration_signal':'unavailable',
            'data_quality': {'valid':False, 'warnings':['舊集保快取比例超出有效範圍，保留原值但不採用。'],
                             'invalid_fields':invalid}}


def summarize_tdcc_levels(rows: list[dict[str, Any]], *, large_levels=(15,), retail_levels=(1, 2, 3, 4, 5)) -> dict[str, Any]:
    levels: dict[int, dict[str, Any]] = {}
    issues: list[str] = []
    duplicates = 0
    for row in rows:
        try:
            level = int(row['level'])
        except (KeyError, TypeError, ValueError):
            issues.append('持股分級無效')
            continue
        if level in levels:
            if levels[level] == row:
                duplicates += 1
            else:
                issues.append(f'第 {level} 級有互相矛盾的重複列')
            continue
        levels[level] = row
        if 1 <= level <= 15:
            pct = row.get('pct')
            if not isinstance(pct, (int, float)) or not math.isfinite(pct) or not 0 <= pct <= 100:
                issues.append(f'第 {level} 級比例無效')
    buckets = {level: row for level, row in levels.items() if 1 <= level <= 15}
    if not buckets:
        issues.append('沒有可用的實際持股級距')
    if not issues and sum(row['pct'] for row in buckets.values()) > 100.1:
        issues.append('互斥持股級距比例合計超過百分之百')
    valid = not issues
    return {
        'status': 'covered' if valid else 'invalid',
        'large_holder_pct': round(sum(row['pct'] for level, row in buckets.items() if level in large_levels), 2) if valid else None,
        'retail_holder_pct': round(sum(row['pct'] for level, row in buckets.items() if level in retail_levels), 2) if valid else None,
        'total_people': sum(int(row.get('people') or row.get('holder_count') or 0) for row in buckets.values()) if valid else None,
        'duplicate_row_count': duplicates,
        'data_quality': {'valid': valid, 'warnings': list(dict.fromkeys(issues)),
                         'excluded_summary_levels': sorted(level for level in levels if level in (16, 17)),
                         'large_holder_levels': list(large_levels), 'retail_holder_levels': list(retail_levels)},
        'raw_levels': rows,
    }
