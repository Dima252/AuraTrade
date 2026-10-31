"""The server's source names no write (README §5.10, #37): a guard on top of the read-only
database session and the reader-typed broker, for writes those can't see."""

import ast
from pathlib import Path

import auratrade.mcp_server

PACKAGE = Path(auratrade.mcp_server.__file__).parent

# Order calls, SQL writes, object puts, the CLI's flag writers, the halt's writers.
WRITES = frozenset(
    {
        "submit",
        "replace",
        "cancel",
        "cancel_entries",
        "sell",
        "sell_plain",
        "buy",
        "insert",
        "update",
        "delete",
        "put",
        "put_object",
        "publish",
        "write_lots",
        "write_stop_level",
        "reset",
        "turn_on",
        "turn_off",
        "_set_trading",
    }
)
# Modules that only exist to write.
WRITERS = ("auratrade.cli", "auratrade.execution.orders", "auratrade.execution.portfolio")
# Ways to lift the session's read-only default.
SQL_WRITES = ("READ WRITE", "default_transaction_read_only", "INSERT ", "UPDATE ", "DELETE ")


def _sources() -> list[Path]:
    files = sorted(PACKAGE.glob("*.py"))
    assert len(files) >= 6
    return files


def test_no_write_is_called() -> None:
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                assert name not in WRITES, f"{path.name}:{node.lineno} calls {name}"


def test_no_writer_is_imported() -> None:
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                names = {a.name for a in node.names}
                assert node.module not in WRITERS, f"{path.name} imports {node.module}"
                assert not names & WRITES, f"{path.name} imports {names & WRITES}"
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name not in WRITERS, f"{path.name} imports {alias.name}"


def test_no_sql_write_or_read_write_transaction() -> None:
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        for marker in SQL_WRITES:
            assert marker not in text, f"{path.name} contains {marker!r}"
