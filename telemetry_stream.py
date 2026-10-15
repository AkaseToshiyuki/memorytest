"""One unprivileged sampling stream shared by the UI and stress supervisor."""
import json
import math
from pathlib import Path
import time


def publish_snapshot(path, sample):
    path = Path(path)
    pending = path.with_suffix(".tmp")
    pending.write_text(json.dumps(dict(sample, sampled_at=time.monotonic()), ensure_ascii=True,
                                  allow_nan=False), encoding="utf-8")
    pending.replace(path)


def read_snapshot(path, max_age=3):
    try:
        if Path(path).stat().st_size > 524288:
            return None
        with Path(path).open(encoding="utf-8") as stream:
            data = json.loads(stream.read(524289))
        if not isinstance(data, dict) or not isinstance(data.get("catalog"), dict) or not isinstance(data.get("values"), dict):
            return None
        age = time.monotonic() - data["sampled_at"]
        if not math.isfinite(age) or not 0 <= age < max_age or len(data["catalog"]) > 512:
            return None
        for key, item in data["catalog"].items():
            if not isinstance(key, str) or not isinstance(item, dict) or not isinstance(item.get("unit"), str) or "label" not in item:
                return None
        if any(value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value))
               for value in data["values"].values()):
            return None
        return data
    except (OSError, ValueError, TypeError, KeyError):
        return None
