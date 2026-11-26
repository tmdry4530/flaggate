#!/usr/bin/env python3
"""Build the submission demo video end to end. ONE command (see demo-video/make-final-video.sh):

    demo-video/make-final-video.sh                 # record everything fresh, edit, render -> demo-video/final-render/
    demo-video/make-final-video.sh --edit-only     # re-edit/re-render the last recording (subtitles, cards, timing)
    demo-video/make-final-video.sh --agent planner # keyless rule planner instead of Codex (fallback)

Stages: (1) throw-away Flagsmith org + gatekeeper (stack.py); (2) record terminal A (agent) and terminal B (human)
with asciinema, concurrently; (3) edit: cut, align, retime, subtitle (edit.py); (4) render with agg + ffmpeg.
The mp4 is never committed (demo-video/final-render/ is gitignored).
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from stack import DemoStack, ROOT, clean_env, dirty_env_vars  # noqa: E402

RENDER = ROOT / "demo-video" / "final-render"
COLS, ROWS = 120, 32


def record(attempt: int, agent: str, identities: int) -> bool:
    rec = RENDER / "casts"
    rec.mkdir(parents=True, exist_ok=True)
    asciinema = ROOT / "demo-video" / ".tools" / "bin" / "asciinema"
    if not asciinema.exists():
        sys.exit("run demo-video/setup-tools.sh first")
    t0 = time.time()
    with DemoStack(identities) as st:
        env = {**st.env(), "DEMO_COLS": str(COLS), "DEMO_ROWS": str(ROWS), "DEMO_TMP": str(st.tmp),
               "DEMO_APPROVER_PASS": st.approver_pass, "DEMO_AGENT": agent}
        procs = {}
        for name in ("agent", "human"):
            procs[name] = subprocess.Popen(
                [str(asciinema), "rec", "--overwrite", "--quiet", "--cols", str(COLS), "--rows", str(ROWS),
                 "--command", f"{sys.executable} {HERE / 'scenes.py'} {name}", str(rec / f"{name}.cast")],
                env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, start_new_session=True)
        codes: dict[str, int] = {}
        deadline = time.time() + 900
        try:
            while len(codes) < len(procs) and time.time() < deadline:
                for name, p in procs.items():
                    if name not in codes and p.poll() is not None:
                        codes[name] = p.returncode
                if any(c != 0 for c in codes.values()):     # one terminal failed: stop the other, do not wait
                    break
                time.sleep(0.5)
        finally:                                            # never leave recorders, shells or Codex behind
            for name, p in procs.items():
                if p.poll() is None:
                    codes.setdefault(name, -9)
                    try:
                        os.killpg(p.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        p.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(p.pid, signal.SIGKILL)
        print(f"attempt {attempt}: recorded in {time.time() - t0:.0f}s, exit codes {codes}", flush=True)
    return all(c == 0 for c in codes.values())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--edit-only", action="store_true", help="skip recording; re-edit the last recording")
    ap.add_argument("--agent", choices=("codex", "planner"), default="codex")
    ap.add_argument("--attempts", type=int, default=2, help="re-record if the live run fails (e.g. Codex)")
    ap.add_argument("--identities", type=int, default=200)
    ap.add_argument("--record-only", action="store_true")
    ap.add_argument("--write-doc", action="store_true", help="write docs/VIDEO.md from storyboard.py and exit")
    ap.add_argument("--out", default=str(RENDER / "flagsmith-demo.mp4"))
    a = ap.parse_args()
    if a.write_doc:
        import storyboard
        (ROOT / "docs" / "VIDEO.md").write_text(storyboard.markdown() + "\n")
        return 0
    bad = dirty_env_vars()
    if bad:
        sys.exit("inherited credentials in the environment (" + ", ".join(bad) +
                 "); start through demo-video/make-final-video.sh (it uses a clean environment)")
    RENDER.mkdir(exist_ok=True)
    if not a.edit_only:
        for attempt in range(1, a.attempts + 1):
            if record(attempt, a.agent, a.identities):
                break
        else:
            print("recording failed; casts of the last attempt are kept for inspection", file=sys.stderr)
            return 1
    if a.record_only:
        return 0
    import edit
    edit.build(RENDER, Path(a.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
