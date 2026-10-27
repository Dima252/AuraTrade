import importlib
from pathlib import Path

import pytest

import auratrade

# README §13 repository layout.
SUBPACKAGES = [
    "calendar",
    "db",
    "data",
    "indicators",
    "regime",
    "strategies",
    "llm",
    "guardrails",
    "execution",
    "journal",
    "sim",
    "storage",
    "notify",
    "jobs",
]


@pytest.mark.parametrize("name", SUBPACKAGES)
def test_subpackage_imports(name: str) -> None:
    module = importlib.import_module(f"auratrade.{name}")
    assert module.__doc__


def test_package_is_typed() -> None:
    assert (Path(auratrade.__file__).parent / "py.typed").is_file()
