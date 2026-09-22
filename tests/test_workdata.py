from pathlib import Path

import pytest

from stock_ai_bot.workdata import (
    WORKDATA_PROFILE_ENV,
    WORKDATA_ROOT_ENV,
    get_workdata_path,
    get_workdata_profile,
    get_workdata_root,
    get_workspace_data_root,
)


def test_defaults_to_project_local_prod(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv(WORKDATA_ROOT_ENV, raising=False)
    monkeypatch.delenv(WORKDATA_PROFILE_ENV, raising=False)

    assert get_workdata_root(tmp_path) == (tmp_path / ".workdata").resolve()
    assert get_workspace_data_root(tmp_path) == (tmp_path / ".workdata" / "prod").resolve()


def test_development_profile_is_isolated(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(WORKDATA_ROOT_ENV, str(tmp_path / "shared"))
    monkeypatch.setenv(WORKDATA_PROFILE_ENV, "tmf-monitor")

    assert get_workdata_profile() == "tmf-monitor"
    assert get_workdata_path("reports", "run.json") == (
        tmp_path / "shared" / "dev" / "tmf-monitor" / "reports" / "run.json"
    ).resolve()


@pytest.mark.parametrize("profile", ["../prod", "bad/name", "", "has space"])
def test_rejects_unsafe_profile(profile: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        get_workspace_data_root(tmp_path, profile=profile)


@pytest.mark.parametrize("parts", [("..", "prod"), (str(Path.cwd().anchor),)])
def test_rejects_paths_outside_profile(parts: tuple[str, ...], tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        get_workdata_path(*parts, project_root=tmp_path)
