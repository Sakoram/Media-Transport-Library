#!/usr/bin/env python3
"""Judge of question FM: can a multi-packet fastmeta TX unit leave within ST 2110-41 §7, and
does it then reach the library's own RX? Carries fm-b's model (the
ports of ncm_c_max / ncm_max_c_inst of fmd_build_packet_test.cpp:66-84 at a6fc9093, plus the
epoch-anchored drain of §7's text).

Run: python3 -I fastmeta_tx.py        (exit 1 unless every expectation below holds)

Part 1, the reading of §7. §7 is one leaky bucket per stream (drain at k x TDRAIN since the
epoch, CINST <= CMAX "at all times"), so CMAX is a burst allowance of the stream, not a rule per
unit. Two readings of contract.md's "at most CMAX = 4 packets back to back, then one per TDRAIN":
  PER-UNIT  every unit restarts with 4 back to back (fm-d's A2),
  CARRIED   one GCRA over the stream, 4 then one per 1.25 ms carried across units (fm-b, fm-c).
Part 2, the library's own RX (timing.md §11.7): due = first arrival + unit_period + flush,
unit_period = TFRAME for fastmeta, flush = rx.skew_budget_ns (10 ms) with two legs, 1 ms with
one. A packet after the due time is dropped from the published (INCOMPLETE) unit.
Part 3, the verdict's TX unit (one data item, one packet per unit) on the same checks.
"""

import collections
import random
import sys
import types

# fm-b's ports (r0/design/fm/fm-b/tx_schedule_model.py), copied verbatim
ITEM = 1436  # max data item, mtl.h struct mtl_fastmeta_config; st_tx_ancillary_session.c:1803
T_GCRA = 1e9 / 800  # ns
CMAX_SAFE = 4


def ncm_c_max(r):
    return max(4, int(r / 43200))


def ncm_max_c_inst(arrivals, r):
    t_drain = 1e9 / max(800.0, r * 1.1)
    q = collections.deque()
    c = 0
    for a in arrivals:
        while q and q[0] <= a:
            q.popleft()
        q.append((q[-1] if q else a) + t_drain)
        c = max(c, len(q))
    return c


def ncm_epoch_c_inst(arrivals, r):
    """Drain one packet, if present, at each k x TDRAIN since the epoch (time 0)."""
    t_drain = 1e9 / max(800.0, r * 1.1)
    level = 0
    c = 0
    last_k = None
    for a in arrivals:
        k = int(a // t_drain)
        if last_k is not None and k > last_k:
            level = max(0, level - (k - last_k))
        last_k = k if last_k is None else max(last_k, k)
        level += 1
        c = max(c, level)
    return c


def packets(n_bytes):
    return max(1, -(-n_bytes // ITEM))


def schedule(units, period, rule):
    """units: list of byte counts, one unit per period; returns sorted launch instants (ns)."""
    out = []
    tat = 0.0
    cursor_free = 0.0  # the frame tasklet sends one unit after the other
    for k, n_bytes in enumerate(units):
        n = packets(n_bytes)
        m = 1e9 + k * period
        start = max(m, cursor_free)
        for j in range(n):
            if rule == "F2":
                t = max(start, tat - (CMAX_SAFE - 1) * T_GCRA)
                tat = max(tat, t) + T_GCRA
            elif rule == "SPREAD":
                t = start + j * period / n
            else:
                t = start
            out.append(t)
        cursor_free = out[-1]
    return out


def rates(units, period):
    total = sum(packets(b) for b in units)
    dur = len(units) * period / 1e9
    return total / dur


def window_rates(arr):
    """RNOMINAL read three ways: the stream's average, and the lowest and highest average over
    a 10 s window (§7 averages over at least 10 s and does not say which window)."""
    t0, t1 = arr[0], arr[-1]
    avg = len(arr) / max((t1 - t0) / 1e9, 1e-9)
    ws = []
    start = t0
    while start + 10e9 <= t1 + 1:
        n = sum(1 for a in arr if start <= a < start + 10e9)
        ws.append(n / 10.0)
        start += 0.5e9
    return sorted({avg, min(ws or [avg]), max(ws or [avg])})


def check(units, period, rule):
    arr = schedule(units, period, rule)
    worst = 0
    for r in window_rates(arr):
        for f in (ncm_max_c_inst, ncm_epoch_c_inst):
            worst = max(worst, f(arr, r) - ncm_c_max(r))
    return worst, arr


b = types.SimpleNamespace(
    T_GCRA=T_GCRA,
    packets=packets,
    schedule=schedule,
    window_rates=window_rates,
    ncm_max_c_inst=ncm_max_c_inst,
    ncm_epoch_c_inst=ncm_epoch_c_inst,
    ncm_c_max=ncm_c_max,
)

T = b.T_GCRA
PERIODS = {
    "23.98": 1e9 * 1001 / 24000,
    "50": 1e9 / 50,
    "59.94": 1e9 * 1001 / 60000,
    "119.88": 1e9 * 1001 / 120000,
}


def per_unit(units, period):
    """fm-d A2: each unit 4 back to back then 1.25 ms, restarted at each unit's launch (or at
    the end of the previous unit, which the frame tasklet sends first)."""
    out, free = [], 0.0
    for k, nb in enumerate(units):
        start = max(1e9 + k * period, free)
        n = b.packets(nb)
        ts = [start + max(0, j - 3) * T for j in range(n)]
        out += ts
        free = ts[-1]
    return out


def sec7_worst(arr):
    worst = 0
    for r in b.window_rates(arr):
        for f in (b.ncm_max_c_inst, b.ncm_epoch_c_inst):
            worst = max(worst, f(arr, r) - b.ncm_c_max(r))
    return worst


def cut_by_due(units, arr, period, flush):
    """units whose packets arrive after the RX due time (first + TFRAME + flush), and the
    packets cut; sender egress = arrival (no network delay, no skew)."""
    i = cut_units = cut_pkts = 0
    for nb in units:
        n = b.packets(nb)
        first = arr[i]
        due = first + period + flush
        late = sum(1 for t in arr[i : i + n] if t > due)
        cut_units += late > 0
        cut_pkts += late
        i += n
    return cut_units, cut_pkts


def main():
    ok = True

    def expect(cond, text):
        nonlocal ok
        print(("ok   " if cond else "FAIL ") + text)
        ok &= bool(cond)

    rng = random.Random(41)
    print("== Part 1: the reading of §7 (12 s streams) ==")
    for name, period in PERIODS.items():
        frames = int(12e9 / period)
        for sname, units in (
            ("64 KiB every unit", [65536] * frames),
            ("random 0..64 KiB", [rng.randint(0, 65536) for _ in range(frames)]),
            ("2 items every unit", [2 * 1436] * frames),
        ):
            pu = sec7_worst(per_unit(units, period))
            ca = sec7_worst(b.schedule(units, period, "F2"))
            print(
                f"  {name:>6} fps {sname:<19} PER-UNIT CINST-CMAX {pu:+d}   CARRIED {ca:+d}"
            )
            if sname == "64 KiB every unit":
                expect(
                    pu > 0 and ca <= 0,
                    f"{name}: per-unit reading breaks §7, carried holds",
                )
            if sname == "random 0..64 KiB":
                expect(
                    pu > 0 and ca <= 0,
                    f"{name}: varying sizes, per-unit breaks §7, carried " f"holds",
                )
    print("== Part 2: CARRIED schedule against the library's own RX due time ==")
    for name, period in PERIODS.items():
        frames = int(12e9 / period)
        n_fit1 = 4 + int((period + 1e6) // T)
        n_fit2 = 4 + int((period + 10e6) // T)
        for sname, units in (
            ("64 KiB every unit", [65536] * frames),
            (
                "one 64 KiB unit in 12 s",
                [0] * (frames // 2) + [65536] + [0] * (frames - frames // 2 - 1),
            ),
        ):
            arr = b.schedule(units, period, "F2")
            c1 = cut_by_due(units, arr, period, 1e6)
            c2 = cut_by_due(units, arr, period, 10e6)
            print(
                f"  {name:>6} fps {sname:<24} cut (units, packets): one leg {c1}, two legs {c2}; "
                f"a unit fits the due time up to {n_fit1} packets (one leg), {n_fit2} (two)"
            )
            if sname.startswith("one 64 KiB"):
                expect(
                    c1[0] == 1 and c2[0] == (1 if 46 > n_fit2 else 0),
                    f"{name}: a lone 64 KiB unit (46 packets, 52.5 ms) is cut by the RX due "
                    f"time" + (" on one leg" if 46 <= n_fit2 else ""),
                )
    print("== Part 3: the verdict's TX unit, one data item (<= 1436 B) per unit ==")
    for name, period in PERIODS.items():
        frames = int(12e9 / period)
        for sname, units in (
            ("one item, random size", [rng.randint(0, 1436) for _ in range(frames)]),
            ("keep-alive only", [0] * frames),
            (
                "bursty one item",
                [1436 if rng.random() < 0.05 else 0 for _ in range(frames)],
            ),
        ):
            for label, arr in (
                ("PER-UNIT", per_unit(units, period)),
                ("CARRIED", b.schedule(units, period, "F2")),
            ):
                w = sec7_worst(arr)
                c = cut_by_due(units, arr, period, 1e6)
                expect(
                    w <= 0 and c == (0, 0),
                    f"{name} fps {sname:<22} {label}: CINST-CMAX {w:+d}, " f"cut {c}",
                )
    print("fastmeta_tx:", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
