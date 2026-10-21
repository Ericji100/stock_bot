"""Restore the accepted research artifacts from the 2026-10-20 cleanup ZIP.

The cleanup archive contains roughly three million entries, so Python's
``zipfile.ZipFile`` index would be unnecessarily memory-hungry.  This helper
streams the ZIP64 central directory, keeps metadata only for the approved
prefixes below, then extracts and verifies those entries one at a time.

The destination is intentionally under ``docs/research/dual_ma`` so the
accepted, human-readable research packages are versioned independently from
the disposable ``.workdata`` workspace.
"""

from __future__ import annotations

import argparse
import binascii
import hashlib
import json
import os
import shutil
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterator


SELECTIONS = (
    (
        "D_workdata/dev/dual-ma/02-etf-0050-final-strategy-v1-6/",
        "0050/run-chain/v1.6/",
    ),
    (
        "D_workdata/dev/dual-ma/02-etf-0050-final-strategy-v1-7/",
        "0050/run-chain/v1.7/",
    ),
    (
        "D_workdata/reports/dual-ma/etf/0050/final/2006-2026-final-strategy-v1-7/",
        "0050/final-v1.7/",
    ),
    (
        "D_workdata/reports/dual-ma/etf/0050/final/2006-2026-final-strategy-v1-6/",
        "0050/source-v1.6/",
    ),
    (
        "D_workdata/reports/dual-ma/etf/0050/2017-2026-base-v1/",
        "0050/accepted-history/2017-2026-base-v1/",
    ),
    (
        "D_workdata/reports/dual-ma/etf/0050/base-v4/",
        "0050/accepted-history/base-v4/",
    ),
    (
        "D_workdata/reports/dual-ma/etf/0050/experiments/2006-2026-fixed-methods-v1/",
        "0050/accepted-history/2006-2026-fixed-methods-v1/",
    ),
    (
        "D_workdata/reports/dual-ma/etf/0050/experiments/2017-2026-ma21-fixed-stop-macd-v1-1/",
        "0050/accepted-history/2017-2026-ma21-fixed-stop-macd-v1-1/",
    ),
    (
        "D_workdata/reports/dual-ma/etf/00635u/experiments/2015-2026-kd-cycle-ma21-breakout-v3-0/",
        "00635u/kd-cycle-pivot-stop/",
    ),
    (
        "D_workdata/reports/dual-ma/experiments/kd-2r-death-cross-source-strategy-v2/",
        "source-strategy/kd-2r-death-cross-source-strategy-v2/",
    ),
    (
        "D_workdata/reports/dual-ma/analysis/first-round-five-candidates-v2/",
        "source-strategy/legacy-five-candidates/",
    ),
    (
        "D_workdata/reports/dual-ma/analysis/first-round-five-candidates-top10-winners-v1/",
        "source-strategy/legacy-five-candidates-top10/",
    ),
    (
        "D_workdata/reports/dual-ma/experiments/phase1-source-strategy-v23-b/",
        "source-strategy/v23/",
    ),
    (
        "D_workdata/reports/dual-ma/experiments/phase1-source-strategy-regime-v25-b/",
        "source-strategy/regime-v25/",
    ),
)

SINGLE_FILES = {
    "D_workdata/reports/dual-ma/analysis/2022-2026-candidate-ranking-v1.md":
        "source-strategy/kd-2r-death-cross-source-strategy-v2/2022-2026-candidate-ranking-v1.md",
    "D_workdata/reports/dual-ma/analysis/2022-2026-candidate-ranking-v1.manifest.json":
        "source-strategy/kd-2r-death-cross-source-strategy-v2/2022-2026-candidate-ranking-v1.manifest.json",
    "D_workdata/reports/dual-ma/experiments/phase1-source-strategy-v23-bc-exact-rebuild-audit.json":
        "source-strategy/v23/qa/exact-rebuild-audit.json",
    "D_workdata/reports/dual-ma/experiments/phase1-source-strategy-v23-v1-chart-qa.png":
        "source-strategy/v23/qa/v1-chart-qa.png",
    "D_workdata/reports/dual-ma/experiments/phase1-source-strategy-v23-v4-chart-qa.png":
        "source-strategy/v23/qa/v4-chart-qa.png",
    "D_workdata/reports/dual-ma/experiments/phase1-source-strategy-regime-v25-bc-exact-rebuild-audit.json":
        "source-strategy/regime-v25/qa/exact-rebuild-audit.json",
}


@dataclass(frozen=True)
class Entry:
    archive_path: str
    output_path: str
    flags: int
    method: int
    crc32: int
    compressed_size: int
    size: int
    local_header_offset: int


def _zip64_directory(stream: BinaryIO) -> tuple[int, int]:
    stream.seek(0, os.SEEK_END)
    end = stream.tell()
    tail_size = min(end, 131_072)
    stream.seek(end - tail_size)
    tail = stream.read(tail_size)
    index = tail.rfind(b"PK\x05\x06")
    if index < 0:
        raise ValueError("ZIP end-of-central-directory record not found")
    eocd_offset = end - tail_size + index
    eocd = tail[index : index + 22]
    entries = struct.unpack_from("<H", eocd, 10)[0]
    directory_offset = struct.unpack_from("<I", eocd, 16)[0]
    if entries != 0xFFFF and directory_offset != 0xFFFFFFFF:
        return entries, directory_offset

    stream.seek(eocd_offset - 20)
    locator = stream.read(20)
    if locator[:4] != b"PK\x06\x07":
        raise ValueError("ZIP64 locator not found")
    zip64_offset = struct.unpack_from("<Q", locator, 8)[0]
    stream.seek(zip64_offset)
    zip64_eocd = stream.read(56)
    if zip64_eocd[:4] != b"PK\x06\x06":
        raise ValueError("ZIP64 end-of-central-directory record not found")
    return (
        struct.unpack_from("<Q", zip64_eocd, 32)[0],
        struct.unpack_from("<Q", zip64_eocd, 48)[0],
    )


def _decode_name(raw: bytes, flags: int) -> str:
    encodings = ("utf-8",) if flags & 0x800 else ("cp950", "cp437")
    for encoding in encodings:
        try:
            return raw.decode(encoding).replace("\\", "/")
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace").replace("\\", "/")


def _zip64_values(
    extra: bytes, size: int, compressed_size: int, local_offset: int
) -> tuple[int, int, int]:
    if 0xFFFFFFFF not in (size, compressed_size, local_offset):
        return size, compressed_size, local_offset
    cursor = 0
    while cursor + 4 <= len(extra):
        field_id, field_size = struct.unpack_from("<HH", extra, cursor)
        data = extra[cursor + 4 : cursor + 4 + field_size]
        cursor += 4 + field_size
        if field_id != 1:
            continue
        value_cursor = 0
        if size == 0xFFFFFFFF:
            size = struct.unpack_from("<Q", data, value_cursor)[0]
            value_cursor += 8
        if compressed_size == 0xFFFFFFFF:
            compressed_size = struct.unpack_from("<Q", data, value_cursor)[0]
            value_cursor += 8
        if local_offset == 0xFFFFFFFF:
            local_offset = struct.unpack_from("<Q", data, value_cursor)[0]
        return size, compressed_size, local_offset
    raise ValueError("required ZIP64 extended information is missing")


def _output_name(archive_name: str) -> str | None:
    direct = SINGLE_FILES.get(archive_name)
    if direct is not None:
        return direct
    for source_prefix, target_prefix in SELECTIONS:
        if archive_name.startswith(source_prefix):
            return target_prefix + archive_name[len(source_prefix) :]
    return None


def selected_entries(stream: BinaryIO) -> Iterator[Entry]:
    count, directory_offset = _zip64_directory(stream)
    stream.seek(directory_offset)
    for index in range(count):
        header = stream.read(46)
        if len(header) != 46 or header[:4] != b"PK\x01\x02":
            raise ValueError(f"invalid central-directory record at entry {index}")
        flags = struct.unpack_from("<H", header, 8)[0]
        method = struct.unpack_from("<H", header, 10)[0]
        crc32 = struct.unpack_from("<I", header, 16)[0]
        compressed_size = struct.unpack_from("<I", header, 20)[0]
        size = struct.unpack_from("<I", header, 24)[0]
        name_size, extra_size, comment_size = struct.unpack_from("<HHH", header, 28)
        local_offset = struct.unpack_from("<I", header, 42)[0]
        raw_name = stream.read(name_size)
        extra = stream.read(extra_size)
        stream.seek(comment_size, os.SEEK_CUR)
        archive_name = _decode_name(raw_name, flags)
        output_name = _output_name(archive_name)
        if output_name is None or archive_name.endswith("/"):
            continue
        size, compressed_size, local_offset = _zip64_values(
            extra, size, compressed_size, local_offset
        )
        yield Entry(
            archive_path=archive_name,
            output_path=output_name,
            flags=flags,
            method=method,
            crc32=crc32,
            compressed_size=compressed_size,
            size=size,
            local_header_offset=local_offset,
        )


def _safe_target(root: Path, relative_name: str) -> Path:
    pure = PurePosixPath(relative_name)
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"unsafe archive path: {relative_name}")
    target = root.joinpath(*pure.parts).resolve()
    target.relative_to(root.resolve())
    return target


def extract_entry(stream: BinaryIO, entry: Entry, root: Path) -> dict[str, object]:
    stream.seek(entry.local_header_offset)
    header = stream.read(30)
    if len(header) != 30 or header[:4] != b"PK\x03\x04":
        raise ValueError(f"invalid local header: {entry.archive_path}")
    name_size, extra_size = struct.unpack_from("<HH", header, 26)
    stream.seek(name_size + extra_size, os.SEEK_CUR)

    target = _safe_target(root, entry.output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    digest = hashlib.sha256()
    crc = 0
    written = 0
    remaining = entry.compressed_size
    decompressor = zlib.decompressobj(-15) if entry.method == 8 else None
    if entry.method not in (0, 8):
        raise ValueError(f"unsupported ZIP method {entry.method}: {entry.archive_path}")

    with partial.open("wb") as output:
        while remaining:
            chunk = stream.read(min(1 << 20, remaining))
            if not chunk:
                raise EOFError(f"truncated compressed data: {entry.archive_path}")
            remaining -= len(chunk)
            data = decompressor.decompress(chunk) if decompressor else chunk
            if data:
                output.write(data)
                digest.update(data)
                crc = binascii.crc32(data, crc)
                written += len(data)
        if decompressor:
            data = decompressor.flush()
            output.write(data)
            digest.update(data)
            crc = binascii.crc32(data, crc)
            written += len(data)

    if written != entry.size or (crc & 0xFFFFFFFF) != entry.crc32:
        partial.unlink(missing_ok=True)
        raise ValueError(f"size/CRC mismatch after extraction: {entry.archive_path}")
    partial.replace(target)
    return {
        "archive_path": entry.archive_path,
        "output_path": entry.output_path,
        "size": written,
        "crc32": f"{entry.crc32:08x}",
        "sha256": digest.hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--allow-existing",
        action="store_true",
        help="add or refresh only approved files in an existing curated root",
    )
    args = parser.parse_args()
    existed = args.output_root.exists()
    if existed and not args.allow_existing:
        raise SystemExit(f"refusing to overwrite existing output: {args.output_root}")
    args.output_root.mkdir(parents=True, exist_ok=True)

    restored: list[dict[str, object]] = []
    try:
        with args.archive.open("rb") as stream:
            entries = list(selected_entries(stream))
            if not entries:
                raise ValueError("no approved archive entries were found")
            for entry in entries:
                restored.append(extract_entry(stream, entry, args.output_root))
    except Exception:
        if not existed:
            shutil.rmtree(args.output_root, ignore_errors=True)
        raise

    manifest = {
        "schema": "CURATED_RESEARCH_ARCHIVE_RESTORE_V1",
        "source_archive": str(args.archive.resolve()),
        "source_archive_size": args.archive.stat().st_size,
        "file_count": len(restored),
        "logical_bytes": sum(int(row["size"]) for row in restored),
        "files": sorted(restored, key=lambda row: str(row["output_path"])),
    }
    (args.output_root / "RESTORE_MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in manifest.items() if k != "files"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
