"""Presentation-neutral results. No Qt, browser, scores for partial runs or prose inference."""
from dataclasses import dataclass, field, asdict
import json
import math
from pathlib import Path
import re
import statistics

from benchmark_registry import TESTS
from compute_score import compute_mark_score
from i18n import ref

GROUPS = {"cache_group": ["cache_hierarchy"], "memory_group": ["memory_bandwidth"],
          "core_group": ["cpu_alu", "cpu_float", "cpu_branch", "cpu_multi", "inter_core"]}


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


def operation(value, index=1):
    lowered = value.strip().lower()
    if lowered in OPS:
        return ref(OPS[lowered])
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


def column(key, unit=""):
    return {"label": ref(key), "unit": unit}


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
        for pattern, key in [(r"^- Memory Size: ([\d.]+\s*(?:Mi?B|Gi?B|Ki?B))\s*$", "buffer_size"),
                             (r"^- Thread Count: (\d+)\s*$", "threads")]:
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
                result.conditions = [(ref("protocol"), "ComputeMark v2"), (ref("threads"), score.threads),
                                     (ref("placement"), ref(policy)), (ref("compiler"), score.compiler)]
                ns = sorted({1, score.threads})
                for kind, single, multi, unit in [("integer", score.single_integer_gops, score.integer_gops, "Gop/s"),
                                                ("fp64", score.single_fp64_gflops, score.fp64_gflops, "GFLOP/s")]:
                    rates = [single] if score.threads == 1 else [single, multi]
                    result.charts.append(Chart("scaling", "line", unit, ns,
                        [{"label": ref(kind), "values": rates}], ns, "threads"))
                    result.tables.append(Table("scaling", [column("threads"), column(kind, unit), column("speedup", "×")],
                                               [[n, rate, rate / single] for n, rate in zip(ns, rates)]))
                return result
            except (OSError, ValueError, KeyError, TypeError):
                result.notice = "score_unavailable"
        for title, headers, rows in parsed:
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
            add_generic(result, headers, rows, "cache_scan" if "rdlat(ns)" in lower else "results")
            if "rdlat(ns)" in lower:
                valid = sorted([(size_kib(row[0]), row) for row in rows if size_kib(row[0]) is not None], key=lambda pair: pair[0])
                xs = [pair[0] for pair in valid]
                series = []
                for key, heading in [("read_latency", "rdlat(ns)"), ("write_latency", "wrlat(ns)")]:
                    if heading in lower:
                        values = [number(row[lower.index(heading)]) for _, row in valid]
                        series.append({"label": ref(key), "values": values})
                result.charts.append(Chart("cache_scan", "line", "ns", xs, series, xs, "workset", True))
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
                    result.charts.append(Chart(name, "bar", measure, labels, [{"label": ref("latency"), "values": values}]))
                    result.kpis.extend({"label": label, "value": value, "unit": measure} for label, value in zip(labels[:4], values[:4]))
        if not result.tables:
            result.status, result.reason = "invalid_report", "read_error"
    except (OSError, ValueError, KeyError, TypeError):
        result.status, result.reason = "invalid_report", "read_error"
    return result


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
    modules = list(modules or TESTS)
    records = {record["module"]: record for record in (manifest or {}).get("modules", [])}
    results = [build_module(name, reports, records.get(name, {"status": "not_run"}) if manifest is not None else None) for name in modules]
    levels = perflevel_table(manifest)
    if levels.rows:
        for result in results:
            if result.name == "cache_hierarchy" and result.status == "passed":
                result.tables = [table for table in result.tables if table.title != "reported_cache"]
                result.tables.insert(0, levels)
    return results
