# Detector de GitHub Agentic Workflows

Este proyecto identifica, a partir de un CSV de repositorios obtenido desde
SEART GitHub Search, cuáles utilizan GitHub Agentic Workflows (GH-AW).

## Criterio de detección

Para cada repositorio se consulta únicamente:

```text
GET /repos/{owner}/{repo}/contents/.github/workflows
```

El repositorio se marca con `gh_aw = 1` si el listado contiene al menos un
par exacto `X.md` y `X.lock.yml`, con el mismo nombre base `X`. Si la consulta
termina correctamente pero no existe el par, se marca `gh_aw = 0`.
No se analiza el contenido de los archivos, no se usa GitHub Code Search y no
se clonan repositorios.

## Requisitos e instalación

- Python 3.10 o superior.
- Un GitHub Personal Access Token con permisos de lectura para los
  repositorios que se desean revisar. Para repositorios públicos también se
  puede ejecutar sin token, pero el límite de requests es mucho menor.

Crear un entorno virtual e instalar dependencias:

```powershell
py -3 -m venv .venv
& .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

En Bash:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Configurar autenticación

PowerShell:

```powershell
$env:GITHUB_TOKEN="..."
```

Bash:

```bash
export GITHUB_TOKEN="..."
```

El token se lee solamente desde la variable de entorno, se envía en el header
de autenticación y no se imprime ni se guarda en archivos. Si la variable no
existe, el programa muestra una advertencia y continúa sin autenticación.

## Inspeccionar el CSV

El script no presupone nombres de columnas. Detecta automáticamente una
columna que contenga una URL o un valor `owner/repository`, o una combinación
de columnas `owner` + `repository`. La selección se informa en consola y las
columnas originales se conservan sin renombrarlas.

Antes de consultar GitHub se puede revisar el archivo así:

```powershell
python src\detect_gh_aw.py --input "ruta\al\archivo.csv" --inspect-only
```

El modo de inspección muestra el nombre exacto del archivo, filas, columnas,
separador, codificación, problemas de formato y la referencia seleccionada.
Si la referencia es ambigua o no se puede identificar, el programa termina
con un error explícito en vez de inventar una columna.

## Ejecución completa

PowerShell:

```powershell
$env:GITHUB_TOKEN="..."
python src\detect_gh_aw.py --input "ruta\al\archivo.csv" --output-dir data\output
```

Bash:

```bash
export GITHUB_TOKEN="..."
python src/detect_gh_aw.py --input "ruta/al/archivo.csv" --output-dir data/output
```

Si el archivo se copia como `data/input.csv`, el comando queda:

```powershell
python src\detect_gh_aw.py --input data\input.csv --output-dir data\output
```

Opciones útiles:

- `--checkpoint-every 25`: guarda cada 25 repositorios nuevos; es el valor
  predeterminado.
- `--retry-errors`: vuelve a consultar repositorios que quedaron con un error
  técnico en una ejecución anterior.
- `--reset-checkpoint`: reinicia el checkpoint después de cambiar el CSV.
- `--max-retries 5` y `--timeout 30`: ajustan reintentos y timeout por request.

## Archivos generados

Para un archivo de entrada llamado `input.csv`, se crean en `data/output/`:

- `input_gh_aw_enriched.csv`: todas las filas y columnas originales, más
  `gh_aw`, `gh_aw_status`, `gh_aw_matches` y `gh_aw_error`.
- `input_gh_aw.csv`: únicamente las filas con `gh_aw == 1`. Este es el archivo
  final para entregar en el Campus Virtual.
- `input.checkpoint.json`: resultados por repositorio para reanudar.

Los nombres se basan en el nombre real del CSV y también pueden personalizarse
con `--enriched-output`, `--final-output` y `--checkpoint`.

`gh_aw` siempre contiene `0` o `1`. Para no confundir una ausencia real con un
fallo técnico se debe revisar `gh_aw_status`:

- `detected`: se encontró al menos un par válido.
- `not_detected`: GitHub respondió correctamente y no se encontró ningún par.
- `not_found`: GitHub respondió 404 para el repositorio o el directorio.
- `input_error`: esa fila no permitió obtener `owner/repository`.
- `network_error`, `rate_limit_error`, `temporary_error`, `forbidden`,
  `auth_error`, `unexpected_response` o `http_error`: la comprobación no fue
  concluyente.

## Checkpoint y reanudación

El proceso es secuencial y deduplica repositorios repetidos antes de consultar
la API. Cada checkpoint guarda un hash SHA-256 del CSV, la estrategia de
columnas elegida y el resultado de cada repositorio. Al volver a ejecutar el
mismo comando, los repositorios ya guardados no se consultan nuevamente.

También se actualiza el CSV enriquecido en cada checkpoint. Si se interrumpe
la ejecución, se conservan los resultados hasta el último checkpoint y basta
con ejecutar de nuevo el comando. Por defecto, los errores quedan registrados
y no se repiten; usa `--retry-errors` para reintentarlos explícitamente.

El script respeta `X-RateLimit-Remaining` y `X-RateLimit-Reset`, espera ante
límite primario o secundario, reintenta respuestas 403/429 relacionadas con
rate limiting, errores 5xx, timeouts y errores de conexión, y evita
concurrencia agresiva.

## Tests

```powershell
python -m pytest -q
```

Los tests cubren el matching exacto de nombres y varios casos negativos, además
del parseo de referencias y la combinación de columnas `owner` y
`repository_name`.

## Limitaciones conocidas

- La GitHub Contents API devuelve como máximo 1.000 elementos para un listado
  de directorio; si se alcanza ese límite, el repositorio queda como
  `unexpected_response` para evitar clasificarlo incorrectamente.
- Un HTTP 404 en Contents API puede significar que falta `.github/workflows/`
  o que el repositorio no es visible para el token. Ambos casos quedan
  separados de `not_detected` mediante `not_found`.
- Los repositorios privados requieren un token con permisos suficientes.
- Este repositorio no incluye el CSV original ni los resultados generados para
  evitar publicar datos de la actividad o información sensible.
