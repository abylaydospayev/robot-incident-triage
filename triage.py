#!/usr/bin/env python3
"""Robot incident triage: detect -> collect context -> find the first anomaly -> group into fault families.

Input: a run directory (logs/*.log, telemetry/joint_control.txt, optional operator_notes.txt,
optional ground_truth.json for evaluation). Output: events.json and report.html.

No ML and no knowledge of specific faults: it only knows
  1. how to spot a break in the control stream (gap > 100 ms, or rate < 48 Hz for >= 2 s),
  2. which log templates are unusual *around* that break compared with quiet time,
  3. that the earliest unusual template is the best guess for where the chain started.
"""
import argparse
import bisect
import json
import math
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from report import render

LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}) - (\w+) - (\S+) - (.*)$")
NUM = re.compile(r"[-+]?\d+(?:\.\d+)?")
PT = timezone(timedelta(hours=-7))

GAP_THR = 0.10      # s, missing control commands longer than this = event
LOW_HZ = 48         # commands per second below this = low-rate second
BEFORE, AFTER = 20, 10   # wide context window around an event (s)
TIGHT = 1.0         # tight window around the event edges (s)
P_THR = 0.01        # Poisson p-value threshold for "unusual here"


def load_logs(run):
    entries = []
    d = os.path.join(run, "logs")
    for f in sorted(os.listdir(d)):
        svc = os.path.splitext(f)[0]
        with open(os.path.join(d, f), encoding="utf-8") as fh:
            for raw in fh:
                m = LINE.match(raw.rstrip("\n"))
                if not m:
                    continue
                ts, level, src, msg = m.groups()
                t = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=timezone.utc).timestamp()
                entries.append(dict(t=t, svc=svc, level=level, src=src, msg=msg,
                                    tpl=f"{svc} | {NUM.sub('<N>', msg)}"))
    entries.sort(key=lambda e: e["t"])
    return entries


def load_telemetry(run):
    with open(os.path.join(run, "telemetry", "joint_control.txt")) as fh:
        return [float(x) for x in fh if x.strip()]


# ---------------------------------------------------------------- 1. detect
def detect(ts):
    events = []
    for a, b in zip(ts, ts[1:]):
        if b - a > GAP_THR:
            events.append(dict(start=a, end=b, kinds={"gap"}, gap=b - a, min_hz=None))
    touched = set()
    for e in events:
        touched.update(range(int(e["start"]), int(e["end"]) + 1))
    counts = Counter(int(x) for x in ts)
    run = []
    for s in range(int(ts[0]) + 1, int(ts[-1])):
        if s not in touched and counts.get(s, 0) < LOW_HZ:
            run.append(s)
            continue
        if len(run) >= 2:
            events.append(dict(start=float(run[0]), end=float(run[-1] + 1), kinds={"low_rate"}, gap=0.0,
                               min_hz=min(counts.get(x, 0) for x in run)))
        run = []
    events.sort(key=lambda e: e["start"])
    merged = []
    for e in events:  # merge pieces of the same incident
        if merged and e["start"] - merged[-1]["end"] < 2.0:
            m = merged[-1]
            m["end"] = max(m["end"], e["end"])
            m["kinds"] |= e["kinds"]
            m["gap"] = max(m["gap"], e["gap"])
            if e["min_hz"] is not None:
                m["min_hz"] = e["min_hz"] if m["min_hz"] is None else min(m["min_hz"], e["min_hz"])
        else:
            merged.append(e)
    return merged


# ---------------------------------------------------------------- 2-3. context + first anomaly
def poisson_sf(k, lam):
    """P(X >= k) for X ~ Poisson(lam)."""
    if k <= 0:
        return 1.0
    term, cdf = math.exp(-lam), 0.0
    for i in range(k):
        cdf += term
        term *= lam / (i + 1)
    return max(0.0, 1.0 - cdf)


def analyse(events, entries, span):
    times = [e["t"] for e in entries]
    wins = [(ev["start"] - BEFORE, ev["end"] + AFTER) for ev in events]
    # baseline: how often each template shows up in quiet time (outside every event window)
    covered, cur = 0.0, None
    for a, b in sorted(wins):
        if cur and a <= cur[1]:
            cur[1] = max(cur[1], b)
        else:
            if cur:
                covered += cur[1] - cur[0]
            cur = [a, b]
    if cur:
        covered += cur[1] - cur[0]
    quiet = max(span - covered, 1.0)
    outside = Counter()
    wi = sorted(wins)
    for e in entries:
        k = bisect.bisect_right(wi, (e["t"], float("inf"))) - 1
        inside = any(a <= e["t"] <= b for a, b in wi[max(0, k - 2):k + 1])
        if not inside:
            outside[e["tpl"]] += 1
    rate = lambda tpl: (outside[tpl] + 0.5) / quiet

    for ev, (a, b) in zip(events, wins):
        lo, hi = bisect.bisect_left(times, a), bisect.bisect_right(times, b)
        window = entries[lo:hi]
        wide = Counter(e["tpl"] for e in window)
        ta, tb = ev["start"] - TIGHT, ev["end"] + TIGHT
        tight = Counter(e["tpl"] for e in window if ta <= e["t"] <= tb)
        unusual = {}
        for tpl, c in wide.items():
            p_wide = poisson_sf(c, rate(tpl) * (b - a))
            p_tight = poisson_sf(tight.get(tpl, 0), rate(tpl) * (tb - ta)) if tight.get(tpl) else 1.0
            p = min(p_wide, p_tight)
            if p < P_THR:
                first = next(e for e in window if e["tpl"] == tpl)
                unusual[tpl] = dict(tpl=tpl, count=c, p=p, first_t=first["t"],
                                    offset=round(first["t"] - ev["start"], 3),
                                    example=f"{first['svc']}: {first['msg']}")
        chain = sorted(unusual.values(), key=lambda u: u["first_t"])
        ev["chain"] = chain
        ev["root"] = chain[0]["tpl"] if chain else "(no unusual log lines)"
        ev["window"] = [dict(t=e["t"], svc=e["svc"], level=e["level"], msg=e["msg"],
                             unusual=e["tpl"] in unusual) for e in window]


# ---------------------------------------------------------------- 4. group
def group(events):
    families = _group(events)
    # second pass: a rare "root" that happened to coincide with a known chain is probably noise.
    # If a small family's event contains >= 80% of a big family's typical chain, move it there.
    big = [f for f in families if f["n"] >= 3]
    moved = False
    for ev in events:
        fam = families[ev["family"]]
        if fam["n"] >= 3:
            continue
        tpls = {u["tpl"] for u in ev["chain"]}
        for bf in big:
            need = {c["tpl"] for c in bf["chain"]}
            if need and len(need & tpls) / len(need) >= 0.8:
                ev["coincident"] = ev["root"]
                ev["root"] = bf["root"]
                moved = True
                break
    return _group(events) if moved else families


def _group(events):
    fams = defaultdict(list)
    for ev in events:
        fams[ev["root"]].append(ev)
    out = []
    for root, evs in sorted(fams.items(), key=lambda kv: -len(kv[1])):
        tpl_share, offsets, examples = Counter(), defaultdict(list), {}
        for ev in evs:
            for u in ev["chain"]:
                tpl_share[u["tpl"]] += 1
                offsets[u["tpl"]].append(u["offset"])
                examples.setdefault(u["tpl"], u["example"])
        typical = [dict(tpl=t, share=tpl_share[t] / len(evs),
                        offset=sorted(offsets[t])[len(offsets[t]) // 2], example=examples[t])
                   for t in tpl_share if tpl_share[t] / len(evs) >= 0.5]
        typical.sort(key=lambda x: x["offset"])
        out.append(dict(id=len(out), root=root, n=len(evs), first=evs[0]["start"], last=evs[-1]["start"],
                        gap_max=max(e["gap"] for e in evs), gap_min=min(e["gap"] for e in evs),
                        low_rate=any("low_rate" in e["kinds"] for e in evs), chain=typical))
        for ev in evs:
            ev["family"] = out[-1]["id"]
    return out


# ---------------------------------------------------------------- operator notes + evaluation
def load_notes(run, t_ref):
    p = os.path.join(run, "operator_notes.txt")
    if not os.path.exists(p):
        return []
    day = datetime.fromtimestamp(t_ref, PT).date()
    notes = []
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            m = re.match(r"^\s*(\d{1,2}):(\d\d)\s+(.*)$", line)
            if not m:
                continue
            h, mi, label = int(m.group(1)), int(m.group(2)), m.group(3).strip()
            base = datetime(day.year, day.month, day.day, tzinfo=PT)
            cands = [base + timedelta(hours=hh, minutes=mi) for hh in (h % 12, h % 12 + 12)]
            t = min(cands, key=lambda c: abs(c.timestamp() - t_ref - 3600)).timestamp()
            notes.append(dict(t=t, label=label, raw=line.strip()))
    return notes


def link_notes(events, notes):
    for n in notes:
        best = min(events, key=lambda e: abs(e["start"] - n["t"]), default=None)
        if best and abs(best["start"] - n["t"]) <= 150:
            best.setdefault("notes", []).append(n["raw"])
            n["event"] = events.index(best)


def evaluate(run, events, families):
    p = os.path.join(run, "ground_truth.json")
    if not os.path.exists(p):
        return None
    faults = json.load(open(p))["faults"]
    for e in events:
        e["truth"] = None
    for f in faults:
        hit = next((e for e in events if e["start"] - 1.5 <= f["end"] and f["start"] <= e["end"] + 1.5), None)
        f["detected"] = hit is not None
        if hit:
            hit["truth"] = f["type"]
            hit["arm_dropped"] = f["arm_dropped"]
    by_type = defaultdict(lambda: [0, 0, 0])
    for f in faults:
        r = by_type[f["type"]]
        r[0] += 1
        r[1] += f["detected"]
        r[2] += f.get("operator_noted", False)
    purity = []
    for fam in families:
        c = Counter(e["truth"] for e in events if e["family"] == fam["id"])
        top, n = c.most_common(1)[0]
        fam["truth_majority"] = top
        purity.append((fam["id"], top, n, fam["n"]))
    drops = [f for f in faults if f["arm_dropped"]]
    watchdog_hits = 0
    for f in drops:
        e = next((e for e in events if e.get("truth") and e["start"] - 1.5 <= f["end"] and f["start"] <= e["end"] + 1.5), None)
        if e and any("E_CTRL_EXPIRED" in u["tpl"] for u in e["chain"]):
            watchdog_hits += 1
    return dict(by_type={k: dict(total=v[0], detected=v[1], operator=v[2]) for k, v in by_type.items()},
                false_positives=sum(1 for e in events if e["truth"] is None),
                purity=purity, drops=len(drops), drops_with_watchdog=watchdog_hits,
                total=len(faults), detected=sum(f["detected"] for f in faults),
                operator=sum(f.get("operator_noted", False) for f in faults))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run", nargs="?", default="runs/run1")
    ap.add_argument("--out", default=None, help="output dir (default: <run>/triage)")
    a = ap.parse_args()
    out = a.out or os.path.join(a.run, "triage")
    os.makedirs(out, exist_ok=True)

    entries, ts = load_logs(a.run), load_telemetry(a.run)
    events = detect(ts)
    analyse(events, entries, ts[-1] - ts[0])
    families = group(events)
    notes = load_notes(a.run, ts[0])
    link_notes(events, notes)
    ev_eval = evaluate(a.run, events, families)

    for e in events:
        e["kinds"] = sorted(e["kinds"])
    meta = dict(t0=ts[0], t1=ts[-1], n_log=len(entries), n_ctrl=len(ts))
    with open(os.path.join(out, "events.json"), "w") as fh:
        json.dump(dict(meta=meta, families=families,
                       events=[{k: v for k, v in e.items() if k != "window"} for e in events],
                       evaluation=ev_eval), fh, indent=1, default=str)
    with open(os.path.join(out, "report.html"), "w", encoding="utf-8") as fh:
        fh.write(render(meta, events, families, notes, ev_eval))

    print(f"{len(events)} events, {len(families)} families -> {out}")
    for f in families:
        print(f"  [{f['id']}] x{f['n']:<3} root: {f['root']}" + (f"   (truth: {f.get('truth_majority')})" if ev_eval else ""))
    if ev_eval:
        print(f"detected {ev_eval['detected']}/{ev_eval['total']} injected faults, "
              f"{ev_eval['false_positives']} false positives; operator noted {ev_eval['operator']}/{ev_eval['total']}")


if __name__ == "__main__":
    main()
