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

Filtrar órdenes completadas en un rango de fechas, ordenar descendente por `payment.final_total` y obtener el Top-1000 sin índices previos en el total.

Variantes de estrés de memoria:

- MongoDB: `allowDiskUse: false` / `allowDiskUse: true`
- PostgreSQL: `SET work_mem = '4MB'` / `SET work_mem = '64MB'`

MongoDB (pipeline):

```javascript
[
    { "$match": {
        "payment.status": "completed",
        "timestamp": { "$gte": "2026-01-01T00:00:00Z" }
    } },
    { "$sort": { "payment.final_total": -1 } },
    { "$limit": 1000 },
    { "$project": {
        "order_id": 1, "customer.id": 1, "payment.final_total": 1
    } }
]
```

PostgreSQL:

```sql
SELECT data->>'order_id' AS order_id,
      data->'customer'->>'id' AS customer_id,
      (data->'payment'->>'final_total')::numeric AS final_total
FROM orders
WHERE data->'payment'->>'status' = 'completed'
  AND (data->>'timestamp')::timestamptz >= '2026-01-01 00:00:00Z'
ORDER BY (data->'payment'->>'final_total')::numeric DESC
LIMIT 1000;
```

## 6. Protocolo de ejecución

- Concurrencia: 1, 10, 50 clientes.
- Repeticiones por escenario: 3.
- Ciclo de vida:
  - Flush de caché: `sync; echo 3 >/proc/sys/vm/drop_caches`
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
