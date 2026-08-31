"""Download the complete SEART GitHub Search export for a commit-date filter.

SEART exposes a dedicated CSV download endpoint.  This module deliberately
uses that export instead of replacing SEART with GitHub Search.  The response
is gzip-compressed; the decompressed bytes are copied to the input CSV without
parsing and reserializing them, so the source dataset is preserved.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode


SEART_API = "https://seart-ghs.si.usi.ch/api"
DEFAULT_COMMITTED_MIN = "2026-02-13"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_urls(committed_min: str) -> tuple[str, str]:
    query = urlencode({"committedMin": committed_min})
    search_url = f"{SEART_API}/r/search?{query}&page=0&size=1"
    download_url = f"{SEART_API}/r/download/csv?{query}"
    return search_url, download_url


def _curl_executable() -> str:
    executable = shutil.which("curl.exe") or shutil.which("curl")
    if not executable:
        raise RuntimeError(
            "No se encontró curl. Instala curl o ejecuta el proceso en un "
            "entorno con curl disponible."
        )
    return executable


def _curl_json(url: str) -> dict[str, Any]:
    executable = _curl_executable()
    completed = subprocess.run(
        [
            executable,
            "--fail",
            "--location",
            "--silent",
            "--show-error",
            "--retry",
            "4",
            "--retry-delay",
            "3",
            "--connect-timeout",
            "30",
            url,
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    payload = json.loads(completed.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError("La respuesta de búsqueda de SEART no es un objeto JSON.")
    return payload


def download_archive(url: str, archive_path: Path, force: bool = False) -> None:
    """Download a gzip export, retaining a partial file for safe retries."""

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if archive_path.exists() and not force:
        return

    part_path = archive_path.with_name(f".{archive_path.name}.part")
    executable = _curl_executable()

    def run_download(resume: bool) -> None:
        command = [
            executable,
            "--fail",
            "--location",
            "--retry",
            "5",
            "--retry-delay",
            "5",
            "--retry-all-errors",
            "--connect-timeout",
            "30",
            "--output",
            str(part_path),
            url,
        ]
        if resume:
            command[8:8] = ["--continue-at", "-"]
        subprocess.run(command, check=True)

    if force and part_path.exists():
        part_path.unlink()

    try:
        run_download(part_path.exists() and part_path.stat().st_size > 0)
    except subprocess.CalledProcessError:
        # Some HTTP servers do not support byte ranges on streamed exports.
        # In that case restart cleanly rather than appending a corrupt stream.
        if not part_path.exists():
            raise
        part_path.unlink()
        run_download(False)

    if not part_path.exists() or part_path.stat().st_size == 0:
        raise RuntimeError("SEART no produjo una exportación CSV no vacía.")
    os.replace(part_path, archive_path)


def decompress_archive(archive_path: Path, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f".{output_path.name}.tmp")
    try:
        with gzip.open(archive_path, "rb") as source, temporary_path.open("wb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
        os.replace(temporary_path, output_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def inspect_csv(path: Path) -> tuple[int, list[str], str, str]:
    """Return row count, columns, delimiter and detected text encoding."""

    encodings = ("utf-8-sig", "utf-8", "cp1252", "latin-1")
    for encoding in encodings:
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                sample = handle.read(100_000)
                handle.seek(0)
                try:
                    delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
                except csv.Error:
                    delimiter = ","
                reader = csv.reader(handle, delimiter=delimiter)
                header = next(reader)
                rows = sum(1 for _ in reader)
            return rows, header, delimiter, encoding
        except UnicodeDecodeError:
            continue
    raise RuntimeError(f"No se pudo decodificar el CSV generado por SEART: {path}")


def write_metadata(path: Path, metadata: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary_path, path)


def run(args: argparse.Namespace) -> int:
    output_path = Path(args.output).expanduser()
    metadata_path = Path(args.metadata).expanduser()
    archive_path = (
        Path(args.archive).expanduser()
        if args.archive
        else output_path.with_suffix(output_path.suffix + ".gz")
    )

    if output_path.exists() and not args.force:
        raise RuntimeError(
            f"Ya existe {output_path}. Usa --force solo si deseas reemplazarlo."
        )

    search_url, download_url = build_urls(args.committed_min)
    print("Consultando el total de resultados de SEART...")
    probe = _curl_json(search_url)
    total_items = probe.get("totalItems")
    total_pages = probe.get("totalPages")
    if not isinstance(total_items, int) or not isinstance(total_pages, int):
        raise RuntimeError(
            "La respuesta de SEART no contiene totalItems/totalPages; "
            "no se descargará un resultado incompleto."
        )
    print(f"SEART informa {total_items:,} repositorios ({total_pages:,} páginas).")

    if archive_path.exists() and not args.force:
        print(f"Reutilizando exportación gzip existente: {archive_path.resolve()}")
    else:
        print("Descargando la exportación CSV completa de SEART...")
        download_archive(download_url, archive_path, force=args.force)

    print("Descomprimiendo la exportación en el CSV de entrada...")
    decompress_archive(archive_path, output_path)
    rows, columns, delimiter, encoding = inspect_csv(output_path)
    if rows != total_items:
        raise RuntimeError(
            f"El CSV contiene {rows:,} filas, pero SEART informó {total_items:,}; "
            "se conserva el archivo para inspección, pero no se considera válido."
        )

    metadata = {
        "source": "SEART GitHub Search",
        "seart_site": "https://seart-ghs.si.usi.ch/",
        "search_endpoint": search_url,
        "download_endpoint": download_url,
        "download_format": "csv.gz (decompressed to CSV)",
        "filters": {"committedMin": args.committed_min},
        "extracted_at_utc": utc_now(),
        "probe_total_items": total_items,
        "probe_total_pages": total_pages,
        "input_path": str(output_path.resolve()),
        "input_rows": rows,
        "input_columns": columns,
        "input_delimiter": delimiter,
        "input_encoding": encoding,
        "input_sha256": sha256_file(output_path),
        "archive_path": str(archive_path.resolve()),
        "archive_sha256": sha256_file(archive_path),
        "probe_first_item": (
            probe.get("items", [{}])[0] if probe.get("items") else None
        ),
    }
    write_metadata(metadata_path, metadata)
    print(f"CSV SEART guardado: {output_path.resolve()}")
    print(f"Filas: {rows:,}; columnas: {', '.join(columns)}")
    print(f"Metadata: {metadata_path.resolve()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Descarga el CSV completo de SEART para un filtro de commits."
    )
    parser.add_argument(
        "--committed-min",
        default=DEFAULT_COMMITTED_MIN,
        help=f"Fecha mínima inclusiva de último commit (por defecto: {DEFAULT_COMMITTED_MIN}).",
    )
    parser.add_argument("--output", default="data/input.csv", help="CSV de salida.")
    parser.add_argument(
        "--metadata",
        default="data/seart_metadata.json",
        help="Archivo JSON con la metadata de extracción.",
    )
    parser.add_argument(
        "--archive",
        help="Ruta de una exportación .csv.gz existente o a descargar.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reemplazar el CSV y la exportación gzip existentes.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        return run(build_parser().parse_args(argv))
    except (OSError, RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
