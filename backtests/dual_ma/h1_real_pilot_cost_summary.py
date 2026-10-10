'''Research-only cost-inclusive closed-leg summary over the frozen v1 engine events.

Task-00 scope: read the frozen 'h1-real-pilot-2022-0419-0428-v1' run's
'engine_events.jsonl' -- the frozen engine's own already-costed output -- and
aggregate, per (matrix_id, slippage_bps) cell, the cost-inclusive PnL of every
*closed* leg::

    costed_net_pnl = Decimal(sell_net) - Decimal(buy_total)

'engine.py::_close_leg' already netted the sell-side commission and the STOCK
sell tax into 'sell_net' and the buy-side commission into 'buy_total', so this
module subtracts the two engine fields verbatim.  It never re-runs the engine,
never routes events through the strict adapter allow-list, and never widens the
frozen trading rules.

'open_censored' legs get their own artifact and are never treated as a zero
return.  Legs whose closed-leg cost fields are missing or implausible land in an
exception table instead of being dropped or zero-filled.
'''

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .approx_h1_cost_report_core import canonical_bytes


MODULE_VERSION = 'dual-ma-h1-real-pilot-cost-summary-v1'
RUN_ID_DEFAULT = 'h1-real-pilot-cost-summary-v1'
SUMMARY_SCHEMA_VERSION = 'dual-ma-h1-real-pilot-cost-summary-v1'
READBACK_SCHEMA_VERSION = 'dual-ma-h1-real-pilot-cost-summary-readback-v1'

MATRIX_IDS: tuple[str, ...] = ('M1', 'M2', 'M3', 'M4', 'M5', 'M6')
SLIPPAGE_SCENARIOS: tuple[int, ...] = (0, 10, 20)

# Frozen input expectations, taken from the 00 task brief.
FROZEN_ENGINE_EVENTS_SHA256 = (
    '82ea7e519429d3a00719ca5f62d2f97816563c3cab448acbf7ba8acc8f0387ce'
)
FROZEN_LEG_ROW_COUNT = 13514
FROZEN_OPEN_CENSORED_COUNT = 93
FROZEN_UNIVERSE_SOURCES_SHA256 = (
    '41ce724fee9b00d8ceafba990a287a07c337d52b7695aee2c85abf0b74ce03ac'
)
FROZEN_UNIVERSE_SOURCE_ID = 'dual-ma-universe-sources#ea8eddeba3577309'

# The two frozen labels this summary is allowed to carry into its own records.
PRICE_BASIS_DECLARED = 'UNADJUSTED_VENDOR_CLOSE_PROXY'
ENGINE_INSTRUMENT_PARAMETER = 'STOCK'

# The 00-specified hand check: engine sell_net 9597.4 minus buy_total 9988.
DEFAULT_HAND_CHECK: Mapping[str, Any] = {
    'label': '1102.TW M1 0 bps LONG_21_144-L1',
    'match': {
        'symbol': '1102.TW',
        'matrix_id': 'M1',
        'slippage_bps': 0,
        'leg_id': 'LONG_21_144-L1',
        'entry_date': '2022-04-28',
        'exit_date': '2022-05-09',
    },
    'expected_sell_net': '9597.4',
    'expected_buy_total': '9988',
    'expected_net_pnl': '-390.6',
}

UNIVERSE_LIST_KEYS: tuple[str, ...] = (
    'twse_current',
    'tpex_current',
    'twse_delisted',
    'tpex_delisted',
    'twse_new_listings',
    'tpex_new_listings',
)
TWSE_LIST_KEYS = frozenset({'twse_current', 'twse_delisted', 'twse_new_listings'})
TPEX_LIST_KEYS = frozenset({'tpex_current', 'tpex_delisted', 'tpex_new_listings'})

_CODE_LIKE = re.compile(r'^[0-9]{4,6}$')
_QUANTUM = Decimal('0.000001')

LEG_IDENTIFYING_FIELDS: tuple[str, ...] = (
    'code',
    'symbol',
    'strategy',
    'matrix_id',
    'slippage_bps',
    'leg_id',
    'leg_number',
    'episode_key',
    'pullback_event_id',
    'entry_date',
    'exit_date',
    'exit_reason',
    'open_censored',
    'shares',
)

EXCEPTION_MISSING_OPEN_CENSORED_FLAG = 'EXCEPTION_MISSING_OPEN_CENSORED_FLAG'
EXCEPTION_UNPARSEABLE_OPEN_CENSORED_FLAG = 'EXCEPTION_UNPARSEABLE_OPEN_CENSORED_FLAG'
EXCEPTION_MISSING_EXIT_DATE = 'EXCEPTION_MISSING_EXIT_DATE'
EXCEPTION_MISSING_BUY_TOTAL = 'EXCEPTION_MISSING_BUY_TOTAL'
EXCEPTION_UNPARSEABLE_BUY_TOTAL = 'EXCEPTION_UNPARSEABLE_BUY_TOTAL'
EXCEPTION_NONPOSITIVE_BUY_TOTAL = 'EXCEPTION_NONPOSITIVE_BUY_TOTAL'
EXCEPTION_MISSING_SELL_NET = 'EXCEPTION_MISSING_SELL_NET'
EXCEPTION_UNPARSEABLE_SELL_NET = 'EXCEPTION_UNPARSEABLE_SELL_NET'
EXCEPTION_NEGATIVE_SELL_NET = 'EXCEPTION_NEGATIVE_SELL_NET'

OPEN_ROW_NOTE = 'OPEN_CENSORED_AT_WINDOW_END_NOT_A_ZERO_RETURN'


class CostSummaryError(RuntimeError):
    '''Raised when a frozen input contradicts a stated data assumption.'''


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: str | Path) -> str:
    return _sha256_bytes(Path(path).read_bytes())


def _json_bytes(value: Any) -> bytes:
    return canonical_bytes(value) + b'\n'


def _jsonl_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    chunks = [
        json.dumps(
            _jsonable(row), ensure_ascii=False, sort_keys=True, separators=(',', ':')
        )
        for row in rows
    ]
    return (('\n'.join(chunks) + '\n') if chunks else '').encode('utf-8')


def _csv_bytes(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerow(header)
    for row in rows:
        writer.writerow(['' if value is None else str(value) for value in row])
    return buffer.getvalue().encode('utf-8')


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _decimal_str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _decimal_str(value: Decimal) -> str:
    text = format(value, 'f')
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return text or '0'


def _quantized(value: Decimal) -> str:
    return _decimal_str(value.quantize(_QUANTUM, rounding=ROUND_HALF_UP))


def _read_json(path: str | Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError as error:
        raise CostSummaryError(f'missing frozen input: {path}') from error
    except json.JSONDecodeError as error:
        raise CostSummaryError(f'unreadable JSON input: {path}') from error


def _parse_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    if not parsed.is_finite():
        return None
    return parsed


@dataclass(frozen=True)
class LegVerdict:
    state: str  # 'CLOSED' | 'OPEN' | 'EXCEPTION'
    net_pnl: Decimal | None
    reason: str | None


def classify_leg(row: Mapping[str, Any]) -> LegVerdict:
    '''Split one frozen LEG row into closed / open / exception.'''

    flag = row.get('open_censored')
    if not isinstance(flag, bool):
        reason = (
            EXCEPTION_MISSING_OPEN_CENSORED_FLAG
            if flag is None
            else EXCEPTION_UNPARSEABLE_OPEN_CENSORED_FLAG
        )
        return LegVerdict('EXCEPTION', None, reason)
    if flag:
        return LegVerdict('OPEN', None, None)
    if not row.get('exit_date'):
        return LegVerdict('EXCEPTION', None, EXCEPTION_MISSING_EXIT_DATE)
    buy_raw = row.get('buy_total')
    buy_total = _parse_decimal(buy_raw)
    if buy_total is None:
        reason = (
            EXCEPTION_MISSING_BUY_TOTAL
            if buy_raw is None
            else EXCEPTION_UNPARSEABLE_BUY_TOTAL
        )
        return LegVerdict('EXCEPTION', None, reason)
    if buy_total <= 0:
        return LegVerdict('EXCEPTION', None, EXCEPTION_NONPOSITIVE_BUY_TOTAL)
    sell_raw = row.get('sell_net')
    sell_net = _parse_decimal(sell_raw)
    if sell_net is None:
        reason = (
            EXCEPTION_MISSING_SELL_NET
            if sell_raw is None
            else EXCEPTION_UNPARSEABLE_SELL_NET
        )
        return LegVerdict('EXCEPTION', None, reason)
    if sell_net < 0:
        return LegVerdict('EXCEPTION', None, EXCEPTION_NEGATIVE_SELL_NET)
    return LegVerdict('CLOSED', sell_net - buy_total, None)


@dataclass(frozen=True)
class CompactLeg:
    code: str
    matrix_id: str
    slippage_bps: int
    state: str
    net_pnl: Decimal | None


@dataclass
class Cell:
    matrix_id: str
    slippage_bps: int
    closed_leg_count: int = 0
    open_leg_count: int = 0
    exception_leg_count: int = 0
    identity_hold_leg_count: int = 0
    costed_closed_leg_count: int = 0
    win_count: int = 0
    net_pnl: Decimal = Decimal('0')

    @property
    def leg_count(self) -> int:
        return self.closed_leg_count + self.open_leg_count + self.exception_leg_count

    def as_dict(self) -> dict[str, Any]:
        closed = self.costed_closed_leg_count
        return {
            'matrix_id': self.matrix_id,
            'slippage_bps': self.slippage_bps,
            'leg_count': self.leg_count,
            'closed_leg_count': self.closed_leg_count,
            'open_leg_count': self.open_leg_count,
            'exception_leg_count': self.exception_leg_count,
            'identity_hold_leg_count': self.identity_hold_leg_count,
            'costed_closed_leg_count': closed,
            'costed_net_pnl_sum': _decimal_str(self.net_pnl),
            'costed_mean_net_pnl_per_leg': (
                _quantized(self.net_pnl / closed) if closed else None
            ),
            'win_count': self.win_count,
            'win_rate': _quantized(Decimal(self.win_count) / closed) if closed else None,
        }


def cell_keys() -> list[tuple[str, int]]:
    return [(matrix, bps) for matrix in MATRIX_IDS for bps in SLIPPAGE_SCENARIOS]


def aggregate_cells(
    legs: Iterable[CompactLeg], held_codes: frozenset[str] = frozenset()
) -> dict[tuple[str, int], Cell]:
    '''Aggregate one cell per (matrix_id, slippage_bps); never mix cells.'''

    cells: dict[tuple[str, int], Cell] = {
        key: Cell(matrix_id=key[0], slippage_bps=key[1]) for key in cell_keys()
    }
    for leg in legs:
        cell = cells.get((leg.matrix_id, leg.slippage_bps))
        if cell is None:
            continue
        if leg.code in held_codes:
            cell.identity_hold_leg_count += 1
            continue
        if leg.state == 'CLOSED':
            cell.closed_leg_count += 1
            cell.costed_closed_leg_count += 1
            assert leg.net_pnl is not None
            cell.net_pnl += leg.net_pnl
            if leg.net_pnl > 0:
                cell.win_count += 1
        elif leg.state == 'OPEN':
            cell.open_leg_count += 1
        else:
            cell.exception_leg_count += 1
    return cells


@dataclass
class ScanResult:
    path: str
    raw_sha256: str
    row_count: int
    kind_counts: Counter
    legs: list[CompactLeg] = field(default_factory=list)
    open_rows: list[dict[str, Any]] = field(default_factory=list)
    exception_rows: list[dict[str, Any]] = field(default_factory=list)
    unmapped_rows: list[dict[str, Any]] = field(default_factory=list)
    code_suffixes: dict[str, Counter] = field(default_factory=dict)
    entry_dates: list[str] = field(default_factory=list)
    exit_dates: list[str] = field(default_factory=list)
    hand_check_matches: list[dict[str, Any]] = field(default_factory=list)

    @property
    def leg_row_count(self) -> int:
        return len(self.legs)

    @property
    def open_censored_count(self) -> int:
        return sum(1 for leg in self.legs if leg.state == 'OPEN')

    @property
    def exception_leg_count(self) -> int:
        return sum(1 for leg in self.legs if leg.state == 'EXCEPTION') + len(
            self.unmapped_rows
        )


def _project(row: Mapping[str, Any], extra: Mapping[str, Any] | None = None) -> dict:
    projected = {name: row.get(name) for name in LEG_IDENTIFYING_FIELDS}
    if extra:
        projected.update(extra)
    return projected


def scan_engine_events(
    path: str | Path, *, hand_check: Mapping[str, Any] | None = None
) -> ScanResult:
    '''Stream engine_events.jsonl once, keeping only kind == LEG rows.'''

    path = Path(path)
    if not path.is_file():
        raise CostSummaryError(f'missing frozen engine events: {path}')
    result = ScanResult(
        path=str(path), raw_sha256=sha256_file(path), row_count=0, kind_counts=Counter()
    )
    match_keys = dict((hand_check or {}).get('match') or {})
    valid_cells = set(cell_keys())
    with path.open('r', encoding='utf-8') as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            result.row_count += 1
            kind = row.get('kind')
            result.kind_counts[kind] += 1
            if kind != 'LEG':
                continue
            verdict = classify_leg(row)
            matrix_id = row.get('matrix_id')
            slippage_bps = row.get('slippage_bps')
            if isinstance(slippage_bps, bool) or not isinstance(slippage_bps, int):
                mapped = False
            else:
                mapped = (matrix_id, slippage_bps) in valid_cells
            if not mapped:
                result.unmapped_rows.append(
                    _project(row, {'classification': verdict.reason or verdict.state})
                )
                continue
            code = str(row.get('code'))
            result.legs.append(
                CompactLeg(
                    code=code,
                    matrix_id=str(matrix_id),
                    slippage_bps=int(slippage_bps),
                    state=verdict.state,
                    net_pnl=verdict.net_pnl,
                )
            )
            suffix = str(row.get('symbol') or '').split('.', 1)
            result.code_suffixes.setdefault(code, Counter())[
                suffix[1] if len(suffix) > 1 else ''
            ] += 1
            entry_date = row.get('entry_date')
            if isinstance(entry_date, str) and entry_date:
                result.entry_dates.append(entry_date)
            exit_date = row.get('exit_date')
            if isinstance(exit_date, str) and exit_date:
                result.exit_dates.append(exit_date)
            if verdict.state == 'OPEN':
                result.open_rows.append(_project(row, {'note': OPEN_ROW_NOTE}))
            elif verdict.state == 'EXCEPTION':
                result.exception_rows.append(
                    _project(
                        row,
                        {
                            'exception_reason': verdict.reason,
                            'buy_total_raw': row.get('buy_total'),
                            'sell_net_raw': row.get('sell_net'),
                        },
                    )
                )
            if match_keys and all(
                row.get(name) == value for name, value in match_keys.items()
            ):
                result.hand_check_matches.append(row)
    return result


def load_universe_code_registry(path: str | Path) -> dict[str, frozenset[str]]:
    '''Codes present in the frozen current-registration universe source.

    The source stores its rows in mixed encodings, so codes are collected by
    walking every value of the six vendor lists rather than by a fixed key name.
    '''

    payload = _read_json(path)
    registry: dict[str, set[str]] = {}
    for list_key in UNIVERSE_LIST_KEYS:
        _collect_codes(payload.get(list_key), list_key, registry)
    return {code: frozenset(sources) for code, sources in registry.items()}


def _collect_codes(value: Any, source: str, registry: dict[str, set[str]]) -> None:
    if isinstance(value, str):
        candidate = value.strip()
        if _CODE_LIKE.match(candidate):
            registry.setdefault(candidate, set()).add(source)
    elif isinstance(value, Mapping):
        for item in value.values():
            _collect_codes(item, source, registry)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_codes(item, source, registry)


@dataclass(frozen=True)
class InstrumentBasisCheck:
    codes_total: int
    codes_resolved: int
    unresolved_codes: tuple[str, ...]
    suffix_conflicts: tuple[str, ...]
    exchange_mismatches: tuple[str, ...]
    suffix_counts: Mapping[str, int]

    @property
    def resolved(self) -> bool:
        return not self.suffix_conflicts and not self.exchange_mismatches

    def as_dict(self) -> dict[str, Any]:
        return {
            'codes_total': self.codes_total,
            'codes_resolved': self.codes_resolved,
            'unresolved_codes': list(self.unresolved_codes),
            'suffix_conflicts': list(self.suffix_conflicts),
            'exchange_mismatches': list(self.exchange_mismatches),
            'suffix_counts': dict(sorted(self.suffix_counts.items())),
            'resolved': self.resolved,
        }


def check_instrument_basis(
    code_suffixes: Mapping[str, Mapping[str, int]],
    registry: Mapping[str, frozenset[str]],
) -> InstrumentBasisCheck:
    unresolved: list[str] = []
    conflicts: list[str] = []
    mismatches: list[str] = []
    suffix_counts: Counter = Counter()
    for code in sorted(code_suffixes):
        suffixes = {name for name, count in code_suffixes[code].items() if count}
        for name, count in code_suffixes[code].items():
            suffix_counts[name] += count
        if len(suffixes) > 1:
            conflicts.append(code)
            continue
        sources = registry.get(code)
        if not sources:
            unresolved.append(code)
            continue
        suffix = next(iter(suffixes)) if suffixes else ''
        wants_twse = suffix == 'TW'
        wants_tpex = suffix == 'TWO'
        has_twse = bool(sources & TWSE_LIST_KEYS)
        has_tpex = bool(sources & TPEX_LIST_KEYS)
        if wants_twse and has_tpex and not has_twse:
            mismatches.append(code)
        elif wants_tpex and has_twse and not has_tpex:
            mismatches.append(code)
    return InstrumentBasisCheck(
        codes_total=len(code_suffixes),
        codes_resolved=len(code_suffixes) - len(unresolved) - len(conflicts),
        unresolved_codes=tuple(unresolved),
        suffix_conflicts=tuple(conflicts),
        exchange_mismatches=tuple(mismatches),
        suffix_counts=dict(suffix_counts),
    )


def run_hand_check(
    spec: Mapping[str, Any], matches: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'label': spec.get('label'),
        'match': dict(spec.get('match') or {}),
        'matched_rows': len(matches),
        'expected_sell_net': spec.get('expected_sell_net'),
        'expected_buy_total': spec.get('expected_buy_total'),
        'expected_net_pnl': spec.get('expected_net_pnl'),
        'note': (
            "costed PnL is the engine's own sell_net minus buy_total; no engine "
            'field is recomputed and no formal adapter is involved'
        ),
    }
    if len(matches) != 1:
        payload['passed'] = False
        payload['failure'] = f'expected exactly one matching closed leg, found {len(matches)}'
        return payload
    row = matches[0]
    verdict = classify_leg(row)
    payload['sell_net'] = row.get('sell_net')
    payload['buy_total'] = row.get('buy_total')
    payload['state'] = verdict.state
    payload['computed_net_pnl'] = (
        _decimal_str(verdict.net_pnl) if verdict.net_pnl is not None else None
    )
    checks = [
        verdict.state == 'CLOSED',
        str(row.get('sell_net')) == str(spec.get('expected_sell_net')),
        str(row.get('buy_total')) == str(spec.get('expected_buy_total')),
        verdict.net_pnl is not None
        and verdict.net_pnl == _parse_decimal(spec.get('expected_net_pnl')),
    ]
    payload['passed'] = all(checks)
    return payload


def summarize(
    scan: ScanResult,
    *,
    registry: Mapping[str, frozenset[str]],
) -> tuple[list[dict[str, Any]], dict[str, Any], InstrumentBasisCheck]:
    basis = check_instrument_basis(scan.code_suffixes, registry)
    held = frozenset(basis.unresolved_codes)
    cells = aggregate_cells(scan.legs, held_codes=held)
    payload = [cells[key].as_dict() for key in cell_keys()]
    totals = {
        'cell_count': len(payload),
        'leg_row_count': scan.leg_row_count,
        'closed_leg_count': sum(cell.closed_leg_count for cell in cells.values()),
        'costed_closed_leg_count': sum(
            cell.costed_closed_leg_count for cell in cells.values()
        ),
        'open_censored_leg_count': sum(cell.open_leg_count for cell in cells.values()),
        'exception_leg_count': sum(
            cell.exception_leg_count for cell in cells.values()
        )
        + len(scan.unmapped_rows),
        'identity_hold_leg_count': sum(
            cell.identity_hold_leg_count for cell in cells.values()
        ),
        'costed_net_pnl_by_slippage': {
            str(bps): _decimal_str(
                sum(
                    (cells[(matrix, bps)].net_pnl for matrix in MATRIX_IDS),
                    Decimal('0'),
                )
            )
            for bps in SLIPPAGE_SCENARIOS
        },
        'costed_net_pnl_by_matrix': {
            matrix: _decimal_str(
                sum(
                    (cells[(matrix, bps)].net_pnl for bps in SLIPPAGE_SCENARIOS),
                    Decimal('0'),
                )
            )
            for matrix in MATRIX_IDS
        },
        'sample_entry_date_min': min(scan.entry_dates) if scan.entry_dates else None,
        'sample_entry_date_max': max(scan.entry_dates) if scan.entry_dates else None,
        'sample_exit_date_min': min(scan.exit_dates) if scan.exit_dates else None,
        'sample_exit_date_max': max(scan.exit_dates) if scan.exit_dates else None,
    }
    return payload, totals, basis


def build_cost_summary(
    *,
    engine_events_path: str | Path,
    universe_sources_path: str | Path,
    out_dir: str | Path,
    price_basis_binding_path: str | Path | None = None,
    expected_engine_events_sha256: str | None = FROZEN_ENGINE_EVENTS_SHA256,
    expected_universe_sources_sha256: str | None = FROZEN_UNIVERSE_SOURCES_SHA256,
    expected_leg_row_count: int | None = FROZEN_LEG_ROW_COUNT,
    expected_open_censored_count: int | None = FROZEN_OPEN_CENSORED_COUNT,
    run_id: str | None = None,
    hand_check: Mapping[str, Any] | None = DEFAULT_HAND_CHECK,
) -> dict[str, Any]:
    engine_events_path = Path(engine_events_path)
    universe_sources_path = Path(universe_sources_path)
    out_dir = Path(out_dir)
    if out_dir.exists():
        raise CostSummaryError(f'refusing to overwrite an existing run directory: {out_dir}')
    if price_basis_binding_path is None:
        price_basis_binding_path = engine_events_path.parent / 'price_basis_binding.json'
    price_basis_binding_path = Path(price_basis_binding_path)
    run_id = run_id or out_dir.name or RUN_ID_DEFAULT

    scan = scan_engine_events(engine_events_path, hand_check=hand_check)
    if (
        expected_engine_events_sha256
        and scan.raw_sha256 != expected_engine_events_sha256.lower()
    ):
        raise CostSummaryError(
            'engine_events.jsonl sha256 does not match the frozen expectation: '
            f'{scan.raw_sha256} != {expected_engine_events_sha256.lower()}'
        )

    registry = load_universe_code_registry(universe_sources_path)
    registry_sha = sha256_file(universe_sources_path)
    if (
        expected_universe_sources_sha256
        and registry_sha != expected_universe_sources_sha256.lower()
    ):
        raise CostSummaryError(
            'universe sources sha256 does not match the frozen expectation: '
            f'{registry_sha} != {expected_universe_sources_sha256.lower()}'
        )
    registry_payload = _read_json(universe_sources_path)

    binding_raw = (
        price_basis_binding_path.read_bytes()
        if price_basis_binding_path.is_file()
        else None
    )
    if binding_raw is None:
        raise CostSummaryError(
            f'missing price-basis binding for the frozen price basis: {price_basis_binding_path}'
        )
    binding = json.loads(binding_raw.decode('utf-8'))
    declared = binding.get('declared_price_basis')
    if declared != PRICE_BASIS_DECLARED:
        raise CostSummaryError(
            'price basis label contradicts the frozen C1 label: '
            f'{declared!r} != {PRICE_BASIS_DECLARED!r}'
        )

    cells, totals, basis = summarize(scan, registry=registry)
    if basis.suffix_conflicts or basis.exchange_mismatches:
        raise CostSummaryError(
            'program symbol/market data conflicts with the current-registration '
            'instrument proxy; refusing to cost the grid: '
            f'suffix_conflicts={list(basis.suffix_conflicts)} '
            f'exchange_mismatches={list(basis.exchange_mismatches)}'
        )

    hand_check_payload = run_hand_check(hand_check or {}, scan.hand_check_matches)

    gates = {
        'cell_count_is_18': 'PASS' if len(cells) == 18 else 'FAIL',
        'leg_rows_match_frozen_expectation': (
            'PASS'
            if expected_leg_row_count is None
            or scan.leg_row_count == expected_leg_row_count
            else 'FAIL'
        ),
        'open_censored_count_matches_frozen_expectation': (
            'PASS'
            if expected_open_censored_count is None
            or scan.open_censored_count == expected_open_censored_count
            else 'FAIL'
        ),
        'closed_open_exception_partition_leg_rows': (
            'PASS'
            if totals['closed_leg_count']
            + totals['open_censored_leg_count']
            + totals['exception_leg_count']
            == totals['leg_row_count']
            else 'FAIL'
        ),
        'no_closed_leg_cost_field_anomaly': (
            'PASS' if totals['exception_leg_count'] == 0 else 'FAIL'
        ),
        'every_code_resolved_to_current_registration_registry': (
            'PASS' if not basis.unresolved_codes else 'FAIL'
        ),
        'no_symbol_market_conflict': (
            'PASS'
            if not basis.suffix_conflicts and not basis.exchange_mismatches
            else 'FAIL'
        ),
        'price_basis_label_unchanged': 'PASS' if declared == PRICE_BASIS_DECLARED else 'FAIL',
        'hand_check_pass': 'PASS' if hand_check_payload['passed'] else 'FAIL',
    }
    status = 'PASS' if all(value == 'PASS' for value in gates.values()) else 'HOLD'

    summary_payload = {
        'schema_version': SUMMARY_SCHEMA_VERSION,
        'module_version': MODULE_VERSION,
        'run_id': run_id,
        'research_only': True,
        'matrix_ids': list(MATRIX_IDS),
        'slippage_scenarios': list(SLIPPAGE_SCENARIOS),
        'cells': cells,
        'totals': totals,
        'instrument_basis': basis.as_dict(),
        'note': (
            'costed_net_pnl_sum is the sum of Decimal(sell_net) - Decimal(buy_total) '
            'over closed legs only; open_censored legs are listed in '
            'open_censored_rows.csv and are never counted as a zero return'
        ),
    }
    cell_csv_header = [
        'matrix_id',
        'slippage_bps',
        'leg_count',
        'closed_leg_count',
        'open_leg_count',
        'exception_leg_count',
        'identity_hold_leg_count',
        'costed_closed_leg_count',
        'costed_net_pnl_sum',
        'costed_mean_net_pnl_per_leg',
        'win_count',
        'win_rate',
    ]
    cell_csv_rows = [[entry[name] for name in cell_csv_header] for entry in cells]
    open_csv_header = list(LEG_IDENTIFYING_FIELDS) + ['note']
    open_csv_rows = [[row.get(name) for name in open_csv_header] for row in scan.open_rows]

    out_dir.mkdir(parents=True, exist_ok=False)
    artifacts = {
        'cost_summary.json': _json_bytes(summary_payload),
        'cost_summary.csv': _csv_bytes(cell_csv_header, cell_csv_rows),
        'open_censored_rows.csv': _csv_bytes(open_csv_header, open_csv_rows),
        'exception_rows.jsonl': _jsonl_bytes(scan.exception_rows + scan.unmapped_rows),
    }
    for name, payload in artifacts.items():
        (out_dir / name).write_bytes(payload)
    artifact_sha256 = {name: _sha256_bytes(payload) for name, payload in artifacts.items()}

    readback = {
        'schema_version': READBACK_SCHEMA_VERSION,
        'module_version': MODULE_VERSION,
        'run_id': run_id,
        'status': status,
        'research_only': True,
        'performance_read_or_computed': True,
        'rules_changed': False,
        'engine_rerun': False,
        'strict_adapter_allow_list_touched': False,
        'sample_scope': (
            'the frozen v1 run for the eight 01 selection dates 2022-04-19 .. '
            '2022-04-28, positions monitored through 2022; a 6/8 partial '
            'exploration sample, not H1 and not formal historical performance; '
            '2023 / 2024 data is never read'
        ),
        'inputs': {
            'engine_events': {
                'path': scan.path,
                'raw_sha256': scan.raw_sha256,
                'expected_sha256': (
                    expected_engine_events_sha256.lower()
                    if expected_engine_events_sha256
                    else None
                ),
                'matched': (
                    expected_engine_events_sha256 is None
                    or scan.raw_sha256 == expected_engine_events_sha256.lower()
                ),
                'row_count': scan.row_count,
                'kind_counts': dict(sorted(scan.kind_counts.items())),
                'leg_row_count': scan.leg_row_count,
                'open_censored_count': scan.open_censored_count,
                'expected_leg_row_count': expected_leg_row_count,
                'expected_open_censored_count': expected_open_censored_count,
            },
            'universe_sources': {
                'path': str(universe_sources_path),
                'raw_sha256': registry_sha,
                'expected_sha256': (
                    expected_universe_sources_sha256.lower()
                    if expected_universe_sources_sha256
                    else None
                ),
                'matched': (
                    expected_universe_sources_sha256 is None
                    or registry_sha == expected_universe_sources_sha256.lower()
                ),
                'source_id': registry_payload.get('source_id'),
                'coverage_start': registry_payload.get('coverage_start'),
                'coverage_end': registry_payload.get('coverage_end'),
            },
            'price_basis_binding': {
                'path': str(price_basis_binding_path),
                'raw_sha256': _sha256_bytes(binding_raw),
                'declared_price_basis': declared,
                'adapter_price_basis_allowed': binding.get('adapter_price_basis_allowed'),
                'c1_binding_file_sha256': binding.get('c1_binding_file_sha256'),
                'c1_binding_path': binding.get('c1_binding_path'),
            },
        },
        'data_assumptions': {
            'price_basis': {
                'label': PRICE_BASIS_DECLARED,
                'is_official_close': False,
                'note': (
                    'the C1 vendor proxy label is carried verbatim; it is the '
                    'current data-exploration proxy and is never relabelled as an '
                    'official closing price'
                ),
            },
            'instrument_type_proxy': {
                'engine_parameter_used': ENGINE_INSTRUMENT_PARAMETER,
                'proxy_source_id': registry_payload.get('source_id'),
                'proxy_source_sha256': registry_sha,
                'proxy_basis': 'current company registration (TWSE / TPEX vendor lists)',
                'dated_2022_instrument_identity_evidence': False,
                'note': (
                    "the engine's own STOCK parameter is reused and justified by the "
                    'current company-registration lists only; this summary claims no '
                    'per-day 2022 instrument identity, and any unresolved code would '
                    'be held individually instead of being costed'
                ),
            },
            'cost_source': {
                'model': 'engine _close_leg sell_net and buy_total, verbatim',
                'engine_cost_model': 'CATHAY_WEB_028_TW_V1',
                'commission_rate': '0.000399',
                'stock_sell_tax_rate': '0.003',
                'note': (
                    'no cost quantity is recomputed here and the strict adapter '
                    'allow-list is untouched'
                ),
            },
        },
        'totals': totals,
        'instrument_basis': basis.as_dict(),
        'hand_check': hand_check_payload,
        'gates': gates,
        'artifact_sha256': artifact_sha256,
        'artifact_sha256_recomputed': {
            name: sha256_file(out_dir / name) for name in sorted(artifact_sha256)
        },
        'checkpoint': {
            'kind': 'ATOMIC_SINGLE_BATCH',
            'state': 'COMPLETE_AWAITING_00_QA',
            'resumable': False,
            'next_action': (
                '00 reviews the 18-cell research_only cost summary; the strict '
                'adapter price-basis label and the dated instrument identity source '
                'still need 00 adjudication before any formal use'
            ),
        },
    }
    readback['artifacts_verified'] = (
        readback['artifact_sha256'] == readback['artifact_sha256_recomputed']
    )
    (out_dir / 'readback.json').write_bytes(_json_bytes(readback))
    return readback


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine-events', required=True, type=Path)
    parser.add_argument('--universe-sources', required=True, type=Path)
    parser.add_argument('--out-dir', required=True, type=Path)
    parser.add_argument('--run-id', default=None)
    parser.add_argument('--price-basis-binding', type=Path, default=None)
    parser.add_argument(
        '--expected-engine-events-sha256', default=FROZEN_ENGINE_EVENTS_SHA256
    )
    parser.add_argument(
        '--expected-universe-sources-sha256', default=FROZEN_UNIVERSE_SOURCES_SHA256
    )
    args = parser.parse_args(argv)
    readback = build_cost_summary(
        engine_events_path=args.engine_events,
        universe_sources_path=args.universe_sources,
        out_dir=args.out_dir,
        price_basis_binding_path=args.price_basis_binding,
        expected_engine_events_sha256=args.expected_engine_events_sha256,
        expected_universe_sources_sha256=args.expected_universe_sources_sha256,
        run_id=args.run_id,
    )
    print(
        json.dumps(
            {
                'run_id': readback['run_id'],
                'status': readback['status'],
                'cell_count': readback['totals']['cell_count'],
                'costed_closed_leg_count': readback['totals']['costed_closed_leg_count'],
                'open_censored_leg_count': readback['totals']['open_censored_leg_count'],
                'exception_leg_count': readback['totals']['exception_leg_count'],
                'hand_check_passed': readback['hand_check']['passed'],
                'artifacts_verified': readback['artifacts_verified'],
            },
            sort_keys=True,
        )
    )
    return 0 if readback['status'] == 'PASS' else 1


if __name__ == '__main__':  # pragma: no cover
    sys.exit(main())
