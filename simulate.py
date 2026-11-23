#!/usr/bin/env python3
"""Synthetic 60 Hz robot control loop with injected faults.

Writes a run directory that looks like what a real station produces:
  logs/<service>.log           text logs from several services (with background noise)
  telemetry/joint_control.txt  epoch timestamps of every published control command
  operator_notes.txt           what a human operator managed to write down (sloppy, local time)
  ground_truth.json            what was actually injected (used only for evaluation)

Fault families (each has a different upstream cause and log chain):
  camera_empty   camera returns empty frames -> policy server 503 -> control gap (long gap = arm drop)
  clock_step     system clock steps -> frames dropped -> camera_empty chain
  solver_stall   scheduler / trajectory solver stalls 110-200 ms, recurring ~every 90 s
  thermal        GPU throttles -> control rate drops -> low-rate error -> robot sent home
"""
import argparse
import json
import os
import random
from datetime import datetime, timedelta, timezone

RATE = 60.0
PT = timezone(timedelta(hours=-7))


class Sim:
    def __init__(self, t0, duration, seed):
        self.t0 = t0
        self.base = t0.timestamp()
        self.D = duration
        self.r = random.Random(seed)
        self.logs = {}
        self.gaps = []   # (start, end) relative seconds: no commands published
        self.slow = []   # (start, end, hz)
        self.truth = []
        self.busy = []   # occupied intervals for scheduling discrete faults

    # ---------- helpers ----------
    def stamp(self, t):
        return (self.t0 + timedelta(seconds=t)).strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]

    def log(self, svc, t, level, src, msg):
        if 0 <= t < self.D:
            self.logs.setdefault(svc, []).append((t, f"{self.stamp(t)} - {level} - {src} - {msg}"))

    def free_slot(self, lo, hi, pad):
        for _ in range(1000):
            t = self.r.uniform(lo, hi)
            if all(not (a - pad < t < b + pad) for a, b in self.busy):
                return t
        raise RuntimeError("could not schedule fault")

    # ---------- background noise ----------
    def background(self):
        r = self.r
        t = 7.0
        while t < self.D:  # CAN status probe: looks scary, harmless, every ~5 min
            for i, dev in enumerate(["left_arm", "right_arm", "torso", "base", "head"]):
                self.log("hw_driver", t + i, "WARNING", "can_probe.py:41",
                         f"node {dev} did not answer status probe")
            self.log("hw_driver", t + 5, "INFO", "can_probe.py:58", "status probe done: 0 of 5 nodes answered")
            t += 300 + r.uniform(-0.5, 0.5)
        t = 1.0
        while t < self.D:  # UI polling
            self.log("ui_api", t, "INFO", "http.py:120", "GET /api/status 200")
            t += 2.0 + r.uniform(-0.05, 0.05)
        t = 30.0
        while t < self.D:  # recorder segment rollover
            self.log("recorder", t, "INFO", "segmenter.py:77", f"recording segment closed in {r.uniform(0.6, 0.75):.2f}s")
            t += 60 + r.uniform(-0.2, 0.2)
        for _ in range(10):  # harmless small scheduler overruns, below any visible effect
            t = r.uniform(0, self.D)
            self.log("controller", t, "WARNING", "scheduler.py:144", f"scheduler cycle overrun: {r.uniform(40, 70):.2f} ms")

    # ---------- faults ----------
    def camera_empty(self, t0, dur):
        r = self.r
        t = t0
        while t < t0 + dur:
            self.log("policy_client", t, "ERROR", "frame_source.py:211", "camera 'cam_top' returned empty frame")
            self.log("policy_client", t + 0.004, "ERROR", "policy_client.py:88", "policy server responded HTTP 503")
            t += 0.25 + r.uniform(-0.03, 0.03)
        g0, g1 = t0 + 0.3, t0 + dur + 0.3
        gap = g1 - g0
        self.gaps.append((g0, g1))
        if gap > 0.5:
            self.log("controller", g1 + 0.01, "WARNING", "arm_controller.py:233",
                     f"no fresh command for {gap:.2f}s, holding last target")
        dropped = gap > 3.0
        if dropped and r.random() > 0.15:  # watchdog code is NOT 1:1 with physical drops
            self.log("hw_driver", g0 + 3.0, "ERROR", "watchdog.py:57", "E_CTRL_EXPIRED: control watchdog expired")
        return g0, g1, dropped

    def add_camera_empty(self, t0):
        g0, g1, dropped = self.camera_empty(t0, self.r.uniform(0.8, 9.0))
        self.truth.append(dict(type="camera_empty", start=g0, end=g1, gap=g1 - g0, arm_dropped=dropped))

    def add_clock_step(self, t0):
        r = self.r
        step = r.choice([-1, 1]) * r.uniform(0.4, 2.5)
        self.log("timesync", t0, "WARNING", "timesync.py:132", f"system clock stepped by {step:+.3f}s")
        self.log("policy_client", t0 + 0.05, "WARNING", "frame_source.py:174", "frame timestamp out of range, dropping")
        g0, g1, dropped = self.camera_empty(t0 + 0.2, r.uniform(1.0, 3.5))
        self.truth.append(dict(type="clock_step", start=g0, end=g1, gap=g1 - g0, arm_dropped=dropped))

    def add_thermal(self, t0):
        r = self.r
        self.log("sysmon", t0, "WARNING", "thermal.py:44",
                 f"gpu clock throttled to {r.randint(55, 70)}% (temp {r.randint(86, 93)} C)")
        s0, dur, hz = t0 + 1.5, r.uniform(6, 12), r.uniform(34, 42)
        self.slow.append((s0, s0 + dur, hz))
        self.log("hw_driver", s0 + 3, "ERROR", "rate_monitor.py:31", f"E_CTRL_LOW_RATE: control rate low ({hz:.1f} Hz)")
        self.log("controller", s0 + 3.05, "INFO", "supervisor.py:212", "sending robot to home pose")
        self.log("sysmon", s0 + dur, "INFO", "thermal.py:51", "gpu clock restored")
        self.truth.append(dict(type="thermal", start=s0, end=s0 + dur, gap=0.0, arm_dropped=False))

    def add_solver_stall(self, t0):
        ms = self.r.uniform(110, 200)
        te = t0 + ms / 1000
        self.gaps.append((t0, te))
        self.log("controller", te, "WARNING", "scheduler.py:144", f"scheduler cycle overrun: {ms:.2f} ms")
        self.log("controller", te + 0.002, "WARNING", "scheduler.py:151", f"predicting pose ahead by {ms / 1000 - 0.03:.4f}s")
        self.log("controller", te + 0.010, "WARNING", "planner.py:92", f"planner tick late by {ms / 1000 + 0.025:.4f}s")
        self.truth.append(dict(type="solver_stall", start=t0, end=te, gap=ms / 1000, arm_dropped=False))

    def schedule(self):
        half = self.D / 2
        # phase 1: camera / clock problems; phase 2 (after a "mitigation"): recurring solver stalls
        for kind, n, lo, hi in [("camera_empty", 7, 300, half - 100), ("clock_step", 3, 300, half - 100),
                                ("thermal", 3, 300, self.D - 100)]:
            for _ in range(n):
                t = self.free_slot(lo, hi, 90)
                self.busy.append((t - 5, t + 20))
                getattr(self, "add_" + kind)(t)
        t = half + 100
        while t < self.D - 30:
            if all(not (a - 40 < t < b + 40) for a, b in self.busy):
                self.add_solver_stall(t)
            t += 90 + self.r.uniform(-6, 6)

    # ---------- telemetry ----------
    def telemetry(self):
        gaps = sorted(self.gaps)
        slow = sorted(self.slow)
        out, t, gi, si = [], 0.0, 0, 0
        while t < self.D:
            while gi < len(gaps) and gaps[gi][1] <= t:
                gi += 1
            if gi < len(gaps) and gaps[gi][0] <= t:
                t = gaps[gi][1]
                continue
            while si < len(slow) and slow[si][1] <= t:
                si += 1
            period = 1.0 / slow[si][2] if si < len(slow) and slow[si][0] <= t else 1.0 / RATE
            out.append(t)
            t += period + self.r.uniform(-0.001, 0.001)
        # FPS summary log every 10 s, computed from what was really published
        j = 0
        for w in range(10, int(self.D), 10):
            c = 0
            while j < len(out) and out[j] < w:
                c += 1
                j += 1
            self.log("controller", w + 0.003, "INFO", "fps_monitor.py:20", f"FPS summary: joint_control={c / 10:.1f} Hz")
        return out

    # ---------- operator ----------
    def operator_notes(self):
        r = self.r
        notes = []
        for f in self.truth:
            if f["type"] == "solver_stall":
                p, label = 0.12, "stutter"
            elif f["type"] == "thermal":
                p, label = 0.5, "froze, went home"
            elif f["arm_dropped"]:
                p, label = 0.75, "arms dropped on table"
            else:
                p, label = 0.55, "freeze"
            f["operator_noted"] = r.random() < p
            if f["operator_noted"]:
                local = self.t0 + timedelta(seconds=f["start"] + r.uniform(-60, 90))
                local = local.astimezone(PT)
                notes.append((local, label))
        notes.sort()
        lines = [f"Freezes {self.t0.astimezone(PT).month}/{self.t0.astimezone(PT).day} (Pacific)"]
        for local, label in notes:
            h = local.hour % 12 or 12
            lines.append(f"{h}:{local.minute:02d} {label}")
        return lines

    def write(self, out):
        os.makedirs(os.path.join(out, "logs"), exist_ok=True)
        os.makedirs(os.path.join(out, "telemetry"), exist_ok=True)
        self.background()
        self.schedule()
        tele = self.telemetry()
        notes = self.operator_notes()
        for svc, lines in self.logs.items():
            lines.sort()
            with open(os.path.join(out, "logs", svc + ".log"), "w", encoding="utf-8") as fh:
                fh.write("\n".join(l for _, l in lines) + "\n")
        with open(os.path.join(out, "telemetry", "joint_control.txt"), "w") as fh:
            fh.write("\n".join(f"{self.base + t:.4f}" for t in tele) + "\n")
        with open(os.path.join(out, "operator_notes.txt"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(notes) + "\n")
        truth = [dict(f, start=round(self.base + f["start"], 4), end=round(self.base + f["end"], 4),
                      gap=round(f["gap"], 3)) for f in sorted(self.truth, key=lambda f: f["start"])]
        with open(os.path.join(out, "ground_truth.json"), "w") as fh:
            json.dump(dict(t0=self.base, duration=self.D, faults=truth), fh, indent=1)
        print(f"wrote {out}: {len(tele)} control msgs, {sum(len(v) for v in self.logs.values())} log lines, "
              f"{len(truth)} injected faults, {len(notes) - 1} operator notes")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="runs/run1")
    ap.add_argument("--hours", type=float, default=2.0)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--start", default="2026-03-02T17:00:00", help="UTC start time")
    a = ap.parse_args()
    t0 = datetime.fromisoformat(a.start).replace(tzinfo=timezone.utc)
    Sim(t0, a.hours * 3600, a.seed).write(a.out)


if __name__ == "__main__":
    main()
