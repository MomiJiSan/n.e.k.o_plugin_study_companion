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


def test_runtime_dependencies_are_direct_and_dev_group_stays_test_only() -> None:
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

    assert {"httpx", "pillow"} <= runtime_dependencies
    assert {"pytest", "pytest-asyncio"} <= dev_dependencies
    assert {"httpx", "pillow"}.isdisjoint(dev_dependencies)


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
    assert {"httpx", "pillow"} <= exported_distributions
