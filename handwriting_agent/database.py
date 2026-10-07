import os
import sqlite3
from pathlib import Path
from typing import Any, Protocol


class DatabaseConnection(Protocol):
    def execute(self, statement: str, parameters: Any = None) -> Any: ...

    def executescript(self, script: str) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


class _RemoteCursor:
    def __init__(self, result: Any) -> None:
        columns = result.columns
        self._rows = [
            {column: value for column, value in zip(columns, row)}
            for row in result.rows
        ]
        self._lastrowid = result.last_insert_rowid
        self.rowcount = result.rows_affected

    @property
    def lastrowid(self) -> int | None:
        return self._lastrowid

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows.pop(0) if self._rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        rows, self._rows = self._rows, []
        return rows


class _TursoConnection:
    def __init__(self, database_url: str, auth_token: str) -> None:
        from libsql_client import create_client_sync

        self._client = create_client_sync(database_url, auth_token=auth_token)
        self._transaction: Any | None = None

    def execute(self, statement: str, parameters: Any = None) -> Any:
        command = statement.strip().rstrip(";").upper()
        if command in {"BEGIN", "BEGIN IMMEDIATE", "BEGIN TRANSACTION"}:
            if self._transaction is not None:
                raise RuntimeError("A database transaction is already active.")
            self._transaction = self._client.transaction()
            return _RemoteCursor(_empty_result())
        if command in {"COMMIT", "END", "END TRANSACTION"}:
            self.commit()
            return _RemoteCursor(_empty_result())
        if command == "ROLLBACK":
            self.rollback()
            return _RemoteCursor(_empty_result())

        target = self._transaction or self._client
        try:
            result = target.execute(statement, parameters)
        except Exception as exc:
            if _is_integrity_error(exc):
                raise sqlite3.IntegrityError(str(exc)) from exc
            raise
        return _RemoteCursor(result)

    def executescript(self, script: str) -> None:
        statement = ""
        for line in script.splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                if statement.strip():
                    self.execute(statement)
                statement = ""
        if statement.strip():
            self.execute(statement)

    def commit(self) -> None:
        if self._transaction is not None:
            transaction, self._transaction = self._transaction, None
            try:
                transaction.commit()
            finally:
                transaction.close()

    def rollback(self) -> None:
        if self._transaction is not None:
            transaction, self._transaction = self._transaction, None
            try:
                transaction.rollback()
            finally:
                transaction.close()

    def close(self) -> None:
        if self._transaction is not None:
            self.rollback()
        self._client.close()


def connect() -> sqlite3.Connection | _TursoConnection:
    database_url = os.getenv("TURSO_DATABASE_URL", "").strip()
    auth_token = os.getenv("TURSO_AUTH_TOKEN", "").strip()
    if database_url or auth_token:
        if not database_url or not auth_token:
            raise RuntimeError(
                "Set both TURSO_DATABASE_URL and TURSO_AUTH_TOKEN, or unset both to use local SQLite."
            )
        if not database_url.startswith("libsql://"):
            raise RuntimeError(
                "TURSO_DATABASE_URL must be a libsql:// URL to support atomic account transactions."
            )
        try:
            connection = _TursoConnection(database_url, auth_token)
        except ImportError as exc:
            raise RuntimeError(
                "Turso support requires the libsql-client dependency. Install the project requirements."
            ) from exc
    else:
        database = Path(os.getenv("DATABASE_PATH", str(Path(__file__).with_name("likho.sqlite3"))))
        database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _empty_result() -> Any:
    class EmptyResult:
        columns: tuple[str, ...] = ()
        rows: list[Any] = []
        rows_affected = 0
        last_insert_rowid = None

    return EmptyResult()


def _is_integrity_error(error: Exception) -> bool:
    code = str(getattr(error, "code", "")).upper()
    message = str(error).upper()
    return "CONSTRAINT" in code or any(
        marker in message
        for marker in (
            "UNIQUE CONSTRAINT",
            "FOREIGN KEY CONSTRAINT",
            "NOT NULL CONSTRAINT",
            "CHECK CONSTRAINT",
        )
    )
