"""Engineering replay for the rolling approximate daily selection (v0.2).

This module owns the 02 side of the four-day engineering gate described by the
rolling approximate selection contract (v0.2). It reads only the twelve frozen,
allow-listed daily selection manifests, derives the monitor window from the
bound official TWSE/TPEX calendar, and refuses to invent anything the frozen
contracts do not state.

Causal rules enforced here:

* A selection day D may only be monitored or transacted from the next official
  trading session, derived from the frozen official calendar rather than from
  weekday arithmetic or an index proxy.
* Only groups marked SUCCESS may contribute candidates; every HOLD group must
  carry an empty candidate list.
* The manifests stay approximate research: research_only=true,
  point_in_time_verified=false, source_revision_unknown=true.

No performance quantity is read, computed or emitted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence


MODULE_VERSION = "APPROX_DAILY_ENGINEERING_REPLAY_V0_1"
SELECTION_SCHEMA = "dual-ma-approx-selection-daily-v0.2"
READBACK_SCHEMA = "dual-ma-approx-daily-input-readback-v0.1"
CALENDAR_ID = "dual-ma-official-calendar#a468084f0a95f961"
CALENDAR_CONTENT_SHA256 = "a468084f0a95f961147fa829b16d3d64fd67ad4a06cd151e1c6599dd79d833f6"

APPROX_DAILY_ROOT = "data/dual_ma/approx_research_v0_2/daily"
CALENDAR_RELATIVE_PATH = (
    "data/dual_ma/v2/official_trading_calendar/2021-2022/"
    "dual-ma-official-trading-calendar#a468084f0a95f961.json"
)

JAN04_COMMIT = "c1ae3da3abdbe09a7207cf11eb1349b55709467b"
ROLLING_COMMIT = "e282fa160d2db5d911d648161bfdb4d232d3fcf0"

# (selection_date, scenario, manifest_tag, file_sha256, content_sha256, git_commit)
ALLOWLIST: tuple[tuple[str, str, str, str, str, str], ...] = (
    ("2022-01-04", "L10", "af620188aa1bd261",
     "5478a1661e28c599c83b5c1cb5a3ab7059a3b7d14c71b3f83470d3ab15a4f1d5",
     "af620188aa1bd26105ae96cbe7a8e78db43b76d9a7b55b4799ae0b8d2a8fca8c", JAN04_COMMIT),
    ("2022-01-04", "L05", "e2a6a5461a289c94",
     "890d3e5dcfecec93f8ed29ed49f7a679aff7f7c56ddabcb0363c48792b766741",
     "e2a6a5461a289c940d7c5e46d7069456c634328febcecc99d5dc1c83cde84ca8", JAN04_COMMIT),
    ("2022-01-04", "L00", "fd6ff9564b51994b",
     "36f8a3cb6f711a65db1c2e21f50a1946be1397826d09d36d31c0cfc6d0f47de8",
     "fd6ff9564b51994b5c7749439829b1601cb11c4f45018dc68586ccaac19f059d", JAN04_COMMIT),
    ("2022-01-05", "L10", "89c109f907c7f8b9",
     "04b4c67ac408605863f9dd8cefebf5d2918c2a3fd6c7b09fdb27f654dea7244b",
     "89c109f907c7f8b962f098d7c51f7fc4b13eb4d0a7a4d1ba6b61a4e3e586ebfd", ROLLING_COMMIT),
    ("2022-01-05", "L05", "b139fa85d1acc5a2",
     "141a83df775d1ca41ff44f69eb3bc908b381cb9f1b21cf2b41804690be2c0bb2",
     "b139fa85d1acc5a2b110bab6db0a6934b4198b18090a877e45ea0a489b616a28", ROLLING_COMMIT),
    ("2022-01-05", "L00", "5cd6ed0d1c96d82e",
     "24777167b70fd3cc1ad13dd9fc09c4ff4de9f3e164094e10e0040317c77be682",
     "5cd6ed0d1c96d82ec0cab1bffa08bdf55f66be4786a825275d729deaa2f8064a", ROLLING_COMMIT),
    ("2022-01-06", "L10", "693b17674e4ade8d",
     "f4db097c16b30c5d5052e10a36f6a0f7187121fbd8a2a50033cc76144d2ce5e1",
     "693b17674e4ade8d4c6637a903d29418781609344f9cf9bf3c0d8efc31220dca", ROLLING_COMMIT),
    ("2022-01-06", "L05", "33934fc8b40e20fc",
     "14ce61b1577109715015c4e818b9ccdcde6f62db5d8acb51b5b9913e42a0af7e",
     "33934fc8b40e20fc761c19814cc893a3a8d2cc531aee834380cd1363649f1ee8", ROLLING_COMMIT),
    ("2022-01-06", "L00", "2ba4297e0a0fec0c",
     "fecddddc4c5bf242a727cbda2c55142522ebb7097d58272fd24154f23076c073",
     "2ba4297e0a0fec0c928187d6ee0df3eb0d0b502dcd884f1dcc6374eb13a93dea", ROLLING_COMMIT),
    ("2022-01-07", "L10", "635d47a7b82d19bb",
     "74cc72104845cba131f3abef2556d2917cc0d57ab33a72f97215851027b3fced",
     "635d47a7b82d19bbc0d44c3029b6e18b3a7bd658551ba540d5dc95cd84bf9e32", ROLLING_COMMIT),
    ("2022-01-07", "L05", "e570c68e47681587",
     "da1525a37ff72a13439a9841fa0935873f5032ef7148f08b4f5e2bccbc8eb5a5",
     "e570c68e47681587528e64381b08f66700f6aae4010c2512e87ee40b8540283a", ROLLING_COMMIT),
    ("2022-01-07", "L00", "b8009580d39f143d",
     "1f5a6e9d0dc3b1ad7ea2543e54bae669626abd2c3d7ba10cc879df0bb613b3c0",
     "b8009580d39f143d89cc0e8a776731ed4c4a3301ef0a62f74d586c7c6e77f337", ROLLING_COMMIT),
)

REQUIRED_FLAGS: Mapping[str, Any] = {
    "research_only": True,
    "point_in_time_verified": False,
    "source_revision_unknown": True,
}

# Evidence keys whose referenced file must be resolvable from the workdata root.
SOURCE_IDENTITY_EVIDENCE_KEYS = ("enriched_preflight", "market_identity", "route_lineage_sidecar")
# Only the enriched preflight is per-selection-day; the route lineage sidecar is
# an as-of artefact that the rolling days legitimately share, and market identity
# is a shared universe file.
DATE_SCOPED_EVIDENCE_KEYS = ("enriched_preflight",)


class ApproxReplayError(RuntimeError):
    """Fail-closed error carrying a stable reason code."""

    code = "APPROX_REPLAY_ERROR"


class ManifestNotAllowlisted(ApproxReplayError):
    code = "MANIFEST_NOT_ALLOWLISTED"


class ManifestIntegrityError(ApproxReplayError):
    code = "MANIFEST_INTEGRITY_FAILED"


class CalendarIntegrityError(ApproxReplayError):
    code = "CALENDAR_INTEGRITY_FAILED"


class CandidateLeakError(ApproxReplayError):
    code = "HOLD_GROUP_CANDIDATE_LEAK"


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


@dataclass(frozen=True)
class OfficialCalendar:
    """Frozen official trading calendar with content-addressed identity."""

    path: str
    file_sha256: str
    content_sha256: str
    open_dates: tuple[date, ...]

    def next_session(self, day: date, *, field: str) -> date:
        after = [item for item in self.open_dates if item > day]
        if not after:
            raise CalendarIntegrityError(
                f"{field}: official calendar has no session after {day.isoformat()}"
            )
        return after[0]

    def session_on_or_before(self, day: date) -> date | None:
        candidates = [item for item in self.open_dates if item <= day]
        return candidates[-1] if candidates else None


def load_official_calendar(
    path: Path, *, expected_content_sha256: str = CALENDAR_CONTENT_SHA256
) -> OfficialCalendar:
    path = Path(path)
    if not path.is_file():
        raise CalendarIntegrityError(f"official calendar is missing: {path}")
    raw = path.read_bytes()
    doc = json.loads(raw.decode("utf-8-sig"))
    core = {
        key: value
        for key, value in doc.items()
        if key not in {"content_sha256", "calendar_id"}
    }
    if canonical_sha256(core) != doc.get("content_sha256"):
        raise CalendarIntegrityError("official calendar content hash does not match its payload")
    if doc.get("content_sha256") != expected_content_sha256:
        raise CalendarIntegrityError("official calendar differs from the frozen contract binding")
    expected_id = f"dual-ma-official-calendar#{str(doc.get('content_sha256'))[:16]}"
    if doc.get("calendar_id") != expected_id:
        raise CalendarIntegrityError("official calendar identity does not match its content hash")
    if doc.get("performance_read_or_computed") is not False:
        raise CalendarIntegrityError("official calendar must not have read performance")
    open_dates = tuple(date.fromisoformat(value) for value in doc.get("open_dates_union") or ())
    if not open_dates or tuple(sorted(set(open_dates))) != open_dates:
        raise CalendarIntegrityError("official calendar open dates are empty, unordered or duplicated")
    if canonical_sha256(doc.get("open_dates_union")) != doc.get("open_dates_union_sha256"):
        raise CalendarIntegrityError("official calendar union hash does not match its open dates")
    return OfficialCalendar(
        path=str(path),
        file_sha256=sha256_bytes(raw),
        content_sha256=str(doc["content_sha256"]),
        open_dates=open_dates,
    )


def manifest_name(scenario: str, tag: str) -> str:
    return f"dual-ma-approx-selection-daily@{scenario}#{tag}.json"


def allowlist_names() -> set[str]:
    return {manifest_name(scenario, tag) for _, scenario, tag, _, _, _ in ALLOWLIST}


@dataclass(frozen=True)
class VerifiedDailySelection:
    selection_date: date
    scenario: str
    manifest_name: str
    manifest_path: str
    file_sha256: str
    content_sha256: str
    git_commit: str
    next_official_trading_day: date
    calendar_next_session: date
    available_groups: tuple[str, ...]
    hold_groups: tuple[str, ...]
    candidate_codes: tuple[str, ...]
    group_candidate_counts: Mapping[str, int]
    program_statuses: Mapping[str, str]
    success_group_union_size: int
    declared_source_date_checks: int
    declared_source_date_violations: tuple[Mapping[str, Any], ...]
    declared_source_date_unresolved: tuple[Mapping[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "selection_date": self.selection_date,
            "scenario": self.scenario,
            "manifest_name": self.manifest_name,
            "manifest_path": self.manifest_path,
            "file_sha256": self.file_sha256,
            "content_sha256": self.content_sha256,
            "git_commit": self.git_commit,
            "next_official_trading_day": self.next_official_trading_day,
            "calendar_next_session": self.calendar_next_session,
            "monitor_effective_from": self.next_official_trading_day,
            "same_day_monitoring_allowed": False,
            "available_groups": list(self.available_groups),
            "hold_groups": list(self.hold_groups),
            "group_candidate_counts": dict(self.group_candidate_counts),
            "candidate_count": len(self.candidate_codes),
            "candidate_codes_sha256": canonical_sha256(list(self.candidate_codes)),
            "success_group_union_size": self.success_group_union_size,
            "declared_source_date_checks": self.declared_source_date_checks,
        }


def _verify_declared_source_dates(
    *,
    doc: Mapping[str, Any],
    expected_name: str,
    day: date,
    scenario: str,
    calendar_next: date,
    calendar: OfficialCalendar,
    workdata_root: Path,
) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate only what the manifest declares about its own source dates.

    Nothing here proves how a price provider truncates events; that belongs to
    the engine slice.  Unprovable declarations are reported as unresolved so the
    gate can hold instead of claiming more than the artefact shows.
    """

    checks = 0
    violations: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    iso_day = day.isoformat()

    def record(kind: str, field: str, detail: str, *, soft: bool = False) -> None:
        entry = {"manifest": expected_name, "program": current_program, "field": field, "detail": detail}
        (unresolved if soft else violations).append(entry)
        del kind

    current_program = ""
    vintage = doc.get("vintage")
    checks += 1
    if not isinstance(vintage, Mapping) or "selection_date" not in vintage:
        record(
            "vintage",
            "vintage.selection_date",
            "manifest declares no vintage selection date to verify",
            soft=True,
        )
    elif vintage.get("selection_date") != iso_day:
        record("vintage", "vintage.selection_date", f"{vintage.get('selection_date')!r} != {iso_day!r}")

    programs = doc.get("programs") or {}
    for name in doc.get("program_order") or ():
        current_program = str(name)
        evidence = (programs[name] or {}).get("evidence")
        if not isinstance(evidence, Mapping):
            unresolved.append(
                {
                    "manifest": expected_name,
                    "program": current_program,
                    "field": "evidence",
                    "detail": "program carries no evidence block to verify",
                }
            )
            continue
        for field, expected in (
            ("selection_date", iso_day),
            ("scenario", scenario),
            ("next_official_trading_day", calendar_next.isoformat()),
            ("program", current_program),
        ):
            checks += 1
            if evidence.get(field) != expected:
                record("evidence", f"evidence.{field}", f"{evidence.get(field)!r} != {expected!r}")
        checks += 1
        coverage_from = evidence.get("price_coverage_from")
        if coverage_from is None:
            record("evidence", "evidence.price_coverage_from", "missing", soft=True)
        elif str(coverage_from) > iso_day:
            record(
                "evidence",
                "evidence.price_coverage_from",
                f"{coverage_from!r} is after {iso_day!r}",
            )

        window = evidence.get("chip_window")
        if isinstance(window, Mapping):
            checks += 1
            required = [item.isoformat() for item in calendar.open_dates if item <= day][-60:]
            dates = list(window.get("dates") or ())
            if len(required) < 60:
                record(
                    "chip_window",
                    "chip_window.dates",
                    "official calendar cannot supply 60 sessions up to the selection day",
                    soft=True,
                )
            else:
                if dates != required:
                    record("chip_window", "chip_window.dates", "does not equal the 60 official sessions ending on the selection day")
                if window.get("count") != 60 or len(dates) != len(set(dates)):
                    record("chip_window", "chip_window.count", f"count={window.get('count')!r} dates={len(dates)}")
                if window.get("first") != required[0] or window.get("last") != iso_day:
                    record("chip_window", "chip_window.bounds", f"first={window.get('first')!r} last={window.get('last')!r}")
                if any(value not in {item.isoformat() for item in calendar.open_dates} for value in dates):
                    record("chip_window", "chip_window.dates", "contains a non-trading day")

        for key in SOURCE_IDENTITY_EVIDENCE_KEYS:
            item = evidence.get(key)
            if not isinstance(item, Mapping):
                continue
            checks += 1
            relative = str(item.get("path") or "")
            content_hash = str(item.get("content_sha256") or item.get("sha256") or "")
            file_hash = str(item.get("file_sha256") or content_hash)
            if not relative or not file_hash:
                record("identity", f"evidence.{key}", "path or hash missing")
                continue
            date_scoped = key in DATE_SCOPED_EVIDENCE_KEYS
            if date_scoped and content_hash and not relative.endswith("#" + content_hash[:16] + ".json"):
                record(
                    "identity",
                    f"evidence.{key}.path",
                    f"path tag does not match content hash {content_hash[:16]}",
                )
            if date_scoped and iso_day not in relative:
                record("identity", f"evidence.{key}.path", f"path does not carry the selection date {iso_day}")
            candidate = Path(workdata_root) / relative
            if not candidate.is_file():
                record("identity", f"evidence.{key}.path", "referenced file is absent", soft=True)
            elif sha256_file(candidate) != file_hash:
                record("identity", f"evidence.{key}.sha256", "referenced file hash mismatch")
    return checks, violations, unresolved


def verify_daily_manifest(
    *,
    manifest_path: Path,
    selection_date: str,
    scenario: str,
    tag: str,
    file_sha256: str,
    content_sha256: str,
    git_commit: str,
    calendar: OfficialCalendar,
    workdata_root: Path,
    frozen_allowlist: Sequence[tuple[str, str, str, str, str, str]] = ALLOWLIST,
) -> VerifiedDailySelection:
    """Verify one allow-listed manifest against every frozen binding."""

    manifest_path = Path(manifest_path)
    if not manifest_path.is_file():
        raise ManifestIntegrityError(f"allow-listed manifest is missing: {manifest_path}")
    expected_name = manifest_name(scenario, tag)
    if manifest_path.name != expected_name:
        raise ManifestNotAllowlisted(
            f"manifest file name is not the allow-listed artefact: {manifest_path.name}"
        )
    if expected_name not in {manifest_name(entry[1], entry[2]) for entry in frozen_allowlist}:
        raise ManifestNotAllowlisted(
            f"manifest {expected_name} is outside the frozen allow list"
        )
    raw = manifest_path.read_bytes()
    actual_file = sha256_bytes(raw)
    if actual_file != file_sha256:
        raise ManifestIntegrityError(f"file SHA-256 mismatch for {expected_name}")
    doc = json.loads(raw.decode("utf-8-sig"))
    core = {key: value for key, value in doc.items() if key != "content_sha256"}
    recomputed = canonical_sha256(core)
    if recomputed != doc.get("content_sha256"):
        raise ManifestIntegrityError(f"declared content hash does not match payload: {expected_name}")
    if doc.get("content_sha256") != content_sha256:
        raise ManifestIntegrityError(f"content SHA-256 differs from the allow list: {expected_name}")
    if doc.get("schema_version") != SELECTION_SCHEMA:
        raise ManifestIntegrityError(f"unexpected schema for {expected_name}")
    if doc.get("selection_date") != selection_date or doc.get("scenario") != scenario:
        raise ManifestIntegrityError(f"selection date or scenario mismatch for {expected_name}")
    if doc.get("git_commit") != git_commit:
        raise ManifestIntegrityError(f"git commit mismatch for {expected_name}")
    for key, expected in REQUIRED_FLAGS.items():
        if doc.get(key) is not expected:
            raise ManifestIntegrityError(f"{key} must be {expected!r} for {expected_name}")

    day = date.fromisoformat(selection_date)
    declared_next = date.fromisoformat(str(doc.get("next_official_trading_day")))
    calendar_next = calendar.next_session(day, field=f"{expected_name}.next_official_trading_day")
    if declared_next != calendar_next:
        raise ManifestIntegrityError(
            f"{expected_name}: next official trading day {declared_next} disagrees with the "
            f"official calendar {calendar_next}"
        )
    if declared_next <= day:
        raise ManifestIntegrityError(
            f"{expected_name}: monitor window must start strictly after the selection day"
        )

    programs = doc.get("programs") or {}
    program_order = tuple(doc.get("program_order") or ())
    available = tuple(doc.get("available_groups") or ())
    if set(program_order) != set(programs):
        raise ManifestIntegrityError(f"{expected_name}: program order does not match programs")
    group_counts: dict[str, int] = {}
    union: list[str] = []
    success_union: set[str] = set()
    for name in program_order:
        body = programs[name] or {}
        codes = [str(code) for code in body.get("candidate_codes") or ()]
        group_counts[name] = len(codes)
        if name in available:
            if body.get("status") != "SUCCESS":
                raise ManifestIntegrityError(
                    f"{expected_name}: available group {name} is not SUCCESS"
                )
            success_union.update(codes)
        else:
            if body.get("status") == "SUCCESS":
                raise ManifestIntegrityError(
                    f"{expected_name}: group {name} is SUCCESS but absent from available_groups"
                )
            if codes:
                raise CandidateLeakError(
                    f"{expected_name}: non-available group {name} carries {len(codes)} candidates"
                )
    declared_candidates = [str(code) for code in doc.get("candidate_codes") or ()]
    if len(set(declared_candidates)) != len(declared_candidates):
        raise ManifestIntegrityError(f"{expected_name}: candidate union carries duplicates")
    if declared_candidates != sorted(declared_candidates):
        raise ManifestIntegrityError(
            f"{expected_name}: candidate union is not in deterministic order"
        )
    declared_set = set(declared_candidates)
    for name in program_order:
        codes = {str(code) for code in (programs[name] or {}).get("candidate_codes") or ()}
        if not codes <= declared_set:
            raise CandidateLeakError(
                f"{expected_name}: group {name} lists candidates outside the daily union"
            )
    if declared_set != success_union:
        extra = sorted(declared_set - success_union)[:5]
        missing = sorted(success_union - declared_set)[:5]
        raise CandidateLeakError(
            f"{expected_name}: candidate union does not equal the SUCCESS-group set union "
            f"(extra={extra} missing={missing})"
        )
    union = declared_candidates
    checks, violations, unresolved = _verify_declared_source_dates(
        doc=doc,
        expected_name=expected_name,
        day=day,
        scenario=scenario,
        calendar_next=calendar_next,
        calendar=calendar,
        workdata_root=Path(workdata_root),
    )
    if violations:
        raise ManifestIntegrityError(
            f"{expected_name}: declared source dates disagree with the frozen bindings: {violations[:3]}"
        )
    return VerifiedDailySelection(
        selection_date=day,
        scenario=scenario,
        manifest_name=expected_name,
        manifest_path=str(manifest_path),
        file_sha256=actual_file,
        content_sha256=str(doc["content_sha256"]),
        git_commit=str(doc["git_commit"]),
        next_official_trading_day=declared_next,
        calendar_next_session=calendar_next,
        available_groups=available,
        hold_groups=tuple(name for name in program_order if name not in available),
        candidate_codes=tuple(union),
        group_candidate_counts=group_counts,
        program_statuses={name: str((programs[name] or {}).get("status")) for name in program_order},
        success_group_union_size=len(success_union),
        declared_source_date_checks=checks,
        declared_source_date_violations=tuple(violations),
        declared_source_date_unresolved=tuple(unresolved),
    )


def inspect_day_directory(
    day_dir: Path,
    *,
    allowlist: Sequence[tuple[str, str, str, str, str, str]] = ALLOWLIST,
) -> dict[str, tuple[str, ...]]:
    """List the allow-listed manifests and the files that stay unread.

    Superseded pilot artefacts legitimately live next to the frozen manifests,
    so their presence is recorded as evidence instead of aborting the run.
    They are never opened; only the allow-listed paths are read.
    """

    day_dir = Path(day_dir)
    if not day_dir.is_dir():
        raise ManifestIntegrityError(f"selection day directory is missing: {day_dir}")
    allowed = {manifest_name(entry[1], entry[2]) for entry in allowlist}
    present = sorted(path.name for path in day_dir.glob("*.json"))
    return {
        "allowlisted": tuple(name for name in present if name in allowed),
        "ignored_non_allowlisted": tuple(name for name in present if name not in allowed),
    }


def load_verified_selections(
    *,
    workdata_root: Path,
    calendar: OfficialCalendar,
    allowlist: Sequence[tuple[str, str, str, str, str, str]] = ALLOWLIST,
    frozen_allowlist: Sequence[tuple[str, str, str, str, str, str]] = ALLOWLIST,
) -> tuple[VerifiedDailySelection, ...]:
    root = Path(workdata_root)
    frozen_ids = {(entry[0], entry[1], entry[2]) for entry in frozen_allowlist}
    expanded = [
        entry for entry in allowlist if (entry[0], entry[1], entry[2]) not in frozen_ids
    ]
    if expanded:
        raise ManifestNotAllowlisted(
            "allow list was expanded beyond the frozen identities: "
            f"{sorted((entry[0], entry[1], entry[2]) for entry in expanded)}"
        )
    for day_dir in sorted({entry[0] for entry in allowlist}):
        inspect_day_directory(root / APPROX_DAILY_ROOT / day_dir, allowlist=allowlist)
    verified: list[VerifiedDailySelection] = []
    for selection_date, scenario, tag, file_sha, content_sha, commit in allowlist:
        verified.append(
            verify_daily_manifest(
                manifest_path=root / APPROX_DAILY_ROOT / selection_date / manifest_name(scenario, tag),
                selection_date=selection_date,
                scenario=scenario,
                tag=tag,
                file_sha256=file_sha,
                content_sha256=content_sha,
                git_commit=commit,
                calendar=calendar,
                workdata_root=root,
                frozen_allowlist=frozen_allowlist,
            )
        )
    return tuple(verified)


def build_readback_manifest(
    *,
    workdata_root: Path,
    calendar: OfficialCalendar,
    selections: Sequence[VerifiedDailySelection],
    allowlist: Sequence[tuple[str, str, str, str, str, str]] = ALLOWLIST,
) -> dict[str, Any]:
    by_day: dict[str, list[dict[str, Any]]] = {}
    for record in selections:
        by_day.setdefault(record.selection_date.isoformat(), []).append(record.to_dict())
    directory_evidence = {
        day: inspect_day_directory(
            Path(workdata_root) / APPROX_DAILY_ROOT / day, allowlist=allowlist
        )
        for day in sorted({entry[0] for entry in allowlist})
    }
    manifest = {
        "schema_version": READBACK_SCHEMA,
        "module_version": MODULE_VERSION,
        "workdata_root": str(Path(workdata_root).resolve()),
        "calendar": {
            "path": calendar.path,
            "file_sha256": calendar.file_sha256,
            "content_sha256": calendar.content_sha256,
            "open_date_count": len(calendar.open_dates),
        },
        "allowlist_count": len(selections),
        "days": {key: value for key, value in sorted(by_day.items())},
        "selection_directories": directory_evidence,
        "candidate_unions": {
            record.selection_date.isoformat() + ":" + record.scenario: {
                "monitor_effective_from": record.next_official_trading_day,
                "candidate_count": len(record.candidate_codes),
                "candidate_codes_sha256": canonical_sha256(list(record.candidate_codes)),
                "available_groups": list(record.available_groups),
                "hold_groups": list(record.hold_groups),
            }
            for record in selections
        },
        "audit": {
            "manifest_count": len(selections),
            "same_day_monitoring_allowed": 0,
            "hold_group_candidates": sum(
                count
                for record in selections
                for name, count in record.group_candidate_counts.items()
                if name in record.hold_groups
            ),
            "invalid_monitor_effective_date_count": sum(
                1
                for record in selections
                if record.next_official_trading_day <= record.selection_date
            ),
            "ignored_non_allowlisted_files": sum(
                len(entry["ignored_non_allowlisted"]) for entry in directory_evidence.values()
            ),
            "declared_source_date_checks": sum(
                record.declared_source_date_checks for record in selections
            ),
            "manifest_declared_source_date_violations": sum(
                len(record.declared_source_date_violations) for record in selections
            ),
            "manifest_declared_source_date_unresolved": sum(
                len(record.declared_source_date_unresolved) for record in selections
            ),
        },
        "gates": {
            "candidate_union_equals_success_group_union": (
                "PASS"
                if all(
                    len(record.candidate_codes) == record.success_group_union_size
                    for record in selections
                )
                else "HOLD"
            ),
            "candidate_union_is_sorted_and_unique": "PASS",
            "monitor_effective_date_after_selection_date": (
                "PASS"
                if all(
                    record.next_official_trading_day > record.selection_date
                    for record in selections
                )
                else "HOLD"
            ),
            "manifest_declared_source_date_violations": (
                "PASS"
                if not any(record.declared_source_date_violations for record in selections)
                else "HOLD"
            ),
            "manifest_declared_source_date_fully_proven": (
                "PASS"
                if not any(record.declared_source_date_unresolved for record in selections)
                else "HOLD"
            ),
        },
        "declared_source_date_unresolved_examples": [
            dict(entry)
            for record in selections
            for entry in record.declared_source_date_unresolved
        ][:10],
        "scope_notes": {
            "manifest_declared_source_date_violations": (
                "Validates only what the frozen manifests declare about their own source dates "
                "(vintage date, per-program evidence identity, chip window against the official "
                "calendar, and referenced evidence file hashes)."
            ),
            "not_covered_here": (
                "How the price provider truncates per-event queries to <= D is an engine-slice "
                "check; this readback makes no claim about it."
            ),
            "invalid_monitor_effective_date_count": (
                "Counts manifests whose declared next official trading day is not after the "
                "selection day. It is not a measure of future source references."
            ),
        },
        "performance_read_or_computed": False,
        "rules_changed": False,
    }
    # Return exactly what will be serialised so the in-memory and on-disk views agree.
    return json.loads(canonical_bytes(manifest).decode("utf-8"))


def run_input_readback(
    *,
    workdata_root: Path,
    output_dir: Path,
    calendar_relative_path: str = CALENDAR_RELATIVE_PATH,
    expected_calendar_content_sha256: str = CALENDAR_CONTENT_SHA256,
    allowlist: Sequence[tuple[str, str, str, str, str, str]] = ALLOWLIST,
    frozen_allowlist: Sequence[tuple[str, str, str, str, str, str]] = ALLOWLIST,
) -> dict[str, Any]:
    """Verify the twelve frozen manifests and emit the readback artefact."""

    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite readback artefact: {output_dir}")
    root = Path(workdata_root)
    calendar = load_official_calendar(
        root / calendar_relative_path, expected_content_sha256=expected_calendar_content_sha256
    )
    selections = load_verified_selections(
        workdata_root=root,
        calendar=calendar,
        allowlist=allowlist,
        frozen_allowlist=frozen_allowlist,
    )
    manifest = build_readback_manifest(
        workdata_root=root, calendar=calendar, selections=selections, allowlist=allowlist
    )
    # Two independent verifications and serialisations must be byte-identical.
    first = canonical_bytes(manifest) + b"\n"
    # Second pass is fully independent: a fresh calendar object drives a fresh
    # verification of the manifests before anything is compared or written.
    second_calendar = load_official_calendar(
        root / calendar_relative_path,
        expected_content_sha256=expected_calendar_content_sha256,
    )
    if second_calendar is calendar:
        raise ApproxReplayError("second replay must not reuse the first calendar object")
    second_selections = load_verified_selections(
        workdata_root=root,
        calendar=second_calendar,
        allowlist=allowlist,
        frozen_allowlist=frozen_allowlist,
    )
    second = canonical_bytes(
        build_readback_manifest(
            workdata_root=root,
            calendar=second_calendar,
            selections=second_selections,
            allowlist=allowlist,
        )
    ) + b"\n"
    if first != second:
        raise ApproxReplayError("readback serialisation is not deterministic")
    output_dir.mkdir(parents=True)
    (output_dir / "input_readback_manifest.json").write_bytes(first)
    digest = sha256_bytes(first)
    (output_dir / "artifact_manifest.json").write_text(
        json.dumps(
            [
                {
                    "path": "input_readback_manifest.json",
                    "sha256": digest,
                    "bytes": len(first),
                }
            ],
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return {**manifest, "output_dir": str(output_dir.resolve()), "artifact_sha256": digest}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdata-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    manifest = run_input_readback(
        workdata_root=args.workdata_root, output_dir=args.output_dir
    )
    print(
        json.dumps(
            {
                "schema_version": manifest["schema_version"],
                "allowlist_count": manifest["allowlist_count"],
                "audit": manifest["audit"],
                "output_dir": manifest["output_dir"],
                "artifact_sha256": manifest["artifact_sha256"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - manual entrypoint
    sys.exit(main())
