"""Application-owned archives, automatic output location and bounded retention.

Legacy manifests may be read, but only explicitly owned archives are deleted.
No cleanup follows links or deletes arbitrary children of the output folder.
"""
from contextlib import contextmanager
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

from benchmark_registry import ROOT, TESTS

OWNER = "MemoryTest/archive-v1"
KEEP_RUNS = 10
RUN_NAME = re.compile(r"^\d{8}-\d{6}-[a-z0-9_]{8}$")


def application_directory(executable=None, frozen=None):
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if not frozen:
        return ROOT
    path = Path(executable or sys.executable).resolve()
    # Never write into a signed .app or PyInstaller's resource directory.
    for parent in path.parents:
        if parent.suffix.lower() == ".app":
            return parent.parent
    return path.parent


def user_data_directory():
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "MemoryTest"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/MemoryTest"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "MemoryTest"


def writable_output(preferred=None, fallback=None):
    preferred = Path(preferred) if preferred is not None else application_directory() / "Outputs"
    fallback = Path(fallback) if fallback is not None else user_data_directory() / "Outputs"
    failure = None
    for candidate in dict.fromkeys((preferred, fallback)):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=candidate):
                pass
            return candidate.resolve(), candidate != preferred
        except OSError as error:
            failure = error
    raise failure


def archive_identity(kind):
    writer = {"pid": os.getpid()}
    try:
        import psutil
        writer["created"] = psutil.Process().create_time()
    except ImportError:
        pass  # Standalone benchmark CLI remains standard-library-only.
    return {"application": OWNER, "kind": kind,
            "writer": writer}


def active_writer(data):
    writer = data.get("writer", {})
    if not writer:
        return False
    try:
        pid = int(writer["pid"])
        if pid <= 0:
            return True
    except (KeyError, TypeError, ValueError):
        return True
    try:
        import psutil
    except ImportError:
        # POSIX signal zero only tests existence. On Windows os.kill(0) is NOT
        # an existence probe; retain uncertain archives until writer is removed.
        if os.name == "nt":
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            pass
        return True
    try:
        process = psutil.Process(pid)
        created = writer.get("created")
        return process.is_running() and (created is None or abs(process.create_time() - float(created)) < .001)
    except (KeyError, TypeError, ValueError, psutil.NoSuchProcess):
        return False
    except psutil.AccessDenied:
        return True  # Uncertain ownership must not authorize deletion.


def local_file(directory, relative):
    """Resolve an archive file without accepting escape paths or symbolic links."""
    if not isinstance(relative, str) or not relative:
        return None
    path = directory / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        return None
    try:
        if not path.resolve().is_relative_to(directory.resolve()):
            return None
        part = path
        while part != directory:
            if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
                return None
            part = part.parent
        return path if path.is_file() else None
    except (OSError, RuntimeError):
        return None


def read_archive(directory, kind):
    path = local_file(directory, "manifest.json")
    if path is None:
        return None
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("run_id") != directory.name:
            return None
        if data.get("kind", "benchmark") != kind:
            return None
        stamp = datetime.fromisoformat(data["started_at"]).timestamp()
        if not math.isfinite(stamp):
            return None
        if kind == "benchmark":
            if not isinstance(data.get("modules"), list) or not data["modules"]:
                return None
            if any(not isinstance(row, dict) or row.get("module") not in TESTS for row in data["modules"]):
                return None
            if len({row["module"] for row in data["modules"]}) != len(data["modules"]):
                return None
        elif not isinstance(data.get("catalog"), dict) or not isinstance(data.get("statistics"), dict):
            return None
        return (stamp, directory, data)
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return None


def archives(output):
    found = []
    for folder, kind in (("runs", "benchmark"), ("stress-runs", "stress")):
        parent = Path(output) / folder
        if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
            continue
        try:
            children = list(parent.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_symlink() or not child.is_dir() or not RUN_NAME.fullmatch(child.name):
                continue
            entry = read_archive(child, kind)
            if entry:
                found.append(entry)
    return sorted(found, key=lambda row: (row[0], str(row[1])), reverse=True)


def recent_archives(outputs, kind):
    found = {}
    for output in outputs:
        for entry in archives(output):
            data = entry[2]
            if data.get("kind", "benchmark") == kind and data.get("finished_at"):
                found[entry[1].resolve()] = entry
    return sorted(found.values(), key=lambda row: (row[0], str(row[1])), reverse=True)


@contextmanager
def retention_lock(output):
    """Nonblocking OS lock, automatically released even if the process crashes."""
    lock = output / ".retention.lock"
    if lock.is_symlink():
        yield False
        return
    with lock.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            pass
        try:
            yield acquired
        finally:
            if acquired:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def prune_archives(output, keep=KEEP_RUNS, protect=()):
    """Keep the newest N owned rounds across benchmark and stress sessions.

    Active workers, legacy/unknown files, linked trees and protected paths survive.
    Returns exact removed paths and errors for callers to surface/log.
    """
    if keep < 1:
        raise ValueError("retention must keep at least one round")
    output = Path(output).resolve()
    protected = {Path(path).resolve() for path in protect}
    removed, errors = [], []
    if not output.is_dir():
        return removed, errors
    try:
        with retention_lock(output) as locked:
            if not locked:
                return removed, errors
            owned = [entry for entry in archives(output) if entry[2].get("application") == OWNER]
            for _, directory, data in owned[keep:]:
                if directory.resolve() in protected or active_writer(data):
                    continue
                try:
                    # rmtree does not follow symlinks, but reject linked archives
                    # altogether, including Windows junctions, as extra caution.
                    linked = lambda p: p.is_symlink() or getattr(p, "is_junction", lambda: False)()
                    if linked(directory) or any(linked(p) for p in directory.rglob("*")):
                        continue
                    shutil.rmtree(directory)
                    removed.append(directory)
                except OSError as error:
                    errors.append(f"{directory}: {error}")
    except OSError as error:
        errors.append(str(error))
    return removed, errors


def sample_tail(directory):
    """Bound bytes as well as sample count; never scan an hours-long file on launch."""
    path = local_file(directory, "samples.jsonl")
    if path is None:
        return []
    with path.open("rb") as stream:
        stream.seek(0, 2)
        start = max(0, stream.tell() - 2 * 1024 * 1024)
        stream.seek(start)
        if start:
            stream.readline()
        lines = stream.read().splitlines()[-601:]
    samples = []
    for line in lines:
        try:
            sample = json.loads(line)
            elapsed, values = sample["elapsed"], sample["values"]
            if not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed < 0 or not isinstance(values, dict):
                continue
            if samples and elapsed < samples[-1]["elapsed"]:
                continue
            sample["values"] = {key: value for key, value in values.items()
                                if value is None or isinstance(value, (int, float)) and math.isfinite(value)}
            samples.append(sample)
        except (ValueError, TypeError, KeyError):
            continue
    return samples
