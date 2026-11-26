"""Edit stage: cut/align/retime the two recorded casts, add cards and English subtitles, render the mp4.

Casts are asciicast v2. Markers (invisible OSC 7777 strings written by typist.marker) tell us where things happen:
  * `hold:N`  keeps the screen for N seconds (time to read the impact preview) instead of the real gap
  * other names anchor cuts and subtitles
Idle gaps are capped (--cap), so a 90 s Codex run plays in a few seconds. Nothing on screen is invented: only the
timing changes; the subtitle numbers are read back from the recorded output.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import storyboard as sb  # noqa: E402

MARK = re.compile(r"\x1b\]7777;([^;\x07]*);([^\x07]*)\x07")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]")
FONT = "Geist Mono,DejaVu Sans Mono,Liberation Mono"
TERM_H, TERM_PAD = 880, 12
FPS = 30


# ----------------------------------------------------------------------------------------------- casts
@dataclass
class Cast:
    header: dict
    seq: list[tuple[float, str, object]] = field(default_factory=list)   # (t, "e"|"m", data | (name, epoch))

    @classmethod
    def load(cls, path: Path) -> "Cast":
        lines = path.read_text().splitlines()
        c = cls(json.loads(lines[0]))
        for line in lines[1:]:
            t, kind, data = json.loads(line)
            if kind != "o":
                continue
            pos = 0
            for m in MARK.finditer(data):
                if m.start() > pos:
                    c.seq.append((t, "e", data[pos:m.start()]))
                c.seq.append((t, "m", (m.group(1), float(m.group(2) or 0))))
                pos = m.end()
            if pos < len(data):
                c.seq.append((t, "e", data[pos:]))
        return c

    def marker(self, name: str, nth: int = 0) -> float:
        hits = [t for t, k, v in self.seq if k == "m" and v[0] == name]
        if len(hits) <= nth:
            raise KeyError(f"marker {name!r} not in the recording (is the run complete?)")
        return hits[nth]

    def has(self, name: str) -> bool:
        return any(k == "m" and v[0] == name for _, k, v in self.seq)

    def epoch(self, name: str) -> float:
        return next(v[1] for _, k, v in self.seq if k == "m" and v[0] == name)

    def plain(self) -> str:
        return ANSI.sub("", "".join(v for _, k, v in self.seq if k == "e")).replace("\r", "")

    def find_text(self, regex: str, after: float = 0.0) -> float:
        """Time of the first output event after `after` whose text matches `regex`."""
        rx = re.compile(regex)
        for t, k, v in self.seq:
            if k == "e" and t >= after and rx.search(ANSI.sub("", v)):
                return t
        raise KeyError(f"text /{regex}/ not found after {after:.1f}s")

    @property
    def end(self) -> float:
        return self.seq[-1][0]


def split_lines(seq):
    """Split output events at line ends so that holds can be placed between lines (times stay the same)."""
    for t, kind, v in seq:
        if kind == "e" and "\n" in v[:-1]:
            for piece in re.findall(r"[^\n]*\n|[^\n]+", v):
                yield t, kind, piece
        else:
            yield t, kind, v


def retime(cast: Cast, t0: float, t1: float, cap: float, holds=()) -> tuple[list[tuple[float, str]], dict[str, float], float]:
    """Window [t0, t1] of the cast with idle gaps capped; everything before t0 is replayed at once so the
    screen is correct. `holds` = ordered [(regex, seconds, anchor)]: after the first output line matching the next
    rule the screen is held for `seconds` (and `anchor` is recorded as a marker). Returns (events, marker name ->
    new time, duration)."""
    events: list[tuple[float, str]] = []
    marks: dict[str, float] = {}
    newt, prev, hold = 0.0, None, None
    rules, ri = list(holds), 0
    for t, kind, v in split_lines(cast.seq):
        if kind == "m" and v[0].startswith("hold:"):
            if t0 <= t <= t1:
                hold = float(v[0][5:])
            continue
        if t < t0:
            if kind == "e":
                events.append((0.0, v))
            continue
        if t > t1:
            break
        if kind == "m":
            marks.setdefault(v[0], newt)
            continue
        if prev is not None:
            newt += hold if hold is not None else (min(t - prev, cap) if t - prev > 0.05 else t - prev)
            hold = None
        prev = t
        events.append((round(newt, 4), v))
        if ri < len(rules) and re.search(rules[ri][0], ANSI.sub("", v), re.M):
            marks.setdefault(rules[ri][2], newt)
            hold = rules[ri][1]
            ri += 1
    return events, marks, newt


def write_cast(path: Path, header: dict, events: list[tuple[float, str]]) -> None:
    h = {k: header[k] for k in ("version", "width", "height") if k in header}
    path.write_text(json.dumps(h) + "\n" + "".join(json.dumps([t, "o", d]) + "\n" for t, d in events))


# ----------------------------------------------------------------------------------------------- render
def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def render_cast(events, header, tail: float, name: str, work: Path) -> Path:
    cast = work / f"{name}.cast"
    ev = list(events)
    write_cast(cast, header, ev)
    gif, mp4 = work / f"{name}.gif", work / f"{name}.mp4"
    run(["agg", "--font-size", "16", "--line-height", "1.2", "--font-family", FONT, "--theme", "dracula",
         "--idle-time-limit", "999", "--fps-cap", str(FPS), str(cast), str(gif)])
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(gif), "-vf",
         f"fps={FPS},tpad=stop_mode=clone:stop_duration={tail:.2f},scale=1920:{TERM_H}:force_original_aspect_ratio=decrease:flags=lanczos,"
         f"pad=1920:1080:(ow-iw)/2:{TERM_PAD}:color=0x1c1d29,format=yuv420p",
         "-c:v", "libx264", "-crf", "19", "-preset", "medium", "-an", str(mp4)])
    return mp4


def render_card(png: Path, seconds: float, name: str, work: Path) -> Path:
    mp4 = work / f"{name}.mp4"
    run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-framerate", str(FPS), "-i", str(png), "-t", str(seconds),
         "-vf", f"fade=t=in:st=0:d=0.5,fade=t=out:st={seconds - 0.5}:d=0.5,format=yuv420p",
         "-c:v", "libx264", "-crf", "19", "-an", str(mp4)])
    return mp4


def duration(mp4: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(mp4)],
                         check=True, capture_output=True, text=True).stdout
    return float(out)


# ----------------------------------------------------------------------------------------------- subtitles
def ass_time(t: float) -> str:
    h, rem = divmod(max(t, 0.0), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def ass_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", "\\N")


ASS_HEAD = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,DejaVu Sans,40,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,110,110,34,1
Style: Label,DejaVu Sans,25,&H007BFA50,&H000000FF,&H00000000,&H00000000,1,0,0,0,100,100,1,0,1,1,0,8,110,110,%d,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def build_ass(path: Path, subs: list[tuple[float, float, str, str]]) -> None:
    lines = [ASS_HEAD % (TERM_H + TERM_PAD + 8)]
    for start, end, label, text in subs:
        if label:
            lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Label,,0,0,0,,{ass_escape(label)}")
        lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Sub,,0,0,0,,{ass_escape(text)}")
    path.write_text("\n".join(lines) + "\n")


# ----------------------------------------------------------------------------------------------- facts
def facts(human: Cast, agent: Cast) -> dict[str, str]:
    h, a = human.plain(), agent.plain()

    def grab(rx: str, text: str, default: str = "…", flags: int = re.M) -> str:
        m = re.search(rx, text, flags)
        return m.group(1).strip() if m else default

    f = {
        "who": grab(r"^WHO CHANGES: (.*)$", h),
        "guard": grab(r"^GUARDRAILS: (.*)$", h),
        "verdict": grab(r"^VERDICT: (.*)$", h),
        "match": grab(r"prediction matched (\d+/\d+)", h),
        "steps": grab(r"manual steps replaced: (\d+)", h + a),
        "elapsed": grab(r"elapsed prompt→applied: (\S+)", h + a),
        "metrics": grab(r"^METRICS  (.*)$", h),
        "pvalue": grab(r"p=([0-9.]+)", h),
        "winner": grab(r"winner (\w+) vs control", h),
        "rolled": grab(r"^(ROLLED BACK[^\n]*)$", h),
        "audit": grab(r"^OK\s+(chain OK[^\n]*)$", h),
        "failed_tries": str(len(re.findall(r"plan_from_prompt \(failed\)", a))),
    }
    if f["pvalue"] and float(f["pvalue"]) < 0.0001:
        f["pvalue"] = "< 0.0001"
    else:
        f["pvalue"] = "= " + f["pvalue"] if f["pvalue"] else f["pvalue"]
    n = int(f["failed_tries"])
    f["plan_note"] = ("The planner accepted it on the first try." if n == 0 else
                      "The planner rejected the first wording and said what it supports; Codex rewrote it and retried.")
    return f


# ----------------------------------------------------------------------------------------------- build
def build(render_dir: Path, out: Path) -> None:
    work = render_dir / "work"
    work.mkdir(exist_ok=True)
    agent, human = Cast.load(render_dir / "casts" / "agent.cast"), Cast.load(render_dir / "casts" / "human.cast")
    fx = facts(human, agent)
    print("facts:", json.dumps(fx, ensure_ascii=False))

    segs: list[Path] = []
    subs: list[tuple[float, float, str, str]] = []
    clock = 0.0
    warnings: list[str] = []

    def plan_subs(spec, marks: dict[str, float], seg_dur: float):
        """Sequential, never overlapping: a later subtitle waits for the earlier one. Returns [(start, end, label, text)]
        relative to the segment (end may run past seg_dur: the caller then extends the segment's tail)."""
        planned = []
        for anchor, dt, dur, label, text in spec:
            try:
                text = text.format(**fx)
            except KeyError as e:
                warnings.append(f"subtitle uses unknown fact {e}")
            at = {"start": 0.0, "end": seg_dur}.get(anchor, marks.get(anchor))
            if at is None:
                warnings.append(f"subtitle anchor {anchor!r} not found; skipped: {text[:50]}")
                continue
            planned.append((max(0.0, at + dt), max(dur, 1.2 + 0.045 * len(text)), label, text))
        planned.sort(key=lambda p: p[0])
        out_, free = [], 0.0
        for start, need, label, text in planned:
            start = max(start, free)
            out_.append((start, start + need, label, text))
            free = start + need + 0.15
        return out_

    def commit_subs(planned, base: float) -> None:
        for st, en, label, text in planned:
            subs.append((base + st, base + en, label, text))

    # 1) cards
    run([str(HERE.parent / ".tools" / "bin" / "python"), str(HERE / "cards.py"), str(work)])   # needs Pillow
    for key, seconds in sb.CARDS_BEFORE:
        png = work / f"card-{key}.png"
        mp4 = render_card(png, seconds, f"card-{key}", work)
        commit_subs(plan_subs(sb.CARD_SUBS.get(key, []), {}, seconds), clock)
        segs.append(mp4)
        clock += duration(mp4)
    # 2) terminal segments
    cut_a1 = None
    for seg in sb.TERMINAL_SEGMENTS:
        cast = agent if seg["cast"] == "agent" else human
        t0 = seg["start"](cast, cut_a1)
        t1 = seg["end"](cast, cut_a1)
        if seg["key"] == "agent1":
            cut_a1 = t1
        events, marks, dur = retime(cast, t0, t1, seg.get("cap", 1.3), seg.get("holds", ()))
        if os.environ.get("DEMO_DEBUG"):
            print("   marks", seg["key"], {k: round(v, 1) for k, v in sorted(marks.items(), key=lambda kv: kv[1])})
        tail = seg.get("tail", 1.0)
        planned = plan_subs(seg["subs"], marks, dur + tail)
        if planned:                                   # the last subtitle must be readable: freeze the final frame if needed
            tail = max(tail, max(e for _, e, _, _ in planned) - dur + 0.6)
        mp4 = render_cast(events, cast.header, tail, seg["key"], work)
        dur_v = duration(mp4)
        commit_subs(planned, clock)
        segs.append(mp4)
        print(f"  segment {seg['key']:8s} cast {t0:6.1f}-{t1:6.1f}s -> {dur_v:5.1f}s video (at {clock:5.1f}s)")
        clock += dur_v
    # 3) outro
    for key, seconds in sb.CARDS_AFTER:
        png = work / f"card-{key}.png"
        mp4 = render_card(png, seconds, f"card-{key}", work)
        commit_subs(plan_subs(sb.CARD_SUBS.get(key, []), {}, seconds), clock)
        segs.append(mp4)
        clock += duration(mp4)

    lst = work / "list.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in segs))
    joined = work / "joined.mp4"
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(joined)])
    ass = work / "subs.ass"
    build_ass(ass, subs)
    srt = out.with_suffix(".srt")
    srt.write_text("".join(f"{i}\n{ass_time(s).replace('.', ',')}0 --> {ass_time(e).replace('.', ',')}0\n"
                           f"{(lab + chr(10)) if lab else ''}{txt}\n\n" for i, (s, e, lab, txt) in enumerate(subs, 1)))
    out.parent.mkdir(parents=True, exist_ok=True)
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(joined), "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
         "-vf", f"ass={ass}:fontsdir=/usr/share/fonts/truetype/dejavu", "-c:v", "libx264", "-crf", "20",
         "-preset", "medium", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "48k", "-shortest",
         "-movflags", "+faststart", str(out)])
    (render_dir / "scenes.txt").write_text(scene_list(segs))
    for w in warnings:
        print("WARNING:", w)
    print(f"video: {out}  {duration(out):.1f}s  {out.stat().st_size / 1e6:.1f} MB")
    print(f"subtitles: {srt}")


def scene_list(segs) -> str:
    t, rows = 0.0, []
    for p in segs:
        d = duration(p)
        rows.append(f"{int(t // 60)}:{t % 60:04.1f}  {p.stem}  ({d:.1f}s)")
        t += d
    return "\n".join(rows) + "\n"
