# Automatización de Fase 4

El pipeline ejecuta la matriz autorizada, valida sus 108 filas y genera el reporte de siete secciones con gráficos SVG. El CSV de entrada se conserva sin cambios; el reporte se crea en un directorio nuevo.

## Iniciar una matriz nueva

Desde WSL2, en la raíz del repositorio:

```bash
python3 phase4_pipeline.py --csv results/matrix-final-YYYYMMDD.csv
```

Si `POSTGRES_PASSWORD` no está definida, el pipeline la solicita con entrada oculta; no la escribe en el CSV ni en el log. Luego ejecuta `run_benchmarks.sh`, conserva el log de ejecución junto al CSV, y al finalizar valida la matriz antes de generar cualquier reporte.

## Completar una matriz que ya está corriendo

El pipeline puede esperar a un proceso existente sin modificarlo:

```bash
python3 phase4_pipeline.py \
  --csv results/matrix-final-YYYYMMDD.csv \
  --wait-for-pid <PID> \
  --execution-log /ruta/al/log-de-ejecucion.log
```

El PID debe corresponder a `run_benchmarks.sh` para ese CSV. Al terminar, el pipeline exige al menos 108 vaciados exitosos, exactamente un estado final registrado para cada una de las 108 claves, readiness de cada perfil y una validación CSV completa antes de continuar.

## Reanudar una matriz interrumpida

Después de detenerse una corrida, el pipeline valida cada fila existente y sus claves antes de iniciar servicios. `--resume` conserva el CSV y el log, omite las repeticiones ya registradas (incluidos estados `error`) y ejecuta solo las claves faltantes:

```bash
python3 phase4_pipeline.py \
  --resume \
  --csv results/matrix-final-YYYYMMDD.csv \
  --execution-log /ruta/al/log-original.log
```

El log se abre en modo append y el CSV nunca se reescribe. Si el CSV tiene filas duplicadas, claves desconocidas o métricas mal formadas, la reanudación falla antes de arrancar los servicios. Los errores ya registrados se conservan; reintentar una clave fallida requiere una decisión explícita, no se hace silenciosamente.

## Salidas y salvaguardas

- Por defecto crea `results/<nombre-del-CSV>-report/` con `report.md` y tres gráficos SVG.
- El modo normal no reutiliza ni sobrescribe CSV, logs o directorios de reporte existentes. El modo `--resume` solo se permite sobre un CSV y log existentes que pasen validación; añade filas faltantes y nunca reescribe las anteriores.
- Si falla la matriz, falta evidencia de ejecución o `check_spec.py` rechaza el CSV, no genera el reporte.
- Datos de host o planes `explain` no capturados se marcan explícitamente; no se inventan ni se ejecutan consultas adicionales.
- El reporte muestra cada corrida, media, mediana, rango min-max, estados y spill observado. Valores ausentes no se convierten en cero. En una corrida reanudada puede haber vaciados adicionales por intentos interrumpidos; el total observado se informa, pero el log debe conservar exactamente un estado final por cada clave de la matriz.

Consulta `python3 phase4_pipeline.py --help` para ver las opciones.
