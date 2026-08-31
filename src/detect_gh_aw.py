"""Detect GitHub Agentic Workflows in repositories listed by a CSV.

The detector only inspects the ``.github/workflows/`` directory through the
GitHub Contents REST API.  It never uses Code Search and never clones a
repository.
"""

from __future__ import annotations

import argparse
import csv as csv_module
import hashlib
import io
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import unquote, urlparse

import pandas as pd
import requests
from requests import Response


LOGGER = logging.getLogger("detect_gh_aw")
API_BASE_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
WORKFLOWS_PATH = ".github/workflows"
CHECKPOINT_VERSION = 1
DEFAULT_CHECKPOINT_EVERY = 25
DEFAULT_MAX_RETRIES = 5
DEFAULT_TIMEOUT = 30.0

REPOSITORY_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

SINGLE_COLUMN_ALIASES = {
    "full_name": 60,
    "fullname": 60,
    "name_with_owner": 60,
    "repository": 45,
    "repository_name": 35,
    "repo": 45,
    "repo_name": 35,
    "repository_url": 45,
    "repo_url": 45,
    "github_repository": 45,
    "github_url": 35,
    "html_url": 35,
    "url": 30,
}

OWNER_COLUMN_ALIASES = {
    "owner",
    "owner_name",
    "repository_owner",
    "repo_owner",
    "github_owner",
    "organization",
    "organisation",
    "org",
    "login",
    "username",
}

REPOSITORY_COLUMN_ALIASES = {
    "repo",
    "repo_name",
    "repository",
    "repository_name",
    "github_repository",
    "name",
    "project",
    "project_name",
}

ERROR_STATUSES = {
    "auth_error",
    "forbidden",
    "network_error",
    "rate_limit_error",
    "temporary_error",
    "unexpected_response",
    "http_error",
}


class CsvInputError(ValueError):
    """Raised when the input CSV cannot be interpreted safely."""


@dataclass(frozen=True)
class RepositoryRef:
    """A normalized GitHub ``owner/repository`` reference."""

    owner: str
    repository: str

    @property
    def key(self) -> str:
        """Case-insensitive key used to deduplicate API requests."""

        return f"{self.owner.casefold()}/{self.repository.casefold()}"

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repository}"


@dataclass(frozen=True)
class RepositorySource:
    """How repository references are obtained from the input DataFrame."""

    mode: str
    columns: tuple[str, ...]
    score: float
    coverage: float
    valid_count: int
    nonempty_count: int

    def describe(self) -> str:
        if self.mode == "single":
            return f"columna '{self.columns[0]}'"
        return f"combinación '{self.columns[0]}' + '{self.columns[1]}'"


@dataclass
class CheckResult:
    """Result of inspecting one repository."""

    gh_aw: int
    status: str
    matches: list[str]
    error: str = ""
    http_status: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CsvInfo:
    encoding: str
    delimiter: str
    format_warnings: tuple[str, ...]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_column_name(name: object) -> str:
    """Normalize a column name only for heuristics, never in the output."""

    normalized = re.sub(r"[^a-z0-9]+", "_", str(name).strip().casefold())
    return normalized.strip("_")


def _text_value(value: object) -> str:
    if value is None:
        return ""
    try:
        if bool(pd.isna(value)):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _valid_repository_component(value: str) -> bool:
    return bool(value) and bool(REPOSITORY_COMPONENT.fullmatch(value))


def parse_repository_reference(value: object) -> RepositoryRef | None:
    """Parse a GitHub URL or ``owner/repository`` value.

    The parser accepts common SEART/export forms, including GitHub web URLs,
    API URLs, SSH remotes and plain ``owner/repository`` values.  It returns
    ``None`` for values that cannot be identified unambiguously.
    """

    raw = _text_value(value)
    if not raw:
        return None

    path: str
    lowered = raw.casefold()
    if lowered.startswith("git@github.com:"):
        path = raw.split(":", 1)[1]
    elif "://" in raw:
        parsed = urlparse(raw)
        hostname = (parsed.hostname or "").casefold()
        if hostname not in {"github.com", "www.github.com", "api.github.com"}:
            return None
        path = parsed.path
    else:
        path = raw
        for prefix in ("github.com/", "www.github.com/"):
            if lowered.startswith(prefix):
                path = raw[len(prefix) :]
                break

    path = unquote(path).split("?", 1)[0].split("#", 1)[0].strip("/")
    parts = [part for part in path.split("/") if part]
    if parts and parts[0].casefold() == "repos":
        parts = parts[1:]
    if len(parts) < 2:
        return None

    owner = parts[0].strip()
    repository = parts[1].strip()
    if repository.casefold().endswith(".git"):
        repository = repository[:-4]

    if not _valid_repository_component(owner) or not _valid_repository_component(repository):
        return None
    return RepositoryRef(owner=owner, repository=repository)


def _parse_owner_repository_pair(owner_value: object, repository_value: object) -> RepositoryRef | None:
    owner = _text_value(owner_value).strip("/")
    repository = _text_value(repository_value).strip("/")
    if not owner or not repository or "/" in owner or "/" in repository:
        return None
    if not _valid_repository_component(owner) or not _valid_repository_component(repository):
        return None
    return RepositoryRef(owner=owner, repository=repository.removesuffix(".git"))


def _column_values(df: pd.DataFrame, column: str) -> list[object]:
    return df[column].tolist()


def _score_single_column(df: pd.DataFrame, column: str) -> tuple[int, int, float]:
    values = _column_values(df, column)
    nonempty = sum(bool(_text_value(value)) for value in values)
    valid = sum(parse_repository_reference(value) is not None for value in values)
    coverage = valid / nonempty if nonempty else 0.0
    alias_bonus = SINGLE_COLUMN_ALIASES.get(normalize_column_name(column), 0)
    score = valid * 10 + coverage * 100 + alias_bonus
    return valid, nonempty, score


def _is_owner_candidate(column: str) -> bool:
    normalized = normalize_column_name(column)
    return normalized in OWNER_COLUMN_ALIASES or any(
        token in normalized for token in ("owner", "organization", "organisation", "org")
    )


def _is_repository_candidate(column: str) -> bool:
    normalized = normalize_column_name(column)
    return normalized in REPOSITORY_COLUMN_ALIASES or any(
        token in normalized for token in ("repo", "repository", "project")
    )


def _pair_bonus(owner_column: str, repository_column: str) -> int:
    owner_name = normalize_column_name(owner_column)
    repository_name = normalize_column_name(repository_column)
    bonus = 0
    if owner_name in OWNER_COLUMN_ALIASES:
        bonus += 35
    if repository_name in REPOSITORY_COLUMN_ALIASES:
        bonus += 35
    return bonus


def _score_pair(df: pd.DataFrame, owner_column: str, repository_column: str) -> tuple[int, int, float]:
    owner_values = _column_values(df, owner_column)
    repository_values = _column_values(df, repository_column)
    nonempty = 0
    valid = 0
    for owner_value, repository_value in zip(owner_values, repository_values):
        if _text_value(owner_value) and _text_value(repository_value):
            nonempty += 1
            valid += _parse_owner_repository_pair(owner_value, repository_value) is not None
    coverage = valid / nonempty if nonempty else 0.0
    score = valid * 10 + coverage * 100 + _pair_bonus(owner_column, repository_column)
    return valid, nonempty, score


def resolve_repository_source(df: pd.DataFrame) -> RepositorySource:
    """Choose a single repository column or an owner/repository pair.

    Selection is based on values plus conservative column-name hints.  A
    source must parse at least half of its non-empty values; otherwise the
    function fails instead of silently guessing.
    """

    if df.empty or len(df.columns) == 0:
        raise CsvInputError("El CSV no contiene filas o columnas utilizables.")

    candidates: list[RepositorySource] = []
    for column in df.columns:
        valid, nonempty, score = _score_single_column(df, str(column))
        if valid and nonempty and valid / nonempty >= 0.5:
            candidates.append(
                RepositorySource(
                    mode="single",
                    columns=(str(column),),
                    score=score,
                    coverage=valid / nonempty,
                    valid_count=valid,
                    nonempty_count=nonempty,
                )
            )

    owner_columns = [str(column) for column in df.columns if _is_owner_candidate(str(column))]
    repository_columns = [
        str(column) for column in df.columns if _is_repository_candidate(str(column))
    ]
    if not owner_columns:
        owner_columns = [str(column) for column in df.columns[:30]]
    if not repository_columns:
        repository_columns = [str(column) for column in df.columns[:30]]

    for owner_column in owner_columns[:30]:
        for repository_column in repository_columns[:30]:
            if owner_column == repository_column:
                continue
            valid, nonempty, score = _score_pair(df, owner_column, repository_column)
            if valid and nonempty and valid / nonempty >= 0.5:
                candidates.append(
                    RepositorySource(
                        mode="pair",
                        columns=(owner_column, repository_column),
                        score=score,
                        coverage=valid / nonempty,
                        valid_count=valid,
                        nonempty_count=nonempty,
                    )
                )

    if not candidates:
        columns = ", ".join(repr(str(column)) for column in df.columns)
        raise CsvInputError(
            "No se pudo identificar una columna o combinación owner/repositorio "
            f"en el CSV. Columnas disponibles: {columns}"
        )

    candidates.sort(key=lambda candidate: candidate.score, reverse=True)
    best = candidates[0]
    if len(candidates) > 1 and abs(best.score - candidates[1].score) < 1e-9:
        alternatives = "; ".join(candidate.describe() for candidate in candidates[:5])
        raise CsvInputError(
            "La referencia del repositorio es ambigua; no se elegirá una columna "
            f"automáticamente. Alternativas: {alternatives}"
        )
    return best


def extract_repository_references(
    df: pd.DataFrame, source: RepositorySource
) -> list[RepositoryRef | None]:
    """Extract one normalized reference per input row."""

    if source.mode == "single":
        return [parse_repository_reference(value) for value in _column_values(df, source.columns[0])]

    owners = _column_values(df, source.columns[0])
    repositories = _column_values(df, source.columns[1])
    return [
        _parse_owner_repository_pair(owner, repository)
        for owner, repository in zip(owners, repositories)
    ]


def detect_matching_workflow_names(files: Iterable[Mapping[str, Any]]) -> list[str]:
    """Return matching base names for exact ``X.md`` + ``X.lock.yml`` pairs."""

    filenames = {
        str(item["name"])
        for item in files
        if item.get("type") == "file" and isinstance(item.get("name"), str)
    }
    md_names = {
        filename[:-3] for filename in filenames if filename.endswith(".md")
    }
    lock_names = {
        filename[:-9]
        for filename in filenames
        if filename.endswith(".lock.yml")
    }
    return sorted(md_names.intersection(lock_names))


def _decode_csv(raw: bytes) -> tuple[str, str]:
    encodings = ("utf-8-sig", "utf-8", "cp1252", "latin-1")
    for encoding in encodings:
        try:
            text = raw.decode(encoding)
            return text, encoding
        except UnicodeDecodeError:
            continue
    raise CsvInputError("No se pudo decodificar el CSV como UTF-8, cp1252 o latin-1.")


def _sniff_delimiter(text: str) -> str:
    sample = text[:100_000]
    try:
        dialect = csv_module.Sniffer().sniff(sample, delimiters=",;\t|")
        return dialect.delimiter
    except csv_module.Error:
        return ","


def read_input_csv(path: Path) -> tuple[pd.DataFrame, CsvInfo]:
    """Read a CSV while retaining original column names and values."""

    if not path.exists():
        raise CsvInputError(f"No existe el archivo CSV: {path}")
    if not path.is_file():
        raise CsvInputError(f"La ruta de entrada no es un archivo: {path}")

    raw = path.read_bytes()
    if not raw.strip():
        raise CsvInputError(f"El CSV está vacío: {path}")
    text, encoding = _decode_csv(raw)
    delimiter = _sniff_delimiter(text)

    format_warnings: list[str] = []
    try:
        header = next(csv_module.reader(io.StringIO(text), delimiter=delimiter))
    except StopIteration as exc:
        raise CsvInputError(f"El CSV no contiene encabezado: {path}") from exc

    stripped_header = [item.strip() for item in header]
    if any(not item for item in stripped_header):
        format_warnings.append("El encabezado contiene al menos una columna sin nombre.")
    duplicates = sorted(
        {item for item in stripped_header if item and stripped_header.count(item) > 1}
    )
    if duplicates:
        raise CsvInputError(
            "El CSV contiene encabezados duplicados, que impedirían conservar las "
            f"columnas sin cambios: {', '.join(duplicates)}"
        )

    try:
        df = pd.read_csv(
            path,
            sep=delimiter,
            dtype=str,
            keep_default_na=False,
            na_filter=False,
            encoding=encoding,
            on_bad_lines="error",
        )
    except (pd.errors.ParserError, UnicodeDecodeError, ValueError) as exc:
        raise CsvInputError(
            "El CSV no pudo interpretarse; revisa el separador, el encabezado y la "
            "cantidad de campos por fila."
        ) from exc

    if len(df.columns) == 0:
        raise CsvInputError("El CSV no contiene columnas.")
    if df.empty:
        format_warnings.append("El CSV tiene encabezado, pero no contiene filas de datos.")
    return df, CsvInfo(
        encoding=encoding,
        delimiter=delimiter,
        format_warnings=tuple(format_warnings),
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _api_message(response: Response) -> str:
    try:
        payload = response.json()
        message = payload.get("message") if isinstance(payload, dict) else None
        if isinstance(message, str):
            return message.strip()[:240]
    except (ValueError, TypeError):
        pass
    return ""


class GitHubClient:
    """Small sequential REST client with rate-limit-aware retries."""

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
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "gh-aw-detector/1.0",
            }
        )
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self.not_before = 0.0

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
        remaining_header = response.headers.get("X-RateLimit-Remaining")
        reset_header = response.headers.get("X-RateLimit-Reset")
        try:
            remaining = int(remaining_header) if remaining_header is not None else None
        except ValueError:
            remaining = None
        if remaining == 0 and reset_header:
            try:
                reset_at = float(reset_header)
                self.not_before = max(self.not_before, reset_at + 1.0)
            except ValueError:
                pass
        return remaining

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
        delay = max(exponential, retry_after or 0.0)
        self._sleep(delay + random.uniform(0.0, 1.0), reason)

    def inspect_workflows(self, repository: RepositoryRef) -> CheckResult:
        url = f"{API_BASE_URL}/repos/{repository.owner}/{repository.repository}/contents/{WORKFLOWS_PATH}"

        for attempt in range(self.max_retries + 1):
            self._wait_for_rate_limit()
            response: Response | None = None
            try:
                response = self.session.get(url, timeout=self.timeout)
            except requests.Timeout:
                if attempt < self.max_retries:
                    self._backoff(attempt, None, "timeout de GitHub")
                    continue
                return CheckResult(0, "network_error", [], "timeout de conexión", None)
            except requests.ConnectionError:
                if attempt < self.max_retries:
                    self._backoff(attempt, None, "error de conexión con GitHub")
                    continue
                return CheckResult(0, "network_error", [], "error de conexión", None)
            except requests.RequestException:
                if attempt < self.max_retries:
                    self._backoff(attempt, None, "error temporal de requests")
                    continue
                return CheckResult(0, "network_error", [], "error inesperado de requests", None)

            remaining = self._update_rate_state(response)
            status_code = response.status_code

            if status_code == 200:
                try:
                    payload = response.json()
                except ValueError:
                    return CheckResult(
                        0,
                        "unexpected_response",
                        [],
                        "respuesta 200 sin JSON válido",
                        status_code,
                    )
                if not isinstance(payload, list):
                    return CheckResult(
                        0,
                        "unexpected_response",
                        [],
                        "la API no devolvió una lista de contenidos",
                        status_code,
                    )
                if len(payload) >= 1000:
                    return CheckResult(
                        0,
                        "unexpected_response",
                        [],
                        "el listado alcanzó el límite de 1000 elementos de Contents API",
                        status_code,
                    )
                matches = detect_matching_workflow_names(payload)
                return CheckResult(
                    1 if matches else 0,
                    "detected" if matches else "not_detected",
                    matches,
                    "",
                    status_code,
                )

            if status_code == 404:
                message = _api_message(response)
                detail = (
                    "GitHub devolvió 404 para el repositorio o el directorio "
                    f"{WORKFLOWS_PATH}"
                )
                if message:
                    detail = f"{detail}: {message}"
                return CheckResult(0, "not_found", [], detail, status_code)

            if status_code == 401:
                return CheckResult(
                    0,
                    "auth_error",
                    [],
                    "token ausente, inválido o sin autenticación aceptada",
                    status_code,
                )

            message = _api_message(response)
            looks_like_rate_limit = status_code == 429 or remaining == 0 or any(
                marker in message.casefold()
                for marker in ("rate limit", "secondary rate", "abuse detection")
            )
            if status_code in (403, 429) and looks_like_rate_limit:
                if attempt < self.max_retries:
                    self._backoff(attempt, response, "rate limit primario/secundario de GitHub")
                    continue
                return CheckResult(
                    0,
                    "rate_limit_error",
                    [],
                    message or f"GitHub respondió HTTP {status_code} por rate limit",
                    status_code,
                )

            if status_code == 403:
                return CheckResult(
                    0,
                    "forbidden",
                    [],
                    message or "GitHub denegó el acceso al repositorio",
                    status_code,
                )

            if 500 <= status_code <= 599:
                if attempt < self.max_retries:
                    self._backoff(attempt, response, f"error temporal HTTP {status_code}")
                    continue
                return CheckResult(
                    0,
                    "temporary_error",
                    [],
                    message or f"GitHub respondió HTTP {status_code}",
                    status_code,
                )

            return CheckResult(
                0,
                "http_error",
                [],
                message or f"respuesta HTTP inesperada {status_code}",
                status_code,
            )

        return CheckResult(0, "temporary_error", [], "se agotaron los reintentos", None)


def _atomic_write_text(path: Path, content: str) -> None:
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(content, encoding="utf-8")
    os.replace(temporary_path, path)


def load_checkpoint(
    checkpoint_path: Path, input_path: Path, input_sha256: str
) -> dict[str, Any]:
    if not checkpoint_path.exists():
        return {"repositories": {}}
    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CsvInputError(
            f"El checkpoint existe pero no se pudo leer: {checkpoint_path}. "
            "Corrígelo o usa --reset-checkpoint."
        ) from exc
    if checkpoint.get("version") != CHECKPOINT_VERSION:
        raise CsvInputError(
            f"Versión de checkpoint no compatible: {checkpoint.get('version')}. "
            "Usa --reset-checkpoint para iniciar uno nuevo."
        )
    recorded_input = checkpoint.get("input", {})
    if recorded_input.get("sha256") != input_sha256:
        raise CsvInputError(
            "El checkpoint pertenece a otro contenido de entrada "
            f"({input_path}). Usa --reset-checkpoint si cambiaste el CSV."
        )
    repositories = checkpoint.get("repositories", {})
    if not isinstance(repositories, dict):
        raise CsvInputError("El checkpoint no contiene un mapa de repositorios válido.")
    return checkpoint


def save_checkpoint(
    checkpoint_path: Path,
    input_path: Path,
    input_sha256: str,
    source: RepositorySource,
    repositories: Mapping[str, Mapping[str, Any]],
) -> None:
    checkpoint = {
        "version": CHECKPOINT_VERSION,
        "updated_at": utc_now(),
        "input": {
            "path": str(input_path.resolve()),
            "sha256": input_sha256,
            "size_bytes": input_path.stat().st_size,
        },
        "source": asdict(source),
        "repositories": repositories,
    }
    _atomic_write_text(
        checkpoint_path,
        json.dumps(checkpoint, ensure_ascii=False, indent=2),
    )


def _records_for_output(
    df: pd.DataFrame,
    references: Sequence[RepositoryRef | None],
    repositories: Mapping[str, Mapping[str, Any]],
) -> pd.DataFrame:
    output = df.copy()
    reserved = {"gh_aw", "gh_aw_status", "gh_aw_matches", "gh_aw_error"}
    collisions = sorted(reserved.intersection({str(column) for column in output.columns}))
    if collisions:
        raise CsvInputError(
            "El CSV ya contiene columnas reservadas por la salida: "
            + ", ".join(collisions)
        )

    gh_aw: list[int] = []
    statuses: list[str] = []
    matches: list[str] = []
    errors: list[str] = []
    for reference in references:
        if reference is None:
            gh_aw.append(0)
            statuses.append("input_error")
            matches.append("")
            errors.append("no se pudo identificar owner/repository en esta fila")
            continue

        record = repositories.get(reference.key)
        if record is None:
            gh_aw.append(0)
            statuses.append("pending")
            matches.append("")
            errors.append("")
            continue

        gh_aw.append(int(record.get("gh_aw", 0)))
        statuses.append(str(record.get("status", "error")))
        matches.append("|".join(str(item) for item in record.get("matches", [])))
        errors.append(str(record.get("error", "")))

    output["gh_aw"] = gh_aw
    output["gh_aw_status"] = statuses
    output["gh_aw_matches"] = matches
    output["gh_aw_error"] = errors
    return output


def write_outputs(
    enriched: pd.DataFrame,
    enriched_path: Path,
    final_path: Path,
) -> None:
    """Write both CSVs atomically so an interruption cannot truncate them."""

    enriched_temporary = enriched_path.with_name(f".{enriched_path.name}.tmp")
    final_temporary = final_path.with_name(f".{final_path.name}.tmp")
    enriched.to_csv(enriched_temporary, index=False, encoding="utf-8-sig")
    final = enriched[enriched["gh_aw"] == 1]
    final.to_csv(final_temporary, index=False, encoding="utf-8-sig")
    os.replace(enriched_temporary, enriched_path)
    os.replace(final_temporary, final_path)


def print_input_summary(
    input_path: Path,
    df: pd.DataFrame,
    csv_info: CsvInfo,
    source: RepositorySource,
    references: Sequence[RepositoryRef | None],
) -> None:
    valid_count = sum(reference is not None for reference in references)
    unique_count = len({reference.key for reference in references if reference is not None})
    print(f"CSV: {input_path.resolve()}")
    print(f"Filas: {len(df):,}")
    print(f"Columnas ({len(df.columns)}): {', '.join(str(column) for column in df.columns)}")
    print(
        f"Formato: encoding={csv_info.encoding}, separador="
        f"{csv_info.delimiter!r}"
    )
    for warning in csv_info.format_warnings:
        print(f"Advertencia de formato: {warning}")
    print(
        f"Referencia seleccionada: {source.describe()} "
        f"({valid_count:,}/{len(references):,} filas válidas; "
        f"{unique_count:,} repositorios únicos)"
    )


def _default_paths(input_path: Path, output_dir: Path) -> tuple[Path, Path, Path]:
    stem = input_path.stem
    return (
        output_dir / f"{stem}_gh_aw_enriched.csv",
        output_dir / f"{stem}_gh_aw.csv",
        output_dir / f"{stem}.checkpoint.json",
    )


def run(args: argparse.Namespace) -> int:
    input_path = Path(args.input).expanduser()
    output_dir = Path(args.output_dir).expanduser()
    df, csv_info = read_input_csv(input_path)
    source = resolve_repository_source(df)
    references = extract_repository_references(df, source)
    print_input_summary(input_path, df, csv_info, source, references)

    if args.inspect_only:
        return 0

    if any(str(column) in {"gh_aw", "gh_aw_status", "gh_aw_matches", "gh_aw_error"} for column in df.columns):
        raise CsvInputError(
            "El CSV de entrada ya usa una columna reservada de salida "
            "(gh_aw/gh_aw_status/gh_aw_matches/gh_aw_error)."
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    default_enriched, default_final, default_checkpoint = _default_paths(
        input_path, output_dir
    )
    enriched_path = Path(args.enriched_output).expanduser() if args.enriched_output else default_enriched
    final_path = Path(args.final_output).expanduser() if args.final_output else default_final
    checkpoint_path = Path(args.checkpoint).expanduser() if args.checkpoint else default_checkpoint
    enriched_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

    if args.reset_checkpoint and checkpoint_path.exists():
        checkpoint_path.unlink()
        LOGGER.info("Checkpoint reiniciado: %s", checkpoint_path)

    input_sha256 = sha256_file(input_path)
    checkpoint = load_checkpoint(checkpoint_path, input_path, input_sha256)
    repositories: dict[str, dict[str, Any]] = {
        str(key): dict(value)
        for key, value in checkpoint.get("repositories", {}).items()
        if isinstance(value, dict)
    }

    unique_references: dict[str, RepositoryRef] = {}
    for reference in references:
        if reference is not None:
            unique_references.setdefault(reference.key, reference)

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        LOGGER.warning(
            "GITHUB_TOKEN no está definido. Se usará la API sin autenticación, "
            "con un límite de requests mucho menor."
        )
    client = GitHubClient(token, max_retries=args.max_retries, timeout=args.timeout)

    def persist() -> None:
        save_checkpoint(
            checkpoint_path,
            input_path,
            input_sha256,
            source,
            repositories,
        )
        enriched = _records_for_output(df, references, repositories)
        write_outputs(enriched, enriched_path, final_path)

    persist()
    total_unique = len(unique_references)
    newly_processed = 0
    since_checkpoint = 0
    skipped = 0

    try:
        for position, (key, reference) in enumerate(unique_references.items(), start=1):
            cached = repositories.get(key)
            cached_status = str(cached.get("status")) if cached else ""
            if cached and not (args.retry_errors and cached_status in ERROR_STATUSES):
                skipped += 1
                continue

            result = client.inspect_workflows(reference)
            repositories[key] = {
                **result.as_dict(),
                "repository": reference.full_name,
                "checked_at": utc_now(),
            }
            newly_processed += 1
            since_checkpoint += 1
            suffix = f" [{', '.join(result.matches)}]" if result.matches else ""
            LOGGER.info(
                "[%d/%d] %s -> GH-AW: %d%s (%s)",
                position,
                total_unique,
                reference.full_name,
                result.gh_aw,
                suffix,
                result.status,
            )

            if since_checkpoint >= args.checkpoint_every:
                persist()
                LOGGER.info(
                    "Checkpoint guardado: %d repositorios nuevos; salida incremental actualizada.",
                    newly_processed,
                )
                since_checkpoint = 0
    except KeyboardInterrupt:
        persist()
        LOGGER.warning(
            "Ejecución interrumpida. Se conservaron los resultados procesados en %s; "
            "puedes reanudar ejecutando el mismo comando.",
            checkpoint_path,
        )
        return 130

    persist()
    enriched = _records_for_output(df, references, repositories)
    status_counts = enriched["gh_aw_status"].value_counts().to_dict()
    detected_rows = int((enriched["gh_aw"] == 1).sum())
    LOGGER.info(
        "Proceso terminado. Filas GH-AW=1: %d; estados: %s",
        detected_rows,
        status_counts,
    )
    LOGGER.info("CSV enriquecido: %s", enriched_path.resolve())
    LOGGER.info("CSV final filtrado: %s", final_path.resolve())
    LOGGER.info(
        "Checkpoint: %s (%d nuevos, %d reutilizados)",
        checkpoint_path.resolve(),
        newly_processed,
        skipped,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Detecta pares X.md + X.lock.yml en .github/workflows/ de repositorios GitHub."
    )
    parser.add_argument("--input", required=True, help="Ruta al CSV original de SEART.")
    parser.add_argument(
        "--output-dir",
        default="data/output",
        help="Directorio de salidas (por defecto: data/output).",
    )
    parser.add_argument("--enriched-output", help="Ruta personalizada del CSV enriquecido.")
    parser.add_argument("--final-output", help="Ruta personalizada del CSV final filtrado.")
    parser.add_argument("--checkpoint", help="Ruta personalizada del checkpoint JSON.")
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=DEFAULT_CHECKPOINT_EVERY,
        help=f"Guardar cada N repositorios nuevos (por defecto: {DEFAULT_CHECKPOINT_EVERY}).",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
        help=f"Reintentos por error temporal (por defecto: {DEFAULT_MAX_RETRIES}).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"Timeout en segundos por request (por defecto: {DEFAULT_TIMEOUT}).",
    )
    parser.add_argument(
        "--retry-errors",
        action="store_true",
        help="Volver a consultar repositorios que quedaron con estado técnico de error.",
    )
    parser.add_argument(
        "--reset-checkpoint",
        action="store_true",
        help="Eliminar el checkpoint indicado y comenzar el procesamiento desde cero.",
    )
    parser.add_argument(
        "--inspect-only",
        action="store_true",
        help="Mostrar columnas, formato y referencia detectada sin llamar a GitHub.",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
        help="Nivel de logging (por defecto: INFO).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.checkpoint_every <= 0:
        parser.error("--checkpoint-every debe ser mayor que cero")
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
        LOGGER.exception("Error inesperado; revisa el log y el checkpoint.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
