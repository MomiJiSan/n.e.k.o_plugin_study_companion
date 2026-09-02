"""Contracts for dependencies required by the packaged plugin at runtime."""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"


def _distribution_name(requirement: str) -> str:
    return requirement.partition(">=")[0].partition("<")[0].partition("[")[0].lower()


def _dependency_groups() -> tuple[set[str], set[str]]:
    with PYPROJECT.open("rb") as file:
        configuration = tomllib.load(file)

    runtime_dependencies = {
        _distribution_name(requirement)
        for requirement in configuration["project"]["dependencies"]
    }
    dev_dependencies = {
        _distribution_name(requirement)
        for requirement in configuration["dependency-groups"]["dev"]
    }
    return runtime_dependencies, dev_dependencies


def test_runtime_dependencies_are_direct_and_dev_group_stays_test_only() -> None:
    runtime_dependencies, dev_dependencies = _dependency_groups()

    assert "httpx" in runtime_dependencies
    assert "pillow" not in runtime_dependencies
    assert "pillow" in dev_dependencies
    assert {"pytest", "pytest-asyncio"} <= dev_dependencies
    assert "httpx" not in dev_dependencies


def test_native_pillow_is_host_provided_not_plugin_vendored() -> None:
    runtime_dependencies, dev_dependencies = _dependency_groups()

    assert "pillow" not in runtime_dependencies
    assert "pillow" in dev_dependencies


def test_no_dev_export_resolves_declared_runtime_dependencies() -> None:
    uv = shutil.which("uv")
    assert uv is not None, "uv is required to verify the locked runtime dependency export"

    completed = subprocess.run(
        [
            uv,
            "export",
            "--locked",
            "--no-dev",
            "--no-hashes",
            "--no-emit-project",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    exported_distributions = {
        line.partition("==")[0].lower()
        for line in completed.stdout.splitlines()
        if line and not line.startswith(("#", "-"))
    }
    assert "httpx" in exported_distributions
    assert "pillow" not in exported_distributions
