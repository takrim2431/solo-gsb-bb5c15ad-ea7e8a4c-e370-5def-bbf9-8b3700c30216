"""Generate samples/telemetry.jsonl — a deterministic demo dataset.

Scenario (all timestamps UTC, one sample per minute):
  pump-1 temp  : normal 60ish, rises above 80 for 11 consecutive minutes
                 (samples t=6..16, held on [6,17)) -> a sustained fault.
                 A single one-minute spike at t=20 must NOT trigger the
                 duration-based rule.
  pump-1 vib   : normal <= 2; high (>3) held only on [12,14).
  pump-2 vib   : >5 held on [3,6) (samples t=3,4,5).
  pump-2 press : <30 held on the touching interval [6,9) (samples t=6,7,8)
                 and again, separately, on [10,14).

Designed fault results (after importing + evaluating demo rules):
  pump-1 high temp >=80 for 300s        -> one fault [6,17), length 11 min;
                                           the 1-minute t=20 spike is filtered.
  pump-1 hot AND vibrating for 120s     -> one fault [12,14), length 2 min.
  pump-2 vib>5 OR press<30 for 200s     -> [3,6) and [6,9) touch at t=6 and
                                           merge into one continuous [3,9)
                                           fault (6 min); the separate
                                           [10,14) press dip is a second
                                           fault (4 min).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

BASE = datetime(2026, 9, 23, 0, 0, 0, tzinfo=timezone.utc)
N = 31  # minutes [0 .. 30]


def ts(minute: int) -> str:
    return (BASE + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z")


def temp_pump1(m: int) -> float:
    if 6 <= m <= 16:  # 11 samples: held on [6,17)
        return 80.0 + (m - 6) * 0.8
    if m == 20:  # isolated one-minute spike -> [20,21), filtered by duration
        return 91.5
    return 62.0 + (m % 3) * 0.4


def vib_pump1(m: int) -> float:
    if 12 <= m <= 13:  # held [12,14)
        return 3.6
    return 1.2 + (m % 4) * 0.15


def vib_pump2(m: int) -> float:
    if 3 <= m <= 5:  # held [3,6)
        return 6.1 + (m % 2) * 0.3
    return 2.0 + (m % 5) * 0.2


def press_pump2(m: int) -> float:
    # low on the touching [6,9) and on the separate [10,14)
    if 6 <= m <= 8 or 10 <= m <= 13:
        return 26.0 + m * 0.1
    return 52.0 - (m % 4)


def main() -> None:
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples", "telemetry.jsonl")
    with open(out, "w", encoding="utf-8") as fh:
        for m in range(N):
            for device, metric, value in (
                ("pump-1", "temp", temp_pump1(m)),
                ("pump-1", "vib", vib_pump1(m)),
                ("pump-2", "vib", vib_pump2(m)),
                ("pump-2", "press", press_pump2(m)),
            ):
                fh.write(json.dumps({
                    "device": device,
                    "metric": metric,
                    "ts": ts(m),
                    "value": round(value, 3),
                }, ensure_ascii=False) + "\n")
    print(f"wrote {N * 4} events to {out}")


if __name__ == "__main__":
    main()
