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
  - Flush de caché: paso manual del usuario antes de cada corrida WSL2: `sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'`. El asistente no lo ejecutará ni automatizará. Esta escritura libera page cache y objetos slab recuperables; reconstruirlos puede aumentar I/O y CPU. La transcripción fuente del playbook se conserva en `docs/playbook.md`.
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

Do not execute benchmarks, cache flushing, the 500,000-record load, or write/fabricate result rows. Never invent metrics, explain plans, or logs. Keep `results/` trackable; exclude generated dataset files, `.env`, and credentials. Ask before any unresolved requirement not covered by the playbook or decisions.

### S4 — Delivery phases

Phase 1 is documentation and decision closure only. Phase 2 builds one logical deliverable at a time after decisions close. Phase 3 provides a single pilot for the user to execute and reviews only the pasted evidence. Phase 4 builds tables, charts, and the seven-section report from real CSV data only.

## Tasks

- T1 — S1-S3; route: inline; status: complete; commit: pending phase closure.
- T2 — S2-S4; route: inline; status: complete (Phase 1 contract and decisions closed).
- T3.1 — S1-S4; route: delegated (prompt-pipeline skill); status: complete (static and independent verification passed; native review acknowledged with non-blocking advisory R3-001); commit: `06bb4e3` (`docker-compose.yml`; user approved cAdvisor Docker socket with read-only host mounts, no `privileged: true`).
- T3.2 — S1-S4; route: delegated (prompt-pipeline skill); status: complete (static checks and focused independent review passed; native review acknowledged with informational advisory R3-001); commit: `979f89d` (`synthetic_data_generator.py`; `synthetic_data_generator.py (con parámetro de tamaño para pruebas de 5 000)`).
- T3.3 — S1-S4; route: delegated (prompt-pipeline skill); status: complete (static and independent review passed; native assessment: medium/under_budget, no review due yet); commit: `6302768` (`queries/`; `queries/ (agregaciones MongoDB y SQL PostgreSQL)`).
- T3.4 — S1-S4; route: delegated (prompt-pipeline skill); status: in progress (one scenario per invocation; `run_benchmarks.sh` will switch profiles serially; data provisioning remains manual; caller-supplied CSV output path required).
- T3.5 — S1-S4; route: pending (`run_benchmarks.sh`).
- T3.6 — S1-S4; route: pending (`check_spec.py`).
- T4 — S1-S4; route: inline; status: pending (Phase 3 pilot, user-run).
- T5 — S1-S4; route: inline; status: pending (Phase 4 real-data report).

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
