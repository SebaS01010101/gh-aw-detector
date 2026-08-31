"""Process a SEART CSV with a rate-limit-aware batched GitHub GraphQL query.

The query resolves up to 100 repositories per HTTP request and reads only the
tree at ``.github/workflows``.  It does not use Code Search or clone any
repository.  Results are stored in SQLite so an interrupted run can resume
without querying completed repositories again.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd
import requests
from requests import Response

from detect_gh_aw import (
    API_VERSION,
    ERROR_STATUSES,
    WORKFLOWS_PATH,
    CheckResult,
    CsvInputError,
    RepositoryRef,
    RepositorySource,
    extract_repository_references,
    read_input_csv,
    resolve_repository_source,
    sha256_file,
    utc_now,
)


LOGGER = logging.getLogger("process_gh_aw_graphql")
GRAPHQL_URL = "https://api.github.com/graphql"
CHECKPOINT_VERSION = 1
DEFAULT_BATCH_SIZE = 100
DEFAULT_MAX_RETRIES = 5
DEFAULT_TIMEOUT = 30.0
RETRYABLE_STATUSES = ERROR_STATUSES | {"pending", "not_found"}
SUCCESS_STATUSES = {"detected", "not_detected"}


def _api_error_text(error: object) -> str:
    if isinstance(error, Mapping):
        message = error.get("message")
        if isinstance(message, str):
            return message.strip()[:240]
    return str(error).strip()[:240]


def _error_markers(errors: Iterable[object]) -> list[str]:
    return [
        _api_error_text(error)
        for error in errors
        if _api_error_text(error)
    ]


def _looks_like_rate_limit(messages: Iterable[str]) -> bool:
    markers = (
        "rate limit",
        "secondary rate",
        "abuse detection",
        "too many requests",
        "slow down",
        "temporarily blocked",
    )
    return any(marker in message.casefold() for message in messages for marker in markers)


class GitHubGraphQLClient:
    """Sequential GraphQL client with retries and primary/secondary limits."""

    def __init__(
        self,
        token: str | None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.max_retries = max(0, max_retries)
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "gh-aw-detector/1.0",
            }
        )
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self.not_before = 0.0
        self.remaining: int | None = None

    @staticmethod
    def _parse_retry_after(value: str | None) -> float | None:
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(0.0, retry_at.timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                return None

    def _update_rate_state(self, response: Response) -> int | None:
        value = response.headers.get("X-RateLimit-Remaining")
        reset_value = response.headers.get("X-RateLimit-Reset")
        try:
            remaining = int(value) if value is not None else None
        except ValueError:
            remaining = None
        self.remaining = remaining
        if remaining == 0 and reset_value:
            try:
                self.not_before = max(self.not_before, float(reset_value) + 1.0)
            except ValueError:
                pass
        return remaining

    def _update_rate_state_from_payload(self, payload: object) -> None:
        if not isinstance(payload, Mapping):
            return
        data = payload.get("data")
        if not isinstance(data, Mapping):
            return
        rate = data.get("rateLimit")
        if not isinstance(rate, Mapping):
            return
        remaining = rate.get("remaining")
        if isinstance(remaining, int):
            self.remaining = remaining
        reset_at = rate.get("resetAt")
        if self.remaining == 0 and isinstance(reset_at, str):
            try:
                parsed = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
                self.not_before = max(self.not_before, parsed.timestamp() + 1.0)
            except ValueError:
                pass

    def _sleep(self, seconds: float, reason: str) -> None:
        remaining = max(0.0, seconds)
        if remaining <= 0:
            return
        LOGGER.warning("Esperando %.0f s antes de continuar (%s).", remaining, reason)
        while remaining > 0:
            pause = min(60.0, remaining)
            time.sleep(pause)
            remaining -= pause

    def _wait_for_rate_limit(self) -> None:
        wait_seconds = self.not_before - time.time()
        if wait_seconds > 0:
            self._sleep(wait_seconds, "X-RateLimit-Remaining=0")

    def _backoff(self, attempt: int, response: Response | None, reason: str) -> None:
        retry_after = self._parse_retry_after(
            response.headers.get("Retry-After") if response is not None else None
        )
        exponential = min(60.0, max(5.0, 2.0 ** (attempt + 1)))
        self._sleep(max(exponential, retry_after or 0.0) + random.uniform(0.0, 1.0), reason)

    @staticmethod
    def _query(references: Sequence[RepositoryRef]) -> tuple[str, dict[str, RepositoryRef]]:
        aliases: dict[str, RepositoryRef] = {}
        fields: list[str] = []
        expression = json.dumps(f"HEAD:{WORKFLOWS_PATH}", ensure_ascii=True)
        for index, reference in enumerate(references):
            alias = f"r{index}"
            aliases[alias] = reference
            owner = json.dumps(reference.owner, ensure_ascii=True)
            repository = json.dumps(reference.repository, ensure_ascii=True)
            fields.append(
                f"{alias}: repository(owner:{owner}, name:{repository}) "
                f"{{ name object(expression:{expression}) {{ __typename "
                "... on Tree { entries { name type } } } }"
            )
        query = "query { rateLimit { remaining cost resetAt } " + " ".join(fields) + " }"
        return query, aliases

    @staticmethod
    def _all_results(
        references: Sequence[RepositoryRef], status: str, error: str, http_status: int | None
    ) -> dict[str, CheckResult]:
        return {
            reference.key: CheckResult(0, status, [], error, http_status)
            for reference in references
        }

    def inspect_batch(self, references: Sequence[RepositoryRef]) -> dict[str, CheckResult]:
        """Inspect one batch and return one result per reference."""

        if not references:
            return {}
        query, aliases = self._query(references)

        for attempt in range(self.max_retries + 1):
            self._wait_for_rate_limit()
            response: Response | None = None
            try:
                response = self.session.post(
                    GRAPHQL_URL,
                    json={"query": query},
                    timeout=self.timeout,
                )
            except requests.Timeout:
                if attempt < self.max_retries:
                    self._backoff(attempt, None, "timeout de GitHub")
                    continue
                return self._all_results(
                    references, "network_error", "timeout de conexión", None
                )
            except requests.ConnectionError:
                if attempt < self.max_retries:
                    self._backoff(attempt, None, "error de conexión con GitHub")
                    continue
                return self._all_results(
                    references, "network_error", "error de conexión", None
                )
            except requests.RequestException:
                if attempt < self.max_retries:
                    self._backoff(attempt, None, "error temporal de requests")
                    continue
                return self._all_results(
                    references,
                    "network_error",
                    "error inesperado de requests",
                    None,
                )

            remaining = self._update_rate_state(response)
            status_code = response.status_code
            if status_code == 200:
                try:
                    payload = response.json()
                except ValueError:
                    if attempt < self.max_retries:
                        self._backoff(attempt, response, "JSON inválido de GitHub")
                        continue
                    return self._all_results(
                        references,
                        "unexpected_response",
                        "respuesta 200 sin JSON válido",
                        status_code,
                    )

                self._update_rate_state_from_payload(payload)
                if not isinstance(payload, Mapping):
                    if attempt < self.max_retries:
                        self._backoff(attempt, response, "respuesta inesperada de GitHub")
                        continue
                    return self._all_results(
                        references,
                        "unexpected_response",
                        "la API no devolvió un objeto JSON",
                        status_code,
                    )

                data = payload.get("data")
                raw_errors = payload.get("errors", [])
                errors = raw_errors if isinstance(raw_errors, list) else []
                messages = _error_markers(errors)
                if _looks_like_rate_limit(messages) or not isinstance(data, Mapping):
                    rate_error = _looks_like_rate_limit(messages)
                    if attempt < self.max_retries:
                        self._backoff(
                            attempt,
                            response,
                            "rate limit de GraphQL" if rate_error else "respuesta GraphQL incompleta",
                        )
                        continue
                    return self._all_results(
                        references,
                        "rate_limit_error" if rate_error else "unexpected_response",
                        "; ".join(messages) or "GraphQL no devolvió data",
                        status_code,
                    )

                errors_by_alias: dict[str, list[str]] = {}
                for error in errors:
                    if isinstance(error, Mapping):
                        path = error.get("path")
                        if isinstance(path, list) and path and isinstance(path[0], str):
                            errors_by_alias.setdefault(path[0], []).append(_api_error_text(error))

                results: dict[str, CheckResult] = {}
                missing = object()
                for alias, reference in aliases.items():
                    alias_errors = errors_by_alias.get(alias, [])
                    value = data.get(alias, missing)
                    if value is None:
                        message = "; ".join(alias_errors)
                        results[reference.key] = CheckResult(
                            0,
                            "not_found",
                            [],
                            message or "GitHub no resolvió el repositorio",
                            status_code,
                        )
                        continue
                    if value is missing:
                        results[reference.key] = CheckResult(
                            0,
                            "unexpected_response",
                            [],
                            "la respuesta GraphQL no contiene el alias solicitado",
                            status_code,
                        )
                        continue
                    if alias_errors:
                        results[reference.key] = CheckResult(
                            0,
                            "unexpected_response",
                            [],
                            "; ".join(alias_errors),
                            status_code,
                        )
                        continue
                    if not isinstance(value, Mapping):
                        results[reference.key] = CheckResult(
                            0,
                            "unexpected_response",
                            [],
                            "el alias GraphQL no contiene un objeto repositorio",
                            status_code,
                        )
                        continue

                    tree = value.get("object")
                    if tree is None:
                        results[reference.key] = CheckResult(
                            0,
                            "not_detected",
                            [],
                            "el repositorio no contiene .github/workflows/ en HEAD",
                            status_code,
                        )
                        continue
                    if not isinstance(tree, Mapping):
                        results[reference.key] = CheckResult(
                            0,
                            "unexpected_response",
                            [],
                            "el objeto GraphQL de workflows no es válido",
                            status_code,
                        )
                        continue
                    if tree.get("__typename") != "Tree":
                        results[reference.key] = CheckResult(
                            0,
                            "unexpected_response",
                            [],
                            "la ruta .github/workflows/ no devolvió un árbol",
                            status_code,
                        )
                        continue
                    entries = tree.get("entries")
                    if not isinstance(entries, list):
                        results[reference.key] = CheckResult(
                            0,
                            "unexpected_response",
                            [],
                            "GraphQL no devolvió las entradas de workflows",
                            status_code,
                        )
                        continue

                    normalized_entries = [
                        {
                            "name": entry.get("name"),
                            "type": "file" if entry.get("type") == "blob" else "dir",
                        }
                        for entry in entries
                        if isinstance(entry, Mapping)
                    ]
                    from detect_gh_aw import detect_matching_workflow_names

                    matches = detect_matching_workflow_names(normalized_entries)
                    results[reference.key] = CheckResult(
                        1 if matches else 0,
                        "detected" if matches else "not_detected",
                        matches,
                        "",
                        status_code,
                    )
                return results

            message = "; ".join(
                item
                for item in [
                    response.text[:240] if response.text else "",
                ]
                if item
            )
            looks_like_rate_limit = (
                status_code == 429
                or remaining == 0
                or _looks_like_rate_limit([message])
            )
            if status_code in (403, 429) and looks_like_rate_limit:
                if attempt < self.max_retries:
                    self._backoff(attempt, response, "rate limit primario/secundario de GitHub")
                    continue
                return self._all_results(
                    references,
                    "rate_limit_error",
                    message or f"GitHub respondió HTTP {status_code} por rate limit",
                    status_code,
                )
            if status_code == 401:
                return self._all_results(
                    references,
                    "auth_error",
                    "token ausente, inválido o no aceptado por GitHub",
                    status_code,
                )
            if status_code == 403:
                return self._all_results(
                    references,
                    "forbidden",
                    message or "GitHub denegó el acceso",
                    status_code,
                )
            if 500 <= status_code <= 599:
                if attempt < self.max_retries:
                    self._backoff(attempt, response, f"error temporal HTTP {status_code}")
                    continue
                return self._all_results(
                    references,
                    "temporary_error",
                    message or f"GitHub respondió HTTP {status_code}",
                    status_code,
                )
            return self._all_results(
                references,
                "http_error",
                message or f"respuesta HTTP inesperada {status_code}",
                status_code,
            )

        return self._all_results(
            references,
            "temporary_error",
            "se agotaron los reintentos",
            None,
        )


class SQLiteCheckpoint:
    """Incremental checkpoint keyed by normalized repository reference."""

    def __init__(
        self,
        path: Path,
        input_path: Path,
        input_sha256: str,
        source: RepositorySource,
        reset: bool = False,
    ) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        if reset and path.exists():
            path.unlink()
            for suffix in ("-wal", "-shm"):
                path.with_name(path.name + suffix).unlink(missing_ok=True)
        self.connection = sqlite3.connect(str(path), timeout=60.0)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS results (
                repo_key TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                repository TEXT NOT NULL,
                gh_aw INTEGER NOT NULL,
                status TEXT NOT NULL,
                matches_json TEXT NOT NULL,
                error TEXT NOT NULL,
                http_status INTEGER,
                checked_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS revalidations (
                repo_key TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                repository TEXT NOT NULL,
                gh_aw INTEGER NOT NULL,
                status TEXT NOT NULL,
                matches_json TEXT NOT NULL,
                error TEXT NOT NULL,
                http_status INTEGER,
                checked_at TEXT NOT NULL
            );
            """
        )
        existing = dict(self.connection.execute("SELECT key, value FROM metadata"))
        if existing:
            if existing.get("version") != str(CHECKPOINT_VERSION):
                raise CsvInputError("El checkpoint SQLite tiene una versión incompatible.")
            if existing.get("input_sha256") != input_sha256:
                raise CsvInputError(
                    f"El checkpoint pertenece a otro contenido de entrada ({input_path}). "
                    "Usa --reset-checkpoint si cambiaste el CSV."
                )
        else:
            metadata = {
                "version": str(CHECKPOINT_VERSION),
                "created_at": utc_now(),
                "input_path": str(input_path.resolve()),
                "input_sha256": input_sha256,
                "source": json.dumps(asdict(source), ensure_ascii=False),
            }
            self.connection.executemany(
                "INSERT INTO metadata(key, value) VALUES (?, ?)", metadata.items()
            )
        self.connection.commit()

    @staticmethod
    def _record_values(key: str, record: Mapping[str, Any]) -> tuple[Any, ...]:
        repository = str(record.get("repository", ""))
        if "/" in repository:
            owner, repo_name = repository.split("/", 1)
        else:
            owner, repo_name = "", repository
        return (
            key,
            owner,
            repo_name,
            int(record.get("gh_aw", 0)),
            str(record.get("status", "error")),
            json.dumps(list(record.get("matches", [])), ensure_ascii=False),
            str(record.get("error", "")),
            record.get("http_status"),
            str(record.get("checked_at", utc_now())),
        )

    def save_batch(self, table: str, records: Mapping[str, Mapping[str, Any]]) -> None:
        if table not in {"results", "revalidations"}:
            raise ValueError(f"Tabla de checkpoint no permitida: {table}")
        self.connection.executemany(
            f"""INSERT OR REPLACE INTO {table}
                (repo_key, owner, repository, gh_aw, status, matches_json,
                 error, http_status, checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [self._record_values(key, record) for key, record in records.items()],
        )
        self.connection.commit()

    def load(self, table: str) -> dict[str, dict[str, Any]]:
        if table not in {"results", "revalidations"}:
            raise ValueError(f"Tabla de checkpoint no permitida: {table}")
        records: dict[str, dict[str, Any]] = {}
        rows = self.connection.execute(
            f"""SELECT repo_key, owner, repository, gh_aw, status, matches_json,
                       error, http_status, checked_at
                FROM {table}"""
        )
        for key, owner, repository, gh_aw, status, matches_json, error, http_status, checked_at in rows:
            try:
                matches = json.loads(matches_json)
            except json.JSONDecodeError:
                matches = []
            records[str(key)] = {
                "repository": f"{owner}/{repository}",
                "gh_aw": int(gh_aw),
                "status": str(status),
                "matches": matches if isinstance(matches, list) else [],
                "error": str(error),
                "http_status": http_status,
                "checked_at": str(checked_at),
            }
        return records

    def close(self) -> None:
        self.connection.close()


def resolve_github_token() -> str | None:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        LOGGER.info("Autenticación GitHub: variable GITHUB_TOKEN.")
        return token

    executable = shutil.which("gh.exe") or shutil.which("gh")
    if executable:
        try:
            completed = subprocess.run(
                [executable, "auth", "token"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
            )
            token = completed.stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            token = ""
        if token:
            LOGGER.info("Autenticación GitHub: sesión existente de GitHub CLI.")
            return token

    LOGGER.warning(
        "No existe GITHUB_TOKEN y no se pudo obtener una sesión autenticada de gh; "
        "las consultas GraphQL probablemente serán rechazadas o tendrán un límite menor."
    )
    return None


def _check_reserved_columns(df: pd.DataFrame) -> None:
    reserved = {
        "gh_aw",
        "gh_aw_status",
        "gh_aw_matches",
        "gh_aw_error",
        "gh_aw_initial_matches",
        "gh_aw_revalidated",
    }
    collisions = sorted(reserved.intersection({str(column) for column in df.columns}))
    if collisions:
        raise CsvInputError(
            "El CSV de entrada ya usa columnas reservadas por la salida: "
            + ", ".join(collisions)
        )


def _matches_text(record: Mapping[str, Any] | None) -> str:
    if not record:
        return ""
    return "|".join(str(item) for item in record.get("matches", []))


def build_output(
    df: pd.DataFrame,
    references: Sequence[RepositoryRef | None],
    initial: Mapping[str, Mapping[str, Any]],
    revalidations: Mapping[str, Mapping[str, Any]],
) -> pd.DataFrame:
    output = df.copy()
    gh_aw: list[object] = []
    statuses: list[str] = []
    matches: list[str] = []
    errors: list[str] = []
    initial_matches: list[str] = []
    revalidated: list[object] = []

    for reference in references:
        if reference is None:
            gh_aw.append("")
            statuses.append("input_error")
            matches.append("")
            errors.append("no se pudo identificar owner/repository en esta fila")
            initial_matches.append("")
            revalidated.append("")
            continue

        record = initial.get(reference.key)
        if record is None:
            gh_aw.append("")
            statuses.append("pending")
            matches.append("")
            errors.append("")
            initial_matches.append("")
            revalidated.append("")
            continue

        initial_status = str(record.get("status", "error"))
        initial_text = _matches_text(record)
        initial_matches.append(initial_text)
        if initial_status == "detected":
            validation = revalidations.get(reference.key)
            if validation is None:
                gh_aw.append("")
                statuses.append("pending_revalidation")
                matches.append(initial_text)
                errors.append("falta la segunda validación del positivo")
                revalidated.append("")
                continue
            validation_status = str(validation.get("status", "error"))
            if validation_status == "detected":
                gh_aw.append(1)
                statuses.append("detected")
                matches.append(_matches_text(validation))
                errors.append("")
                revalidated.append(1)
            elif validation_status == "not_detected":
                gh_aw.append(0)
                statuses.append("revalidation_not_detected")
                matches.append("")
                errors.append(str(validation.get("error", "")))
                revalidated.append(0)
            else:
                gh_aw.append("")
                statuses.append(f"revalidation_{validation_status}")
                matches.append("")
                errors.append(str(validation.get("error", "")))
                revalidated.append("")
            continue

        if initial_status == "not_detected":
            gh_aw.append(0)
            statuses.append(initial_status)
            matches.append("")
            errors.append(str(record.get("error", "")))
            revalidated.append("")
            continue

        # An inaccessible repository, malformed response or technical error
        # must not become a misleading 0.
        gh_aw.append("")
        statuses.append(initial_status)
        matches.append(initial_text)
        errors.append(str(record.get("error", "")))
        revalidated.append("")

    output["gh_aw"] = gh_aw
    output["gh_aw_status"] = statuses
    output["gh_aw_matches"] = matches
    output["gh_aw_error"] = errors
    output["gh_aw_initial_matches"] = initial_matches
    output["gh_aw_revalidated"] = revalidated
    return output


def write_outputs(enriched: pd.DataFrame, enriched_path: Path, final_path: Path) -> None:
    enriched_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    enriched_tmp = enriched_path.with_name(f".{enriched_path.name}.tmp")
    final_tmp = final_path.with_name(f".{final_path.name}.tmp")
    try:
        enriched.to_csv(enriched_tmp, index=False, encoding="utf-8-sig")
        final = enriched[
            (enriched["gh_aw"] == 1)
            & (enriched["gh_aw_revalidated"] == 1)
        ]
        final.to_csv(final_tmp, index=False, encoding="utf-8-sig")
        os.replace(enriched_tmp, enriched_path)
        os.replace(final_tmp, final_path)
    except Exception:
        enriched_tmp.unlink(missing_ok=True)
        final_tmp.unlink(missing_ok=True)
        raise


def _unique_references(references: Sequence[RepositoryRef | None]) -> dict[str, RepositoryRef]:
    unique: dict[str, RepositoryRef] = {}
    for reference in references:
        if reference is not None:
            unique.setdefault(reference.key, reference)
    return unique


def _process_pass(
    client: GitHubGraphQLClient,
    checkpoint: SQLiteCheckpoint,
    table: str,
    references: Mapping[str, RepositoryRef],
    records: dict[str, dict[str, Any]],
    batch_size: int,
    retry_errors: bool,
    pass_name: str,
) -> int:
    pending = [
        reference
        for key, reference in references.items()
        if key not in records
        or (retry_errors and str(records[key].get("status")) in RETRYABLE_STATUSES)
    ]
    if not pending:
        LOGGER.info("%s: no quedan repositorios pendientes.", pass_name)
        return 0

    total = len(pending)
    completed = 0
    for start in range(0, total, batch_size):
        batch = pending[start : start + batch_size]
        batch_results = client.inspect_batch(batch)
        serializable: dict[str, dict[str, Any]] = {}
        for reference in batch:
            result = batch_results.get(reference.key)
            if result is None:
                result = CheckResult(
                    0,
                    "unexpected_response",
                    [],
                    "la respuesta no incluyó el repositorio del lote",
                    None,
                )
            serializable[reference.key] = {
                **result.as_dict(),
                "repository": reference.full_name,
                "checked_at": utc_now(),
            }
        checkpoint.save_batch(table, serializable)
        records.update(serializable)
        completed += len(batch)
        detected = sum(record["status"] == "detected" for record in serializable.values())
        technical = sum(
            record["status"] not in SUCCESS_STATUSES for record in serializable.values()
        )
        LOGGER.info(
            "%s [%d-%d/%d] lote guardado; GH-AW=1: %d; no concluyentes: %d; "
            "rate restante: %s",
            pass_name,
            completed - len(batch) + 1,
            completed,
            total,
            detected,
            technical,
            client.remaining if client.remaining is not None else "desconocido",
        )
    return completed


def _validate_outputs(
    input_df: pd.DataFrame,
    enriched_path: Path,
    final_path: Path,
    references: Sequence[RepositoryRef | None],
    initial: Mapping[str, Mapping[str, Any]],
    revalidations: Mapping[str, Mapping[str, Any]],
) -> tuple[int, int]:
    if not enriched_path.exists() or not final_path.exists():
        raise RuntimeError("No se generaron ambos CSV de salida.")
    enriched = pd.read_csv(enriched_path, dtype=str, keep_default_na=False, na_filter=False)
    final = pd.read_csv(final_path, dtype=str, keep_default_na=False, na_filter=False)
    original_columns = [str(column) for column in input_df.columns]
    if len(enriched) != len(input_df):
        raise RuntimeError("El CSV enriquecido no conserva la cantidad de filas de entrada.")
    if enriched.columns[: len(original_columns)].tolist() != original_columns:
        raise RuntimeError("El CSV enriquecido no conserva el orden de columnas originales.")
    required = {
        "gh_aw",
        "gh_aw_status",
        "gh_aw_matches",
        "gh_aw_error",
        "gh_aw_initial_matches",
        "gh_aw_revalidated",
    }
    if not required.issubset(set(enriched.columns)):
        raise RuntimeError("Faltan columnas técnicas requeridas en el CSV enriquecido.")
    values = set(enriched["gh_aw"].unique()) - {""}
    if not values.issubset({"0", "1"}):
        raise RuntimeError(f"gh_aw contiene valores inválidos: {sorted(values)}")
    if not final.columns.tolist() == enriched.columns.tolist():
        raise RuntimeError("El CSV final no tiene la misma estructura que el enriquecido.")
    if not final.empty:
        if set(final["gh_aw"].unique()) != {"1"}:
            raise RuntimeError("El CSV final contiene filas que no tienen gh_aw=1.")
        if set(final["gh_aw_revalidated"].unique()) != {"1"}:
            raise RuntimeError("El CSV final contiene positivos sin segunda validación.")
        if set(final["gh_aw_status"].unique()) != {"detected"}:
            raise RuntimeError("El CSV final contiene estados distintos de detected.")

    expected_rows = 0
    for reference in references:
        if reference is None:
            continue
        first = initial.get(reference.key)
        second = revalidations.get(reference.key)
        if first and second and first.get("status") == "detected" and second.get("status") == "detected":
            expected_rows += 1
    if len(final) != expected_rows:
        raise RuntimeError(
            f"El CSV final tiene {len(final):,} filas y se esperaban {expected_rows:,}."
        )

    positive_keys = {
        key
        for key, record in initial.items()
        if record.get("status") == "detected"
    }
    for key in positive_keys:
        record = revalidations.get(key)
        if not record or record.get("status") != "detected" or not record.get("matches"):
            raise RuntimeError(f"El positivo {key} no tiene una segunda validación válida.")
    return len(enriched), len(final)


def run(args: argparse.Namespace) -> int:
    input_path = Path(args.input).expanduser()
    output_dir = Path(args.output_dir).expanduser()
    enriched_path = Path(args.enriched_output).expanduser()
    final_path = Path(args.final_output).expanduser()
    checkpoint_path = Path(args.checkpoint).expanduser()

    df, csv_info = read_input_csv(input_path)
    _check_reserved_columns(df)
    source = resolve_repository_source(df)
    references = extract_repository_references(df, source)
    unique = _unique_references(references)
    valid_rows = sum(reference is not None for reference in references)
    print(f"CSV SEART: {input_path.resolve()}")
    print(f"Filas: {len(df):,}; repositorios únicos: {len(unique):,}")
    print(f"Columnas ({len(df.columns)}): {', '.join(str(column) for column in df.columns)}")
    print(
        f"Referencia seleccionada: {source.describe()} "
        f"({valid_rows:,}/{len(references):,} filas válidas)"
    )
    print(f"Formato: encoding={csv_info.encoding}, separador={csv_info.delimiter!r}")
    for warning in csv_info.format_warnings:
        print(f"Advertencia de formato: {warning}")

    input_sha256 = sha256_file(input_path)
    checkpoint = SQLiteCheckpoint(
        checkpoint_path,
        input_path,
        input_sha256,
        source,
        reset=args.reset_checkpoint,
    )
    client = GitHubGraphQLClient(
        resolve_github_token(),
        max_retries=args.max_retries,
        timeout=args.timeout,
    )
    initial = checkpoint.load("results")
    revalidations = checkpoint.load("revalidations")

    try:
        passes = 2 if args.retry_errors else 1
        for pass_number in range(1, passes + 1):
            completed = _process_pass(
                client,
                checkpoint,
                "results",
                unique,
                initial,
                args.batch_size,
                retry_errors=pass_number > 1,
                pass_name=f"Detección (pasada {pass_number})",
            )
            if pass_number > 1 or completed == 0:
                break

        positive_refs = {
            key: reference
            for key, reference in unique.items()
            if initial.get(key, {}).get("status") == "detected"
        }
        for pass_number in range(1, passes + 1):
            completed = _process_pass(
                client,
                checkpoint,
                "revalidations",
                positive_refs,
                revalidations,
                args.batch_size,
                retry_errors=pass_number > 1,
                pass_name=f"Revalidación de positivos (pasada {pass_number})",
            )
            if pass_number > 1 or completed == 0:
                break

        enriched = build_output(df, references, initial, revalidations)
        write_outputs(enriched, enriched_path, final_path)
        enriched_rows, final_rows = _validate_outputs(
            df,
            enriched_path,
            final_path,
            references,
            initial,
            revalidations,
        )
    except KeyboardInterrupt:
        LOGGER.warning(
            "Ejecución interrumpida. El checkpoint incremental quedó guardado en %s; "
            "puedes volver a ejecutar el mismo comando.",
            checkpoint_path.resolve(),
        )
        return 130
    finally:
        checkpoint.close()

    initial_counts = Counter(str(record.get("status")) for record in initial.values())
    revalidation_counts = Counter(
        str(record.get("status")) for record in revalidations.values()
    )
    unresolved_initial = sum(
        status not in SUCCESS_STATUSES for status in initial_counts.elements()
    )
    unresolved_revalidation = sum(
        status not in SUCCESS_STATUSES
        for key, status in (
            (key, str(record.get("status")))
            for key, record in revalidations.items()
            if key in positive_refs
        )
    )
    confirmed_keys = {
        key
        for key, record in initial.items()
        if record.get("status") == "detected"
        and revalidations.get(key, {}).get("status") == "detected"
    }
    not_detected_keys = {
        key
        for key, record in initial.items()
        if record.get("status") == "not_detected"
    }
    not_detected_keys.update(
        key
        for key, record in revalidations.items()
        if record.get("status") == "not_detected"
    )
    print("\nResumen final")
    print(f"Repositorios originales: {len(df):,} filas")
    print(f"Repositorios únicos candidatos: {len(unique):,}")
    print(f"Procesados correctamente: {sum(initial_counts[s] for s in SUCCESS_STATUSES):,}")
    print(f"GH-AW detectados y revalidados: {len(confirmed_keys):,} repositorios únicos")
    print(f"No GH-AW confirmados: {len(not_detected_keys):,} repositorios únicos")
    print(
        "Errores/no verificables restantes: "
        f"{unresolved_initial + unresolved_revalidation:,} registros de checkpoint"
    )
    print(f"Estados detección: {dict(initial_counts)}")
    print(f"Estados revalidación: {dict(revalidation_counts)}")
    print(f"Filas enriquecidas: {enriched_rows:,}")
    print(f"Filas CSV final: {final_rows:,}")
    print(f"CSV enriquecido: {enriched_path.resolve()}")
    print(f"CSV final: {final_path.resolve()}")
    print(f"Checkpoint SQLite: {checkpoint_path.resolve()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Detecta GH-AW en un CSV SEART mediante GraphQL por lotes."
    )
    parser.add_argument("--input", default="data/input.csv", help="CSV original de SEART.")
    parser.add_argument("--output-dir", default="data/output", help="Directorio de salida.")
    parser.add_argument(
        "--enriched-output",
        default="data/output/repositories_enriched.csv",
        help="CSV enriquecido.",
    )
    parser.add_argument(
        "--final-output",
        default="data/output/gh_aw_repositories.csv",
        help="CSV final filtrado.",
    )
    parser.add_argument(
        "--checkpoint",
        default="data/output/gh_aw_checkpoint.sqlite",
        help="Checkpoint SQLite incremental.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Repositorios por query GraphQL (máximo 100; por defecto: {DEFAULT_BATCH_SIZE}).",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
        help=f"Reintentos de errores temporales (por defecto: {DEFAULT_MAX_RETRIES}).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"Timeout por request en segundos (por defecto: {DEFAULT_TIMEOUT}).",
    )
    parser.add_argument(
        "--retry-errors",
        action="store_true",
        help="Ejecutar una segunda pasada automática para errores y repositorios no encontrados.",
    )
    parser.add_argument(
        "--reset-checkpoint",
        action="store_true",
        help="Eliminar el checkpoint indicado y comenzar de nuevo.",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
        help="Nivel de logging.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 1 <= args.batch_size <= 100:
        parser.error("--batch-size debe estar entre 1 y 100")
    if args.max_retries < 0:
        parser.error("--max-retries no puede ser negativo")
    if args.timeout <= 0:
        parser.error("--timeout debe ser mayor que cero")
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        return run(args)
    except CsvInputError as exc:
        LOGGER.error("%s", exc)
        return 2
    except Exception:
        LOGGER.exception("Error inesperado; el checkpoint conserva el progreso confirmado.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
