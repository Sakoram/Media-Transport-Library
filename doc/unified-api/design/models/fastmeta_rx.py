#!/usr/bin/env python3
"""Judge of question FM: the RX unit-end rules on two legs, on one engine, same scenarios.

Run: python3 -I fastmeta_rx.py        (exit 1 unless every expectation below holds)

The engine below is fm-a's port of rx_ancillary_session_handle_pkt at a6fc9093
(st_rx_ancillary_session.c:424-705; r0/design/fm/fm-a/fm_a_model.py), copied verbatim. The engine
is unchanged except one hook: each arrival is shown to the sink before the dedup (fm-b's P5
needs it for its leg rule).

Sinks (one record per RTP timestamp, payloads keyed by sequence number unless stated):
  TODAY  core.md §2.2 as written: records in arrival order, the unit published when a packet of
         the next timestamp arrives, or at the due time.
  ALL    fm-d's N3 mode (and fm-b's E16 without its liveness term): published when every leg
         has shown a newer timestamp (taken before the dedup), or at the due time.
  PICK   fm-a's rule, which the verdict takes: a newer timestamp ends the unit as ANC's marker
         does; with two legs a sequence gap from the previous unit's last packet to the newer
         timestamp's first waits at most rx.skew_budget_ns (timeout()); the due time publishes
         what is open; COMPLETE when that range is full.
PICK's late check (a packet of a timestamp already published) in three variants:
  set    fm-a's model: any timestamp ever published (unbounded; not an implementation);
  naive  "not newer than the last published timestamp" (mt_seq32_greater), the O(1) reading;
  last2  "one of the last two published timestamps", the verdict's rule.

Scenarios:
  S1  fm-a's exhaustive cases: every loss pattern (none/P/R[/both]) and every merge of the legs,
      with the skew-budget timeout at every instant (PICK) or never (all sinks).
  S2  fm-d's case the proposers did not run on PICK: each leg loses at most one packet and swaps
      at most one adjacent pair (a reorder inside one leg), every merge.
  S3  a dead leg R: only P delivers. Units published at the next timestamp vs only at due time.
  S4  a sender restart: the RTP timestamp steps back and the sequence jumps; the engine's
      threshold bypass (20 per leg) lets the new stream in; the sink must follow it.
  S5  sequence wrap (0xfffd) through S1's first case.
"""

import itertools
import sys

THRESHOLD = 20  # ST_SESSION_REDUNDANT_ERROR_THRESHOLD, st_header.h:75
BITMAP_BITS = 64  # ST_RX_ANC_BITMAP_BITS, st_header.h:1207


def s16_greater(a, b):  # mt_seq16_greater, mt_util.h:114-117
    d = (a - b) & 0xFFFF
    return (d & 0x8000) == 0 and d != 0


def s32_greater(a, b):  # mt_seq32_greater, mt_util.h:123-126
    d = (a - b) & 0xFFFFFFFF
    return (d & 0x80000000) == 0 and d != 0


class Window:
    __slots__ = ("valid", "ts", "base", "bitmap")

    def __init__(self, valid=False, ts=0, base=0, bitmap=0):
        self.valid, self.ts, self.base, self.bitmap = valid, ts, base, bitmap

    def copy(self):
        return Window(self.valid, self.ts, self.base, self.bitmap)


class Engine:
    """rx_ancillary_session_handle_pkt after the PT/SSRC checks, fast metadata mode (no F bits)."""

    def __init__(self, num_port, sink):
        self.num_port = num_port
        self.session_seq = -1
        self.tmstamp = -1
        self.prev_tmstamp = -1
        self.cur = Window()
        self.prev = Window()
        self.red_cnt = [0] * num_port
        self.redundant = 0
        self.received = 0
        self.unrecovered = 0
        self.sink = sink

    def handle(self, seq, ts, port):
        if self.session_seq == -1:
            self.session_seq = (seq - 1) & 0xFFFF
        if self.tmstamp == -1:
            self.tmstamp = (ts - 1) & 0xFFFFFFFF
        bypass = False
        accept = False
        if s32_greater(self.tmstamp, ts) or not s16_greater(seq, self.session_seq):
            c = self.cur
            if c.valid and ts == c.ts and not s32_greater(self.tmstamp, ts):
                off = (seq - c.base) & 0xFFFF
                if off < BITMAP_BITS and not (c.bitmap >> off) & 1:
                    if self.unrecovered > 0:
                        self.unrecovered -= 1
                    accept = True
            p = self.prev
            if (
                not accept
                and self.prev_tmstamp >= 0
                and ts == self.prev_tmstamp
                and p.valid
                and ts == p.ts
            ):
                off = (seq - p.base) & 0xFFFF
                if off < BITMAP_BITS and not (p.bitmap >> off) & 1:
                    if self.unrecovered > 0:
                        self.unrecovered -= 1
                    accept = True
            if not accept:
                self.red_cnt[port] += 1
                self.redundant += 1
                if any(self.red_cnt[i] < THRESHOLD for i in range(self.num_port)):
                    return -11  # -EAGAIN
                self.redundant -= 1
                bypass = True
        self.red_cnt[port] = 0
        old_session_seq = self.session_seq
        if seq != (self.session_seq + 1) & 0xFFFF and s16_greater(
            seq, self.session_seq
        ):
            self.unrecovered += (seq - self.session_seq - 1) & 0xFFFF
        if s16_greater(seq, self.session_seq):
            self.session_seq = seq
        if self.prev.valid and ts == self.prev.ts:
            off = (seq - self.prev.base) & 0xFFFF
            if off < BITMAP_BITS:
                self.prev.bitmap |= 1 << off
        else:
            if not self.cur.valid or ts != self.cur.ts:
                if self.cur.valid:
                    self.prev = self.cur.copy()
                self.cur = Window(True, ts, (old_session_seq + 1) & 0xFFFF, 0)
            off = (seq - self.cur.base) & 0xFFFF
            if off < BITMAP_BITS:
                self.cur.bitmap |= 1 << off
        # N3: the sink replaces the ring enqueue (st_rx_ancillary_session.c:657-675)
        self.sink.packet(seq, ts)
        if ts != self.tmstamp and (s32_greater(ts, self.tmstamp) or bypass):
            self.prev_tmstamp = self.tmstamp
            self.tmstamp = ts
        self.received += 1
        return 0


class LegEngine(Engine):
    """fm-a's port of the engine; the sink sees every arrival before the dedup (leg_seen)."""

    def handle(self, seq, ts, port):
        if hasattr(self.sink, "leg_seen"):
            self.sink.leg_seen(ts, port)
        self.sink.cur_leg = port
        return super().handle(seq, ts, port)


class Rec:
    __slots__ = ("ts", "seqs", "order", "closed")

    def __init__(self, ts):
        self.ts, self.seqs, self.order, self.closed = ts, {}, [], False


class SinkBase:
    def __init__(self, num_port=2, late_rule="set"):
        self.num_port = num_port
        self.late_rule = late_rule
        self.inflight = []
        self.published = []  # (ts, seq -> copies, arrival order, complete, how)
        self.pub_ts = []
        self.last_pub_max = None
        self.delivered = set()
        self.late = 0
        self.forced = 0
        self.cur_leg = 0
        self.step = 0
        self.bounded_drops = 0

    def _is_late(self, ts):
        if not self.pub_ts:
            return False
        if self.late_rule == "set":
            return ts in self.pub_ts
        if self.late_rule == "naive":
            return not s32_greater(ts, self.pub_ts[-1])
        return ts in self.pub_ts[-2:]  # last2

    def _max(self, r):
        m = None
        for s in r.seqs:
            if m is None or s16_greater(s, m):
                m = s
        return m

    def _min(self, r):
        m = None
        for s in r.seqs:
            if m is None or s16_greater(m, s):
                m = s
        return m

    def _full(self, start, end, r):
        if not r.seqs:
            return True
        if start is None:
            start = self._min(r)
        n = (end - start) & 0xFFFF
        if n >= 0x8000:
            return True
        return all(((start + i) & 0xFFFF) in r.seqs for i in range(n + 1))

    def _complete(self, r, nxt):
        start = None if self.last_pub_max is None else (self.last_pub_max + 1) & 0xFFFF
        end = (
            (self._min(nxt) - 1) & 0xFFFF
            if nxt is not None and nxt.seqs
            else self._max(r)
        )
        return self._full(start, end, r)

    def _publish(self, r, nxt, how):
        self.published.append(
            (r.ts, dict(r.seqs), list(r.order), self._complete(r, nxt), how, self.step)
        )
        self.pub_ts.append(r.ts)
        m = self._max(r)
        if m is not None:
            self.last_pub_max = m
        self.inflight.remove(r)

    def _add(self, r, seq, ts):
        if seq in r.seqs:
            r.seqs[seq].append((ts, seq))
        else:
            r.seqs[seq] = [(ts, seq)]
        r.order.append(seq)

    def timeout(self):
        while self.inflight and self.inflight[0].closed:
            r = self.inflight[0]
            self._publish(
                r, self.inflight[1] if len(self.inflight) > 1 else None, "timeout"
            )

    def due(self):
        while self.inflight:
            r = self.inflight[0]
            self._publish(
                r, self.inflight[1] if len(self.inflight) > 1 else None, "due"
            )


class TodaySink(SinkBase):
    """core.md §2.2 as written: publish when a packet of the next timestamp arrives."""

    def packet(self, seq, ts):
        self.step += 1
        self.delivered.add(seq)
        for r in self.inflight:
            if r.ts == ts:
                if seq not in r.seqs:
                    self._add(r, seq, ts)
                return
        if self._is_late(ts):
            self.late += 1
            return
        while self.inflight:
            self._publish(self.inflight[0], None, "next")
        r = Rec(ts)
        self._add(r, seq, ts)
        self.inflight.append(r)


class AllSink(SinkBase):
    """fm-d's N3 mode: published once every leg has shown a newer timestamp, or at due.
    Its proposers define no COMPLETE rule here, so only the content is checked (no loss of a
    delivered packet, no copy twice, one slot per timestamp), not the status flag."""

    flags_checked = False

    def __init__(self, num_port=2, late_rule="set"):
        super().__init__(num_port, late_rule)
        self.newest = [None] * num_port

    def leg_seen(self, ts, leg):
        if self.newest[leg] is None or s32_greater(ts, self.newest[leg]):
            self.newest[leg] = ts
        self.step += 1
        self._drain()

    def _passed(self, r):
        return all(n is not None and s32_greater(n, r.ts) for n in self.newest)

    def _drain(self):
        while self.inflight and self._passed(self.inflight[0]):
            r = self.inflight[0]
            self._publish(
                r, self.inflight[1] if len(self.inflight) > 1 else None, "legs"
            )

    def packet(self, seq, ts):
        self.delivered.add(seq)
        for r in self.inflight:
            if r.ts == ts:
                if seq not in r.seqs:
                    self._add(r, seq, ts)
                self._drain()
                return
        if self._is_late(ts):
            self.late += 1
            return
        r = Rec(ts)
        self._add(r, seq, ts)
        self.inflight.append(r)
        self._drain()

    def timeout(self):
        pass  # no skew wait in this rule; the due time is the fallback


class PickSink(SinkBase):
    """fm-a's rule: a newer timestamp is the marker; a gap waits at most the skew budget."""

    def __init__(self, num_port=2, late_rule="last2", mutant=None):
        super().__init__(num_port, late_rule)
        self.mutant = mutant

    def _ready(self, r, nxt):
        if self.mutant == "own_max":
            return self._full(
                None if self.last_pub_max is None else (self.last_pub_max + 1) & 0xFFFF,
                self._max(r),
                r,
            )
        return self._complete(r, nxt)

    def _drain(self):
        while self.inflight and self.inflight[0].closed:
            r = self.inflight[0]
            nxt = self.inflight[1] if len(self.inflight) > 1 else None
            if (
                self.num_port > 1
                and self.mutant != "no_wait"
                and not self._ready(r, nxt)
            ):
                return
            self._publish(r, nxt, "next")

    def packet(self, seq, ts):
        self.step += 1
        self.delivered.add(seq)
        for r in self.inflight:
            if r.ts == ts:
                if seq not in r.seqs:
                    self._add(r, seq, ts)
                self._drain()
                return
        if self._is_late(ts):
            self.late += 1
            return
        for r in self.inflight:
            r.closed = True
        if len(self.inflight) == 2:
            self.forced += 1
            self._publish(self.inflight[0], self.inflight[1], "forced")
        r = Rec(ts)
        self._add(r, seq, ts)
        self.inflight.append(r)
        self._drain()


def stream(sizes, seq0=0, ts0=1000, step=1501):
    pkts, seq = [], seq0
    for k, n in enumerate(sizes):
        for _ in range(n):
            pkts.append((seq, (ts0 + step * k) & 0xFFFFFFFF))
            seq = (seq + 1) & 0xFFFF
    return pkts


def merges(a, b):
    if not a:
        yield list(b)
        return
    if not b:
        yield list(a)
        return
    for rest in merges(a[1:], b):
        yield [a[0]] + rest
    for rest in merges(a, b[1:]):
        yield [b[0]] + rest


def run(sink, arrivals, timeout_at=None):
    eng = LegEngine(2, sink)
    for i, (seq, ts, port) in enumerate(arrivals):
        if timeout_at == i:
            sink.timeout()
        eng.handle(seq, ts, port)
    if timeout_at == len(arrivals):
        sink.timeout()
    sink.due()
    return eng


def violation(sink, truth, timed_out, in_order_records=True, pos=None):
    """None, or the first broken property. 'lost' = the engine delivered the packet to the
    sink and it is in no published unit; 'reorder' = a slot's records not in sequence order.
    """
    pub = sink.published
    tss = [u[0] for u in pub]
    if len(tss) != len(set(tss)):
        return "two slots for one timestamp"
    for i, (ts, seqs, order, complete, how, _) in enumerate(pub):
        for sq, copies in seqs.items():
            if len(copies) != 1:
                return f"seq {sq} copied twice"
            if truth.get(sq) != ts:
                return f"seq {sq} in the wrong unit"
        if not in_order_records and [pos[x] for x in order] != sorted(
            pos[x] for x in order
        ):
            return f"reorder in unit {ts}: {order}"
        missing = {sq for sq, t in truth.items() if t == ts} - set(seqs)
        if getattr(sink, "flags_checked", True) and complete and missing and seqs:
            lo = min(pos[x] for x in seqs)
            hi = max(pos[x] for x in seqs)
            if any(lo < pos[m] < hi for m in missing):
                return f"unit {ts} COMPLETE with a hole"
            if any(pos[m] < lo for m in missing) and i != 0:
                return f"unit {ts} COMPLETE without its leading packets"
            if any(pos[m] > hi for m in missing) and i + 1 < len(pub) and pub[i + 1][3]:
                return f"unit {ts} COMPLETE without its trailing packets, the next COMPLETE too"
    in_units = set()
    for u in pub:
        in_units |= set(u[1])
    how_of = {u[0]: u[4] for u in pub}
    for m in sorted(sink.delivered - in_units):
        how = how_of.get(truth[m])
        if how is None or how in ("next", "legs"):
            return f"lost after delivery: {m} (unit published by {how})"
        sink.bounded_drops += (
            1  # after the skew budget, the due time or a forced publish
        )
    if (
        getattr(sink, "flags_checked", True)
        and sink.delivered == set(truth)
        and not timed_out
        and not sink.forced
    ):
        for ts, seqs, order, complete, how, _ in pub:
            if not complete:
                return f"unit {ts} INCOMPLETE though every packet reached the sink"
    return None


def legs_from(pkts, pattern):
    p = [(s, t, 0) for (s, t), lg in zip(pkts, pattern) if lg not in ("P", "both")]
    r = [(s, t, 1) for (s, t), lg in zip(pkts, pattern) if lg not in ("R", "both")]
    return p, r


def s1(make, sizes, losses, seq0=0, timeouts=False, records_by_seq=True):
    pkts = stream(sizes, seq0=seq0)
    truth = {s: t for s, t in pkts}
    pos = {s: i for i, (s, _) in enumerate(pkts)}
    runs = fails = 0
    first = None
    for pattern in itertools.product(losses, repeat=len(pkts)):
        p, r = legs_from(pkts, pattern)
        for arr in merges(p, r):
            tmos = range(len(arr) + 2) if timeouts else [len(arr) + 1]
            for tmo in tmos:
                sink = make()
                run(sink, arr, tmo)
                runs += 1
                v = violation(sink, truth, tmo <= len(arr), records_by_seq, pos)
                if v:
                    fails += 1
                    if first is None:
                        first = (v, pattern, [(s, lg) for s, _, lg in arr], tmo)
    return runs, fails, first


def swaps(leg):
    yield list(leg)
    for i in range(len(leg) - 1):
        x = list(leg)
        x[i], x[i + 1] = x[i + 1], x[i]
        yield x


def s2(make, sizes, records_by_seq=True):
    pkts = stream(sizes)
    truth = {s: t for s, t in pkts}
    pos = {s: i for i, (s, _) in enumerate(pkts)}
    runs = fails = 0
    first = None
    n = len(pkts)
    for lp in [None] + list(range(n)):
        for lr in [None] + list(range(n)):
            p = [(s, t, 0) for i, (s, t) in enumerate(pkts) if i != lp]
            r = [(s, t, 1) for i, (s, t) in enumerate(pkts) if i != lr]
            for ps in swaps(p):
                for rs in swaps(r):
                    for arr in merges(ps, rs):
                        sink = make()
                        run(sink, arr, None)
                        runs += 1
                        v = violation(sink, truth, False, records_by_seq, pos)
                        if v:
                            fails += 1
                            if first is None:
                                first = (v, lp, lr, [(s, lg) for s, _, lg in arr])
    return runs, fails, first


def s3(make, sizes):
    """dead leg R: count units published at the next timestamp (prompt) vs at due."""
    pkts = stream(sizes)
    arr = [(s, t, 0) for s, t in pkts]
    sink = make()
    run(sink, arr, None)
    hows = [u[4] for u in sink.published]
    return hows.count("next") + hows.count("legs"), hows.count("due"), len(hows)


def s4(make, seq_new=5000):
    """a sender restart: 3 units at ts 900000.., then the stream restarts at ts 1000 with a new
    sequence base, 30 one-packet units, both legs in step (P then R)."""
    a = stream([2, 1, 1], seq0=100, ts0=900000)
    b = stream([1] * 30, seq0=seq_new, ts0=1000)
    arr = []
    for s, t in a + b:
        arr += [(s, t, 0), (s, t, 1)]
    sink = make()
    eng = run(sink, arr, None)
    after = [u for u in sink.published if u[0] < 900000]
    return len(after), sink.late, eng.redundant


def main():
    ok = True

    def expect(cond, text):
        nonlocal ok
        print(("ok   " if cond else "FAIL ") + text)
        ok &= bool(cond)

    print("== S1: fm-a's exhaustive cases, all sinks, no skew timeout ==")
    cases = [
        ("T:3,T+1:1,T+2:1 none/P/R", [3, 1, 1], ("none", "P", "R")),
        ("T:2,T+1:1,T+2:1 none/P/R/both", [2, 1, 1], ("none", "P", "R", "both")),
        ("T:1,T+1:2,T+2:2 none/P/R", [1, 2, 2], ("none", "P", "R")),
    ]
    for name, sizes, losses in cases:
        for label, make, byseq in (
            ("TODAY", lambda: TodaySink(), False),
            ("ALL  ", lambda: AllSink(), True),
            ("PICK ", lambda: PickSink(), True),
        ):
            runs, fails, first = s1(make, sizes, losses, records_by_seq=byseq)
            print(
                f"  {label} {name}: {runs} runs, {fails} violations"
                + (f"; first: {first[0]} arrivals {first[2]}" if first else "")
            )
            if label.strip() == "TODAY" and name.startswith("T:3"):
                expect(
                    fails > 0,
                    "TODAY (core.md §2.2 as written) loses or reorders on two legs",
                )
            if label.strip() == "PICK":
                expect(fails == 0, f"PICK holds on {name}")
    print(
        "== S1 with the skew-budget timeout at every instant (PICK, set and last2) =="
    )
    for name, sizes, losses in cases:
        for rule in ("set", "last2"):
            runs, fails, first = s1(
                lambda: PickSink(late_rule=rule), sizes, losses, timeouts=True
            )
            print(
                f"  PICK/{rule} {name}: {runs} runs, {fails} violations"
                + (f"; first: {first}" if first else "")
            )
            expect(fails == 0, f"PICK/{rule} with timeouts on {name}")
    print("== S5: sequence wrap (seq0 0xfffd) ==")
    runs, fails, first = s1(
        lambda: PickSink(), [3, 1, 1], ("none", "P", "R"), seq0=0xFFFD, timeouts=True
    )
    print(f"  PICK wrap: {runs} runs, {fails} violations")
    expect(fails == 0, "PICK across the sequence wrap")
    print("== S2: one loss and one adjacent swap per leg (fm-d's scenario) ==")
    for label, make, byseq in (
        ("TODAY", lambda: TodaySink(), False),
        ("ALL  ", lambda: AllSink(), True),
        ("PICK ", lambda: PickSink(), True),
    ):
        runs, fails, first = s2(make, [3, 1, 1], byseq)
        print(
            f"  {label}: {runs} runs, {fails} violations"
            + (
                f"; first: {first[0]} (lost P#{first[1]}, R#{first[2]}) arrivals {first[3]}"
                if first
                else ""
            )
        )
    print(
        "  PICK's S2 violations are a reorder on BOTH legs at the unit boundary only if "
        "listed; see the verdict"
    )
    print("== S3: a dead leg R (P only, no loss), 6 units of 2 packets ==")
    for label, make in (
        ("TODAY", lambda: TodaySink()),
        ("ALL  ", lambda: AllSink()),
        ("PICK ", lambda: PickSink()),
    ):
        prompt, due, total = s3(make, [2] * 6)
        print(
            f"  {label}: {prompt} of {total} units published at the next timestamp, "
            f"{due} only at the due time"
        )
        if label.strip() == "ALL":
            expect(
                due == total,
                "ALL without a liveness term waits for the due time on a dead leg",
            )
        if label.strip() == "PICK":
            expect(
                prompt == total - 1 and due == 1, "PICK publishes at once on a dead leg"
            )
    print("== S4: a sender restart (timestamp steps back, new sequence base) ==")
    for rule in ("naive", "last2", "set"):
        n, late, red = s4(lambda: PickSink(late_rule=rule))
        print(
            f"  PICK/{rule}: {n} of 30 units of the new stream published, {late} packets "
            f"dropped as late by the sink, engine redundant {red}"
        )
        if rule == "naive":
            expect(
                n == 0,
                "the naive late check (not newer than the last published) never "
                "recovers from a restart: the mutant the verdict's rule kills",
            )
        if rule == "last2":
            expect(
                n > 0 and late == 0,
                "last2 follows the restart once the engine's bypass " "lets it in",
            )
    n, late, red = s4(lambda: PickSink(late_rule="last2"), seq_new=40000)
    print(
        f"  engine fact at a6fc9093, any sink: a restart whose sequence jumps by 32768 or more "
        f"(here 103 -> 40000): {n} of 30 units reach the sink, engine redundant {red} "
        f"(session_seq_id only advances, st_rx_ancillary_session.c:631)"
    )
    expect(
        n <= 2,
        "the engine stays in the threshold-bypass loop (noticed, legacy, not FM's)",
    )
    print("== mutants of PICK ==")
    for mutant in ("no_wait", "own_max"):
        killed = False
        for name, sizes, losses in cases:
            runs, fails, first = s1(lambda: PickSink(mutant=mutant), sizes, losses)
            if fails:
                killed = True
                print(f"  {mutant}: killed on {name}: {first[0]}")
                break
        expect(killed, f"mutant {mutant} killed")
    print("fastmeta_rx:", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
