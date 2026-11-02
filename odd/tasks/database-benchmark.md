# Database benchmark implementation

Objective: Deliver the benchmark practice in authorized phases, treating the playbook as fixed specification.  
Branch: `feature/database-benchmark` · Delivery strategy: `ask-on-risk` · Chain strategy: `feature-branch-chain` · Test runner: TBD (no code/tests in Phase 1).

## Specs

### S1 — Playbook specification (literal transcription)

The following is copied verbatim from `docs/playbook.md`; preserve its queries, profiles, CSV schema, and report sections.
# Medición de métricas en bases documentales

## MongoDB frente a PostgreSQL JSONB bajo contención

Universidad Politécnica de Chiapas  
Ingeniería en Tecnologías de la Información e Innovación Digital (IT2iD)  
Bases de Datos en la Nube · 2026

## Objetivo

Medir latencia, rendimiento y huella de recursos al ejecutar agregaciones JSON anidadas bajo límites estrictos de CPU (CFS throttling) y memoria con Docker cgroups v2, comparando un motor documental con un motor relacional que almacena documentos en JSONB.

## 1. Preguntas sobre las métricas

- **M1** ¿Cómo cambian la latencia P95 y P99 de las agregaciones sobre documentos anidados cuando se limita de forma estricta la CPU y la memoria?
- **M2** ¿Qué motor usa de forma más eficiente los buffers de ordenamiento y agrupamiento antes de volcar a disco temporal o fallar por falta de memoria (OOM)?
- **M3** ¿Cuál es la huella de CPU y memoria por cada 1 000 documentos procesados en el motor documental frente al motor con JSONB?

## 2. Motores objetivo

| Motor | Modelo de datos / versión | Indexación |
|---|---|---|
| MongoDB | Colección `orders` sin sharding; WiredTiger; `latest-stable` | Índice simple en `timestamp` y `order_id` |
| PostgreSQL | Tabla `orders` (`id` UUID PRIMARY KEY, `data` JSONB); `latest-stable` | PK en `id`; B-Tree sobre `timestamp` extraído del JSONB |

## 3. Perfiles de recursos (Docker / cgroups v2)

| ID | Nombre | CPUs | Memoria | `mem_swappiness` |
|---|---|---:|---:|---:|
| `PROFILE_A_BASELINE` | Sin contención (Baseline) | 4,0 | 4096 m | 0 |
| `PROFILE_B_MEDIUM` | Contención media | 2,0 | 2048 m | 0 |
| `PROFILE_C_SEVERE` | Contención severa (throttling activo) | 1,0 | 1024 m | 0 |

## 4. Especificación del dataset

Registros: 500 000 · formato JSON · 3 niveles de anidamiento · promedio 3 ítems por registro.

Atributos:

- `order_id` (UUID), `timestamp` (ISO-8601)
- `customer` (objeto: `id`, `tier`, `segment`, `geo[country, state, city, zip, coordinates]`)
- `items` (arreglo de objetos: `sku`, `category`, `brand`, `qty`, `price`, `tax_pct`, `discounts`)
- `payment` (objeto: `method`, `status`, `transaction_id`, `gateway_latency_ms`, `totals`)
- `metadata` (objeto: `channel`, `user_agent`, `session_id`, `flags`)

## 5. Cargas de trabajo (consultas)

### 5.1. Q1 — Unwind, Filter & Grouping (saturación de CPU)

Desanidar el arreglo de ítems, filtrar `price >50`, calcular subtotal (`qty * price`) y agrupar sumatoria y promedio por país y categoría.

MongoDB (pipeline):

```javascript
[
    { "$unwind": "$items" },
    { "$match": { "items.price": { "$gt": 50 } } },
    {
      "$group": {
        "_id": {
          "country": "$customer.geo.country",
          "category": "$items.category"
        },
        "total_revenue": { "$sum": { "$multiply": ["$items.qty", "$items.price"] } },
        "avg_revenue": { "$avg": { "$multiply": ["$items.qty", "$items.price"] } },
        "count": { "$sum": 1 }
      }
    }
]
```

PostgreSQL:

```sql
SELECT data->'customer'->'geo'->>'country' AS country,
      item->>'category' AS category,
      SUM((item->>'qty')::numeric * (item->>'price')::numeric) AS total_revenue,
      AVG((item->>'qty')::numeric * (item->>'price')::numeric) AS avg_revenue,
      COUNT(*) AS count
FROM orders, jsonb_array_elements(data->'items') AS item
WHERE (item->>'price')::numeric > 50
GROUP BY 1, 2;
```

### 5.2. Q2 — Massive Sort & Top-K (estrés de buffer)

**Ajuste aprobado para esta especificación de trabajo:** como el dataset tendrá `payment.totals.total` como importe final, Q2 ordena por esa ruta en MongoDB y PostgreSQL. La transcripción fuente en `docs/playbook.md` se conserva sin cambios.

**Variante aprobada para mantener las 36 celdas:** MongoDB `allowDiskUse: false`; PostgreSQL `work_mem = '4MB'`. Las variantes alternativas del playbook quedan fuera de esta matriz.

Filtrar órdenes completadas en un rango de fechas, ordenar descendente por `payment.totals.total` y obtener el Top-1000 sin índices previos en el total.

Variantes de estrés de memoria del playbook (la especificación de trabajo usa solo la variante aprobada arriba):

- MongoDB: `allowDiskUse: false` / `allowDiskUse: true`
- PostgreSQL: `SET work_mem = '4MB'` / `SET work_mem = '64MB'`

MongoDB (pipeline):

```javascript
[
    { "$match": {
        "payment.status": "completed",
        "timestamp": { "$gte": "2026-01-01T00:00:00Z" }
    } },
    { "$sort": { "payment.totals.total": -1 } },
    { "$limit": 1000 },
    { "$project": {
        "order_id": 1, "customer.id": 1, "payment.totals.total": 1
    } }
]
```

PostgreSQL:

```sql
SELECT data->>'order_id' AS order_id,
      data->'customer'->>'id' AS customer_id,
      (data->'payment'->'totals'->>'total')::numeric AS final_total
FROM orders
WHERE data->'payment'->>'status' = 'completed'
  AND (data->>'timestamp')::timestamptz >= '2026-01-01 00:00:00Z'
ORDER BY (data->'payment'->'totals'->>'total')::numeric DESC
LIMIT 1000;
```

## 6. Protocolo de ejecución

- Concurrencia: 1, 10, 50 clientes.
- Repeticiones por escenario: 3.
- Ciclo de vida:
  - Flush de caché: el usuario inicialmente lo mantuvo manual en WSL2. Para la matriz completa de Fase 4, autorizó el 2026-11-01 automatizar en WSL2 `sync; echo 3 > /proc/sys/vm/drop_caches` como root antes de cada repetición; no usar sudo interactivo ni cambiar sudoers. Fallar antes de medir si no se puede completar y reportar cada flush. Esta escritura libera page cache y objetos slab recuperables; reconstruirlos puede aumentar I/O y CPU. La transcripción fuente del playbook se conserva sin cambios en `docs/playbook.md`.
  - Warmup: 60 s · medición: 300 s · cooldown: 60 s
- Mediciones totales: 36 celdas de la matriz (2 motores × 3 perfiles × 2 queries × 3 concurrencias); cada celda × 3 repeticiones.

## 7. Telemetría y KPIs

Stack:

- cAdvisor: métricas de contenedor (cgroups v2).
- Node Exporter: métricas de host (I/O, disco, swap).
- Prometheus: `scrape_interval: 1s`.

Indicadores:

| KPI | Unidad | Tipo |
|---|---|---|
| `throughput_qps` | queries/s | Performance |
| `latency_p50_ms` | ms | Performance |
| `latency_p95_ms` | ms | Performance |
| `latency_p99_ms` | ms | Performance |
| `container_cpu_cfs_throttled_seconds_total` | s | OS / Contention |
| `container_memory_rss_bytes` | bytes | OS / Memory |
| `disk_spill_detected` | boolean | Engine / Internal |
| `oom_killed` | boolean | Stability |

## 8. Entregables

### 8.1. Repositorio de código

- `docker-compose.yml` con perfiles de recursos parametrizados
- `synthetic_data_generator.py` (dataset 500k)
- `benchmark_runner` (scripts k6 o Python multiproceso)
- `queries/` (agregaciones MongoDB y SQL PostgreSQL)
- `run_benchmarks.sh` (automatización de la matriz de mediciones)

### 8.2. Esquema CSV de mediciones

```csv
timestamp_epoch,engine,profile,query_id,concurrency,run_number,throughput_qps,latency_p50_ms,latency_p95_ms,latency_p99_ms,cpu_throttled_seconds,max_rss_mb,disk_spill,status
```

### 8.3. Secciones del reporte de métricas

1. Especificaciones del host y del entorno Docker
2. Método de medición y verificación del baseline
3. Resultados consolidados (throughput, P95/P99, tiempo de throttling)
4. Análisis gráfico (P99 vs concurrencia, throttling vs QPS, curva RSS)
5. Planes de ejecución (explain executionStats frente a EXPLAIN ANALYZE BUFFERS)
6. Interpretación: límites de memoria, volcado a disco y throttling de cgroups
7. Recomendaciones técnicas para despliegues documentales en la nube

Actividad de medición · Bases de Datos en la Nube · IT2iD · 2026.

### S2 — Decisions and approval gates

Ask the numbered items in `docs/decisiones.md` one at a time, recommend an option with its reason, record the user's response, and do not implement code that depends on an unanswered decision. Work one phase at a time and wait for approval before the next phase.

### S3 — Safety and reproducibility constraints

Do not execute benchmarks, cache flushing, or the 500,000-record load without explicit user approval for the current phase/session, and never write/fabricate result rows. The user explicitly approved creating and executing PostgreSQL and MongoDB loaders for 500,000 generated orders in benchmark services only; never use `tc-db`. The user explicitly authorized automatic WSL2 cache flushing before each repetition for the Phase 4 matrix only; do not change sudoers or establish a persistent service. PostgreSQL benchmark services publish host port 5434 to container port 5432; the runner's default DSN must use 5434 while preserving an explicit `POSTGRES_DSN` override. Never stop or alter the native PostgreSQL process on host port 5432. Never invent metrics, explain plans, or logs. Keep `results/` trackable; exclude generated dataset files, `.env`, and credentials. Ask before any unresolved requirement not covered by the playbook or decisions.

### S4 — Delivery phases

Phase 1 is documentation and decision closure only. Phase 2 builds one logical deliverable at a time after decisions close. Phase 3 provides a single pilot for the user to execute and reviews only the pasted evidence. Phase 4 builds tables, charts, and the seven-section report from real CSV data only.

### S5 — Automated Phase 4 completion

Solicitud exacta del usuario: “no puedes ahcer un scrip para dejar todoa automatizado”

Scope: provide one unattended pipeline that runs the approved 108-repetition matrix, or can attach a post-processing watcher to the already-running matrix, then validates the completed CSV and generates the Phase 4 report and charts without further manual prompts. It must not modify the active benchmark process, overwrite the source CSV, or overwrite existing report artifacts. On explicit request, it may also generate a separate provisional snapshot report from only the valid rows already present while the run continues; this snapshot must be clearly marked incomplete, must not fill or simulate missing rows, and must never overwrite or be mistaken for the final report. When recovering a stalled matrix, verify and stop only its exact runner process, preserve existing CSV rows, validate the partial matrix before resuming, and run only missing keys; never duplicate, replace, or silently rerun recorded results.

Acceptance criteria: the final report reuses `check_spec.validate_csv` and is generated only after all 108 keys and row invariants pass. A separately requested provisional snapshot validates each observed row and unique expected key, includes only the rows read from the CSV snapshot, states the observed row count versus 108, and is prominently marked incomplete/non-final; malformed, duplicate, or unsupported rows fail closed. Include all seven S4 report sections, every measured run, mean/median/min-max summaries, and the required P99/concurrency, throttling/QPS, and RSS charts. Show missing host/explain/spill evidence as unavailable or unknown; never infer it as a measured fact. Keep CSV inputs read-only and generate report artifacts in a new output directory that is never overwritten. Resume mode validates existing rows and matrix keys, skips already-recorded keys, and appends only missing keys; it never deletes or rewrites the partial CSV.

## Tasks

- T1 — S1-S3; route: inline; status: complete; commit: pending phase closure.
- T2 — S2-S4; route: inline; status: complete (Phase 1 contract and decisions closed).
- T3.1 — S1-S4; route: delegated (prompt-pipeline skill); status: complete (static and independent verification passed; native review acknowledged with non-blocking advisory R3-001); commit: `06bb4e3` (`docker-compose.yml`; user approved cAdvisor Docker socket with read-only host mounts, no `privileged: true`).
- T3.2 — S1-S4; route: delegated (prompt-pipeline skill); status: complete (static checks and focused independent review passed; native review acknowledged with informational advisory R3-001); commit: `979f89d` (`synthetic_data_generator.py`; `synthetic_data_generator.py (con parámetro de tamaño para pruebas de 5 000)`).
- T3.3 — S1-S4; route: delegated (prompt-pipeline skill); status: complete (static and independent review passed; native assessment: medium/under_budget, no review due yet); commit: `6302768` (`queries/`; `queries/ (agregaciones MongoDB y SQL PostgreSQL)`).
- T3.4 — S1-S4; route: inline corrections; status: complete (AST and whitespace checks passed; focused static checks and reviewer recheck passed; no runtime/cache/database test was run); commits: `d39265b`, `901f13e`, `fd50db9`, `3dc5c58`, `1000736` (one scenario per invocation; `run_benchmarks.sh` will switch profiles serially; data provisioning remains manual; caller-supplied CSV path required; manual flush prompt before every repetition; RDD disabled/unmanaged by user authorization).
- T3.5 — S1-S4; route: delegated (prompt-pipeline skill); status: complete (`bash -n`, diff check, and focused independent static verification passed; no script/service/benchmark was run); commit: `ac3dabc` (`run_benchmarks.sh` switches Compose profiles serially; data provisioning remains manual; caller-supplied CSV path required; manual cache flush remains in T3.4; RDD disabled/unmanaged by user authorization). Risk: item 5 (environment/deployment automation).
- T3.6 — S1-S4; route: delegated (prompt-pipeline skill); status: complete (AST/readback and independent static verification passed; no real CSV or checker execution was available/authorized); commit: `69ed566` (`check_spec.py` requires exactly 108 unique rows covering all matrix/repetition keys; it validates but never alters CSV data). Risk: item 6 (no runnable behavioral test under current restrictions).
- T4 [record_id: `DBB-T4-20261101-01`] — S1-S4; route: inline; status: complete for the Phase 3 pilot (the user-flushed MongoDB PROFILE_A_BASELINE/Q1/concurrency-1 pilot completed 3/3 actual repetitions with status=ok, disk_spill=false, and CPU throttling 0.000000; measurements are recorded in the pilot CSV. The supported operationProfiling.filter configuration and spill coverage were exercised against the live container. The filter performance overhead was not isolated, so this single-cell pilot supports no overhead or cross-engine conclusion. 26 mocked tests, AST, whitespace checks, and independent verification passed; existing pilot CSVs remain untouched; port correction remains uncommitted); commit: `51ba72e`; port correction uncommitted. Risk: item 1 (persistent database records), item 3 (CSV status classification consumed by analysis), and item 5 (local Compose publication and runner telemetry collection).
- T5 — S1-S4; route: inline; status: in progress (Phase 4 approved by the user; the 108-row matrix is running in the background at `results/matrix-final-20261101-autoflush.csv`. Profile A readiness passed: both databases reported 500,000 orders and cAdvisor/Node Exporter were up. MongoDB PROFILE_A_BASELINE/Q1/concurrency=1 completed all three repetitions with status=ok; the next scenario has begun. Three real rows are recorded so far. The earlier one-row CSV remains untouched; unrelated `tc-*` containers remain untouched).
- T5.2 — S3-S5; route: inline + delegated direct; status: in progress (queue-hang guard, partial-CSV validation, `--resume`, and finalizer evidence checks implemented; tests passed; resuming the remaining matrix and final report pending). Cache flush runs before each repetition and fails closed; profile readiness checks validate exact Mongo/PostgreSQL service counts and Prometheus targets before scenarios, retrying transient startup failures for up to 60 seconds. Preserve the earlier manual-flush partial CSV with its one real `ok` row. A report must not be generated from a partial/invalid matrix or overwrite an existing output directory; unavailable host/explain/spill evidence remains explicitly unavailable/unknown. The stopped run's 9 measured rows remain intact: 6 `ok`, 3 `error`; all three MongoDB PROFILE_A_BASELINE/Q1/concurrency=50 errors record `worker_forced_shutdown,worker_result_missing`. Resume validates and skips these existing keys; the hidden Compose password must be re-entered for the next run. Risk: item 1 (database effects), item 3 (CSV/report consumed by analysis), item 4 (worker lifecycle/concurrency), item 5 (benchmark services and telemetry).
- T5.1 — S3-S4; route: delegated implementation and independent verification; status: complete (the PostgreSQL loader is implemented; after fixing snapshot and COPY terminator errors, exactly 500,000 rows were imported and independently counted in each benchmark PostgreSQL profile A, B, and C. Each service was stopped after its count check. The 5434/DSN change and both loader fixes passed focused/full tests and independent review. The native PostgreSQL on 5432 and `tc-db` were not touched; no CSV or benchmark run occurred). Risk: item 1 (persistent database records), item 3 (runner connection configuration), and item 5 (host port publication).

### Planned feature-branch chain slices

- Tracker PR: draft/no-merge; not created (remote authorization pending).
- PR1, T3.1-T3.2: `06bb4e3`, `5094742`, `979f89d`, `4c2910f`; child of tracker.
- PR2, T3.3: `6302768`, `f921e82`; child of PR1.
- PR3, T3.4: `d39265b`, `901f13e`, `fd50db9`, `3dc5c58`, `1000736`; child of PR2; approved `size:exception` applies to this cohesive runner slice.
- PR4, T3.5: `ac3dabc`, `683085b`; child of PR3.
- PR5, T3.6: `69ed566`, `0f4ac24`; child of PR4.
- PR6, T4: `2387953`, `51ba72e`, plus task-record commit pending; child of PR5; pilot completed with user-performed cache flushes; final task-record commit pending.
- No PR or push has been performed; destination/credential authorization remains outstanding.

## Log

- L1 (2026-10-31): “Fase 1 – Contrato: crea lo que falte (`.gitignore`, `AGENTS.md`, `docs/playbook.md`, `docs/decisiones.md`, `docs/bitacora.md`, documento de feature ODD), hazme las preguntas de decisiones y regístralas. Sin código de la práctica. Push tras mi `ok`.”
- L2 (2026-10-31): The source playbook visually spans four pages. Text extraction contained mis-mapped glyphs; the Markdown transcription was corrected by checking the rendered source. The slide deck independently shows the 36-cell formula, while the Q2 variants remain unresolved under decision 1.
- L3 (2026-10-31): Engram rejected access because the host session is unregistered. Per project instructions, it was not retried, repaired, deleted, or synchronized; the blocker is recorded in `docs/bitacora.md`.
- L4 (2026-10-31): The user answered “36” for decision 1. The 36-cell count was recorded, and how to include both Q2 variants without increasing that count remains explicitly unresolved.
- L5 (2026-10-31): The user approved `mongo:9.0.4` for MongoDB after checking the official MongoDB release notes and official Docker image tags. Its digest remains deferred until the execution architecture is known.
- L6 (2026-10-31): The user approved `postgres:18.6` after checking official PostgreSQL release notes and Docker image tags. Image digests for both engines remain deferred until the execution architecture is known.
- L7 (2026-10-31): The user said “no, que quede manual” for `drop_caches`. The user will run it manually; the assistant will not execute or automate it. The exact command depends on decision 8.
- L8 (2026-10-31): The user approved MongoDB `usedDisk` log evidence and PostgreSQL `log_temp_files = 0` for actual-run spill detection, with `explain("executionStats")` as a separate check.
- L9 (2026-10-31): The user approved reporting per-run results together with mean, median, and minimum/maximum range.
- L10 (2026-10-31): The user approved retaining the playbook's `mem_swappiness: 0`, verifying its effective setting in the environment selected under decision 8, and reporting it as unverified if its effect cannot be confirmed.
- L11 (2026-10-31): The user approved setting both WiredTiger cache and PostgreSQL `shared_buffers` to 25% of each profile's memory limit: A 1024 MiB, B 512 MiB, C 256 MiB.
- L12 (2026-10-31): The user selected WSL2 for measurement; the collection method was subsequently approved in L13, subject to verifying cgroup visibility from the WSL2 runner.
- L13 (2026-10-31): The user accepted collecting `cpu.stat`/`throttled_usec` and sampling `memory.stat`/`anon` directly from the runner in WSL2, without adding cAdvisor or Prometheus. Verify cgroup visibility before relying on the metrics.
- L14 (2026-10-31): The user corrected the collection choice: use the complete stack specified by the playbook—cAdvisor for container metrics, Node Exporter for host metrics, and Prometheus with `scrape_interval: 1s`. This supersedes L13. Verify the stack is accessible in WSL2 before relying on its metrics.
- L15 (2026-10-31): The user chose to generate only `payment.totals`. The playbook's Q2 still sorts by `payment.final_total`, which is absent from the selected schema; resolve this inconsistency before implementation without changing the query or inventing a subfield.
- L16 (2026-10-31): The user clarified that `payment.totals` is an object containing multiple amounts. The playbook does not name its subfields or identify which one is the final total for Q2; resolve this before implementation without changing the query or inventing a subfield.
- L17 (2026-10-31): The user chose `payment.totals.total` as the final-amount field. Q2 still sorts by `payment.final_total`; do not change the playbook query without explicit authorization.
- L18 (2026-10-31): The user authorized changing Q2 in the working specification to sort by `payment.totals.total`; keep the source transcription in `docs/playbook.md` unchanged.
- L19 (2026-10-31): The user approved the strict Q2 memory variant to preserve the 36-cell matrix: MongoDB `allowDiskUse: false` and PostgreSQL `work_mem = '4MB'`. The alternative variants are excluded from the working matrix; the source transcription remains unchanged.
- L20 (2026-10-31): The user approved pinning Linux/AMD64 image manifests: `mongo:9.0.4` at `sha256:ce91889f87a369d312e50ecbc6c0b7f960e915bc4b69c4f6662402fa7011d242`, and `postgres:18.6` at `sha256:f4b419e350fd98d4a8429603295208976dea4c8fb8326d818d346d3e29f1db6f`. Re-resolve platform-specific digests if architecture changes.
- L21 (2026-10-31): The user approved the manual WSL2 pre-run command `sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'`. The assistant must not execute or automate it. Linux kernel documentation says value `3` drops page cache and reclaimable slab objects; rebuilding them can increase I/O and CPU.
- L22 (2026-10-31): The user approved starting Phase 2, requested a new branch not named `codex`, and asked to push all piece commits at the end. Created local branch `feature/database-benchmark`; no remote operation has occurred. Resolve push authentication/session requirements before any final push.
- L23 (2026-10-31): The user approved current `linux/amd64` monitoring images: `ghcr.io/google/cadvisor:v0.60.6@sha256:bd7c00644d65e6c9fc2c151cab70d2e86d7fe3e0182cfb9863120ec2e017cf01`, `prom/prometheus:v3.15.1@sha256:d445d1806b38f85b5c394a1b87f94113fde0473b45245922c2e8b1bd41fa68ae`, and `prom/node-exporter:v1.12.1@sha256:da83fae85603c4e47e6c68369a7d746e2dda683dc35ea2e234b4f171e0d92798`. Verify availability in WSL2 before using them for measurement.
- L24 (2026-10-31): cAdvisor's official Docker setup mounts host paths and Docker's daemon socket. Docker documentation warns that access to that socket grants root-level privileges. Await explicit user authorization before adding the socket mount; avoid `privileged: true` unless a later verified requirement makes it necessary.
- L25 (2026-10-31): The user authorized the Docker socket and required host paths in read-only mode for cAdvisor, without `privileged: true`. Do not start services or run a smoke test without separate approval for that session.
- L26 (2026-10-31): T3.1 implemented `docker-compose.yml`; `docker compose config -q` passed with a harmless process-environment password placeholder, and an independent isolated static review found no blockers. No services were started. The initial native RDD attempt was blocked because the PATH-selected `gentle-ai` v1.25.6 lacked `review` and `doctor`; see L27 for resolution.
- L27 (2026-10-31): The user approved prioritizing `C:\Users\jaras\go\bin` in user PATH while retaining both Gentle AI installations. New-process resolution now selects v4.3.1; `gentle-ai doctor` still warns that duplicate binaries remain. T3.1 native review was acknowledged; advisory R3-001 notes that a read-only Docker socket mount does not restrict Docker API operations.
- L28 (2026-10-31): The user answered “si” to the recommendation to include `subtotal`, `discount`, `tax`, and `total` under `payment.totals`, with `total = subtotal - discount + tax`. This closes the totals-key decision for T3.2.
- L29 (2026-10-31): T3.2 now streams a single JSON array to the fixed ignored path `dataset/orders.json`, defaults to 500,000 records, and accepts `--count`/`--size`. Python AST and help checks passed; the generator was not run and no dataset or smoke test was created. A focused independent review passed after one correction batch. Native RDD assessment is high and candidate consent remains pending.
- L30 (2026-10-31): T3.2 commit `979f89d` received native RDD acknowledgement. Advisory R3-001 is informational: a failed generation after exclusive file creation could leave a partial JSON file that blocks retry. No correction was opened; do not re-review this acknowledged candidate. The generator remains unexecuted.
- L31 (2026-10-31): T3.3 is the next authorized Phase 2 work unit. Preserve the playbook queries exactly except the user-approved `payment.totals.total` Q2 path and the approved MongoDB `allowDiskUse: false` / PostgreSQL `work_mem = '4MB'` settings. No database execution or smoke test is authorized.
- L32 (2026-10-31): Four query artifacts were added under `queries/`; static comparison and fresh independent review passed. Q1 logic matches S1, and Q2 contains only the approved field-path and strict-memory settings. No query or database service was executed.
- L33 (2026-10-31): T3.3 commit `6302768` was assessed as medium risk with `review_due: false` / `under_budget`; it remains in the open review slice. No query was executed.
- L34 (2026-10-31): The user confirmed “si” to preserve 36 cells with MongoDB `allowDiskUse: false` and PostgreSQL `work_mem = '4MB'`; the session-document alternatives `true` and `64MB` are not matrix scenarios. The runner must require a caller-supplied CSV output path. No benchmark or database execution is authorized.
- L35 (2026-10-31): The user confirmed “si” that T3.4 handles one scenario per invocation, T3.5 will switch Compose profiles serially, and data provisioning remains manual. No services or databases may be started or used in this task.
- L36 (2026-10-31): The user authorized `pymongo` and `psycopg` as explicit dependencies, without version pins or extras.
- L37 (2026-10-31): The user chose `feature-branch-chain` for delivery after the feature exceeded the ~400-line review budget.
- L38 (2026-10-31): The user approved a `size:exception` for the cohesive T3.4 runner slice, which is approximately 410 authored lines.
- L39 (2026-10-31): Native review found blocker R4-001: a worker blocked in a database query could hang past the measurement window because parent joins had no timeout. The user authorized clone-local RDD disablement after native status required manual intervention. A bounded shutdown wait using the existing cooldown duration now terminates remaining workers and classifies the run as `error`; runtime/database checks remain prohibited.
- L40 (2026-10-31): The first bounded-shutdown correction still had a late-result race and an unbounded post-terminate join. A second scoped correction uses a shared shutdown deadline, bounded termination waits, marks post-deadline queries `error`, and stops the scenario if a child remains alive. Focused static verification passed; no runtime test or database operation was run. Risk: item 4 (worker process lifecycle/concurrency). Clone-local RDD is off by user authorization; review remains unmanaged, not approved.
- L41 (2026-10-31): T3.4 completed in commits `d39265b` (`feat: add Python benchmark runner`) and `901f13e` (`fix: bound benchmark worker shutdown`). AST parsing, manifest readback, `git diff --check`, and focused static verification passed. No services, databases, runner, smoke test, or benchmark were executed. Engram mirror remains pending because no authoritative runtime session ID is registered; do not repair or synchronize Engram without approval.
- L42 (2026-10-31): T3.5 is the next authorized Phase 2 work unit. Preserve the user-approved serial Compose profile switching and manual data provisioning; require the caller-supplied CSV output path. Do not execute the script, start services, or run benchmarks.
- L43 (2026-10-31): The user answered “si” to adjusting T3.4 so it pauses before each of the three repetitions for the user to flush cache manually; cache-flush commands must not be automated or executed. Reopen T3.4 for this change; T3.5 must rely on the runner's per-repetition prompt rather than adding a per-scenario flush.
- L44 (2026-10-31): Added a manual flush confirmation before each repetition. The prompt refers to the playbook without printing or executing the cache-flush command; EOF stops before the repetition. AST, diff check, and focused static review passed; no runtime test or cache operation was run. Risk: none (the runner is new and this is the user-approved behavior).
- L45 (2026-10-31): T3.4 was reclosed after the approved manual cache-flush pause was added. Its prompt is user-facing only, appears before each repetition, and aborts on EOF; no cache command is executed. The working tree is ready to proceed to T3.5. Engram mirror remains pending.
- L46 (2026-10-31): T3.5 script performs 36 serial scenario calls, switches Compose profiles serially, pauses for manual database provisioning/readiness per profile, and passes the required CSV path. It refuses matching running services and cleanup reports stop failures with a nonzero result; no cache flush is automated. Static verification passed. Risk: item 5 (environment/deployment). No script or service operation was executed.
- L47 (2026-10-31): T3.5 implementation completed in commit `ac3dabc` (`feat: add serial benchmark matrix script`). Bash syntax, diff checks, and independent static verification passed; no script, database, service, or benchmark was run. RDD is disabled/unmanaged by explicit user authorization; no native approval is implied.
- L48 (2026-10-31): T3.6 is the next authorized Phase 2 work unit. `check_spec.py` must validate only real CSV input and never create/fabricate rows or metrics; no benchmark or database execution is authorized.
- L49 (2026-10-31): The user answered “es mejor las 108 filas” to the completeness decision. `check_spec.py` must require all 108 matrix/repetition rows, with unique keys; no partial CSV passes.
- L50 (2026-10-31): T3.6 checker requires the exact CSV schema, all 108 unique matrix/repetition keys, valid statuses and metric consistency, and read-only file handling. AST parsing, source readback, and independent static verification passed. No real CSV was supplied and the checker was not executed; no results were fabricated. Risk: item 6. RDD remains disabled/unmanaged by user authorization; Engram mirror pending.
- L51 (2026-10-31): T3.6 completed in commit `69ed566` (`feat: validate complete benchmark results CSV`). The feature-branch chain plan records five child slices; no remote operation was performed because destination/credential authorization is unresolved. Phase 2 is complete; Phase 3 requires user approval.
- L52 (2026-10-31): The user confirmed the Phase 3 pilot cell: MongoDB, profile A baseline, Q1, concurrency 1. The runner will produce three repetition rows; review them manually, because `check_spec.py` intentionally requires all 108 rows. No dataset exists at `dataset/orders.json`; data provisioning remains manual, and no runner/service/database was started by the agent.
- L53 (2026-10-31): The user authorized adding `load_mongodb_data.py` because MongoDB's documented `mongoimport --jsonArray` limit is 16 MB and the generator emits a large single JSON array. The loader validates and streams the existing array to batches in `test.orders`, refuses a nonempty collection, and never deletes data. User must generate/import data and run the pilot; the agent did not execute the loader or start services. Risk: item 1 (persistent database records).
- L54 (2026-10-31): Added `load_mongodb_data.py`; it copies the input to a private temporary snapshot, validates the entire JSON array, then imports that same snapshot in bounded batches into an empty `test.orders`. It never deletes existing data; partial imports can remain on failure. Independent static review closed the source TOCTOU finding and accepted the documented single-writer precondition. AST/diff checks passed; no dataset or database operation was executed. User-run data generation/import/pilot remains pending. Risk: item 1.
- L55 (2026-10-31): T4 loader committed as `51ba72e` (`feat: add MongoDB JSON array loader`). AST syntax, diff checks, and focused independent static verification passed. No dataset was generated, no MongoDB service started, and no import or pilot was executed. The user still owns data generation/loading and the one-cell run.
- L56 (2026-10-31): The user reported successful execution of `python3 synthetic_data_generator.py`: it wrote 500,000 synthetic orders to `dataset/orders.json`. This is user-generated data, not a benchmark result. The agent did not inspect or load the file; next check is whether PyMongo is available in WSL before starting the MongoDB import workflow.
- L57 (2026-11-01): The user started MongoDB, cAdvisor, and Node Exporter in WSL2; Prometheus could not bind host port 9090 because unrelated `tc-frontend` already publishes it. The user approved publishing Prometheus on `127.0.0.1:9091:9090`; updated `docker-compose.yml`, the runner's default `PROMETHEUS_URL`, and decision 13. Worker and parent static checks passed; independent review approved the scope, and user-run isolated WSL assertions plus Python AST parse passed. `gentle-ai review assess` and `gentle-ai doctor` are unavailable (`unknown command`). Prometheus restart, data import, and the three-row pilot remain pending. Engram mirror pending because no authoritative runtime session ID is available; the agent did not run services or a benchmark. Risk: item 5 for the port correction; item 1 for the pending data import.
- L58 (2026-11-01): The user restarted Prometheus on the approved host port with `docker compose --profile PROFILE_A_BASELINE up -d prometheus`. `docker compose ps` showed MongoDB, cAdvisor, Node Exporter, and Prometheus up; Prometheus was published on `127.0.0.1:9091->9090/tcp`. A user-run WSL request to the active-targets API reported both `cadvisor` and
ode-exporter` as `up`. Data import and the three-row pilot remain pending; no benchmark was run.
- L59 (2026-11-01): The user activated `~/.venvs/bd-benchmark-pilot` in WSL and confirmed PyMongo `4.19.0`. At that point, the 500,000-record MongoDB import had not yet been run and remained manual and pending.
- L60 (2026-11-01): The user ran `python load_mongodb_data.py` in WSL with the virtual environment active; the terminal ended with `Imported 500000 documents into test.orders.` This confirms user-run data import completion. The agent did not execute, retry, inspect, or delete database records. The three-row pilot remains pending.
- L61 (2026-11-01): After the WSL terminal closed and was reopened, the user reactivated `~/.venvs/bd-benchmark-pilot` and confirmed all four project containers remained up with `docker ps`. The pilot runner has not yet been executed.
- L62 (2026-11-01): The user executed the one-cell MongoDB Q1 pilot. The CSV now contains run numbers 1–3, each with `status=error` and blank CPU throttling, RSS, and spill fields, while QPS/latency fields are populated. This is not a valid pilot result; no rerun or CSV modification was performed. Diagnose Prometheus metric selection and MongoDB spill-log detection before retrying. User cache-flush compliance remains unconfirmed.
- L63 (2026-11-01): The user queried Prometheus for `container_memory_rss_bytes` and `container_cpu_cfs_throttled_seconds_total` grouped by `container_label_com_docker_compose_service`. The RSS query returned no series; the CPU result had no such label. This confirms the runner's current Prometheus selector does not yield the required telemetry; actual metric names and usable container labels must be verified before changing the runner or retrying the pilot.
- L64 (2026-11-01): The user queried the correct `container_memory_rss` metric and CPU throttling grouped by `(name, image, id)`. Both results had only the `id` label, with no Mongo-matching name/image; correlate those cAdvisor IDs with the Docker Mongo container before selecting metrics. No runner/config changes or benchmark retries were made.
- L65 (2026-11-01): The user approved a bounded correction of metric collection and diagnosis of MongoDB `disk_spill`, with no benchmark execution and no results CSV changes. Preserve the three existing pilot rows marked `error`; keep unresolved spill evidence unknown rather than inventing a value.
- L66 (2026-11-01): The runner now resolves one Compose container ID and queries cAdvisor by that observed `id`; RSS uses `container_memory_rss`. Six isolated unit tests, Python AST parsing, and `git diff --check` passed; no live Docker/MongoDB or result files were accessed. Static review confirms `getLog` can omit evidence due its bounded/truncated cache and slow-operation logging. Independent verification is pending: the collaboration runtime rejected the required `gpt-6.1-sol` verifier model, so no independent approval is claimed. Engram mirror remains pending because no authoritative runtime session ID is registered.
- L67 (2026-11-01): After the user authorized the available verifier substitute, independent review approved the telemetry correction against S1-S4 with no blockers. Parent reran the focused six-test unittest command and `git diff --check`; both passed. The old pilot rows remain untouched, MongoDB spill for those runs remains unknown without actual-run log evidence, and the corrected manual pilot has not been rerun. No benchmark, cache flush, database/Docker command, or build was run. Engram mirror remains pending.
- L68 (2026-11-01): Before the manual retry, preflight the runner's Compose-based container-ID lookup. `docker compose ps` requires the compose interpolation variable `POSTGRES_PASSWORD` even for Mongo-only selection, as the user previously observed. If it is absent in the current WSL shell, prompt for it with hidden input and export it only in that shell; never paste or persist the credential.
- L69 (2026-11-01): The user explicitly authorized the agent to execute the corrected one-cell MongoDB `PROFILE_A_BASELINE` Q1, concurrency-1 pilot (“nada lo 0puedes ejcutar , tu , te doy permiso”), superseding the earlier manual-only restriction for this attempt. Use a separate new CSV; preserve all prior rows and datasets. The authorization includes the protocol's manual cache flush before each of the three repetitions; do not load data or restart services.
- L70 (2026-11-01): The agent ran the authorized three-repetition pilot into the new `results/pilot-mongodb-profile-a-q1-c1-agent-run.csv`; all three runner statuses were `error`, but CPU/RSS were populated and `disk_spill` was blank. Read-only inspection of the current MongoDB log buffer found 63/95/122 parsed completed `Slow query` entries for runs 1/2/3, with zero parse errors and no `usedDisk` field in any completed entry. The spill detector returns unknown when no `usedDisk` values exist even when matching completed logs prove the indicator was absent; preserve this CSV as generated and correct the detector for future runs without rewriting its rows.
- L71 (2026-11-01): A fresh independent review found the prior no-spill inference unsafe because MongoDB's bounded global log may omit operations, and found malformed matching evidence could be overridden by `usedDisk:true`. The detector and tests were corrected; `False` is no longer inferred from missing `usedDisk`, and malformed matching evidence yields unknown before a positive result. Focused tests and the full test suite each passed (13 tests), plus AST parsing and diff check; independent targeted recheck approved both corrections. The user then said “sigue” after previously explicitly authorizing the agent to run the pilot, so a second run was executed into `results/pilot-mongodb-profile-a-q1-c1-assisted-rerun-20261101.csv`, with the manual cache flush before each repetition. All three statuses were still `error`; CPU throttling and RSS were populated, but `disk_spill` remained blank/unknown. Both CSVs remain unchanged after capture. Docker services stayed up and Prometheus targets were reported up. No next pilot should be run until an approved, complete actual-run spill-evidence method is available.
- L72 (2026-11-01): Read-only check of official MongoDB documentation confirms `getLog` returns only the most recent 1,024 events from an in-memory cache; the database profiler at level 2 captures all operations and writes to capped `system.profile`, but `system.profile.usedDisk` appears only when true. Profiling adds performance overhead and disk use, so do not enable it automatically. A complete operation-coverage mechanism and an approved profiling decision are still needed before treating absent `usedDisk` as a measured false or running another pilot.
- L73 (2026-11-01): The second new CSV has three `status=error` rows with CPU/RSS present and spill blank, but this does not identify one cause. The runner also sets worker status to `error` if an operation finishes after the five-minute measurement deadline (`benchmark_runner.py:145-149`), while missing spill evidence independently converts an otherwise healthy row to `error` (`:441-444`). It emits no reason per row, so do not attribute these statuses solely to spill; instrument/report the cause before any next run.
- L74 (2026-11-01): The user answered “si” to the request to add a minimal cause diagnostic, without changing CSV output or rerunning the benchmark. The runner now emits stable, sanitized reason codes to `stderr` only for `status=error`; successful output, status rules, and CSV fields/rows are unchanged. It covers query failure, deadline overrun, startup/readiness/result/shutdown issues, unknown spill, and missing CPU/RSS. Regression tests drove both early readiness and startup-error paths with unreaped workers; an initial independent review found a missing `worker_unreaped` reason in those exits, which was fixed and rechecked. Focused tests: 19 passed; AST parsing and `git diff --check` passed. No benchmark, services, cache flush, database, or CSV was accessed. Native `gentle-ai review assess` remains unavailable (`unknown command "review"`). Risk: none for this additive diagnostic; it does not change measurement/status behavior.
- L75 (2026-11-01): The user requested “`gentle-ai review assess` sigue sin estar disponible. ncehchalo y corríjelo, y depeus sigue con el proyecto”. Diagnosis: the current Codex process inherited a stale PATH with Scoop's v1.25.6 shim before Go's v4.3.1 binary; the persistent User PATH already puts `C:\Users\jaras\go\bin` first. No persistent PATH change or duplicate removal was needed. Temporarily prepending Go bin for command invocations resolved the `review assess` command. The first assess was blocked by untracked inventory; after read-only status refreshed the hash and selecting only `tests/test_benchmark_runner_metrics.py` (leaving untracked result files excluded), assessment completed with `risk=high`, reason `process_boundary`, `review_due=true`. `review mode status` confirmed clone-local RDD is off; no review was started. The current shell environment may need Codex restart before bare command lookup reflects the already-correct User PATH.
- L76 (2026-11-01): Continuing T4, the user-authorized MongoDB PROFILE_A_BASELINE/Q1/concurrency-1 pilot ran in `results/pilot-mongodb-profile-a-q1-c1-diagnostic-rerun-20261101.csv`, with the manual cache flush before each repetition. All three actual rows are `status=error`; terminal diagnostics for each report `measurement_deadline_exceeded,spill_evidence_unknown`. CPU throttling and RSS are present; `disk_spill` is blank. Older CSVs were untouched. This confirms both a successful query crossing the 300-second boundary and unavailable negative spill evidence, but the playbook does not define whether the former should mark the row `error`. No further pilot should run until the boundary status semantics and complete spill-evidence method are resolved.
- L77 (2026-11-01): El usuario respondió “si” a “¿Confirmás que corrija esa regla de límite de tiempo, sin tocar los CSV existentes ni repetir el piloto todavía?”. Se cambió `run_worker` para excluir una consulta exitosa que termina después del corte sin marcar error por ese hecho; los fallos independientes siguen como error, y la repetición completa todavía puede quedar en error si falta evidencia de spill o telemetría. RED reprodujo el estado `error`; GREEN dejó 21 pruebas enfocadas aprobadas. AST, `git diff --check` y verificación independiente en scratch aprobaron; el baseline reprodujo el estado anterior. `gentle-ai review assess` indicó `high` (item 3 y `process_boundary`), pero RDD está desactivado en el clon y no se inició revisión nativa. No se ejecutó piloto, no se tocaron CSV, servicios, base de datos ni caché. Riesgo de esta corrección: item 3 (semántica del estado CSV consumida por análisis). El espejo Engram queda pendiente porque el runtime no proveyó session ID.
- L78 (2026-11-01): El usuario autorizó continuar con `gpt-6-luna` para la revisión independiente, manteniendo el límite de no usar MongoDB ni ejecutar el benchmark. La captura de spill cambió de `getLog` acotado a logs retenidos de Compose, con identificadores únicos por consulta y filtro de diagnóstico `bench-`; el profiler continúa apagado y la configuración se monta en lectura. Las 25 pruebas simuladas, AST, `git diff --check` y revisión independiente aprobaron; no se validó el comportamiento real del filtro ni su costo, por lo que T4 sigue pendiente de esa prueba bajo autorización del usuario. `gentle-ai review assess` devolvió `high/unassessable` por falta de inventario canónico de untracked; el `review status` indicado terminó con código 1 sin salida. No se inició revisión nativa porque RDD está apagado en el clon. No se ejecutaron Docker, servicios, bases de datos, benchmark ni caché, ni se inspeccionaron o modificaron CSV. El espejo Engram sigue pendiente por ausencia de un session ID registrado.
- L79 (2026-11-01): El usuario confirmó “las onfirmo , que signan en esa ram ay por aprte si le puedes asigniar id neuiva” y aclaró “al registro t4 , siguele”. Se conserva la rama `feature/database-benchmark` y se asigna al registro T4 el identificador local `DBB-T4-20261101-01`, sin cambiar el ID estable de tarea `T4` ni sus vínculos S1-S4.
- L80 (2026-11-01): El usuario pidió continuar con las tareas pendientes y preguntó por las “12 horas”. No es un límite: la matriz completa estima 108 repeticiones × 420 s (60 s warmup + 300 s medición + 60 s cooldown) = 12 h 36 min de tiempo activo, más vaciados manuales y preparación; el piloto T4 son 3 repeticiones (21 min mínimos). Aunque Compose no declara explícitamente `/data/db`, una inspección de solo lectura encontró un volumen anónimo en esa ruta; se comprobó que la actualización conservó el mismo volumen. No se inspeccionaron documentos ni CSV.
- L81 (2026-11-01): Ante la petición del usuario de ejecutar y continuar, se inspeccionó el montaje del contenedor (sin acceder a datos), se aplicó el cambio de Compose y el primer arranque reveló el error `Unrecognized option: systemLog.filter.attr.command.comment.$regex` (código 2). La causa fue colocar el filtro bajo `systemLog`; MongoDB requiere `operationProfiling.filter` como documento de consulta en una cadena YAML. Se añadió una prueba de regresión: RED, 1 fallo entre 26; tras corregir el archivo, GREEN, 26 aprobadas, AST y `git diff --check` aprobados. El verificador independiente aprobó; por descuido ejecutó su primer chequeo `python -B` fuera del scratch y luego repitió todo en el scratch; no accedió a resultados ni modificó datos. El contenedor arrancó con `mongod startup complete` y el mismo volumen anónimo `/data/db`; Prometheus reportó `cadvisor: up`, `node-exporter: up`, y WSL encontró el venv. El piloto T4 se inició en `results/pilot-mongodb-profile-a-q1-c1-filter-validation-20261101.csv` y está pausado antes de la repetición 1/3 para que el usuario haga el vaciado manual aprobado. No se ejecutó el vaciado ni empezó medición. Riesgo: item 5 (configuración de arranque y telemetría); no hubo pérdida de volumen.
- L82 (2026-11-01): El piloto MongoDB PROFILE_A_BASELINE/Q1/concurrency 1 terminó las tres repeticiones reales en results/pilot-mongodb-profile-a-q1-c1-filter-validation-20261101.csv; todas quedaron status=ok, disk_spill=false y cpu_throttled_seconds=0.000000. El filtro soportado operationProfiling.filter y la cobertura de eventos de spill se ejercitaron contra el contenedor real. No se aisló el overhead del filtro, así que esta celda no sustenta conclusiones de rendimiento ni comparaciones entre motores. La Fase 3 del piloto queda cerrada; T5/Fase 4 sigue pendiente de aprobación explícita y de completar las 108 filas reales. No se modificaron CSV anteriores ni se ejecutó el vaciado de caché por el asistente; el usuario lo realizó manualmente antes de cada repetición. El espejo Engram sigue pendiente: la sesión runtime no está registrada.
- L83 (2026-11-01): El usuario aprobó iniciar Fase 4. La revisión de solo lectura encontró seis CSV de piloto, cada uno con tres filas de datos, y ningún CSV de matriz completa. run_benchmarks.sh exige provisionar manualmente ambos motores y confirmar readiness para cada perfil, además se niega a cambiar servicios de benchmark ya activos. Ahora están activos MongoDB PROFILE_A_BASELINE, cAdvisor, node-exporter y Prometheus; los contenedores tc-* ajenos a este proyecto no se tocaron. No se corrió benchmark ni se modificaron bases, caché o CSV. Fase 4 queda pendiente de provisionamiento por perfil y un entorno sin servicios de benchmark en ejecución.
- L84 (2026-11-01): Tras la autorización explícita, se verificaron etiquetas Compose y se detuvieron solo los cuatro contenedores aprobados: MongoDB profile A, cAdvisor, node-exporter y Prometheus del proyecto database-benchmark. Los cuatro quedaron Exited; tc-frontend, tc-backend y tc-db continuaron Up. No se eliminó ningún contenedor ni volumen y no se ejecutaron pruebas o cargas.
- L85 (2026-11-01): El usuario respondió “si” a “¿Autorizás ese cambio y esa carga?”. Queda autorizada la implementación y ejecución de un cargador PostgreSQL para los servicios PostgreSQL del benchmark, con 500,000 registros del dataset existente. La autorización no incluye `tc-db`, automatizar `drop_caches`, alterar consultas o CSV, ni iniciar por sí sola la matriz completa. El acuerdo anterior de provisionamiento manual queda reemplazado solo para PostgreSQL.
- L86 (2026-11-01): Se implementó `load_postgresql_data.py` y su prueba `tests/test_load_postgresql_data.py`. El cargador valida y copia el dataset antes de conectar, limita el destino por etiquetas del proyecto/servicio Compose, rechaza `public.orders` preexistente, importa en una transacción, verifica 500,000 filas antes del commit y nunca ejecuta DROP/DELETE. El worker reportó 4 pruebas enfocadas, 30 de suite, AST y `git diff --check` aprobados; el padre repitió y aprobó las 4 enfocadas y el diff check. Un verificador independiente aprobó sin bloqueos; advisory: el índice B-tree sobre el texto ISO UTC extraído podría no servir a Q2, que castea a `timestamptz`; no se cambió la consulta. No hubo acceso a Docker/psql, carga ni CSV. La importación real sigue pendiente porque Compose necesita `POSTGRES_PASSWORD` en el proceso WSL activo; no compartir el secreto por chat.
- L87 (2026-11-01): Tras el reporte del usuario de que la terminal cerró, la inspección confirmó que solo existe `database-benchmark-postgres-profile-a-1` en estado Created, sin logs; los perfiles B/C no se crearon. Un intento de iniciar A falló antes de la carga: Windows denegó el bind `127.0.0.1:5432` porque un proceso local `postgres` (PID 7008) escucha ese puerto. No se detuvo ese proceso. El puerto 5434 no tenía listener ni publicación Docker al comprobarlo. No se escribió ninguna fila ni se modificaron CSV. Se requiere aprobación antes de cambiar el puerto publicado/DSN del benchmark.
- L88 (2026-11-01): El usuario respondió “si” a la autorización para publicar el PostgreSQL del benchmark en el puerto host 5434 y configurar su conexión, conservando intacto el PostgreSQL local que ocupa 5432. Se actualizará el mapeo de los tres perfiles y el DSN predeterminado del runner; un `POSTGRES_DSN` explícito seguirá teniendo precedencia.
- L89 (2026-11-01): Se aplicó la decisión 16: los tres servicios PostgreSQL publican `127.0.0.1:5434:5432`; `postgres_dsn_from_env` preserva `POSTGRES_DSN` explícito o deriva un URI local para el puerto 5434 con password percent-encoded. RED falló porque faltaba el helper; GREEN: 4 pruebas enfocadas, suite completa con 34 aprobadas, AST y `git diff --check` aprobados. No se inició Docker ni se cargaron filas; revisión independiente del cambio y ejecución real pendientes. Riesgo: item 3 (configuración de conexión del runner) e item 5 (publicación de puerto del entorno).
- L90 (2026-11-01): El primer intento de carga autorizada de PROFILE_A inició el contenedor PostgreSQL en el puerto host 5434, pero se detuvo en `load_postgresql_data.py` antes de resolver el contenedor o escribir filas: `ValueError: underlying buffer has been detached`. Causa: `TextIOWrapper.detach()` se ejecutaba dentro de un `with`, cuyo `__exit__` intentaba cerrar el wrapper ya desprendido. RED: la nueva prueba del wrapper falló por helper ausente; GREEN: `with_snapshot_text` ahora hace detach en `finally` sin cerrar el snapshot binario reutilizable. Pasaron 5 pruebas enfocadas, 35 de suite, AST y diff check. Verificación independiente aprobó; el fallo anterior fue reproducido y la excepción del callback se probó en scratch. Sin filas importadas, CSV, Mongo, tc-db ni benchmark. Se reintentará PROFILE_A únicamente.
- L91 (2026-11-01): Un segundo intento en PROFILE_A expuso el formato final de `COPY`: el sufijo añadía una línea en blanco (`COPY orders, line 500001: ""`) y psql no devolvía fallo detectable, por lo que el loader informó éxito aunque la transacción fue revertida. Se confirmó con el log del PostgreSQL del benchmark que `public.orders` no quedó creada; los 500,000 rows reportados no estaban persistidos. RED: dos regresiones fallaron para el terminador COPY y `ON_ERROR_STOP=1`; GREEN: el terminador sigue directamente a la última fila, y psql detiene al primer error SQL para que su salida no pueda aparentar éxito. Pasaron 7 pruebas enfocadas, 37 de suite, AST y diff; el verificador independiente aprobó sin bloqueos. PROFILE_A reintentado: cargador informó 500,000 y la consulta independiente `COUNT(*)` confirmó 500000; luego el servicio quedó detenido. PROFILE_B_MEDIUM y PROFILE_C_SEVERE también importaron 500,000 cada uno, con conteo independiente 500000 y contenedor detenido. Las transacciones fallidas no retuvieron tabla/fila; ningún CSV, Mongo ni `tc-db` fue modificado. La matriz de benchmark no se inició.
- L92 (2026-11-01): Tras autorización del usuario, se importaron 500,000 órdenes en MongoDB PROFILE_B_MEDIUM y PROFILE_C_SEVERE mediante el cargador existente; una consulta independiente confirmó countDocuments=500000 en cada uno y ambos quedaron detenidos. El primer arranque de `run_benchmarks.sh` llegó a verificar A y el monitoreo, pero la ejecución hijo falló al resolver `__file__` desde invocación relativa; ningún benchmark ni CSV comenzó. El script ahora invoca `"$SCRIPT_DIR/benchmark_runner.py"`; RED/GREEN, 1 prueba enfocada, 38 suite, `bash -n`, diff check y revisión independiente aprobaron. Reintento del script: perfil A iniciado; conteos Mongo/PG 500,000 y objetivos cAdvisor/node-exporter up verificados. Está pausado antes de repetición 1/3 para que el usuario ejecute manualmente el vaciado de caché. La matriz CSV aún no existe.
- L93 (2026-11-01): El usuario confirmó el vaciado manual de caché. El runner ejecutó MongoDB PROFILE_A_BASELINE/Q1/concurrency 1, repetición 1/3: `status=ok`; la fila real fue añadida a `results/matrix-final-20261101.csv`. El runner espera el vaciado manual previo a la repetición 2/3. No se ejecutó el vaciado por el asistente.
- L94 (2026-11-01): El usuario respondió “si” a cambiar la decisión manual y automatizar todo para que no tenga que estar presente. Para la matriz de Fase 4 queda autorizado ejecutar como root en WSL2 `sync; echo 3 > /proc/sys/vm/drop_caches` antes de cada repetición, sin sudo interactivo, cambio de sudoers ni servicio permanente. Se preserva el CSV parcial de una fila y el nuevo recorrido completo usará otro archivo; también se automatizará la verificación de datos y readiness por perfil. El resultado real y el costo de drop_caches siguen reportándose.
- L95 (2026-11-01): Se implementó el flush WSL2 antes de cada repetición con fallo seguro y sin prompt manual, junto con readiness automática por perfil (Mongo ping y conteo, PostgreSQL conteo y targets Prometheus); pruebas enfocadas, suite completa, `bash -n`, `git diff --check` y verificación independiente pasaron. La revisión detectó un error de clasificación en una excepción de arranque del worker; se corrigió y su recheck dirigido pasó. No se ejecutó flush, benchmark, Docker ni se tocó CSV en estas verificaciones. El arranque de la matriz está bloqueado porque el proceso WSL actual no tiene `POSTGRES_PASSWORD`; `docker compose ps` falla en interpolación antes de mostrar servicios. Sigue pendiente iniciar el CSV completo en una ruta nueva.
- L96 (2026-11-01): Primer inicio automático: Compose arrancó MongoDB, PostgreSQL y monitoreo, pero el chequeo inmediato de MongoDB consultó antes de `mongod startup complete` (el proceso tardó varios segundos en abrir el puerto; al repetir el chequeo después respondió y confirmó 500000). El gate falló cerrado antes de cualquier escenario; el runner detuvo solo los servicios de benchmark y monitoreo que había iniciado; el CSV parcial quedó en 2 líneas y el CSV nuevo no se creó. Se agregaron retries acotados a 60 s ante fallos de conexión/probe para MongoDB, PostgreSQL y targets Prometheus; conteos incorrectos siguen fallando de inmediato. RED/GREEN, suite completa (46, 2 omitidas), `bash -n`, `git diff --check` y verificación independiente aprobaron en scratch. Ningún benchmark ni cache flush se ejecutó. Reintento de la matriz sigue pendiente de ingreso seguro de la contraseña en el proceso de lanzamiento.
- L97 (2026-11-01): Tras el reingreso oculto de la clave, el proceso de matriz quedó activo en segundo plano con destino nuevo `results/matrix-final-20261101-autoflush.csv`; `POSTGRES_PASSWORD` no se escribió a archivos. PROFILE_A_BASELINE pasó readiness (MongoDB=500000, PostgreSQL=500000, cAdvisor y Node Exporter up), y el flush WSL2 autorizado completó antes de la repetición 1/3. Aún no había finalizado ninguna repetición al comprobar; el CSV parcial manual conservaba 2 líneas y el nuevo CSV todavía no existía. La ejecución continúa en background; faltan las otras repeticiones/perfiles, validación de 108 filas y reporte.
- L98 (2026-11-01): La primera celda MongoDB PROFILE_A_BASELINE/Q1/concurrency=1, repetición 1, terminó con `status=ok`; su fila real aparece en el CSV nuevo (latencia P95 5686.929 ms, P99 7030.232 ms, RSS máximo 647.234 MB, CPU throttling 0, spill=false). Se completó el flush previo a la repetición 2/3; la matriz permanece activa. El CSV manual sigue con su fila original y no fue reutilizado.
- L99 (2026-11-01): MongoDB PROFILE_A_BASELINE/Q1/concurrency=1 completó las repeticiones 1/3, 2/3 y 3/3, todas `status=ok`, tras vaciado automático antes de cada una. El CSV nuevo contiene ahora 3 filas de datos reales; la matriz permanece activa y ya inició el escenario siguiente. El CSV parcial manual sigue intacto con 1 fila de datos.
- L100 (2026-11-01): El usuario pidió literalmente “no puedes ahcer un scrip para dejar todoa automatizado”. Se amplía T5.2 para que la automatización posterior espere la finalización de la matriz activa, valide el CSV completo con `check_spec.py` y produzca el reporte de siete secciones y los tres gráficos sin intervención adicional. La revisión de los documentos fuente confirma que host hardware y planes `explain` no están capturados en la matriz; el reporte debe marcarlos como no capturados, no inventarlos. No se debe modificar la corrida activa ni sus CSV; el espejo Engram queda pendiente porque el runtime no proporcionó una identidad de sesión registrada.
- L101 (2026-11-01): Se implementó `phase4_pipeline.py` para lanzar una corrida nueva de extremo a extremo o esperar a un PID previamente verificado y postprocesar al concluir; agrega validación de las 108 filas, salida sin sobrescritura, reporte de siete apartados y tres SVG, y conserva códigos de error sin atribuir causas. Se añadieron `tests/test_phase4_pipeline.py` y `docs/phase4_automation.md`. Se corrigió el detector de flush para aceptar el formato real del runner (`repetition n/3.`); RED observó 0/108 flushes con el fixture exacto y GREEN pasó. Verificación final: 11 pruebas enfocadas y 57 de suite completa, `--help`, `bash -n`, `git diff --check`; revisor independiente y probe local confirmaron aceptación/rechazo de formatos de flush, códigos diagnósticos exactos y ausencia de causalidad inventada. Se inició un finalizador separado que espera el PID de la matriz activa. Estado observado: 9 líneas CSV (8 datos) y dos errores de concurrencia 50 con `worker_forced_shutdown,worker_result_missing`; el runner sigue activo, la carpeta de reporte aún no existe y el CSV manual conserva 2 líneas. El espejo Engram sigue pendiente por falta de identidad registrada.
- L102 (2026-11-01): El usuario pidió literalmente “no comoouna forma con un scrip par similarlo ypara entregar lo que tnega y despues y ya lo croijjo depues depues cunado termine de correrlo demas”. Se autoriza un informe snapshot preliminar separado con filas reales ya escritas, identificado visiblemente como incompleto/no final, sin completar ni simular ejecuciones ausentes y sin tocar el proceso activo ni modificar la matriz CSV. El finalizador definitivo permanece adjunto a la corrida de 108 y generará su salida aparte al terminar.
- L103 (2026-11-01): El usuario respondió “si pues” a detener el runner bloqueado y corregirlo con reanudación segura. Verificado: el proceso `benchmark_runner.py` de MongoDB/PROFILE_A_BASELINE/Q2/concurrency=1 llevaba más de 2 h sin avanzar, estaba en `pipe_read` sin workers activos; no se estableció la causa exacta. Se envió SIGTERM solo al PID verificado 26400; `run_benchmarks.sh` ejecutó su limpieza normal, paró los servicios que había iniciado y el CSV quedó intacto con 9 filas medidas (6 `ok`, 3 `error`). El finalizador salió sin reporte al detectar solo 10/108 flushes. Se autoriza corregir el bloqueo y añadir modo de resume que valide y conserve filas existentes, sin duplicarlas ni reemplazarlas.
- L104 (2026-11-01): Corrección implementada: ante worker forzado/no reapado, `run_repetition` no lee la cola compartida potencialmente truncada y conserva clasificación `error`; `check_spec.validate_partial_csv` valida esquema, filas y claves sin exigir completitud; `--resume` valida y salta claves ya registradas y solo añade las faltantes; el shell hace preflight antes de iniciar servicios; el finalizador permite flushes extras de intentos interrumpidos, pero exige exactamente un estado final por las 108 claves. RED observado para el helper parcial ausente y para el log con flushes sin 108 estados; GREEN verificado. Suite completa: 72 pruebas; `bash -n`, `--help`, `git diff --check` y preflight del CSV real (9 filas, 9 claves, 0 errores) pasaron. La causa raíz exacta del bloqueo no está demostrada. La reanudación aún no se inició; requiere volver a ingresar la contraseña temporal en prompt oculto. Espejo Engram pendiente por falta de identidad de sesión registrada.
- L105 (2026-11-01): La verificación independiente final pasó en scratch: 61 pruebas enfocadas y sondas de reanudación, preservación de hash, cola tras terminación, estado único por clave y rechazo del reporte parcial. No se inspeccionaron CSV reales ni se tocaron servicios durante esa verificación. La continuación autorizada está lista; falta que el usuario vuelva a ingresar la contraseña temporal mediante el prompt oculto de Compose. El espejo Engram sigue pendiente por ausencia de session ID registrado.
- L106 (2026-11-01): El usuario solicitó preparar los documentos de entrega mientras se reanuda la matriz. La reanudación iniciada por el usuario sigue activa (PID 36325; verificación a los 05:24 de tiempo transcurrido); no se reinició ni interrumpió. Se creó un corte separado `results/matrix-final-20261101-autoflush-preliminary-20261102-042517Z/` desde un snapshot estable y validado del CSV: 9/108 filas reales (6 `ok`, 3 `error`), con siete secciones y tres gráficos SVG; el informe y manifiesto marcan el corte como incompleto/no final. No se simularon métricas ni se modificó el CSV. La salida final queda a cargo del pipeline cuando complete sus validaciones.
- L107 (2026-11-01): A solicitud del usuario, se creó `output/pdf/phase4-preliminary-report.pdf` a partir del snapshot preliminar de 9/108 filas, sin cambiar CSV ni datos. PDF de 6 páginas; se revisaron visualmente todas las páginas renderizadas, incluidas las tres gráficas con etiquetas legibles; conserva la advertencia no final y las siete secciones. `pypdf` comprobó título, advertencia y secciones 1-7.
- L108 (2026-11-01): El usuario confirmó actualizar el PDF preliminar. Se verificó un snapshot estable de 13/108 filas, CSV parcial válido (0 errores), estados 9 `ok` y 4 `error`; las tres filas nuevas desde el corte anterior incluyen MongoDB PROFILE_A_BASELINE/Q2/concurrency=1 (3/3 `ok`), y Q2/concurrency=10/run=1 registró `error` con evidencia de spill desconocida y cierre forzado de worker. La reanudación PID 36325 sigue activa, sin reinicio ni intervención. Se generó `output/pdf/phase4-preliminary-13-of-108.pdf` (7 páginas) con las siete secciones, datos solo observados, perfiles CPU/RAM, configuración interna, almacenamiento sin cuota y limitaciones explícitas. Se validó texto/secciones y se renderizaron e inspeccionaron las siete páginas. No es la entrega final: faltan 95 filas.
