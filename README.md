# Detector de GitHub Agentic Workflows

Este proyecto reconstruye el dataset inicial desde SEART GitHub Search y
detecta qué repositorios utilizan GitHub Agentic Workflows (GH-AW).

## Criterio

Un repositorio se marca con `gh_aw = 1` solamente si, dentro de
`.github/workflows/`, existen simultáneamente:

```text
X.md
X.lock.yml
```

Los dos archivos deben tener exactamente el mismo nombre base `X`. No se
analiza su contenido, no se usa GitHub Code Search y no se clonan repositorios.

## Reconstrucción de SEART

`src/fetch_seart_dataset.py` utiliza la exportación CSV oficial de SEART:

```text
GET https://seart-ghs.si.usi.ch/api/r/download/csv?committedMin=2026-02-13
```

El filtro `committedMin` es inclusivo y corresponde a “último commit desde el
13 de febrero de 2026”. Antes de descargar, el programa consulta el endpoint
de búsqueda para comprobar `totalItems` y `totalPages`; luego descarga todos
los resultados de la exportación, no una sola página. El CSV descomprimido se
copia sin reserializar a `data/input.csv`. La fecha, URLs, filtro, cantidad,
columnas y hashes quedan en `data/seart_metadata.json`.

## Requisitos

- Python 3.10 o superior.
- `pandas`, `requests` y `pytest` de `requirements.txt`.
- `curl` para descargar la exportación gzip completa de SEART.
- Una autenticación de GitHub. Se prefiere `GITHUB_TOKEN`; si no existe, el
  detector intenta reutilizar la sesión autenticada de `gh` mediante
  `gh auth token`, sin imprimir ni guardar el token.

Instalación en PowerShell:

```powershell
py -3 -m venv .venv
& .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Instalación en Bash:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Ejecución completa

PowerShell, usando un token explícito:

```powershell
$env:GITHUB_TOKEN="..."
python src\fetch_seart_dataset.py --output data\input.csv
python src\process_gh_aw_graphql.py --input data\input.csv --retry-errors
```

Si `gh` ya está autenticado, se puede omitir la primera línea. En Bash:

```bash
export GITHUB_TOKEN="..."
python src/fetch_seart_dataset.py --output data/input.csv
python src/process_gh_aw_graphql.py --input data/input.csv --retry-errors
```

El procesamiento recomendado usa GraphQL por lotes de hasta 500 repositorios y
hasta 4 workers acotados, y lee exclusivamente el árbol `.github/workflows/`.
También existe
`src/detect_gh_aw.py`, una variante REST secuencial útil para ejecuciones
pequeñas o para revisar un CSV alternativo.

## Archivos generados

- `data/input.csv`: exportación original descomprimida de SEART.
- `data/seart_metadata.json`: metadata y hash de la extracción.
- `data/output/gh_aw_checkpoint.sqlite`: checkpoint incremental.
- `data/output/repositories_enriched.csv`: todas las filas originales más
  `gh_aw`, `gh_aw_status`, `gh_aw_matches`, `gh_aw_error`,
  `gh_aw_initial_matches` y `gh_aw_revalidated`.
- `data/output/gh_aw_repositories.csv`: solamente filas con `gh_aw = 1` y
  segunda validación positiva. Es el archivo para entregar.

`gh_aw = 0` se usa únicamente para una inspección concluyente sin coincidencia.
Los errores, repositorios no accesibles y pendientes dejan `gh_aw` vacío y se
diferencian mediante `gh_aw_status`/`gh_aw_error`.

## Checkpoint y reanudación

El detector guarda cada lote confirmado en SQLite. El checkpoint incluye el
hash SHA-256 del CSV y la estrategia de columnas seleccionada, por lo que no
se puede reutilizar accidentalmente con otro dataset. Al repetir el mismo
comando, los repositorios ya procesados no se vuelven a consultar. La opción
`--retry-errors` ejecuta una pasada adicional sobre errores técnicos y
repositorios no encontrados. La segunda validación de cada positivo también se
guarda en el mismo checkpoint.

La API respeta `X-RateLimit-Remaining`, `X-RateLimit-Reset` y `Retry-After`, y
reintenta 403/429 relacionados con límites, respuestas 5xx, timeouts y errores
de conexión. No se usa concurrencia agresiva.

## Tests

```powershell
python -m pytest -q
```

Los tests cubren el matching exacto `X.md` + `X.lock.yml`, casos negativos,
parseo de referencias y consultas REST simuladas.

## Limitaciones

- SEART es una base viva: una extracción posterior puede tener otra cantidad
  de repositorios.
- Un repositorio privado, eliminado o no visible para el token queda como
  `not_found`/error y no se incluye en el CSV final sin una verificación válida.
- La consulta GraphQL devuelve el árbol del branch por defecto (`HEAD`).
- El archivo completo de SEART y los CSV generados pueden ser grandes; no se
  incluyen automáticamente en el repositorio Git para evitar publicar datos de
  la actividad.
