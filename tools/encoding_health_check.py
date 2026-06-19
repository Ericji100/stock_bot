from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import subprocess
import sys


BINARY_EXTENSIONS = {
    ".7z",
    ".db",
    ".dll",
    ".exe",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".pkl",
    ".png",
    ".pyc",
    ".sqlite",
    ".xls",
    ".xlsx",
    ".zip",
}

EXCLUDED_PARTS = {
    ".git",
    ".runtime",
    ".venv",
    "__pycache__",
    "node_modules",
    "runtime",
    "venv",
}

MOJIBAKE_MARKERS = (
    "�",
    "é¦",
    "è‡",
    "ç",
    "åˆ",
    "æ˜",
    "æ‰",
    "嚗",
    "憿",
    "蝣",
    "蝑",
    "銝",
    "摨",
    "Ã",
    "Â",
    "â€™",
    "â€œ",
    "â€",
    "â€“",
    "â€”",
    "ï»¿",
)

ALLOWLIST_PREFIXES = (
    "tests/",
)

ALLOWLIST_FILES = {
    "research_center/source_text_cleaner.py",
    "research_center/topic_source_sync_service.py",
    "tools/encoding_health_check.py",
}


@dataclass(frozen=True)
class Utf8DecodeIssue:
    path: str
    position: int
    reason: str


@dataclass(frozen=True)
class MarkerIssue:
    path: str
    marker: str
    line: int
    preview: str
    allowed: bool = False


@dataclass
class EncodingHealthReport:
    scanned_files: int = 0
    skipped_files: int = 0
    bom_files: list[str] = field(default_factory=list)
    utf8_errors: list[Utf8DecodeIssue] = field(default_factory=list)
    marker_issues: list[MarkerIssue] = field(default_factory=list)

    @property
    def allowed_marker_count(self) -> int:
        return sum(1 for issue in self.marker_issues if issue.allowed)

    @property
    def suspicious_marker_issues(self) -> list[MarkerIssue]:
        return [issue for issue in self.marker_issues if not issue.allowed]

    @property
    def ok(self) -> bool:
        return not self.utf8_errors and not self.suspicious_marker_issues


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    root = Path(argv[0]).resolve() if argv else Path.cwd()
    report = scan_git_tracked_files(root)
    print(format_report(report))
    return 0 if report.ok else 1


def scan_git_tracked_files(root: Path) -> EncodingHealthReport:
    paths = git_tracked_paths(root)
    return scan_files(root, paths)


def git_tracked_paths(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", "ls-files"],
        cwd=root,
        text=True,
        encoding="utf-8",
        check=True,
        capture_output=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def scan_files(root: Path, paths: list[str]) -> EncodingHealthReport:
    report = EncodingHealthReport()
    for raw_path in paths:
        rel_path = normalize_path(raw_path)
        if should_skip_path(rel_path):
            report.skipped_files += 1
            continue

        path = root / rel_path
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            report.skipped_files += 1
            continue

        if b"\x00" in data:
            report.skipped_files += 1
            continue

        report.scanned_files += 1
        if data.startswith(b"\xef\xbb\xbf"):
            report.bom_files.append(rel_path)

        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            report.utf8_errors.append(Utf8DecodeIssue(rel_path, exc.start, exc.reason))
            continue

        report.marker_issues.extend(find_marker_issues(rel_path, text))

    return report


def find_marker_issues(rel_path: str, text: str) -> list[MarkerIssue]:
    allowed = is_marker_allowed(rel_path)
    issues: list[MarkerIssue] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for marker in MOJIBAKE_MARKERS:
            if marker in line:
                issues.append(
                    MarkerIssue(
                        path=rel_path,
                        marker=marker,
                        line=line_number,
                        preview=line.strip()[:160],
                        allowed=allowed,
                    )
                )
    return issues


def should_skip_path(rel_path: str) -> bool:
    path = Path(rel_path)
    if path.suffix.lower() in BINARY_EXTENSIONS:
        return True
    return any(part in EXCLUDED_PARTS for part in path.parts)


def is_marker_allowed(rel_path: str) -> bool:
    normalized = normalize_path(rel_path)
    if normalized in ALLOWLIST_FILES:
        return True
    return any(normalized.startswith(prefix) for prefix in ALLOWLIST_PREFIXES)


def normalize_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def format_report(report: EncodingHealthReport) -> str:
    lines = [
        "Encoding health check",
        f"- scanned_files: {report.scanned_files}",
        f"- skipped_files: {report.skipped_files}",
        f"- utf8_errors: {len(report.utf8_errors)}",
        f"- suspicious_mojibake_files: {len({issue.path for issue in report.suspicious_marker_issues})}",
        f"- suspicious_mojibake_hits: {len(report.suspicious_marker_issues)}",
        f"- allowed_mojibake_hits: {report.allowed_marker_count}",
        f"- bom_files: {len(report.bom_files)}",
    ]

    if report.utf8_errors:
        lines.append("")
        lines.append("UTF-8 decode errors:")
        for issue in report.utf8_errors:
            lines.append(f"- {issue.path}:{issue.position} {issue.reason}")

    if report.suspicious_marker_issues:
        lines.append("")
        lines.append("Suspicious mojibake markers:")
        for issue in report.suspicious_marker_issues:
            lines.append(f"- {issue.path}:{issue.line} marker={issue.marker!r} text={issue.preview}")

    if report.bom_files:
        lines.append("")
        lines.append("UTF-8 BOM files:")
        for path in report.bom_files[:50]:
            lines.append(f"- {path}")
        if len(report.bom_files) > 50:
            lines.append(f"- ... {len(report.bom_files) - 50} more")

    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
