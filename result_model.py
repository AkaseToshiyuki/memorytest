"""Presentation-neutral results. No Qt, browser, scores for partial runs or prose inference."""
from dataclasses import dataclass, field, asdict, replace
import json
import math
from pathlib import Path
import re
import statistics

from benchmark_registry import TASKS as TESTS, SELECTION_GROUPS, select_modules
from compute_score import compute_mark_score
from i18n import ref

GROUPS = {"cache_group": ["cache_hierarchy"], "memory_group": ["memory_bandwidth"],
          "core_group": ["cpu_alu", "cpu_float", "cpu_branch", "cpu_multi", "inter_core", "cpu_architecture"]}
for _group in ("cache_group", "memory_group"):
    GROUPS[_group] += SELECTION_GROUPS[_group]


@dataclass
class Table:
    title: str
    columns: list
    rows: list


@dataclass
class Chart:
    title: str
    kind: str
    unit: str
    labels: list
    series: list
    x: list = field(default_factory=list)
    x_title: str = "operation"
    log_x: bool = False


@dataclass
class ModuleResult:
    name: str
    status: str = "not_run"
    duration: float | None = None
    exit_code: int | None = None
    reason: str | None = None
    kpis: list = field(default_factory=list)
    tables: list = field(default_factory=list)
    charts: list = field(default_factory=list)
    conditions: list = field(default_factory=list)
    notice: str | None = None

    def to_dict(self):
        return asdict(self)


def read_text(path):
    with Path(path).open(encoding="utf-8", errors="replace") as stream:
        value = stream.read(5_000_001)
    if len(value) > 5_000_000:
        raise ValueError("report exceeds display limit")
    return value


def tables(text):
    """Read Markdown tables, retaining headings and continuation rows."""
    lines = text.splitlines()
    title, index = "", 0
    while index < len(lines):
        line = lines[index].strip()
        if line.startswith("## "):
            title = line[3:]
        if "|" in line and index + 1 < len(lines) and re.fullmatch(r"[\s|:\-]+", lines[index + 1]):
            split = lambda value: [cell.strip().strip("*") for cell in value.strip().strip("|").split("|")]
            headers, rows = split(line), []
            index += 2
            while index < len(lines):
                row = lines[index].strip()
                if not row:
                    # Legacy multi-core tables contain blank rows between workloads.
                    if index + 1 < len(lines) and lines[index + 1].strip().startswith("|"):
                        index += 1
                        continue
                    break
                if "|" not in row:
                    break
                cells = split(row)
                if len(cells) == len(headers) and not re.fullmatch(r"[\s|:\-]+", row):
                    rows.append(cells)
                index += 1
            yield title, headers, rows
        else:
            index += 1


def number(value):
    match = re.match(r"^\s*([-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?)", str(value).replace(",", ""))
    if not match:
        return None
    result = float(match[1])
    return result if math.isfinite(result) else None


def size_kib(value):
    match = re.fullmatch(r"\s*([\d.]+)\s*([KMGT]?i?B)\s*", value, re.I)
    if not match:
        return None
    prefix = match[2][0].upper()
    return float(match[1]) * {"B": 1 / 1024, "K": 1, "M": 1024, "G": 1024**2, "T": 1024**3}[prefix]


OPS = {"add": "add", "sub": "sub", "mul": "mul", "div": "div", "mod": "mod", "sqrt": "sqrt",
       "sin": "sin", "log": "logarithm", "pow": "pow", "mixed": "mixed", "fadd": "add", "fmul": "mul",
       "and": "and", "or": "or", "xor": "xor", "shl": "shl", "shr": "shr", "count": "count",
       "load+alu": "load_alu", "fma": "fma", "dot": "dot", "dot product": "dot", "read": "read",
       "write": "write", "copy": "copy", "always taken": "always", "never taken": "never",
       "random 50%": "random50", "2-bit counter": "counter2", "increasing": "increasing"}
OPS.update({"alternating": "alternating", "ttnn": "ttnn", "scale": "scale", "triad": "triad",
            "dependent random read": "dependent_read", "mul+add": "muladd", "integer": "integer", "fp64": "fp64"})
OPS.update({"dependent random write 64b (ordered)": "dependent_write", "dependent random copy 64b (ordered)": "dependent_copy"})


def operation(value, index=1):
    lowered = value.strip().lower()
    if lowered in OPS:
        return ref(OPS[lowered])
    stride = re.fullmatch(r"stride (\d+)b", lowered)
    if stride:
        return ref("stride_n", bytes=int(stride[1]))
    for prefix in ("float", "double", "simd"):
        if lowered.startswith(prefix + " "):
            return ref("typed_op", type=ref(prefix), operation=operation(value[len(prefix) + 1:], index))
    if lowered.startswith("pattern ") and re.fullmatch(r"[\dKk]+", value[8:]):
        return ref("pattern_n", number=value[8:])
    # Never leak arbitrary untranslated prose from legacy reports into the UI.
    return ref("item_number", number=index)


HEADERS = {"operation": ("operation", ""), "pattern": ("pattern", ""), "size": ("workset", ""),
           "rdlat(ns)": ("read_latency", "ns"), "wrlat(ns)": ("write_latency", "ns"),
           "bw(mb/s)": ("bandwidth", "MB/s"), "bandwidth (mb/s)": ("bandwidth", "MB/s"),
           "read(mb/s)": ("read", "MB/s"), "write(mb/s)": ("write", "MB/s"),
           "copy(mb/s)": ("copy", "MB/s"), "time(ms)": ("time", "ms"),
           "ops/sec": ("rate", "op/s"), "ns/op": ("latency", "ns/op"),
           "ns/branch": ("latency", "ns/branch"), "cpi": ("cpi", ""), "ipc": ("ipc", ""),
           "threads": ("threads", ""), "speedup": ("speedup", "×"), "efficiency": ("efficiency", "%"),
           "mispred rate": ("mispred", "%"), "data source": ("source", ""), "type": ("type", "")}
HEADERS.update({"spread(%)": ("spread", "%"), "operations": ("operations", ""),
                "branches": ("branches", ""), "bytes": ("payload", "B")})


def column(key, unit=""):
    return {"label": ref(key), "unit": unit}


def bandwidth_scale(values):
    """Input is decimal MB/s; choose one readable axis/column unit, capped at GB/s."""
    maximum = max((abs(v) for v in values if isinstance(v, (int, float)) and math.isfinite(v)), default=0)
    if maximum >= 1000:
        return 0.001, "GB/s"
    if maximum >= 1 or maximum == 0:
        return 1, "MB/s"
    if maximum >= 0.001:
        return 1000, "kB/s"
    return 1_000_000, "B/s"


def scale_bandwidth(result):
    # Change only this presentation model; the Markdown/JSON evidence is untouched.
    for table in result.tables:
        for i, col in enumerate(table.columns):
            if col["unit"] != "MB/s":
                continue
            factor, col["unit"] = bandwidth_scale(row[i] for row in table.rows)
            for row in table.rows:
                if isinstance(row[i], (int, float)):
                    row[i] *= factor
    for chart in result.charts:
        if chart.unit == "MB/s":
            factor, chart.unit = bandwidth_scale(v for series in chart.series for v in series["values"])
            for series in chart.series:
                series["values"] = [v * factor if isinstance(v, (int, float)) else v for v in series["values"]]
    for item in result.kpis:
        if item["unit"] == "MB/s":
            factor, item["unit"] = bandwidth_scale([item["value"]])
            if isinstance(item["value"], (int, float)):
                item["value"] *= factor
    return result


def sampling_tables(headers, rows, cache=False):
    if "Samples" not in headers or "Rejected" not in headers or "Drift(%)" not in headers:
        return []
    titles = ("cache_bandwidth_statistics", "cache_latency_statistics") if cache else ("sampling_statistics",)
    result = []
    for title in titles:
        selected = [row for row in rows if not cache or
                    (("dependent load" in row[0]) == (title == "cache_latency_statistics"))]
        if not selected:
            continue
        values = []
        for row in selected:
            size = re.fullmatch(r"(\d+)B (.*)", row[0])
            label = (ref("sized_measurement", size=f"{int(size[1]) / 1024:g} KiB",
                         measurement=ref("dependent_read") if size[2] == "dependent load" else operation(size[2]))
                     if size else operation(row[0]))
            if "Threads" in headers:
                label = ref("threaded_measurement", measurement=label, threads=row[headers.index("Threads")])
            values.append([label, *[number(row[headers.index(h)]) for h in
                                   ("Samples", "Kept", "Rejected", "Raw median(ms)", "Drift(%)")],
                           ref("unstable" if "unstable" in row[headers.index("Status")] else "measured")])
        result.append(Table(title, [column("measurement"), column("samples"), column("kept"), column("rejected"),
                                    column("raw_median", "ms"), column("drift", "%"), column("status")], values))
    return result


def add_generic(result, headers, rows, title="results"):
    indices = [i for i, header in enumerate(headers) if header.lower() in HEADERS]
    if not indices or not rows:
        return
    columns = [column(*HEADERS[headers[i].lower()]) for i in indices]
    values = []
    for row_index, row in enumerate(rows):
        converted = []
        for index in indices:
            key, unit = HEADERS[headers[index].lower()]
            raw = row[index]
            if key in {"operation", "pattern"}:
                value = operation(raw, row_index + 1)
            elif key == "source":
                value = ref("estimated" if "estimated" in raw.lower() else "measured")
            elif key == "type":
                value = ref(raw.lower()) if raw.lower() in {"float", "double", "simd"} else None
            elif key == "workset":
                # Cache BW labels include L1(32KB), RAM(128MB).
                match = re.search(r"([\d.]+\s*[KMGT]?i?B)", raw, re.I)
                value = match[1] if match else None
            else:
                value = number(raw)
            converted.append(value)
        values.append(converted)
    result.tables.append(Table(title, columns, values))


def build_module(name, reports, record=None):
    reports = Path(reports)
    result = ModuleResult(name, **{key: record[key] for key in ("status", "exit_code") if key in (record or {})})
    if record:
        result.duration = record.get("duration_seconds")
        if result.status != "passed":
            result.reason = "missing_report" if record.get("message") == "expected report was not produced" else result.status
            return result
    try:
        text = read_text(reports / TESTS[name]["report"])
        parsed = list(tables(text))
        result.status = "passed"
        if "- Sampling source: shared-cache-latency-scan-v1" in text:
            result.conditions.append((ref("source"), ref("shared_cache_scan")))
        if name == "inter_core" and "- Protocol: macos-observed-cpu-pairs-v1" in text:
            from intercore_observed import read_observed
            try:
                observed = read_observed(json.loads(read_text(reports / "inter_core_observed.json")))
            except (OSError, ValueError, TypeError, KeyError):
                result.status, result.reason = "invalid_report", "invalid_report"
                return result
            ids, pairs = observed["ids"], observed["pairs"]
            matrix = [[pairs[i, j]["median"] if (i, j) in pairs else None for j in ids] for i in ids]
            result.notice = "observed_intercore_notice"
            result.conditions = [(ref("protocol"), "macos-observed-cpu-pairs-v1"),
                                 (ref("placement"), ref("qos")), (ref("samples"), 10)]
            result.kpis = [{"label": ref("observed_pair_coverage"),
                            "value": f'{observed["covered_pairs"]} / {observed["possible_pairs"]}', "unit": ""},
                           {"label": ref("observed_cpu_count"), "value": len(ids), "unit": ""},
                           {"label": ref("migration_rejections"), "value": observed["attempt_counts"].get("migration", 0), "unit": ""}]
            result.charts = [Chart("observed_cas_latency", "heatmap", "ns", ids,
                [{"label": ref("observed_cas_latency"), "values": matrix}], x_title="observed_cpu")]
            result.tables = [Table("observed_cas_latency", [column("from_cpu"), column("to_cpu"), column("samples"),
                column("observed_cas_latency", "ns"), column("drift", "%"), column("status")],
                [[i, j, stats["count"], stats["median"], stats["drift_pct"], ref("unstable" if stats["unstable"] else "measured")]
                 for (i, j), stats in sorted(pairs.items())])]
            return result
        if name == "cpu_architecture":
            from architecture_analysis import read_architecture
            try:
                evidence = read_architecture(json.loads(read_text(reports / "cpu_architecture.json")))
            except (OSError, ValueError, KeyError, TypeError):
                result.status, result.reason = "invalid_report", "invalid_report"
                return result
            metrics = evidence["metrics"]
            result.notice = "" if evidence["stable"] else "architecture_unstable"
            frontend = evidence["frontend"]
            transitions = frontend["btb_transitions"] if frontend else []
            btb_value = ", ".join(f'{r["lower"]}–{r["upper"]}' for r in transitions) or "—"
            def width_kpi(key, label, unit):
                value = frontend.get(key) if frontend else None
                return {"label": ref(label), "value": (("≥" if value["status"] == "lower_bound" else "≈") +
                    f'{value["value"]:.2f}') if value else "—", "unit": value["unit"] if value else unit, "category": "frontend"}
            result.conditions = [(ref("protocol"), evidence["protocol"]), (ref("samples"), 10),
                (ref("sample_target"), f'{evidence["sample_target_ns"] / 1e6:g} ms'),
                (ref("placement"), ref("pinned" if evidence["cpu_id"] >= 0 else "qos" if evidence["placement"].startswith("macos") else "arch_os_scope"))]
            result.kpis = [{"label": ref("arch_instruction_cache"), "value": evidence["reported_cache_bytes"]["l1i"] / 1024 or "—", "unit": "KiB", "category": "cache"},
                {"label": ref("arch_btb_candidate"), "value": btb_value, "unit": "", "category": "frontend"},
                width_kpi("fetch_width", "arch_fetch_width", "B/cycle"),
                width_kpi("decode_width", "arch_decode_width", "instr/cycle")]
            result.charts = [Chart("architecture_diagram", "architecture", "", [], [{"evidence": evidence}])]
            result.tables = [Table("architecture_measurements", [column("measurement"), column("latency", "ns/op"),
                column("samples"), column("rejected"), column("drift", "%"), column("status")],
                [[ref("arch_" + name), value["ns"], 10, value["statistics"]["rejected"], value["statistics"]["drift_pct"],
                  ref("measured" if value["stable"] else "unstable")] for name, value in metrics.items()])]
            if frontend:
                result.conditions.append((ref("arch_frontend_target"), f'{frontend["sample_target_ns"] / 1e6:g} ms'))
                result.conditions.append((ref("arch_counter_source"), ref("unavailable") if frontend["counter_source"] == "unavailable" else frontend["counter_source"]))
                for kind, title, x_name in (("btb", "arch_btb_scan", "arch_target_count"), ("nop", "arch_code_scan", "arch_code_bytes")):
                    records = [r for r in frontend["records"] if r["kind"] == kind]
                    key = "count" if kind == "btb" else "code_bytes"
                    xs = sorted({r[key] for r in records})
                    series = [{"label": f"{stride} B", "values": [r["ns"] for r in sorted(
                        (r for r in records if r["stride"] == stride), key=lambda r: r[key])]}
                        for stride in sorted({r["stride"] for r in records})]
                    result.charts.append(Chart(title, "line", "ns/branch" if kind == "btb" else "ns/instr", xs, series, xs, x_name, True))
                    result.tables.append(Table(title, [column(x_name), column("arch_spacing", "B"), column("latency", "ns"),
                        column("arch_effective_supply", "instr/cycle"), column("status")],
                        [[r[key], r["stride"], r["ns"], r["instructions_per_cycle"], ref("measured" if r["stable"] and (
                            r["instructions_per_cycle"] is None or r["counter_stable"]) else "unstable")]
                         for r in records]))
                cycle_rows = [r for r in frontend["records"] if r["kind"] == "nop" and r["counter_stable"]]
                if cycle_rows:
                    xs = sorted({r["code_bytes"] for r in cycle_rows})
                    series = [{"label": f'{stride} B', "values": [next((r["instructions_per_cycle"] for r in cycle_rows
                        if r["stride"] == stride and r["code_bytes"] == size), None) for size in xs]}
                        for stride in sorted({r["stride"] for r in cycle_rows})]
                    result.charts.append(Chart("arch_decode_scan", "line", "instr/cycle", xs, series, xs, "arch_code_bytes", True))
            scan = evidence.get("width_scan")
            if scan:
                result.conditions.append((ref("arch_width_status"), ref("arch_" + scan["status"])))
                points = scan["records"]
                cycles = all(r["counter_stable"] for r in points)
                field = "cycles_per_iteration" if cycles else "ns_per_iteration"
                xs = list(range(4, 65))
                series = [{"label": ref("arch_crossed" if crossed else "arch_aligned"),
                    "values": [r[field] for r in sorted((r for r in points if r["crossed"] == crossed), key=lambda r: r["count"])]}
                    for crossed in (0, 1)]
                result.charts.append(Chart("arch_fetch_scan", "line", "cycles/loop" if cycles else "ns/loop", xs, series, xs, "arch_loop_instructions"))
                result.tables.append(Table("arch_fetch_scan", [column("arch_loop_instructions"), column("measurement"),
                    column("latency", "ns/loop"), column("arch_width", "cycles/loop"), column("status")],
                    [[r["count"], ref("arch_crossed" if r["crossed"] else "arch_aligned"), r["ns_per_iteration"],
                      r["cycles_per_iteration"], ref("measured" if r["stable"] else "unstable")] for r in points]))
            return result
        for pattern, key in [(r"^- Memory Size: ([\d.]+\s*(?:Mi?B|Gi?B|Ki?B))\s*$", "buffer_size"),
                             (r"^- Protocol: ([\w.-]+)\s*$", "protocol"),
                             (r"^- Latency protocol: ([\w.-]+)\s*$", "protocol"),
                             (r"^- Thread Count: (\d+)\s*$", "threads"),
                             (r"^- Samples: (\d+)\s*$", "samples"),
                             (r"^- Warmup minimum: ([\d.]+ ms)\s*$", "warmup"),
                             (r"^- Sample target: ([\d.]+ ms)\s*$", "sample_target")]:
            match = re.search(pattern, text, re.MULTILINE)
            if match:
                result.conditions.append((ref(key), match[1]))
        for _, headers, rows in parsed:
            if headers == ["Level", "OS name", "Physical CPUs", "Logical CPUs", "L1D KiB", "L2 KiB", "CPUs sharing L2"]:
                result.tables.append(Table("reported_cache", [column("tier"), column("physical"), column("logical"),
                    column("l1d", "KiB"), column("l2", "KiB"), column("cache_shared")],
                    [[ref("performance" if row[1] == "Performance" else "efficiency_cores" if row[1] == "Efficiency" else "tier"),
                      *[number(value) for value in row[2:]]] for row in rows]))
        if name == "cpu_multi":
            try:
                data = json.loads(read_text(reports / "compute_mark_v2.json"))
                score = compute_mark_score(data)
                result.kpis = [{"label": ref("single_points"), "value": score.single_points, "unit": "points"},
                               {"label": ref("multi_points"), "value": score.points, "unit": "points"},
                               {"label": ref("integer"), "value": score.integer_gops, "unit": "Gop/s"},
                               {"label": ref("fp64"), "value": score.fp64_gflops, "unit": "GFLOP/s"}]
                policy = {"macos-qos-user-initiated": "qos", "linux-pinned": "pinned",
                          "windows-pinned-process-group": "process_group"}[score.placement_policy]
                result.conditions = [(ref("protocol"), "ComputeMark v2 / " + score.sampling_protocol + " / " + score.scaling_protocol),
                                     (ref("samples"), score.sample_count), (ref("threads"), score.threads),
                                     (ref("spread"), f"{score.max_spread_pct:.3f}%"),
                                     (ref("placement"), ref(policy)), (ref("compiler"), score.compiler)]
                if not score.stable:
                    result.notice = "unstable_score"
                ns = score.thread_counts
                for kind, rates, unit in [("integer", score.integer_scaling, "Gop/s"),
                                          ("fp64", score.fp64_scaling, "GFLOP/s")]:
                    single = rates[0]
                    result.charts.append(Chart("scaling", "line", unit, ns,
                        [{"label": ref(kind), "values": rates}], ns, "threads"))
                    result.tables.append(Table("scaling", [column("threads"), column(kind, unit), column("speedup", "×")],
                                               [[n, rate, rate / single] for n, rate in zip(ns, rates)]))
                for title, headers, rows in parsed:
                    if title == "Sampling Statistics":
                        result.tables.extend(sampling_tables(headers, rows))
                return result
            except (OSError, ValueError, KeyError, TypeError):
                result.notice = "score_unavailable"
        for title, headers, rows in parsed:
            if title == "Sampling Statistics":
                result.tables.extend(sampling_tables(headers, rows, name.startswith("cache_")))
                continue
            if name.startswith("cache_") and title == "Reported Cache Hierarchy" and rows:
                result.tables.append(Table("reported_cache", [column("level"), column("capacity", "KiB")],
                    [[ref({"L1D": "l1d", "L2": "l2", "L3": "l3"}.get(row[0], "level")), number(row[1])] for row in rows]))
                continue
            if name.startswith("cache_") and title == "Cache Hierarchy Detection":
                if rows:
                    result.tables.append(Table("cache_transitions", [column("transition"), column("lower_bound", "KiB"),
                        column("upper_bound", "KiB"), column("before", "ns"), column("after", "ns"), column("ratio", "×")],
                        [[number(value) for value in row] for row in rows]))
                elif name == "cache_detection":
                    result.notice = "no_transition"
                continue
            if not rows or headers[0].lower() in {"item", "level"}:
                continue
            if headers[0].lower() == "core":
                unit = "Mop/s" if "throughput" in title.lower() else "ns"
                label = "cas_throughput" if unit == "Mop/s" else "cas_latency"
                labels = [int(value) for value in headers[1:] if value.isdigit()]
                if len(labels) != len(headers) - 1 or len(rows) != len(labels):
                    continue
                matrix = [[number(value) if i != j else None for j, value in enumerate(row[1:])]
                          for i, row in enumerate(rows)]
                result.charts.append(Chart(label, "heatmap", unit, labels,
                                          [{"label": ref(label), "values": matrix}], x_title="core"))
                result.tables.append(Table(label, [column("core")] + [{"label": n, "unit": unit} for n in labels],
                    [[number(row[0]), *[value if re.fullmatch(r"[\d.\s\[\](),;±+-]+", value) else matrix[i][j]
                                        for j, value in enumerate(row[1:])]] for i, row in enumerate(rows)]))
                finite = [v for row in matrix for v in row if v is not None and v > 0]
                if finite:
                    result.kpis.append({"label": ref(label), "value": statistics.median(finite), "unit": unit})
                continue
            lower = [header.lower() for header in headers]
            if name.startswith("cache_") and title == "Cache Bandwidth Test":
                add_generic(result, headers, rows, "cache_bandwidth_test")
                sizes = sorted({size_kib(row[0]) for row in rows if size_kib(row[0]) is not None})
                series = []
                for op in ("Read", "Write", "Copy"):
                    by_size = {size_kib(row[0]): number(row[2]) for row in rows if row[1] == op}
                    series.append({"label": operation(op), "values": [by_size.get(size) for size in sizes]})
                result.charts.append(Chart("cache_bandwidth_test", "line", "MB/s", sizes, series, sizes, "workset", True))
                continue
            table_title = "cache_latency_test" if title == "Cache Latency Test" else "cache_scan" if "rdlat(ns)" in lower else "memory_latency" if title == "Memory Latency" else "memory_bandwidth_test" if title == "Memory Bandwidth Test Results" else "results"
            add_generic(result, headers, rows, table_title)
            if "rdlat(ns)" in lower:
                valid = sorted([(size_kib(row[0]), row) for row in rows if size_kib(row[0]) is not None], key=lambda pair: pair[0])
                xs = [pair[0] for pair in valid]
                series = []
                for key, heading in [("read_latency", "rdlat(ns)"), ("write_latency", "wrlat(ns)")]:
                    if heading in lower:
                        values = [number(row[lower.index(heading)]) for _, row in valid]
                        series.append({"label": ref(key), "values": values})
                result.charts.append(Chart(table_title, "line", "ns", xs, series, xs, "workset", True))
                if "bw(mb/s)" in lower:
                    result.charts.append(Chart("cache_bw", "line", "MB/s", xs, [{"label": ref("bandwidth"),
                        "values": [number(row[lower.index("bw(mb/s)")]) for _, row in valid]}], xs, "workset", True))
                # KPIs identify the measured working set, not an inferred cache level.
                if valid:
                    for _, row in (valid[0], valid[-1]):
                        result.kpis.append({"label": ref("read_latency"), "value": number(row[lower.index("rdlat(ns)")]),
                                            "unit": "ns", "detail": row[0]})
            elif "bandwidth (mb/s)" in lower:
                index = lower.index("bandwidth (mb/s)")
                result.charts.append(Chart("memory_bandwidth", "bar", "MB/s", [operation(row[0], i + 1) for i, row in enumerate(rows)],
                    [{"label": ref("bandwidth"), "values": [number(row[index]) for row in rows]}]))
                result.kpis.extend({"label": operation(row[0], i + 1), "value": number(row[index]), "unit": "MB/s"} for i, row in enumerate(rows))
            elif "speedup" in lower and "threads" in lower:
                for op in dict.fromkeys(row[0] for row in rows):
                    selected = [row for row in rows if row[0] == op]
                    xs = [number(row[lower.index("threads")]) for row in selected]
                    result.charts.append(Chart("scaling", "line", "×", xs,
                        [{"label": operation(op), "values": [number(row[lower.index("speedup")]) for row in selected]}], xs, "threads"))
            else:
                measure = next((key for key in ("ns/op", "ns/branch") if key in lower), None)
                if measure:
                    index = lower.index(measure)
                    labels = [operation(row[0], i + 1) for i, row in enumerate(rows)]
                    values = [number(row[index]) for row in rows]
                    result.charts.append(Chart("memory_latency" if title == "Memory Latency" else name, "bar", measure, labels, [{"label": ref("latency"), "values": values}]))
                    result.kpis.extend({"label": label, "value": value, "unit": measure} for label, value in zip(labels[:4], values[:4]))
        if not result.tables:
            result.status, result.reason = "invalid_report", "read_error"
    except (OSError, ValueError, KeyError, TypeError):
        result.status, result.reason = "invalid_report", "read_error"
    return scale_bandwidth(result)


def cache_parts(result):
    """Three independently browsable views, preserving one compatible CLI ID."""
    parts = []
    for key in ("cache_bandwidth_test", "cache_latency_test", "cache_detection"):
        titles = {"cache_bandwidth_test": {"cache_bandwidth_test", "cache_bw", "cache_bandwidth_statistics"},
                  "cache_latency_test": {"cache_latency_test", "cache_scan", "cache_latency_statistics"},
                  "cache_detection": {"reported_cache", "cache_transitions", "cache_latency_statistics"}}[key]
        part = replace(result, name=key, tables=[table for table in result.tables if table.title in titles],
                       charts=[chart for chart in result.charts if chart.title in titles],
                       kpis=result.kpis if key == "cache_latency_test" else [])
        if key == "cache_detection" and result.status == "passed" and not any(t.title == "cache_transitions" for t in part.tables):
            part.notice = "no_transition"
        parts.append(part)
    return parts


def split_legacy_result(result):
    """Restore old combined archives into new pages without inventing fresh runs."""
    if result.name == "cache_hierarchy":
        parts = cache_parts(result)
        for part in parts:
            part.duration = None  # The old manifest timed the aggregate, not each child.
            part.conditions = [*part.conditions, (ref("source"), ref("combined_run"))]
        return parts
    if result.name == "memory_bandwidth":
        parts = []
        for key in ("memory_bandwidth_test", "memory_latency_test"):
            latency = key == "memory_latency_test"
            part = replace(result, name=key, duration=None,
                conditions=[*result.conditions, (ref("source"), ref("combined_run"))],
                tables=[t for t in result.tables if (t.title == "memory_latency") == latency
                        and t.title != "sampling_statistics"],
                charts=[c for c in result.charts if (c.title == "memory_latency") == latency],
                kpis=[k for k in result.kpis if (k["unit"] == "ns/op") == latency])
            for table in result.tables:
                if table.title == "sampling_statistics":
                    rows = [row for row in table.rows if
                            (isinstance(row[0], dict) and row[0].get("key") in
                             {"dependent_read", "dependent_write", "dependent_copy"}) == latency]
                    if rows:
                        part.tables.append(Table(table.title, table.columns, rows))
            # Very old bandwidth-only archives contain no latency evidence.
            if result.status == "passed" and not part.tables:
                part.status, part.reason = "not_run", "no_data"
            parts.append(part)
        return parts
    return [result]


def system_data(reports, modules, manifest=None):
    entries = []
    environment = (manifest or {}).get("environment", {})
    for key in ("os", "architecture"):
        if environment.get(key):
            entries.append((ref(key), environment[key]))
    for name in modules:
        try:
            for _, headers, rows in tables(read_text(Path(reports) / TESTS[name]["report"])):
                if headers == ["Item", "Value"]:
                    model = next((row[1] for row in rows if row[0] == "CPU Model"), None)
                    if model:
                        entries.insert(0, (ref("cpu_model"), model))
                        return entries
        except (OSError, ValueError):
            pass
    model = environment.get("macos_sysctl", {}).get("machdep.cpu.brand_string")
    if model:
        entries.insert(0, (ref("cpu_model"), model))
    return entries


def perflevel_table(manifest):
    rows = []
    for item in (manifest or {}).get("environment", {}).get("macos_perflevels", []):
        name = item.get("name", "")
        tier = ref("performance" if name == "Performance" else "efficiency_cores") if name in {"Performance", "Efficiency"} else ref("tier")
        rows.append([tier, number(item.get("physicalcpu")), number(item.get("logicalcpu")),
                     number(item.get("l1dcachesize")), number(item.get("l2cachesize")), number(item.get("cpusperl2"))])
    return Table("reported_cache", [column("tier"), column("physical"), column("logical"),
                                    column("l1d", "B"), column("l2", "B"), column("cache_shared")], rows)


def load_results(reports, modules=None, manifest=None):
    modules = list(modules or select_modules())
    records = {record["module"]: record for record in (manifest or {}).get("modules", [])}
    results = [build_module(name, reports, records.get(name, {"status": "not_run"}) if manifest is not None else None) for name in modules]
    levels = perflevel_table(manifest)
    if levels.rows:
        for result in results:
            if result.name in {"cache_hierarchy", "cache_detection"} and result.status == "passed":
                result.tables = [table for table in result.tables if table.title != "reported_cache"]
                result.tables.insert(0, levels)
    return results
