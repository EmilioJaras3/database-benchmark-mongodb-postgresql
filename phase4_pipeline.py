#!/usr/bin/env python3
"""Run or finalize the Phase 4 database benchmark without manual handoffs."""

import argparse
import csv
import getpass
import html
import math
import os
import re
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import check_spec


ROOT = Path(__file__).resolve().parent
METRICS = (
    ("throughput_qps", "queries/s"),
    ("latency_p50_ms", "ms"),
    ("latency_p95_ms", "ms"),
    ("latency_p99_ms", "ms"),
    ("cpu_throttled_seconds", "s"),
    ("max_rss_mb", "MB"),
)
PROFILE_ORDER = {name: index for index, name in enumerate(check_spec.PROFILES)}
ENGINE_COLORS = {"mongodb": "#2563eb", "postgresql": "#db2777"}
SERIES_COLORS = (
    "#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c", "#0891b2",
    "#be123c", "#4d7c0f", "#7c3aed", "#0f766e", "#a16207", "#475569",
)


class PipelineError(Exception):
    """A fail-closed pipeline error with a safe user-facing message."""


def parse_number(value):
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def summarize_numbers(values):
    observed = [number for value in values if (number := parse_number(value)) is not None]
    if not observed:
        return {"n": 0, "mean": None, "median": None, "minimum": None, "maximum": None}
    return {
        "n": len(observed),
        "mean": statistics.fmean(observed),
        "median": statistics.median(observed),
        "minimum": min(observed),
        "maximum": max(observed),
    }


def load_rows(csv_path):
    with csv_path.open("r", newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def group_key(row):
    return (row["engine"], row["profile"], row["query_id"], row["concurrency"])


def sorted_groups(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[group_key(row)].append(row)
    return sorted(
        groups.items(),
        key=lambda item: (
            item[0][0], PROFILE_ORDER[item[0][1]], item[0][2], int(item[0][3]),
        ),
    )


def format_number(value):
    return "not observed" if value is None else f"{value:.3f}"


def format_run_values(rows, field):
    by_run = {row["run_number"]: parse_number(row[field]) for row in rows}
    return " / ".join(format_number(by_run.get(run)) for run in check_spec.RUN_NUMBERS)


def format_range(summary):
    if summary["n"] == 0:
        return "not observed"
    return f"{summary['minimum']:.3f}-{summary['maximum']:.3f}"


def status_summary(rows):
    counts = Counter(row["status"] for row in rows)
    return ", ".join(f"{status}: {counts.get(status, 0)}" for status in check_spec.STATUSES)


def execution_evidence(log_path):
    evidence = {
        "flushes": 0, "readiness": Counter(), "present": False, "diagnostics": [],
        "completed_scenarios": 0, "scenario_keys": set(),
    }
    if log_path is None or not log_path.is_file():
        return evidence
    evidence["present"] = True
    scenario_pattern = re.compile(
        r"^(mongodb|postgresql) (\S+) (\S+) concurrency=(\d+) run=(\d+): (ok|spill|oom|error)$"
    )
    diagnostic_pattern = re.compile(r"^run=(\d+) status=(\S+) reason_codes=(\S+)$")
    current_scenario = None
    pending_diagnostics = {}
    with log_path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            line = line.strip()
            if re.fullmatch(r"WSL2 cache flush completed before repetition \d+/\d+\.", line):
                evidence["flushes"] += 1
            if "Readiness checks passed for " in line:
                for profile in check_spec.PROFILES:
                    if f"Readiness checks passed for {profile}:" in line:
                        evidence["readiness"][profile] += 1
            scenario = scenario_pattern.match(line)
            if scenario:
                engine, profile, query_id, concurrency, run_number, status = scenario.groups()
                current_scenario = (engine, profile, query_id, concurrency, run_number, status)
                evidence["completed_scenarios"] += 1
                evidence["scenario_keys"].add(current_scenario[:5])
                diagnostic = pending_diagnostics.pop((run_number, status), None)
                if diagnostic:
                    evidence["diagnostics"].append(
                        f"{engine} {profile} {query_id} concurrency={concurrency} run={run_number}: {diagnostic}"
                    )
            diagnostic = diagnostic_pattern.match(line)
            if diagnostic:
                run_number, status, reason_codes = diagnostic.groups()
                if current_scenario and current_scenario[4:] == (run_number, status):
                    engine, profile, query_id, concurrency, _, _ = current_scenario
                    evidence["diagnostics"].append(
                        f"{engine} {profile} {query_id} concurrency={concurrency} run={run_number}: {reason_codes}"
                    )
                else:
                    pending_diagnostics[(run_number, status)] = reason_codes
    return evidence


def validate_execution_evidence(log_path):
    evidence = execution_evidence(log_path)
    if not evidence["present"]:
        raise PipelineError("Execution log is missing; cannot verify cache flushes and profile readiness.")
    if evidence["flushes"] < 108:
        raise PipelineError(
            f"Execution log shows {evidence['flushes']} successful cache flushes; expected at least 108. No report generated."
        )
    if evidence["completed_scenarios"] != 108 or evidence["scenario_keys"] != check_spec.EXPECTED_KEYS:
        raise PipelineError(
            "Execution log does not contain exactly one completed status for each of the 108 matrix keys. "
            "No report generated."
        )
    missing_profiles = [
        profile for profile in check_spec.PROFILES if evidence["readiness"].get(profile, 0) < 1
    ]
    if missing_profiles:
        raise PipelineError(
            "Execution log lacks successful readiness checks for: " + ", ".join(missing_profiles)
        )
    return evidence


def metric_table(rows):
    lines = [
        "| Engine | Profile | Query | Concurrency | Metric | Unit | Runs 1 / 2 / 3 | n | Mean | Median | Min-max |",
        "|---|---|---|---:|---|---|---|---:|---:|---:|---|",
    ]
    for key, group in sorted_groups(rows):
        engine, profile, query_id, concurrency = key
        for field, unit in METRICS:
            summary = summarize_numbers(row[field] for row in group)
            lines.append(
                f"| {engine} | {profile} | {query_id} | {concurrency} | {field} | {unit} | "
                f"{format_run_values(group, field)} | {summary['n']} | {format_number(summary['mean'])} | "
                f"{format_number(summary['median'])} | {format_range(summary)} |"
            )
    return "\n".join(lines)


def status_table(rows):
    lines = [
        "| Engine | Profile | Query | Concurrency | Run | Status | Spill observed |",
        "|---|---|---|---:|---:|---|---|",
    ]
    ordered = sorted(rows, key=lambda row: (
        row["engine"], PROFILE_ORDER[row["profile"]], row["query_id"],
        int(row["concurrency"]), int(row["run_number"]),
    ))
    for row in ordered:
        spill = row["disk_spill"] or "unknown"
        lines.append(
            f"| {row['engine']} | {row['profile']} | {row['query_id']} | {row['concurrency']} | "
            f"{row['run_number']} | {row['status']} | {spill} |"
        )
    return "\n".join(lines)


def chart_document(title, x_label, y_label, body, width=1000, height=620, x_label_y=605):
    title_xml = html.escape(title)
    x_xml = html.escape(x_label)
    y_xml = html.escape(y_label)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{title_xml}">\n'
        '<rect width="100%" height="100%" fill="#ffffff"/>\n'
        f'<text x="500" y="30" text-anchor="middle" font-family="sans-serif" '
        f'font-size="20" font-weight="bold">{title_xml}</text>\n'
        f'<text x="500" y="{x_label_y}" text-anchor="middle" font-family="sans-serif" '
        f'font-size="14">{x_xml}</text>\n'
        f'<text x="20" y="300" text-anchor="middle" font-family="sans-serif" '
        f'font-size="14" transform="rotate(-90 20 300)">{y_xml}</text>\n'
        f'{body}\n</svg>\n'
    )


def draw_axes(points, x_min, x_max, y_min, y_max, x_ticks, y_ticks, x_format, y_format):
    left, right, top, bottom = 85, 965, 65, 525
    plot_width, plot_height = right - left, bottom - top
    body = []
    for tick in y_ticks:
        y = bottom - ((tick - y_min) / (y_max - y_min)) * plot_height
        body.append(f'<line x1="{left}" y1="{y:.2f}" x2="{right}" y2="{y:.2f}" stroke="#e2e8f0"/>')
        body.append(
            f'<text x="{left - 10}" y="{y + 5:.2f}" text-anchor="end" font-family="sans-serif" '
            f'font-size="12" fill="#334155">{html.escape(y_format(tick))}</text>'
        )
    for tick in x_ticks:
        x = left + ((tick[0] - x_min) / (x_max - x_min)) * plot_width
        body.append(f'<line x1="{x:.2f}" y1="{top}" x2="{x:.2f}" y2="{bottom}" stroke="#f1f5f9"/>')
        body.append(
            f'<text x="{x:.2f}" y="{bottom + 22}" text-anchor="middle" font-family="sans-serif" '
            f'font-size="12" fill="#334155">{html.escape(x_format(tick[1]))}</text>'
        )
    body.extend((
        f'<line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" stroke="#475569"/>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{bottom}" stroke="#475569"/>',
    ))
    return body, (left, right, top, bottom)


def line_chart(rows, field, title, y_label, file_name):
    grouped = defaultdict(list)
    for row in rows:
        value = parse_number(row[field])
        if value is not None:
            grouped[(row["engine"], row["profile"], row["query_id"], row["concurrency"])].append(value)
    series_values = defaultdict(dict)
    for (engine, profile, query_id, concurrency), values in grouped.items():
        series_values[(engine, profile, query_id)][int(concurrency)] = statistics.fmean(values)
    observed = [value for series in series_values.values() for value in series.values()]
    y_low, y_high = (min(0.0, min(observed)), max(observed)) if observed else (0.0, 1.0)
    if y_high == y_low:
        y_high = y_low + 1.0
    padding = (y_high - y_low) * 0.05
    y_low = max(0.0, y_low - padding)
    y_high += padding
    x_positions = {1: 0.0, 10: 1.0, 50: 2.0}
    body, (left, right, top, bottom) = draw_axes(
        observed, 0, 2, y_low, y_high, [(0, "1"), (1, "10"), (2, "50")],
        [y_low + (y_high - y_low) * index / 4 for index in range(5)],
        lambda value: value, lambda value: f"{value:.2f}",
    )
    plot_width, plot_height = right - left, bottom - top
    legend = []
    for series_index, (series, values) in enumerate(sorted(series_values.items())):
        color = SERIES_COLORS[series_index % len(SERIES_COLORS)]
        points = []
        for concurrency in (1, 10, 50):
            if concurrency not in values:
                continue
            x = left + x_positions[concurrency] * plot_width / 2
            y = bottom - (values[concurrency] - y_low) * plot_height / (y_high - y_low)
            points.append((x, y))
            body.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="4" fill="{color}"/>')
        if len(points) > 1:
            coords = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
            body.append(f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="2"/>')
        label = "/".join(series)
        legend.append((color, label))
    if not observed:
        body.append('<text x="525" y="300" text-anchor="middle" font-family="sans-serif">No observed values</text>')
    for index, (color, label) in enumerate(legend):
        column, row_index = index % 3, index // 3
        x, y = 95 + column * 300, 555 + row_index * 18
        body.append(f'<line x1="{x}" y1="{y - 4}" x2="{x + 22}" y2="{y - 4}" stroke="{color}" stroke-width="3"/>')
        body.append(f'<text x="{x + 28}" y="{y}" font-family="sans-serif" font-size="11">{html.escape(label)}</text>')
    return file_name, chart_document(
        title, "Concurrency (workers)", y_label, "\n".join(body), height=700, x_label_y=680
    )


def scatter_chart(rows):
    points = []
    for row in rows:
        x = parse_number(row["throughput_qps"])
        y = parse_number(row["cpu_throttled_seconds"])
        if x is not None and y is not None:
            points.append((row["engine"], x, y))
    x_max = max((point[1] for point in points), default=1.0)
    y_max = max((point[2] for point in points), default=1.0)
    x_max = x_max * 1.05 if x_max else 1.0
    y_max = y_max * 1.05 if y_max else 1.0
    body, (left, right, top, bottom) = draw_axes(
        points, 0, x_max, 0, y_max,
        [(x_max * index / 4, f"{x_max * index / 4:.2f}") for index in range(5)],
        [y_max * index / 4 for index in range(5)],
        lambda value: value, lambda value: f"{value:.2f}",
    )
    for engine, x_value, y_value in points:
        x = left + x_value * (right - left) / x_max
        y = bottom - y_value * (bottom - top) / y_max
        body.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="4" fill="{ENGINE_COLORS[engine]}" fill-opacity="0.75"/>')
    for index, engine in enumerate(check_spec.ENGINES):
        x, y = 760, 78 + index * 22
        body.append(f'<circle cx="{x}" cy="{y}" r="5" fill="{ENGINE_COLORS[engine]}"/>')
        body.append(f'<text x="{x + 12}" y="{y + 4}" font-family="sans-serif" font-size="12">{engine}</text>')
    if not points:
        body.append('<text x="525" y="300" text-anchor="middle" font-family="sans-serif">No observed values</text>')
    return "throttling-vs-qps.svg", chart_document(
        "CPU throttling vs throughput", "Throughput (queries/s)", "CPU throttling (s)", "\n".join(body)
    )


def spill_summary(rows):
    counts = Counter(row["disk_spill"] or "unknown" for row in rows)
    return ", ".join(f"{value}: {counts.get(value, 0)}" for value in ("true", "false", "unknown"))


def build_report(rows, csv_name, evidence):
    readiness = ", ".join(
        f"{profile}: {evidence['readiness'].get(profile, 0)}"
        for profile in check_spec.PROFILES
    ) if evidence["present"] else "execution log not supplied; readiness evidence not captured"
    flushes = str(evidence["flushes"]) if evidence["present"] else "not captured (no execution log supplied)"
    sections = [
        "# Informe de métricas: MongoDB frente a PostgreSQL JSONB",
        f"Fuente de datos: `{csv_name}`. Filas validadas: **{len(rows)} de 108**.",
        "",
        "## 1. Especificaciones del host y del entorno Docker",
        "La matriz CSV no contiene inventario del hardware físico ni una captura de versiones efectivas del host/Docker. "
        "Esos datos se marcan como **no capturados**; no se sustituyen por valores de configuración planeados.",
        "",
        "| Dato | Evidencia |",
        "|---|---|",
        "| CPU, memoria y modelo del host | No capturado en el CSV ni en el registro de ejecución suministrado |",
        "| Versiones efectivas de WSL2 y Docker Engine | No capturadas durante la corrida |",
        "| Perfiles comparados | PROFILE_A_BASELINE, PROFILE_B_MEDIUM, PROFILE_C_SEVERE (configuración declarada en el repositorio) |",
        "| Dataset esperado | 500000 órdenes por motor/perfil; readiness se reporta en la sección 2 |",
        "",
        "## 2. Método de medición y verificación del baseline",
        "Se validó el esquema CSV y la cobertura de 108 claves únicas (2 motores × 3 perfiles × 2 consultas × 3 concurrencias × 3 repeticiones). "
        "Cada ejecución usa warmup de 60 s, medición de 300 s y cooldown de 60 s, según el playbook del proyecto.",
        f"- Vaciados de caché registrados como exitosos en el log: **{flushes}** (esperados: 108).",
        f"- Chequeos readiness registrados por perfil: {readiness}.",
        "- El baseline solo se considera comprobado para perfiles con readiness registrado; no se infiere a partir de métricas vacías.",
        "",
        "## 3. Resultados consolidados",
        f"Estados por corrida: {status_summary(rows)}. Evidencia de spill: {spill_summary(rows)}.",
        "Los valores de cada corrida se conservan. Media, mediana y rango min-max usan únicamente valores numéricos observados; "
        "`not observed` no equivale a cero.",
        "",
        metric_table(rows),
        "",
        "### Estado y spill por corrida",
        status_table(rows),
        "",
        "### Diagnósticos registrados por el runner",
        *(f"- `{diagnostic}`" for diagnostic in evidence.get("diagnostics", [])),
        *([] if evidence.get("diagnostics") else ["No se registraron códigos de diagnóstico en el log suministrado."]),
        "",
        "## 4. Análisis gráfico",
        "Los gráficos incluyen solo puntos con métricas numéricas observadas. Las líneas P99 y RSS muestran la media de las repeticiones disponibles.",
        "",
        "![P99 vs concurrencia](figures/p99-vs-concurrency.svg)",
        "",
        "![Throttling vs QPS](figures/throttling-vs-qps.svg)",
        "",
        "![Curva RSS](figures/rss-vs-concurrency.svg)",
        "",
        "## 5. Planes de ejecución",
        "No se suministraron artefactos MongoDB `explain(\"executionStats\")` ni PostgreSQL `EXPLAIN ANALYZE BUFFERS`; "
        "por lo tanto, los planes quedan **no capturados**. No se generaron consultas adicionales ni se inventaron planes.",
        "",
        "## 6. Interpretación",
        "Esta sección resume observaciones descriptivas de la matriz y no atribuye causalidad fuera de las variables medidas. "
        "Los estados y valores de spill se muestran por corrida; `unknown` se mantiene como desconocido. "
        "La ausencia de inventario del host y planes de ejecución limita cualquier explicación sobre memoria, almacenamiento temporal o escalabilidad.",
        "",
        "## 7. Recomendaciones técnicas para despliegues documentales en la nube",
        "No se emite una recomendación de dimensionamiento de producción a partir de este workload sintético por sí solo. "
        "Antes de trasladar conclusiones, validar con cargas y hardware representativos, capturar el inventario efectivo del host y conservar los planes de ejecución.",
        "",
    ]
    return "\n".join(sections)


def generate_artifacts(csv_path, output_dir, execution_log=None):
    csv_path = Path(csv_path)
    output_dir = Path(output_dir)
    errors, row_count = check_spec.validate_csv(csv_path)
    if errors:
        raise PipelineError("CSV validation failed; no report was generated: " + "; ".join(errors))
    if row_count != 108:
        raise PipelineError(f"CSV validation returned {row_count} rows; expected 108.")
    if output_dir.exists():
        raise PipelineError(f"Output directory already exists; refusing to overwrite: {output_dir}")
    rows = load_rows(csv_path)
    if len(rows) != 108:
        raise PipelineError(f"CSV changed during processing; expected 108 rows, found {len(rows)}.")
    evidence = execution_evidence(Path(execution_log) if execution_log else None)
    output_dir.mkdir(parents=True, exist_ok=False)
    figures_dir = output_dir / "figures"
    figures_dir.mkdir()
    charts = (
        line_chart(rows, "latency_p99_ms", "P99 latency vs concurrency", "P99 latency (ms)", "p99-vs-concurrency.svg"),
        scatter_chart(rows),
        line_chart(rows, "max_rss_mb", "Maximum RSS vs concurrency", "Maximum RSS (MB)", "rss-vs-concurrency.svg"),
    )
    for name, content in charts:
        (figures_dir / name).write_text(content, encoding="utf-8", newline="\n")
    report_path = output_dir / "report.md"
    report_path.write_text(build_report(rows, csv_path.name, evidence), encoding="utf-8", newline="\n")
    return report_path


def runner_process_active(pid, csv_path, proc_root=Path("/proc")):
    process_dir = Path(proc_root) / str(pid)
    if not process_dir.exists():
        return False
    try:
        command = (process_dir / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace")
        stat = (process_dir / "stat").read_text(encoding="utf-8")
    except OSError as exc:
        raise PipelineError(f"Cannot verify benchmark process {pid}: {type(exc).__name__}") from exc
    if "run_benchmarks.sh" not in command or Path(csv_path).name not in command:
        raise PipelineError("PID does not identify the expected matrix process; refusing to finalize.")
    state = stat.rsplit(")", 1)[-1].strip().split(maxsplit=1)[0]
    return state != "Z"


def wait_for_runner(pid, csv_path, poll_seconds=30):
    while runner_process_active(pid, csv_path):
        time.sleep(poll_seconds)


def run_pipeline(csv_path, output_dir, wait_pid=None, execution_log=None, resume=False):
    csv_path = Path(csv_path).resolve()
    output_dir = Path(output_dir).resolve()
    if output_dir.exists():
        raise PipelineError(f"Output directory already exists; refusing to overwrite: {output_dir}")
    if wait_pid is not None and resume:
        raise PipelineError("--resume cannot be combined with --wait-for-pid.")
    if wait_pid is None:
        log_path = Path(execution_log).resolve() if execution_log else csv_path.with_suffix(".execution.log")
        if resume:
            if not csv_path.is_file():
                raise PipelineError("--resume requires an existing partial CSV.")
            if not log_path.is_file():
                raise PipelineError("--resume requires the original execution log to preserve run evidence.")
            errors, row_count, _ = check_spec.validate_partial_csv(csv_path)
            if errors:
                raise PipelineError("Resume CSV validation failed: " + "; ".join(errors))
            print(f"Resume preflight passed: preserving {row_count} valid existing rows.")
            command = ["bash", str(ROOT / "run_benchmarks.sh"), "--resume", str(csv_path)]
            log_mode = "a"
        else:
            if csv_path.exists():
                raise PipelineError(f"CSV path already exists; refusing to append or overwrite: {csv_path}")
            if log_path.exists():
                raise PipelineError(f"Execution log already exists; refusing to overwrite: {log_path}")
            command = ["bash", str(ROOT / "run_benchmarks.sh"), str(csv_path)]
            log_mode = "x"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        prompted_for_password = False
        if not os.environ.get("POSTGRES_PASSWORD"):
            password = getpass.getpass("PostgreSQL/Compose password (input hidden): ")
            if not password:
                raise PipelineError("PostgreSQL/Compose password is required to run the matrix.")
            os.environ["POSTGRES_PASSWORD"] = password
            prompted_for_password = True
        try:
            with log_path.open(log_mode, encoding="utf-8") as log_stream:
                result = subprocess.run(
                    command, cwd=ROOT, stdout=log_stream, stderr=subprocess.STDOUT, text=True,
                    env=os.environ.copy(),
                )
        finally:
            if prompted_for_password:
                os.environ.pop("POSTGRES_PASSWORD", None)
        if result.returncode != 0:
            print(f"Benchmark runner failed with exit code {result.returncode}; no report generated.", file=sys.stderr)
            return result.returncode
        execution_log = log_path
    else:
        wait_for_runner(wait_pid, csv_path, poll_seconds=30)
        if execution_log is None:
            raise PipelineError("--execution-log is required with --wait-for-pid.")
    validate_execution_evidence(execution_log)
    report_path = generate_artifacts(csv_path, output_dir, execution_log=execution_log)
    print(f"Validated 108 rows and generated report: {report_path}")
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path, help="new or completed 108-row matrix CSV")
    parser.add_argument("--output-dir", type=Path, help="new report output directory; defaults beside the CSV")
    parser.add_argument("--wait-for-pid", type=int, help="wait for this run_benchmarks.sh PID, then validate and report")
    parser.add_argument("--execution-log", type=Path, help="runner log to summarize readiness and cache-flush evidence")
    parser.add_argument("--resume", action="store_true", help="resume a validated partial CSV without repeating recorded keys")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    output_dir = args.output_dir or args.csv.with_name(f"{args.csv.stem}-report")
    try:
        return run_pipeline(
            args.csv, output_dir, wait_pid=args.wait_for_pid,
            execution_log=args.execution_log, resume=args.resume,
        )
    except (PipelineError, OSError, EOFError) as exc:
        print(f"Phase 4 pipeline stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
