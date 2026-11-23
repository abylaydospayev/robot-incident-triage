"""Self-contained HTML report for triage results."""
from datetime import datetime, timedelta, timezone
from html import escape

PT = timezone(timedelta(hours=-7))
N_COLORS = 6


def utc(t, ms=False):
    s = datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M:%S.%f" if ms else "%H:%M:%S")
    return s[:-3] if ms else s


def pt(t):
    return datetime.fromtimestamp(t, PT).strftime("%I:%M:%S %p").lstrip("0")


def tpl_html(tpl):
    svc, _, msg = tpl.partition(" | ")
    if not msg:
        return escape(tpl)
    return f'<span class="svc">{escape(svc)}</span> {escape(msg)}'


def severity(fam):
    if fam["gap_max"] >= 3:
        return "high", "long control gaps: arm may drop"
    if fam["gap_max"] >= 0.5 or fam["low_rate"]:
        return "med", "visible freeze / robot interrupted"
    return "low", "short stutter"


CSS = """
:root{--bg:#f7f7f5;--panel:#fff;--ink:#1d1d1b;--mute:#6b6b66;--line:#e4e3de;--code:#f1f0ec;
--hi:#fff4d6;--c0:#2f6fdf;--c1:#d9572b;--c2:#1f9d74;--c3:#9a4fd1;--c4:#c8961a;--c5:#d63f7a;
--high:#c4372b;--med:#c27c0e;--low:#6b6b66}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#151514;--panel:#1e1e1c;--ink:#ecebe6;
--mute:#9a9992;--line:#2f2f2c;--code:#262624;--hi:#3a3220;--c0:#6b9cf5;--c1:#f0815a;--c2:#4cc79d;--c3:#bd86ec;
--c4:#e3b94a;--c5:#ef75a4;--high:#f06b5e;--med:#e7a63c;--low:#9a9992}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 -apple-system,Segoe UI,Inter,Roboto,sans-serif}
main{max-width:1080px;margin:0 auto;padding:32px 16px 64px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:36px 0 12px}
.sub{color:var(--mute);margin:0 0 24px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}
.stat b{display:block;font-size:26px;font-variant-numeric:tabular-nums}.stat span{color:var(--mute);font-size:13px}
.fam{background:var(--panel);border:1px solid var(--line);border-left:5px solid var(--c);border-radius:10px;
padding:16px;margin-bottom:12px}
.fam header{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:baseline}
.fam .n{font-size:22px;font-weight:700;font-variant-numeric:tabular-nums}
.fam .root{font-weight:600;word-break:break-word}
.pill{font-size:12px;padding:2px 8px;border-radius:99px;border:1px solid currentColor}
.pill.high{color:var(--high)}.pill.med{color:var(--med)}.pill.low{color:var(--low)}
.meta{color:var(--mute);font-size:13px;margin:6px 0 10px}
ol.chain{list-style:none;margin:0;padding:0}
ol.chain li{display:grid;grid-template-columns:78px 1fr;gap:10px;padding:6px 0;border-top:1px dashed var(--line)}
.off{font:12px ui-monospace,Consolas,monospace;color:var(--mute);text-align:right;padding-top:2px}
.svc{font:12px ui-monospace,Consolas,monospace;background:var(--code);padding:1px 6px;border-radius:4px}
.ex{display:block;color:var(--mute);font:12px ui-monospace,Consolas,monospace;word-break:break-all}
.timeline{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px;overflow:hidden}
.timeline svg{width:100%;height:auto;display:block}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:13px;color:var(--mute);margin-top:8px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:50%;background:var(--c);margin-right:6px}
details.ev{background:var(--panel);border:1px solid var(--line);border-radius:8px;margin-bottom:6px}
details.ev summary{cursor:pointer;padding:9px 12px;display:grid;grid-template-columns:14px 140px 92px 1fr;
gap:10px;align-items:center;list-style:none}
details.ev summary::-webkit-details-marker{display:none}
.dot{width:10px;height:10px;border-radius:50%;background:var(--c)}
.t{font:13px ui-monospace,Consolas,monospace}.g{font:13px ui-monospace,Consolas,monospace;color:var(--mute)}
.note{font-size:12px;color:var(--c2);margin-left:8px}
.logs{margin:0;padding:10px 12px;background:var(--code);border-top:1px solid var(--line);
font:12px/1.55 ui-monospace,Consolas,monospace;overflow-x:auto;max-height:340px}
.logs div{white-space:pre}.logs .u{background:var(--hi);font-weight:600}.logs .mark{color:var(--high)}
table{border-collapse:collapse;width:100%;background:var(--panel);border:1px solid var(--line);border-radius:10px;
overflow:hidden;font-variant-numeric:tabular-nums}
th,td{padding:8px 12px;border-bottom:1px solid var(--line);text-align:left;font-size:14px}th{color:var(--mute);font-weight:600}
.explain{color:var(--mute);font-size:14px;max-width:760px}
@media (max-width:640px){details.ev summary{grid-template-columns:14px 1fr;}
details.ev summary .g,details.ev summary .r{grid-column:2}}
"""


def render(meta, events, families, notes, ev):
    t0, t1 = meta["t0"], meta["t1"]
    span = t1 - t0
    col = lambda fid: f"--c:var(--c{fid % N_COLORS})"
    noted = sum(1 for e in events if e.get("notes"))
    h = [f"<!doctype html><html lang=en><head><meta charset=utf-8>"
         f"<meta name=viewport content='width=device-width,initial-scale=1'>"
         f"<title>Robot Incident Triage</title><style>{CSS}</style></head><body><main>"]
    h.append("<h1>Robot incident triage</h1>")
    h.append(f"<p class=sub>Station run {datetime.fromtimestamp(t0, timezone.utc):%Y-%m-%d} · "
             f"{utc(t0)}–{utc(t1)} UTC ({pt(t0)}–{pt(t1)} Pacific) · "
             f"{meta['n_ctrl']:,} control messages · {meta['n_log']:,} log lines</p>")

    stats = [(len(events), "control-loop breaks detected"), (len(families), "fault families (by first anomaly)"),
             (sum(1 for e in events if e["gap"] >= 3), "long gaps ≥ 3 s (drop risk)"),
             (f"{noted}/{len(events)}", "events the operator wrote down")]
    if ev:
        stats.append((f"{ev['detected']}/{ev['total']}", "injected faults found (sim ground truth)"))
    h.append("<div class=stats>" + "".join(f"<div class=stat><b>{v}</b><span>{escape(l)}</span></div>" for v, l in stats) + "</div>")

    # ---- families
    h.append("<h2>Fault families</h2><p class=explain>Each event is grouped by the <b>earliest log template that is "
             "unusual around it</b> compared with quiet time. The chain shows what typically happens, with the median "
             "time relative to the start of the control break.</p>")
    for f in families:
        sev, sev_txt = severity(f)
        gap = (f"gap {f['gap_min']:.2f}–{f['gap_max']:.2f} s" if f["gap_max"] > 0 else "")
        gap += (" · low control rate" if f["low_rate"] else "")
        truth = f" · sim truth: <b>{escape(f['truth_majority'] or 'none')}</b>" if ev else ""
        h.append(f"<section class=fam style='{col(f['id'])}'><header><span class=n>×{f['n']}</span>"
                 f"<span class=root>{tpl_html(f['root'])}</span><span class='pill {sev}'>{sev_txt}</span></header>"
                 f"<div class=meta>first {utc(f['first'])} · last {utc(f['last'])} UTC · {gap}{truth}</div><ol class=chain>")
        for c in f["chain"]:
            share = "" if c["share"] >= 0.99 else f" <span class=g>({c['share']:.0%} of events)</span>"
            h.append(f"<li><span class=off>{c['offset']:+.2f} s</span><span>{tpl_html(c['tpl'])}{share}"
                     f"<span class=ex>{escape(c['example'])}</span></span></li>")
        h.append("</ol></section>")

    # ---- timeline
    W, H, pad = 1000, 120, 10
    x = lambda t: pad + (t - t0) / span * (W - 2 * pad)
    svg = [f"<svg viewBox='0 0 {W} {H}' role=img aria-label='timeline of events'>"]
    for k in range(0, int(span // 1800) + 1):
        tt = t0 + k * 1800
        svg.append(f"<line x1={x(tt):.1f} x2={x(tt):.1f} y1=8 y2={H - 18} stroke='var(--line)'/>"
                   f"<text x={x(tt):.1f} y={H - 4} font-size=11 fill='var(--mute)' text-anchor=middle>{utc(tt)[:5]}</text>")
    svg.append(f"<text x={pad} y=16 font-size=11 fill='var(--mute)'>detected</text>"
               f"<text x={pad} y=76 font-size=11 fill='var(--mute)'>operator notes</text>")
    for e in events:
        r = 4 + min(e["gap"], 8) * 0.9 + (2 if e.get("min_hz") else 0)
        svg.append(f"<circle cx={x(e['start']):.1f} cy=42 r={r:.1f} fill='var(--c{e['family'] % N_COLORS})' "
                   f"fill-opacity=.75><title>{utc(e['start'])} gap {e['gap']:.2f}s</title></circle>")
    for n in notes:
        svg.append(f"<rect x={x(n['t']) - 1.5:.1f} y=82 width=3 height=14 fill='var(--ink)'>"
                   f"<title>{escape(n['raw'])}</title></rect>")
    svg.append("</svg>")
    legend = "".join(f"<span style='{col(f['id'])}'><i></i>{escape(f['root'].partition(' | ')[2][:48])}</span>" for f in families)
    h.append("<h2>Timeline</h2><div class=timeline>" + "".join(svg) + f"<div class=legend>{legend}</div></div>")

    # ---- evaluation
    if ev:
        h.append("<h2>Check against simulator ground truth</h2><table><tr><th>Injected fault</th><th>Count</th>"
                 "<th>Found by tool</th><th>Written down by operator</th></tr>")
        for k, v in sorted(ev["by_type"].items()):
            h.append(f"<tr><td>{escape(k)}</td><td>{v['total']}</td><td>{v['detected']}</td><td>{v['operator']}</td></tr>")
        h.append("</table>")
        pur = " · ".join(f"family {fid}: {n}/{tot} {escape(str(top))}" for fid, top, n, tot in ev["purity"])
        h.append(f"<p class=explain>False positives: <b>{ev['false_positives']}</b>. Family purity: {pur}. "
                 f"Physical arm drops: {ev['drops']}, of which only {ev['drops_with_watchdog']} logged a watchdog "
                 f"error — the error code alone undercounts drops, the control-gap length does not.</p>")

    # ---- event feed
    h.append("<h2>Event feed</h2><p class=explain>Click an event to see every log line in its context window. "
             "Unusual lines are highlighted.</p>")
    for i, e in enumerate(events):
        what = (f"gap {e['gap']:.2f} s" if e["gap"] else "") + (f" min {e['min_hz']} Hz" if e.get("min_hz") else "")
        nt = "".join(f"<span class=note>✎ {escape(n)}</span>" for n in e.get("notes", []))
        if e.get("coincident"):
            nt += (f"<span class=g> · also coincided: {escape(e['coincident'].partition(' | ')[2][:50])}"
                   f" (treated as noise)</span>")
        h.append(f"<details class=ev style='{col(e['family'])}'><summary><span class=dot></span>"
                 f"<span class=t>{utc(e['start'], True)}</span><span class=g>{escape(what.strip())}</span>"
                 f"<span class=r>{tpl_html(e['root'])}{nt}</span></summary><div class=logs>")
        marked = False
        for l in e["window"]:
            if not marked and l["t"] >= e["start"]:
                h.append(f"<div class=mark>──── control break starts {utc(e['start'], True)} ────</div>")
                marked = True
            cls = " class=u" if l["unusual"] else ""
            h.append(f"<div{cls}>{utc(l['t'], True)} {escape(l['svc']):<14} {escape(l['level']):<7} {escape(l['msg'])}</div>")
        h.append("</div></details>")
    h.append("</main></body></html>")
    return "\n".join(h)
