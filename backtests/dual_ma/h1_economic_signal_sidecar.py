"""H1 economic-signal identity and leg-attribution sidecar.

Materialises, for every filled LEG of the frozen v0.4.1 engine, the canonical
four-column economic signal key (symbol, strategy, entry_date,
pullback_event_id) and the per-matrix engine trade-episode mother/scale-in
ownership.

The sidecar is derived only from the LEG rows, the reconciling TRANSACTION rows
and the explicit lifecycle registry (episode_windows.json). It never recomputes
or reads costs, PnL, R multiples or win rates, and it never rewrites any frozen
artifact. Engine episode_id values are treated as trade episode identifiers
scoped by (symbol, strategy, lifecycle) and are never used as a global key,
because the frozen engine reuses them.

Design reference:
docs/strategies/dual_ma/2022-h1-event-price-evidence-binding-v1.md
(SHA-256 9E68A5EBA25D32E9C534A999CD26F7F5BA8A0B32681029D966F1D0819CBB0C82).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = "dual-ma-h1-economic-signal-sidecar-v1"

# HOLD codes emitted by the attribution ledger.
HOLD_MISSING_ECONOMIC_SIGNAL_FIELD = "HOLD_MISSING_ECONOMIC_SIGNAL_FIELD"
HOLD_LIFECYCLE_REGISTRY_UNRESOLVED = "HOLD_LIFECYCLE_REGISTRY_UNRESOLVED"
HOLD_CONFLICTING_ECONOMIC_SIGNAL_KEY = "HOLD_CONFLICTING_ECONOMIC_SIGNAL_KEY"
HOLD_DUPLICATE_LEG_KEY = "HOLD_DUPLICATE_LEG_KEY"
HOLD_ENGINE_EPISODE_CROSSES_LIFECYCLE = "HOLD_ENGINE_EPISODE_CROSSES_LIFECYCLE"
HOLD_TRADE_EPISODE_MOTHER_INVALID = "HOLD_TRADE_EPISODE_MOTHER_INVALID"
HOLD_LEG_NUMBER_INVALID = "HOLD_LEG_NUMBER_INVALID"
HOLD_TRANSACTION_UNRECONCILED = "HOLD_TRANSACTION_UNRECONCILED"

HOLD_CODES = (
    HOLD_MISSING_ECONOMIC_SIGNAL_FIELD,
    HOLD_LIFECYCLE_REGISTRY_UNRESOLVED,
    HOLD_CONFLICTING_ECONOMIC_SIGNAL_KEY,
    HOLD_DUPLICATE_LEG_KEY,
    HOLD_ENGINE_EPISODE_CROSSES_LIFECYCLE,
    HOLD_TRADE_EPISODE_MOTHER_INVALID,
    HOLD_LEG_NUMBER_INVALID,
    HOLD_TRANSACTION_UNRECONCILED,
)

# Identity / attribution fields the sidecar is allowed to read. Anything that
# carries a performance or cost value is deliberately absent.
LEG_IDENTITY_FIELDS: tuple[str, ...] = (
    "symbol",
    "strategy",
    "entry_date",
    "pullback_event_id",
    "leg_id",
    "leg_number",
    "matrix_id",
    "slippage_bps",
    "episode_id",
    "episode_ordinal",
    "episode_source_selection_date",
    "code",
    "monitor_effective_from",
    "regime_window_start",
    "regime_window_end",
    "initial_stop",
    "entry_reference_close",
    "exit_date",
    "open_censored",
)

TRANSACTION_IDENTITY_FIELDS: tuple[str, ...] = (
    "symbol",
    "strategy",
    "leg_id",
    "matrix_id",
    "slippage_bps",
    "role",
    "trade_date",
    "code",
)

# Field names the frozen engine marks as excluded / unavailable. The sidecar must
# never propagate any of these, so it records them as readback evidence.
PERFORMANCE_FIELD_NAMES = frozenset(
    {"net_pnl", "gross_pnl", "price", "gross_amount", "return_pct", "r_multiple"}
)
EXCLUDED_MARKER_KEYS = ("performance_fields_excluded", "unavailable_engine_fields")

_ENTRY_ROLES = frozenset({"MOTHER", "ADD"})
_EXIT_ROLE = "EXIT"
_MOTHER_LEG_NUMBER = 1
_MAX_LEG_NUMBER = 4


class EconomicSignalSidecarError(ValueError):
    """Raised for structural errors in the frozen inputs or the registry."""


class MissingSignalFieldError(EconomicSignalSidecarError):
    """A required economic-key field is empty, null, or the literal string None."""

    def __init__(self, field_name: str) -> None:
        super().__init__(f"economic signal field {field_name!r} is missing or not replayable")
        self.field_name = field_name


@dataclass(frozen=True)
class LifecycleWindow:
    """One entry of the explicit lifecycle registry (episode_windows.json)."""

    code: str
    strategy: str
    episode_ordinal: int
    source_selection_date: str
    monitor_effective_from: str
    regime_window_start: str
    regime_window_end: str

    @property
    def lifecycle_episode_id(self) -> str:
        """Registry-native identity; no engine field is used here."""

        return f"{self.code}|{self.strategy}|EP{self.episode_ordinal}|{self.source_selection_date}"


def _require_text(value: Any, field_name: str) -> str:
    if value is None:
        raise MissingSignalFieldError(field_name)
    text = str(value).strip()
    if text == "" or text.lower() in {"none", "null"}:
        raise MissingSignalFieldError(field_name)
    return text


def _is_iso_date(text: str) -> bool:
    return len(text) == 10 and text[4] == "-" and text[7] == "-" and text.replace("-", "").isdigit()


def canonical_economic_key(
    symbol: Any,
    strategy: Any,
    entry_date: Any,
    pullback_event_id: Any,
) -> str:
    """Return the canonical UTF-8 JSON array for the four signal columns.

    Every column must be non-empty and replayable from raw frozen fields; a
    literal None/null placeholder is rejected instead of being spliced into a
    key.
    """

    values = (
        _require_text(symbol, "symbol"),
        _require_text(strategy, "strategy"),
        _require_text(entry_date, "entry_date"),
        _require_text(pullback_event_id, "pullback_event_id"),
    )
    if not _is_iso_date(values[2]):
        raise MissingSignalFieldError("entry_date")
    return json.dumps(list(values), ensure_ascii=False, separators=(",", ":"))


def parse_economic_key(economic_key: str) -> tuple[str, str, str, str]:
    """Inverse of canonical_economic_key for verification."""

    payload = json.loads(economic_key)
    if not isinstance(payload, list) or len(payload) != 4:
        raise EconomicSignalSidecarError(f"not a four-column economic key: {economic_key!r}")
    return tuple(str(part) for part in payload)  # type: ignore[return-value]


def load_lifecycle_registry(
    path: str | Path,
) -> tuple[dict[tuple[str, str, int, str], LifecycleWindow], dict[str, Any]]:
    """Load the explicit lifecycle registry, rejecting ambiguous identities."""

    registry_path = Path(path)
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise EconomicSignalSidecarError("lifecycle registry must be a JSON object")
    windows = payload.get("windows")
    if not isinstance(windows, list) or not windows:
        raise EconomicSignalSidecarError("lifecycle registry has no windows")

    index: dict[tuple[str, str, int, str], LifecycleWindow] = {}
    for raw in windows:
        if not isinstance(raw, Mapping):
            raise EconomicSignalSidecarError("lifecycle window entry must be an object")
        window = LifecycleWindow(
            code=_require_text(raw.get("code"), "code"),
            strategy=_require_text(raw.get("strategy"), "strategy"),
            episode_ordinal=int(raw.get("episode_ordinal")),
            source_selection_date=_require_text(raw.get("source_selection_date"), "source_selection_date"),
            monitor_effective_from=_require_text(raw.get("monitor_effective_from"), "monitor_effective_from"),
            regime_window_start=_require_text(raw.get("regime_window_start"), "regime_window_start"),
            regime_window_end=_require_text(raw.get("regime_window_end"), "regime_window_end"),
        )
        key = (window.code, window.strategy, window.episode_ordinal, window.source_selection_date)
        if key in index:
            raise EconomicSignalSidecarError(f"ambiguous lifecycle registry entry for {key!r}")
        index[key] = window
    return index, payload


def read_engine_events(
    path: str | Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Stream the frozen event log, returning (legs, transactions, kind counts).

    Only LEG and TRANSACTION rows are retained; DECISION rows are counted for
    provenance but not projected. Rows are returned in file order.
    """

    legs: list[dict[str, Any]] = []
    transactions: list[dict[str, Any]] = []
    kind_counts: dict[str, int] = {}
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            kind = row.get("kind")
            kind_counts[kind] = kind_counts.get(kind, 0) + 1
            if kind == "LEG":
                legs.append(row)
            elif kind == "TRANSACTION":
                transactions.append(row)
    return legs, transactions, kind_counts


def declared_excluded_fields(rows: Iterable[Mapping[str, Any]]) -> frozenset[str]:
    """Collect the engine's own excluded/unavailable field markers."""

    declared: set[str] = set()
    for row in rows:
        for marker in EXCLUDED_MARKER_KEYS:
            values = row.get(marker) or ()
            for name in values:
                declared.add(str(name))
    return frozenset(declared)


def _project(row: Mapping[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    return {name: row.get(name) for name in fields}


@dataclass(frozen=True)
class SidecarBuild:
    """Deterministic sidecar result."""

    rows: list[dict[str, Any]]
    holds: list[dict[str, Any]]
    counts: dict[str, Any]
    excluded_field_names: frozenset[str]

    def rows_for_entry_date_at_or_before(self, cutoff: str) -> list[dict[str, Any]]:
        return [row for row in self.rows if row["economic_key_fields"]["entry_date"] <= cutoff]


def _hold_entry(code: str, leg: Mapping[str, Any], detail: str) -> dict[str, Any]:
    return {
        "code": code,
        "symbol": leg.get("symbol"),
        "strategy": leg.get("strategy"),
        "matrix_id": leg.get("matrix_id"),
        "slippage_bps": leg.get("slippage_bps"),
        "leg_id": leg.get("leg_id"),
        "leg_number": leg.get("leg_number"),
        "lifecycle_episode_id": leg.get("_lifecycle_episode_id"),
        "engine_episode_id": leg.get("episode_id"),
        "economic_key": leg.get("_economic_key"),
        "entry_date": leg.get("entry_date"),
        "pullback_event_id": leg.get("pullback_event_id"),
        "detail": detail,
    }


def build_sidecar(
    *,
    legs: Iterable[Mapping[str, Any]],
    transactions: Iterable[Mapping[str, Any]],
    registry_index: Mapping[tuple[str, str, int, str], LifecycleWindow],
    input_lineage: Mapping[str, Any] | None = None,
    exit_visibility_cutoff: str | None = None,
) -> SidecarBuild:
    """Materialise the economic-signal and leg-attribution sidecar.

    The result is a pure function of the projected identity columns: reversing
    the input order yields byte-identical canonical output.

    When exit_visibility_cutoff is set (prefix replay) an exit transaction that
    has not happened yet is not required: at monitor day D the sidecar must only
    use what was visible up to D, so a leg still open at D keeps its identity.
    """

    leg_rows = list(legs)
    tx_rows = list(transactions)
    declared = declared_excluded_fields((*leg_rows, *tx_rows))

    holds: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []

    # Identity conflicts must be established from every original LEG, not
    # merely from the subset that later passes transaction reconciliation.
    # Otherwise a malformed matrix row could hide a conflicting signal while
    # its other matrix copies are incorrectly emitted as valid.
    economic_fingerprints: dict[str, set[tuple[Any, Any]]] = {}
    leg_key_counts: dict[tuple[Any, ...], int] = {}
    for raw in leg_rows:
        try:
            key = canonical_economic_key(
                raw.get("symbol"),
                raw.get("strategy"),
                raw.get("entry_date"),
                raw.get("pullback_event_id"),
            )
        except MissingSignalFieldError:
            continue
        economic_fingerprints.setdefault(key, set()).add(
            (raw.get("initial_stop"), raw.get("entry_reference_close"))
        )
        identity = (raw.get("matrix_id"), _normalise_slippage(raw.get("slippage_bps")), key)
        leg_key_counts[identity] = leg_key_counts.get(identity, 0) + 1
    conflicting_keys = {
        key for key, fingerprints in economic_fingerprints.items() if len(fingerprints) > 1
    }
    duplicate_leg_keys = {key for key, count in leg_key_counts.items() if count > 1}

    # --- transaction reconciliation index ---------------------------------
    entry_tx: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
    exit_tx: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
    for tx in tx_rows:
        key = (
            tx.get("matrix_id"),
            _normalise_slippage(tx.get("slippage_bps")),
            tx.get("symbol"),
            tx.get("strategy"),
            tx.get("leg_id"),
        )
        role = tx.get("role")
        if role in _ENTRY_ROLES:
            entry_tx.setdefault(key, []).append(tx)
        elif role == _EXIT_ROLE:
            exit_tx.setdefault(key, []).append(tx)
        else:
            # An unknown role cannot be reconciled to any leg; keep it visible.
            holds.append(
                _hold_entry(HOLD_TRANSACTION_UNRECONCILED, tx, f"unknown transaction role {role!r}")
            )

    # --- per-leg validation -----------------------------------------------
    for raw in leg_rows:
        leg = _project(raw, LEG_IDENTITY_FIELDS)
        leg["slippage_bps"] = _normalise_slippage(leg.get("slippage_bps"))

        try:
            leg["_economic_key"] = canonical_economic_key(
                leg.get("symbol"),
                leg.get("strategy"),
                leg.get("entry_date"),
                leg.get("pullback_event_id"),
            )
        except MissingSignalFieldError as error:
            holds.append(
                _hold_entry(HOLD_MISSING_ECONOMIC_SIGNAL_FIELD, leg, f"missing {error.field_name}")
            )
            continue

        leg_number = leg.get("leg_number")
        if (
            not isinstance(leg_number, int)
            or isinstance(leg_number, bool)
            or not (1 <= leg_number <= _MAX_LEG_NUMBER)
        ):
            holds.append(
                _hold_entry(
                    HOLD_LEG_NUMBER_INVALID,
                    leg,
                    f"leg_number {leg_number!r} outside 1..{_MAX_LEG_NUMBER}",
                )
            )
            continue

        try:
            _require_text(leg.get("leg_id"), "leg_id")
            _require_text(leg.get("matrix_id"), "matrix_id")
            _require_text(leg.get("episode_id"), "episode_id")
            ordinal = int(leg.get("episode_ordinal"))
            selection_date = _require_text(
                leg.get("episode_source_selection_date"), "episode_source_selection_date"
            )
            code = _require_text(leg.get("code"), "code")
            strategy = _require_text(leg.get("strategy"), "strategy")
            _require_text(leg.get("initial_stop"), "initial_stop")
            _require_text(leg.get("entry_reference_close"), "entry_reference_close")
        except (MissingSignalFieldError, TypeError, ValueError) as error:
            field_name = getattr(error, "field_name", "attribution_key")
            holds.append(
                _hold_entry(HOLD_MISSING_ECONOMIC_SIGNAL_FIELD, leg, f"missing {field_name}")
            )
            continue

        window = registry_index.get((code, strategy, ordinal, selection_date))
        if window is None:
            holds.append(
                _hold_entry(HOLD_LIFECYCLE_REGISTRY_UNRESOLVED, leg, "no unique lifecycle registry window")
            )
            continue
        if (
            window.monitor_effective_from != leg.get("monitor_effective_from")
            or window.regime_window_start != leg.get("regime_window_start")
            or window.regime_window_end != leg.get("regime_window_end")
        ):
            holds.append(
                _hold_entry(HOLD_LIFECYCLE_REGISTRY_UNRESOLVED, leg, "registry window fields disagree with leg")
            )
            continue
        leg["_lifecycle_episode_id"] = window.lifecycle_episode_id
        leg["_registry"] = window
        if not (window.monitor_effective_from <= leg["entry_date"] <= window.regime_window_end):
            holds.append(
                _hold_entry(HOLD_ENGINE_EPISODE_CROSSES_LIFECYCLE, leg, "entry is outside its lifecycle window")
            )
            continue
        exit_date = leg.get("exit_date")
        if exit_date is not None and not (leg["entry_date"] <= str(exit_date) <= window.regime_window_end):
            holds.append(
                _hold_entry(HOLD_ENGINE_EPISODE_CROSSES_LIFECYCLE, leg, "exit is outside its lifecycle window")
            )
            continue

        # --- transaction reconciliation ----------------------------------
        tx_key = (
            leg["matrix_id"],
            leg["slippage_bps"],
            leg["symbol"],
            leg["strategy"],
            leg["leg_id"],
        )
        entries = entry_tx.get(tx_key, [])
        exits = exit_tx.get(tx_key, [])
        entry_role = entries[0].get("role") if entries else None
        if len(entries) != 1 or not _entry_role_matches(entry_role, leg_number):
            holds.append(
                _hold_entry(
                    HOLD_TRANSACTION_UNRECONCILED,
                    leg,
                    f"entry transactions={len(entries)} for leg_number={leg_number}",
                )
            )
            continue
        if entries[0].get("trade_date") != leg["entry_date"]:
            holds.append(
                _hold_entry(HOLD_TRANSACTION_UNRECONCILED, leg, "entry transaction date differs from LEG")
            )
            continue
        expected_exits = 0 if leg.get("open_censored") else 1
        if exit_visibility_cutoff is None and len(exits) != expected_exits:
            holds.append(
                _hold_entry(
                    HOLD_TRANSACTION_UNRECONCILED,
                    leg,
                    f"exit transactions={len(exits)}, expected={expected_exits}",
                )
            )
            continue
        if exit_visibility_cutoff is not None and len(exits) > 1:
            holds.append(
                _hold_entry(
                    HOLD_TRANSACTION_UNRECONCILED,
                    leg,
                    f"duplicate exit transactions={len(exits)} visible at {exit_visibility_cutoff}",
                )
            )
            continue
        if exits and exit_date is not None and exits[0].get("trade_date") != exit_date:
            holds.append(
                _hold_entry(HOLD_TRANSACTION_UNRECONCILED, leg, "exit transaction date differs from LEG")
            )
            continue
        if exits and str(exits[0].get("trade_date")) < leg["entry_date"]:
            holds.append(
                _hold_entry(HOLD_TRANSACTION_UNRECONCILED, leg, "exit transaction precedes entry")
            )
            continue

        candidates.append(leg)

    # --- global consistency checks ----------------------------------------
    # each matrix trade episode needs exactly one leg_number=1 mother
    trade_episode_legs: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for leg in candidates:
        scope = (
            leg["symbol"],
            leg["strategy"],
            leg["_lifecycle_episode_id"],
            leg["matrix_id"],
            leg["slippage_bps"],
            leg["episode_id"],
        )
        trade_episode_legs.setdefault(scope, []).append(leg)
    invalid_episodes: dict[tuple[Any, ...], str] = {}
    for scope, members in trade_episode_legs.items():
        numbers = [member["leg_number"] for member in members]
        mothers = [number for number in numbers if number == _MOTHER_LEG_NUMBER]
        if len(mothers) != 1:
            invalid_episodes[scope] = f"mother leg count={len(mothers)}"
        elif len(set(numbers)) != len(numbers):
            invalid_episodes[scope] = "duplicate leg_number within trade episode"
        elif sorted(numbers) != list(range(1, len(numbers) + 1)):
            invalid_episodes[scope] = "non-consecutive leg_number within trade episode"
        elif len(numbers) > _MAX_LEG_NUMBER:
            invalid_episodes[scope] = f"trade episode has {len(numbers)} legs"

    # --- emit --------------------------------------------------------------
    emitted: list[dict[str, Any]] = []
    for leg in candidates:
        scope = (
            leg["symbol"],
            leg["strategy"],
            leg["_lifecycle_episode_id"],
            leg["matrix_id"],
            leg["slippage_bps"],
            leg["episode_id"],
        )
        if scope in invalid_episodes:
            holds.append(
                _hold_entry(HOLD_TRADE_EPISODE_MOTHER_INVALID, leg, invalid_episodes[scope])
            )
            continue
        if leg["_economic_key"] in conflicting_keys:
            holds.append(
                _hold_entry(
                    HOLD_CONFLICTING_ECONOMIC_SIGNAL_KEY,
                    leg,
                    "economic key has conflicting stop/reference close",
                )
            )
            continue
        if (leg["matrix_id"], leg["slippage_bps"], leg["_economic_key"]) in duplicate_leg_keys:
            holds.append(
                _hold_entry(
                    HOLD_DUPLICATE_LEG_KEY,
                    leg,
                    "duplicate (matrix, slippage, economic_key)",
                )
            )
            continue
        emitted.append(_sidecar_row(leg))

    emitted.sort(key=_row_sort_key)
    holds.sort(key=_hold_sort_key)
    counts = _build_counts(leg_rows, tx_rows, emitted, holds)
    return SidecarBuild(rows=emitted, holds=holds, counts=counts, excluded_field_names=declared)


def _normalise_slippage(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _entry_role_matches(role: Any, leg_number: int) -> bool:
    if leg_number == _MOTHER_LEG_NUMBER:
        return role == "MOTHER"
    return role == "ADD"


def _sidecar_row(leg: Mapping[str, Any]) -> dict[str, Any]:
    window: LifecycleWindow = leg["_registry"]
    trade_episode_key = json.dumps(
        [
            leg["symbol"],
            leg["strategy"],
            leg["_lifecycle_episode_id"],
            leg["matrix_id"],
            leg["slippage_bps"],
            leg["episode_id"],
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "economic_key": leg["_economic_key"],
        "economic_key_fields": {
            "symbol": leg["symbol"],
            "strategy": leg["strategy"],
            "entry_date": leg["entry_date"],
            "pullback_event_id": leg["pullback_event_id"],
        },
        "lifecycle_episode_id": leg["_lifecycle_episode_id"],
        "lifecycle_registry_fields": {
            "code": window.code,
            "strategy": window.strategy,
            "episode_ordinal": window.episode_ordinal,
            "source_selection_date": window.source_selection_date,
        },
        "engine_episode_id": leg["episode_id"],
        "trade_episode_key": trade_episode_key,
        "matrix_id": leg["matrix_id"],
        "slippage_bps": leg["slippage_bps"],
        "leg_id": leg["leg_id"],
        "leg_number": leg["leg_number"],
        "leg_role": "MOTHER" if leg["leg_number"] == _MOTHER_LEG_NUMBER else "ADD",
        "initial_stop": leg.get("initial_stop"),
        "entry_reference_close": leg.get("entry_reference_close"),
    }


def _row_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    fields = row["economic_key_fields"]
    return (
        row["matrix_id"],
        fields["symbol"],
        fields["strategy"],
        fields["entry_date"],
        fields["pullback_event_id"],
        row["leg_number"],
        row["leg_id"],
    )


def _hold_sort_key(hold: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        hold["code"],
        str(hold.get("symbol")),
        str(hold.get("strategy")),
        str(hold.get("matrix_id")),
        str(hold.get("leg_id")),
        json.dumps(hold, ensure_ascii=False, sort_keys=True),
    )


def _build_counts(
    leg_rows: Sequence[Mapping[str, Any]],
    tx_rows: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    holds: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    per_matrix: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = per_matrix.setdefault(row["matrix_id"], {"legs": 0, "mothers": 0, "adds": 0})
        bucket["legs"] += 1
        bucket["mothers" if row["leg_role"] == "MOTHER" else "adds"] += 1

    hold_counts: dict[str, int] = {}
    for hold in holds:
        hold_counts[hold["code"]] = hold_counts.get(hold["code"], 0) + 1

    return {
        "input_leg_rows": len(leg_rows),
        "input_transaction_rows": len(tx_rows),
        "attributed_leg_rows": len(rows),
        "hold_rows": len(holds),
        "hold_leg_count": len({(hold.get("matrix_id"), hold.get("leg_id")) for hold in holds}),
        "distinct_economic_key_count": len({row["economic_key"] for row in rows}),
        "distinct_lifecycle_count": len({row["lifecycle_episode_id"] for row in rows}),
        "distinct_trade_episode_count": len({row["trade_episode_key"] for row in rows}),
        "mother_leg_count": sum(1 for row in rows if row["leg_role"] == "MOTHER"),
        "add_leg_count": sum(1 for row in rows if row["leg_role"] == "ADD"),
        "per_matrix": {matrix: per_matrix[matrix] for matrix in sorted(per_matrix)},
        "hold_counts": {code: hold_counts[code] for code in sorted(hold_counts)},
    }


def build_sidecar_prefix(
    *,
    legs: Iterable[Mapping[str, Any]],
    transactions: Iterable[Mapping[str, Any]],
    registry_index: Mapping[tuple[str, str, int, str], LifecycleWindow],
    cutoff_date: str,
) -> SidecarBuild:
    """Filter entries and transactions at cutoff using the frozen registry.

    The registry still includes completed future lifecycle windows, so this
    checks post-hoc identity stability, not decision-time causal replay.
    """

    visible_legs = [leg for leg in legs if str(leg.get("entry_date")) <= cutoff_date]
    visible_tx = [tx for tx in transactions if str(tx.get("trade_date")) <= cutoff_date]
    return build_sidecar(
        legs=visible_legs,
        transactions=visible_tx,
        registry_index=registry_index,
        exit_visibility_cutoff=cutoff_date,
    )


def canonical_json_bytes(payload: Any) -> bytes:
    """Stable byte serialisation used for replay comparison."""

    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def economic_signals_jsonl(rows: Sequence[Mapping[str, Any]]) -> bytes:
    """Deterministic JSONL payload for the economic-signal rows."""

    chunks = [
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for row in rows
    ]
    return (("\n".join(chunks) + "\n") if chunks else "").encode("utf-8")


def holds_json(build: SidecarBuild) -> bytes:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "hold_codes": list(HOLD_CODES),
        "holds": build.holds,
        "hold_counts": build.counts["hold_counts"],
    }
    return canonical_json_bytes(payload)


def classification_counts_json(build: SidecarBuild) -> bytes:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "counts": build.counts,
        "excluded_field_names": sorted(build.excluded_field_names),
    }
    return canonical_json_bytes(payload)
