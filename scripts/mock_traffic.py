#!/usr/bin/env python3
"""Mock traffic for the `fg promote` demo (demo data only).

Evaluates N synthetic identities against the REAL local Flagsmith with the official SDK (transient identities:
nothing is stored), so each visitor gets the variant Flagsmith really serves, then simulates a conversion with a
per-variant true rate and writes metrics.csv (aggregate) and optionally events.csv.

  .venv/bin/python scripts/mock_traffic.py --feature checkout_button_color \
      --rates blue=0.08,green=0.13,orange=0.09 --n 1500 --seed 7 --out /tmp/metrics.csv

Needs only the environment's client-side key (FS_ENV_KEY from the env or the runtime local.env); never printed.
"""
from __future__ import annotations

import argparse
import csv
import random
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flaggate.flagsmith_io import api_url_from_env, load_credentials  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--feature", required=True)
    ap.add_argument("--rates", required=True, help="variant=rate,... true conversion rates")
    ap.add_argument("--n", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--prefix", default="traffic")
    ap.add_argument("--out", required=True)
    ap.add_argument("--events")
    ap.add_argument("--api-url", default=None)
    a = ap.parse_args()

    rates = {k: float(v) for k, v in (kv.split("=") for kv in a.rates.split(","))}
    env_key = load_credentials().get("FS_ENV_KEY")
    if not env_key:
        sys.exit("FS_ENV_KEY not found (env or runtime local.env)")
    from flagsmith import Flagsmith

    fs = Flagsmith(environment_key=env_key, api_url=(a.api_url or api_url_from_env()).rstrip("/") + "/api/v1/")
    rng = random.Random(a.seed)
    ids = [f"{a.prefix}-{i}" for i in range(a.n)]
    plans = {i: ("pro" if rng.random() < 0.4 else "free") for i in ids}

    def serve(identifier: str) -> str:
        flags = fs.get_identity_flags(identifier, traits={"plan": plans[identifier]}, transient=True)
        return str(flags.get_feature_value(a.feature))

    with ThreadPoolExecutor(8) as pool:
        served = list(pool.map(serve, ids))
    rows = []
    for identifier, variant in zip(ids, served):
        p = rates.get(variant)
        if p is None:
            sys.exit(f"variant {variant!r} served by Flagsmith has no --rates entry")
        rows.append((identifier, variant, 1 if rng.random() < p else 0))
    exp, conv = Counter(v for _, v, _ in rows), Counter(v for _, v, c in rows if c)
    with open(a.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["feature", "variant", "exposures", "conversions"])
        for v in sorted(exp):
            w.writerow([a.feature, v, exp[v], conv[v]])
    if a.events:
        with open(a.events, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["feature", "identity", "variant", "converted"])
            w.writerows((a.feature, *r) for r in rows)
    for v in sorted(exp):
        print(f"{v:>10}: {exp[v]:5d} visitors, {conv[v]:4d} conversions ({conv[v] / exp[v]:.1%})")
    print(f"wrote {a.out}" + (f" and {a.events}" if a.events else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
