from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

TEST_SUITE_MANIFEST_SCHEMA_VERSION = "test_suite_manifest_v1"


@dataclass(frozen=True)
class TestSuiteEntry:
    name: str
    purpose: str
    command: str
    requires_network: bool = False
    requires_ai: bool = False
    manual: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_TEST_SUITES: tuple[TestSuiteEntry, ...] = (
    TestSuiteEntry(
        name="fast_unit",
        purpose="Fast local checks for shared services and pure logic.",
        command=(
            "python -B -m unittest "
            "tests.test_command_runtime_service "
            "tests.test_scheduled_task_service "
            "tests.test_resource_guard_service "
            "tests.test_system_health_service "
            "tests.test_shared_architecture_services "
            "tests.test_report_quality_service"
        ),
    ),
    TestSuiteEntry(
        name="integration",
        purpose="Full local regression suite without intentionally live AI/source calls.",
        command="python -B -m unittest discover tests",
    ),
    TestSuiteEntry(
        name="live_source",
        purpose="Optional live data-source audit for network/source availability.",
        command="python tools/ai_command_live_audit.py --news-smoke",
        requires_network=True,
        manual=True,
    ),
    TestSuiteEntry(
        name="ai_smoke",
        purpose="Optional AI/MCP health smoke checks; may consume provider quota.",
        command="python -B -m unittest tests.test_minimax_mcp_verify tests.test_minimax_integration",
        requires_network=True,
        requires_ai=True,
        manual=True,
    ),
)


def build_test_suite_manifest() -> dict[str, Any]:
    return {
        "schema_version": TEST_SUITE_MANIFEST_SCHEMA_VERSION,
        "suites": [entry.to_dict() for entry in DEFAULT_TEST_SUITES],
    }


def format_test_suite_manifest(manifest: dict[str, Any] | None = None) -> str:
    data = manifest or build_test_suite_manifest()
    lines = ["Test suite layers:"]
    for entry in data.get("suites") or []:
        flags = []
        if entry.get("requires_network"):
            flags.append("network")
        if entry.get("requires_ai"):
            flags.append("ai")
        if entry.get("manual"):
            flags.append("manual")
        suffix = f" ({', '.join(flags)})" if flags else ""
        lines.append(f"- {entry.get('name')}: {entry.get('command')}{suffix}")
    return "\n".join(lines)
