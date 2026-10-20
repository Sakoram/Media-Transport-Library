#!/usr/bin/env python3
"""legacy_wake.py: the legacy wake modes of D-246 (ST_CORE_LEGACY_WAKE_EDGE for TX,
ST_CORE_LEGACY_WAKE_TOKEN for RX; wait-protocol.md §3 LW1-LW2, migration.md §6.5 LB-14, LB-15)
checked against a model of today's legacy pipeline wait (main @ 2fe5599f, the code of a6fc9093).
From the WAKE round of task R0 (proposal wk-b, refined by its judge).

Every interleaving under sequential consistency; each atomic operation, each syscall and each step
under the mutex is one step (the style of final_model.py).

CORE: the object path of wait-protocol.md §3 on lane 0 (ACQUIRE / DEQUEUE): ec = {seq, count},
T1-T9, a tasklet producer (E1, E2, M1 mark, F6, K1), a direct producer, close (CLOSING, E2, K1),
an EVENT without a publish (a spurious wake), and the legacy wake:
  the get  DP (D1 CLOSING -> NULL; D2 one ATTEMPT), then, after a failed DP, WT with the mode
  LW1  wake_block: TOKEN: store(lkick, 1); EDGE: fetch_add(lkick, 1); then E2, K1 (EVENT(s, lane 0))
  LW2  the waiter, in a session with a legacy mode only:
       TOKEN (RX): between T3 and T4 on every pass:
                   if (xchg(lkick, 0)) { r = ATTEMPT; return r >= 0 ? r : -MTL_ECANCELED }
       EDGE  (TX): k0 = load(lkick) at T1; after every failed T4: if (load(lkick) != k0)
                   return -MTL_ECANCELED
  A session without a mode (every unified session) never runs LW2: its WT is T1-T9 unchanged.
  --wt-only runs the get as WT alone (TOKEN's LW2 after the first failed T4, then before T4).

LEGACY: today's st20p RX and TX get_frame with BLOCK_GET (st20_pipeline_rx.c:861-917,
st20_pipeline_tx.c:782-808), their notify, wake_block and destroy hooks, with a mutex and a
condition variable (signal wakes one waiter, chosen by the scheduler; broadcast wakes all).

Time: a deadline passes only when nothing else can run (a quiescent timeout).  So "T" (NULL at the
timeout) means that nothing would have ended the wait, and "E" (NULL early) and "F" (a frame) are
the other results of a get.  --any-deadline lets a deadline pass at any step and checks safety
only (LEAK, STUCK, every result among today's with spurious wakeups).

Checks per case: the property the main test pins, on CORE; CORE's results are a subset of LEGACY's
(no legacy user sees a result today's code cannot give; the exempt cases are within today's
results with POSIX spurious wakeups); every count is 0 at the end (LEAK); every thread finishes
(STUCK).  Each mutant of the design must break a case.  The J* cases are the judge's.
Usage: python3 -I legacy_wake.py [--wt-only | --any-deadline] [--trace CASE MUTANT]; exit 1 unless
every check passes."""

import sys
import time

DPFIRST = [True]
ANYDL = [False]
# ------------------------------------------------------------------------------- CORE ------
# g = (seq, cnt, R, closing, fired, FQ, lk)
SEQ, CNT, RR, CLO, FIRED, FQ, LK = range(7)


def gset(g, **kw):
    g = list(g)
    for k, v in kw.items():
        g[
            {
                "seq": SEQ,
                "cnt": CNT,
                "R": RR,
                "clo": CLO,
                "fired": FIRED,
                "fq": FQ,
                "lk": LK,
            }[k]
        ] = v
    return tuple(g)


def k1(g, ths):
    """K1: FUTEX_WAKE_BITSET on lane 0, INT_MAX: every queued sleeper goes to T9."""
    if not g[FQ]:
        return g, ths
    ths = list(ths)
    for t in g[FQ]:
        ths[t] = ("T9",) + ths[t][1:]
    return gset(g, fq=frozenset()), tuple(ths)


def core_succ(s, t, kinds, par, mut):
    g, ths = s
    k, p, loc = kinds[t], par[t], ths[t]
    out = []

    def put(g2, loc2, ths2=None):
        ths2 = list(ths if ths2 is None else ths2)
        ths2[t] = loc2
        out.append((g2, tuple(ths2)))

    if k != "W":
        pc, i = loc
        prog = p["prog"]
        if pc == "done":
            return out
        if pc == "start":
            if guard_ok(p, ths, kinds, "core"):
                put(g, (prog[0] + ".0", 0))
            return out
        op, st = pc.split(".")
        nxt = (prog[i + 1] + ".0", i + 1) if i + 1 < len(prog) else ("done", 0)
        if op in (
            "pub",
            "pubt",
        ):  # E1 publish; pub: a direct producer (K1 itself), pubt: a tasklet
            if st == "0":
                g2 = gset(g, R=g[RR] + 1)
                if (
                    "notify_sets_token" in mut
                ):  # the defect 1a89339c4 fixed: a frame notify leaves a token
                    g2 = gset(g2, lk=1)
                put(g2, (op + ".rmw", i))
            elif st == "rmw":  # E2: one RMW on ec, E3: a count seen
                w = g[CNT] > 0
                put(
                    gset(g, seq=g[SEQ] + 1),
                    (
                        (op + (".fw" if op == "pub" else ".mark")) if w else nxt[0],
                        i if w else nxt[1],
                    ),
                )
            elif st == "mark":  # M1
                put(gset(g, fired=1), (op + ".fx", i))
            elif st == "fx":  # F6: xchg(fired, 0) after the handlers
                f = g[FIRED]
                put(gset(g, fired=0), (op + ".fw", i) if f else nxt)
            elif st == "fw":  # K1
                g2, ths2 = k1(g, ths)
                put(g2, nxt, ths2)
        elif (
            op == "kick"
        ):  # LW1, then EVENT(s, lane 0) from an application thread: E2, K1 direct
            if st == "0":
                lk = 1 if p["mode"] == "tok" else g[LK] + 1
                put(gset(g, lk=lk), nxt if "kick_no_event" in mut else ("kick.rmw", i))
            elif st == "rmw":
                w = g[CNT] > 0
                put(gset(g, seq=g[SEQ] + 1), ("kick.fw", i) if w else nxt)
            elif st == "fw":
                g2, ths2 = k1(g, ths)
                put(g2, nxt, ths2)
        elif op == "close":  # CLOSING, then E2, K1
            if st == "0":
                put(gset(g, clo=1), ("close.rmw", i))
            elif st == "rmw":
                w = g[CNT] > 0
                put(gset(g, seq=g[SEQ] + 1), ("close.fw", i) if w else nxt)
            elif st == "fw":
                g2, ths2 = k1(g, ths)
                put(g2, nxt, ths2)
        elif op == "spur":  # an EVENT with no publish: a sleeper wakes, finds nothing
            if st == "0":
                w = g[CNT] > 0
                put(gset(g, seq=g[SEQ] + 1), ("spur.fw", i) if w else nxt)
            elif st == "fw":
                g2, ths2 = k1(g, ths)
                put(g2, nxt, ths2)
        return out

    # W: a thread that runs p["gets"] legacy gets in a row (a session with mode p["mode"])
    pc, e, k0, again, tmo, outs = loc
    mode = p["mode"]

    def ret(g2, r, ths2=None):
        if (
            r == "E" and tmo
        ):  # NULL after the deadline passed: the get waited its whole timeout
            r = "T"
        if r == "F" and len(outs) in p.get("conv", ()):
            put(g2, ("CV", None, 0, 0, 0, outs), ths2)
            return
        if r == "F" and "spend_on_return" in mut and mode == "tok":
            g2 = gset(g2, lk=0)
        o2 = outs + (r,)
        put(g2, ("done" if len(o2) == p["gets"] else "T1", None, 0, 0, 0, o2), ths2)

    if pc == "done":
        return out
    if pc == "T1" and DPFIRST[0] and mode is not None:  # DP: D1 CLOSING, D2 one ATTEMPT
        if not outs and not guard_ok(p, ths, kinds, "core"):
            return out
        if g[CLO]:
            ret(g, "E")
        elif g[RR] > 0:
            g2 = gset(g, R=g[RR] - 1)
            if (
                "dp_spends" in mut and mode == "tok"
            ):  # 355fc356e: the fast path spends the token
                g2 = gset(g2, lk=0)
            ret(g2, "F")
        else:
            put(g, ("T1w", None, 0, 0, 0, outs))
    elif (
        pc == "T1w"
    ):  # WT's T1 after DP failed; EDGE: k0 = load(lkick); TOKEN: LW2 on every pass
        put(
            g,
            (
                "T2",
                None,
                g[LK] if mode == "edge" else 0,
                0 if "tok_skip_first" in mut else (1 if mode == "tok" else 0),
                0,
                outs,
            ),
        )
    elif pc == "T1":  # T1: the deadline (tmo = 0); EDGE: k0 = load(lkick)
        if not outs and not guard_ok(p, ths, kinds, "core"):
            return out
        put(g, ("T2", None, g[LK] if mode == "edge" else 0, 0, 0, outs))
    elif (
        pc == "CV"
    ):  # the in-caller conversion of a claimed frame; its hook posts the wake
        if not guard_ok({"after": p.get("conv_after", ())}, ths, kinds, "core"):
            return out
        if "spend_on_return" in mut and mode == "tok":
            g = gset(g, lk=0)
        o2 = outs + ("F",)
        put(g, ("done" if len(o2) == p["gets"] else "T1", None, 0, 0, 0, o2))
    elif (
        pc == "T2" and "edge_reload" in mut and mode == "edge" and k0 != g[LK]
    ):  # judge mutant
        put(g, ("T2", None, g[LK], again, tmo, outs))
    elif pc == "T2":  # T2: e = load(ec)
        if (
            "kick_before_t2" in mut and mode == "tok" and again == 1
        ):  # LW2 before the ec load
            if g[LK]:
                put(gset(g, lk=0), ("AT", None, k0, again, tmo, outs))
            else:
                put(g, ("T2b", None, k0, again, tmo, outs))
        else:
            put(g, ("T3", (g[SEQ], g[CNT]), k0, again, tmo, outs))
    elif pc == "T2b":
        put(g, ("T3", (g[SEQ], g[CNT]), k0, again, tmo, outs))
    elif pc == "T3":  # T3: the state's code
        if g[CLO]:
            ret(g, "E")
        elif (
            mode == "tok"
            and (again == 1 or "tok_fast_path" in mut)
            and "tok_after_t4" not in mut
            and "kick_before_t2" not in mut
        ):
            put(g, ("LW2", e, k0, again, tmo, outs))
        else:
            put(g, ("T4", e, k0, again, tmo, outs))
    elif pc in ("LW2", "LW2p"):  # LW2, TOKEN: xchg(lkick, 0)
        cont = "T4" if pc == "LW2" else "T5"
        if "tok_load_store" in mut:
            put(
                g,
                (
                    ("LWs" + pc[3:], e, k0, again, tmo, outs)
                    if g[LK]
                    else (cont, e, k0, again, tmo, outs)
                ),
            )
        elif g[LK]:
            put(
                gset(g, lk=0),
                (
                    "RET_E" if "no_final_attempt" in mut else "AT",
                    e,
                    k0,
                    again,
                    tmo,
                    outs,
                ),
            )
        else:
            put(g, (cont, e, k0, again, tmo, outs))
    elif pc in ("LWs", "LWsp"):  # mutant: the store of a load-then-store spend
        put(gset(g, lk=0), ("AT", e, k0, again, tmo, outs))
    elif pc == "RET_E":
        ret(g, "E")
    elif pc == "AT":  # the attempt after a spent token (today's "get again")
        if g[RR] > 0:
            ret(gset(g, R=g[RR] - 1), "F")
        else:
            ret(g, "E")
    elif pc == "T4":  # T4: ATTEMPT
        if mode == "edge" and "edge_before_t4" in mut and g[LK] != k0:
            ret(g, "E")
        elif g[RR] > 0:
            ret(gset(g, R=g[RR] - 1), "F")
        elif (
            mode == "tok"
            and (again == 0 or "tok_after_t4" in mut)
            and "tok_skip_first" not in mut
        ):
            put(g, ("LW2p", e, k0, again, tmo, outs))
        elif mode == "edge":
            put(g, ("LE", e, k0, again, tmo, outs))
        else:
            put(g, ("T5", e, k0, again, tmo, outs))
    elif pc == "LE":  # LW2, EDGE: load(lkick) != k0
        if g[LK] != k0 and "edge_as_token" not in mut:
            ret(g, "E")
        elif "edge_as_token" in mut and g[LK]:
            ret(gset(g, lk=0), "E")
        else:
            put(g, ("T5", e, k0, again, tmo, outs))
    elif pc == "T5":  # T5: the deadline
        if tmo:
            ret(g, "T")
        else:
            put(g, ("T7", e, k0, again, tmo, outs))
    elif pc == "T7":  # T7: CAS(ec, e, e + ONE)
        if (g[SEQ], g[CNT]) != e:
            put(g, ("T2", None, k0, 1, tmo, outs))
        else:
            put(gset(g, cnt=g[CNT] + 1), ("T8", e, k0, again, tmo, outs))
    elif pc == "T8":  # T8: futex_wait(seq, e.seq)
        if g[SEQ] == e[0]:
            put(gset(g, fq=g[FQ] | {t}), ("S", e, k0, again, tmo, outs))
        else:
            put(g, ("T9", e, k0, again, tmo, outs))
    elif pc == "T9":  # T9: fetch_sub(ec, ONE)
        put(gset(g, cnt=g[CNT] - 1), ("T2", None, k0, 1, tmo, outs))
    return out


def core_timeouts(s, kinds):
    g, ths = s
    out = []
    for t, loc in enumerate(ths):
        if kinds[t] == "W" and loc[0] == "S":
            l2 = list(ths)
            l2[t] = ("T9",) + loc[1:4] + (1,) + loc[5:]
            out.append((gset(g, fq=g[FQ] - {t}), tuple(l2)))
    return out


def core_init(kinds, par):
    g = (0, 0, 0, 0, 0, frozenset(), 0)
    g = gset(g, R=par[-1].get("R0", 0) if par and "R0" in par[-1] else 0)
    return g, tuple(
        ("T1", None, 0, 0, 0, ()) if k == "W" else ("start", 0) for k in kinds
    )


def core_state(s, t):
    return s[1][t][0]


def core_leak(s):
    return s[0][CNT] != 0


# ----------------------------------------------------------------------------- LEGACY ------
# g = (owner, condq, R, pending, cnt, destroying); owner -1 = the mutex is free
OWN, CQ, LR, PEND, LCNT, DESTR = range(6)


def lset(g, **kw):
    g = list(g)
    for k, v in kw.items():
        g[
            {"own": OWN, "cq": CQ, "R": LR, "pend": PEND, "cnt": LCNT, "destr": DESTR}[
                k
            ]
        ] = v
    return tuple(g)


def leg_succ(s, t, kinds, par, side, spurious):
    g, ths = s
    k, p, loc = kinds[t], par[t], ths[t]
    out = []

    def put(g2, loc2, ths2=None):
        ths2 = list(ths if ths2 is None else ths2)
        ths2[t] = loc2
        out.append((g2, tuple(ths2)))

    free = g[OWN] == -1

    def signal(g2, ths2, all_):
        """cond_signal wakes one waiter of the scheduler's choice; broadcast wakes all."""
        q = sorted(g2[CQ])
        if not q:
            return [(g2, ths2)]
        res = []
        groups = [q] if all_ else [[w] for w in q]
        for grp in groups:
            l2 = list(ths2)
            for w in grp:
                l2[w] = ("RQ",) + l2[w][1:]
            res.append((lset(g2, cq=g2[CQ] - frozenset(grp)), tuple(l2)))
        return res

    if k != "W":
        pc, i = loc
        prog = p["prog"]
        if pc == "done":
            return out
        if pc == "start":
            if guard_ok(p, ths, kinds, "legacy"):
                put(g, (prog[0] + ".0", 0))
            return out
        op, st = pc.split(".")
        nxt = (prog[i + 1] + ".0", i + 1) if i + 1 < len(prog) else ("done", 0)
        if op in (
            "pub",
            "pubt",
        ):  # the frame's state store, then notify: lock, signal, unlock
            if st == "0":
                put(lset(g, R=g[LR] + 1), (op + ".sig", i))
            elif st == "sig" and free:
                for g2, t2 in signal(g, ths, False):
                    put(g2, nxt, t2)
        elif (
            op == "kick" and free
        ):  # wake_block: RX token + broadcast; TX count + signal
            if side == "rx":
                for g2, t2 in signal(lset(g, pend=1), ths, True):
                    put(g2, nxt, t2)
            else:
                for g2, t2 in signal(lset(g, cnt=g[LCNT] + 1), ths, False):
                    put(g2, nxt, t2)
        elif (
            op == "close"
        ):  # lc_destroying, then the destroy hook: RX token + broadcast; TX broadcast
            if st == "0":
                put(lset(g, destr=1), ("close.sig", i))
            elif free:
                g1 = lset(g, pend=1) if side == "rx" else g
                for g2, t2 in signal(g1, ths, True):
                    put(g2, nxt, t2)
        elif op == "spur" and free:  # a notify with no frame
            for g2, t2 in signal(g, ths, False):
                put(g2, nxt, t2)
        return out

    pc, got, k0, tmo, sp, outs = loc

    def ret(g2, r):
        if r == "F" and len(outs) in p.get("conv", ()):
            put(g2, ("CV", 0, 0, 0, sp, outs))
            return
        o2 = outs + (r,)
        put(g2, ("done" if len(o2) == p["gets"] else "G0", 0, 0, 0, sp, o2))

    def null():
        return "T" if tmo else "E"

    if pc == "done":
        return out
    if pc == "G0":  # the fast-path claim
        if not outs and not guard_ok(p, ths, kinds, "legacy"):
            return out
        if g[LR] > 0:
            ret(lset(g, R=g[LR] - 1), "F")
        else:
            put(g, ("L", 0, 0, 0, sp, outs))
    elif pc == "CV":
        if not guard_ok({"after": p.get("conv_after", ())}, ths, kinds, "legacy"):
            return out
        o2 = outs + ("F",)
        put(g, ("done" if len(o2) == p["gets"] else "G0", 0, 0, 0, sp, o2))
    elif pc == "L":  # lock (TX: and the snapshot of wake_block_cnt)
        if free:
            put(lset(g, own=t), ("LT", 0, g[LCNT], 0, sp, outs))
    elif pc == "RQ":  # woken: reacquire the mutex, back to the loop top
        if free:
            put(lset(g, own=t), ("LT", got, k0, tmo, sp, outs))
    elif pc == "RQT":  # timed out: reacquire, leave the loop
        if free:
            put(lset(g, own=t), ("AFT", got, k0, tmo, sp, outs))
    elif (
        pc == "CW"
    ):  # in cond_timedwait; a spurious wakeup (POSIX) if the case allows one
        if spurious and sp > 0:
            put(lset(g, cq=g[CQ] - {t}), ("RQ", got, k0, tmo, sp - 1, outs))
    elif side == "rx":
        if pc == "LT":  # while (!pending && !destroying)
            if not g[PEND] and not g[DESTR]:
                put(g, ("LC", got, k0, tmo, sp, outs))
            else:
                put(g, ("AFT", got, k0, tmo, sp, outs))
        elif pc == "LC":  # claim; if frame break; timedwait
            if g[LR] > 0:
                put(lset(g, R=g[LR] - 1), ("AFT", 1, k0, tmo, sp, outs))
            else:
                put(lset(g, own=-1, cq=g[CQ] | {t}), ("CW", got, k0, tmo, sp, outs))
        elif pc == "AFT":  # pending = false; unlock
            put(lset(g, own=-1, pend=0), ("AF2", got, k0, tmo, sp, outs))
        elif pc == "AF2":
            if got:
                ret(g, "F")
            elif g[DESTR]:
                ret(g, "E")
            else:  # judge: the destroying check and the get-again claim are two steps
                put(g, ("AF3", got, k0, tmo, sp, outs))
        elif pc == "AF3":
            if g[LR] > 0:  # get again
                ret(lset(g, R=g[LR] - 1), "F")
            else:
                ret(g, null())
    else:
        if (
            pc == "LT"
        ):  # while (!destroying) { claim; if frame break; if cnt moved break; wait }
            if g[DESTR]:
                put(g, ("AFT", got, k0, tmo, sp, outs))
            elif g[LR] > 0:
                put(lset(g, R=g[LR] - 1), ("AFT", 1, k0, tmo, sp, outs))
            elif g[LCNT] != k0:
                put(g, ("AFT", got, k0, tmo, sp, outs))
            else:
                put(lset(g, own=-1, cq=g[CQ] | {t}), ("CW", got, k0, tmo, sp, outs))
        elif pc == "AFT":
            put(lset(g, own=-1), ("AF2", got, k0, tmo, sp, outs))
        elif pc == "AF2":
            if g[DESTR]:  # a frame claimed in the wait goes back to FREE
                ret(lset(g, R=g[LR] + got), "E")
            elif got:
                ret(g, "F")
            else:
                ret(g, null())
    return out


def leg_timeouts(s, kinds):
    g, ths = s
    out = []
    for t, loc in enumerate(ths):
        if kinds[t] == "W" and loc[0] == "CW":
            l2 = list(ths)
            l2[t] = ("RQT", loc[1], loc[2], 1, loc[4], loc[5])
            out.append((lset(g, cq=g[CQ] - {t}), tuple(l2)))
    return out


def leg_init(kinds, par):
    r0 = par[-1].get("R0", 0) if par and "R0" in par[-1] else 0
    g = (-1, frozenset(), r0, 0, 0, 0)
    return g, tuple(("G0", 0, 0, 0, 1, ()) if k == "W" else ("start", 0) for k in kinds)


# ----------------------------------------------------------------------------- shared ------
BLOCKED = {"core": ("S",), "legacy": ("CW",)}


def guard_ok(p, ths, kinds, which):
    """A thread's start condition: after(t) = thread t done; blocked(ts) = every t asleep in its
    wait; conv(t) = t converting a frame it claimed."""
    for kind, ts in p.get("after", ()):
        if kind == "anydone":
            if not any(ths[u][0] == "done" for u in ts):
                return False
            continue
        for u in ts:
            st = ths[u][0]
            if kind == "done" and st != "done":
                return False
            if kind == "blocked" and st not in BLOCKED[which]:
                return False
            if kind == "conv" and st != "CV":
                return False
    return True


def explore(case, which, mut=(), spurious=False, trace=False):
    spec = case["threads"]
    kinds = [x[0] for x in spec]
    par = [dict(x[1]) for x in spec]
    for q in par:
        q.setdefault("mode", case["mode"])
    par.append({"R0": case.get("R0", 0)})
    if which == "core":
        s0 = core_init(kinds, par)

        def succ(s, t):
            return core_succ(s, t, kinds, par, mut)

        def tmo(s):
            return core_timeouts(s, kinds)

        leak = core_leak
    else:
        s0 = leg_init(kinds, par)
        side = case["side"]

        def succ(s, t):
            return leg_succ(s, t, kinds, par, side, spurious)

        def tmo(s):
            return leg_timeouts(s, kinds)

        def leak(s):
            return False

    par.pop()
    seen = {s0: None}
    stack = [s0]
    results, bad = set(), set()
    while stack:
        s = stack.pop()
        nxt = [s2 for t in range(len(kinds)) for s2 in succ(s, t)]
        if ANYDL[0]:
            nxt = nxt + tmo(
                s
            )  # --any-deadline: a sleeper's deadline may pass at any step
        elif not nxt:
            nxt = tmo(s)  # a deadline passes only when nothing else can run
        if not nxt:
            g, ths = s

            def unstarted(t_, loc):
                return kinds[t_] == "W" and loc[0] in ("T1", "G0") and loc[-1] == ()

            if any(
                loc[0] != "done"
                for t_, loc in enumerate(ths)
                if not ANYDL[0] or (kinds[t_] == "W" and not unstarted(t_, loc))
            ):
                bad.add("STUCK")
            else:
                if leak(s):
                    bad.add("LEAK")
                r = tuple(ths[t][-1] for t in range(len(kinds)) if kinds[t] == "W")
                if ANYDL[0] and any(loc[0] != "done" for loc in ths):
                    continue  # a guarded thread that never started: not a result
                if trace and r not in results:
                    path, x = [], s
                    while x is not None:
                        path.append(x)
                        x = seen[x]
                    print("  result", r)
                    for y in reversed(path):
                        print("     ", y)
                results.add(r)
            continue
        for s2 in nxt:
            if s2 not in seen:
                seen[s2] = s
                stack.append(s2)
    return len(seen), results, bad


# ------------------------------------------------------------------------------ cases ------
def W(gets=1, **kw):
    return ("W", dict(gets=gets, **kw))


def X(
    *prog, **kw
):  # a non-waiting thread: pub (direct producer), pubt (tasklet), kick, close, spur
    return ("X", dict(prog=list(prog), **kw))


def B(*ts):
    return ("blocked", ts)


def D(*ts):
    return ("done", ts)


def C(*ts):
    return ("conv", ts)


def A(*ts):
    return ("anydone", ts)


# name: (case, the property on CORE's results, what it says).  A result is the tuple of every
# W thread's results, in thread order; each W's results are the tuple of its gets'.
CASES = {
    # RX, ST_CORE_LEGACY_WAKE_TOKEN
    "WakeBlockEndsTheWait": (
        dict(side="rx", mode="tok", threads=[W(), X("kick", after=[B(0)])]),
        lambda r: r == {(("E",),)},
        "the blocked get returns NULL early",
    ),
    "WakePostedBeforeWaitIsNotLost": (
        dict(side="rx", mode="tok", threads=[W(after=[D(1)]), X("kick")]),
        lambda r: r == {(("E",),)},
        "a wake posted before the get ends it",
    ),
    "OneWakeSatisfiesOnlyOneWait": (
        dict(side="rx", mode="tok", threads=[W(2, after=[D(1)]), X("kick")]),
        lambda r: r == {(("E", "T"),)},
        "the second get waits its whole timeout",
    ),
    "OneWakeEndsOneOfTwoBlockedGets": (
        dict(side="rx", mode="tok", threads=[W(), W(), X("kick", after=[B(0, 1)])]),
        lambda r: r == {(("E",), ("T",)), (("T",), ("E",))},
        "exactly one of two blocked gets ends",
    ),
    "FastPathGetDoesNotStealTheWakeOfABlockedGet": (
        dict(
            side="rx",
            mode="tok",
            threads=[
                W(),
                X("pubt", after=[B(0)]),
                X("kick", after=[D(1)]),
                W(after=[D(2)]),
            ],
        ),
        lambda r: all(x[0][0] != "T" for x in r),
        "B never waits to the timeout",
    ),
    "WakePostedDuringAConversionEndsTheNextGet": (
        dict(
            side="rx",
            mode="tok",
            R0=1,
            threads=[W(2, conv=(0,), conv_after=[D(1)]), X("kick", after=[C(0)])],
        ),
        lambda r: r == {(("F", "E"),)},
        "the next get ends at once",
    ),
    "StaleFrameWakeDoesNotSkipTheWait": (
        dict(side="rx", mode="tok", threads=[W(2, after=[D(1)]), X("pubt")]),
        lambda r: r == {(("F", "T"),)},
        "the empty get after a fast-path get waits",
    ),
    "FrameReadyWakesABlockedGet": (
        dict(side="rx", mode="tok", threads=[W(), X("pubt", after=[B(0)])]),
        lambda r: r == {(("F",),)},
        "the frame is returned",
    ),
    "RepeatedNotifiesDoNotEndTheWait": (
        dict(side="rx", mode="tok", threads=[W(), X("spur", "spur", after=[B(0)])]),
        lambda r: r == {(("T",),)},
        "notifies without a frame do not end the wait",
    ),
    "DestroyEndsEveryBlockedGet": (
        dict(side="rx", mode="tok", threads=[W(), W(), X("close", after=[B(0, 1)])]),
        lambda r: r == {(("E",), ("E",))},
        "both gets return NULL early",
    ),
    "DestroyEndsTheWaitWithoutAFrame": (
        dict(side="rx", mode="tok", threads=[W(), X("close", "pub", after=[B(0)])]),
        lambda r: r == {(("E",),)},
        "teardown hands out no frame",
    ),
    "RxFrameReadyTogetherWithTheWake": (
        dict(side="rx", mode="tok", threads=[W(2), X("pub", "kick", after=[B(0)])]),
        lambda r: all(x[0][0] == "F" for x in r),
        "the frame is returned, not NULL",
    ),
    "RxWakeThenFrame": (
        dict(side="rx", mode="tok", threads=[W(2), X("kick", "pub", after=[B(0)])]),
        lambda r: True,
        "only the subset of today's results",
    ),
    "RxWakeRacingTheCall": (
        dict(side="rx", mode="tok", threads=[W(), X("kick")]),
        lambda r: r == {(("E",),)},
        "a wake concurrent with the call is never lost",
    ),
    "RxWakeRacingASpuriousWake": (
        dict(side="rx", mode="tok", threads=[W(), X("spur"), X("kick")]),
        lambda r: r == {(("E",),)},
        "a wake beside another EVENT is never lost",
    ),
    "RxTwoWakesTwoBlockedGets": (
        dict(
            side="rx",
            mode="tok",
            threads=[W(), W(), X("kick", "kick", after=[B(0, 1)])],
        ),
        lambda r: (("E",), ("E",)) in r,
        "two wakes can end both gets (the token is a bool)",
    ),
    "RxThreeBlockedGetsOneWake": (
        dict(
            side="rx",
            mode="tok",
            threads=[W(), W(), W(), X("kick", after=[B(0, 1, 2)])],
        ),
        lambda r: all(sum(x == ("E",) for x in y) == 1 for y in r),
        "exactly one of three ends",
    ),
    "RxWakeRacingClose": (
        dict(side="rx", mode="tok", threads=[W(), X("kick"), X("close")]),
        lambda r: r == {(("E",),)},
        "NULL early either way",
    ),
    # TX, ST_CORE_LEGACY_WAKE_EDGE
    "St20pWakeEndsBlockedGetFrame": (
        dict(side="tx", mode="edge", threads=[W(), X("kick", after=[B(0)])]),
        lambda r: r == {(("E",),)},
        "the blocked get returns NULL early",
    ),
    "TxWakeWithNoWaiterDoesNothing": (
        dict(side="tx", mode="edge", threads=[W(after=[D(1)]), X("kick")]),
        lambda r: r == {(("T",),)},
        "a later empty get waits its whole timeout",
    ),
    "TxFrameReadyTogetherWithTheWake": (
        dict(side="tx", mode="edge", threads=[W(), X("pub", "kick", after=[B(0)])]),
        lambda r: r == {(("F",),)},
        "the freed frame is returned, not NULL",
    ),
    "StaleWakeFromEarlierFastPathDoesNotShortCircuitBlock": (
        dict(side="tx", mode="edge", R0=1, threads=[W(2, after=[D(1)]), X("spur")]),
        lambda r: r == {(("F", "T"),)},
        "the empty get waits",
    ),
    "BlockedGetFrameWakesPromptlyOnGenuineFree": (
        dict(side="tx", mode="edge", threads=[W(), X("pubt", after=[B(0)])]),
        lambda r: r == {(("F",),)},
        "the freed frame is returned",
    ),
    "TxDestroyEndsEveryBlockedGet": (
        dict(side="tx", mode="edge", threads=[W(), W(), X("close", after=[B(0, 1)])]),
        lambda r: r == {(("E",), ("E",))},
        "both gets return NULL early",
    ),
    "TxWakeRacingTheCall": (
        dict(side="tx", mode="edge", threads=[W(), X("kick")]),
        lambda r: r <= {(("E",),), (("T",),)},
        "either result; a subset of today's",
    ),
    "TxFreeThenClose": (
        dict(side="tx", mode="edge", threads=[W(), X("pub", "close", after=[B(0)])]),
        lambda r: r <= {(("F",),), (("E",),)},
        "a frame or NULL, never the timeout",
    ),
    "TxWakeRacingClose": (
        dict(side="tx", mode="edge", threads=[W(), X("kick"), X("close")]),
        lambda r: r == {(("E",),)},
        "NULL early either way",
    ),
    "TxTwoBlockedGetsOneWake": (
        dict(side="tx", mode="edge", threads=[W(), W(), X("kick", after=[B(0, 1)])]),
        lambda r: r == {(("E",), ("E",))},
        "every get blocked at the wake ends (see the report)",
    ),
    # the judge's cases
    "J1RxTwoWakesBeforeTheGets": (
        dict(side="rx", mode="tok", threads=[W(2, after=[D(1)]), X("kick", "kick")]),
        lambda r: r == {(("E", "T"),)},
        "the token is a bool: two wakes, one wait",
    ),
    "J2TxArmedWaiterThenANotify": (
        dict(
            side="tx",
            mode="edge",
            threads=[W(), W(), X("kick", after=[B(0, 1)]), X("spur", after=[A(0, 1)])],
        ),
        lambda r: all("T" not in (x[0] + x[1]) for x in r),
        "today: the other get leaves at the notify",
    ),
    "J3TxArmedWaiterThenAStolenFrame": (
        dict(
            side="tx",
            mode="edge",
            threads=[
                W(),
                W(),
                X("kick", after=[B(0, 1)]),
                X("pub", after=[A(0, 1)]),
                W(after=[D(3)]),
            ],
        ),
        lambda r: True,
        "only the subset of today's results",
    ),
    "J4RxThreeBlockedTwoWakes": (
        dict(
            side="rx",
            mode="tok",
            threads=[W(), W(), W(), X("kick", "kick", after=[B(0, 1, 2)])],
        ),
        lambda r: all(1 <= sum(x == ("E",) for x in y) <= 2 for y in r),
        "one or two of three end",
    ),
    "J5RxFrameAndWakeBesideAFastPathGet": (
        dict(
            side="rx",
            mode="tok",
            threads=[W(), X("pub", "kick", after=[B(0)]), W(after=[D(1)])],
        ),
        lambda r: True,
        "only the subset of today's results (a get that claims in the loop clears the token today)",
    ),
    "J6TxWakeDuringTheFirstGet": (
        dict(side="tx", mode="edge", R0=1, threads=[W(2), X("kick")]),
        lambda r: r <= {(("F", "T"),), (("F", "E"),)},
        "a subset of today's",
    ),
    "J7RxWakeDuringTheFirstGet": (
        dict(side="rx", mode="tok", R0=1, threads=[W(2), X("kick")]),
        lambda r: r == {(("F", "E"),)},
        "the wake ends the second get",
    ),
    "J8RxCloseThenWake": (
        dict(
            side="rx",
            mode="tok",
            threads=[W(), W(), X("close", "kick", after=[B(0, 1)])],
        ),
        lambda r: r == {(("E",), ("E",))},
        "both NULL early",
    ),
    "J9TxThreeBlockedOneWakeOneFrame": (
        dict(
            side="tx",
            mode="edge",
            threads=[W(), W(), W(), X("kick", "pub", after=[B(0, 1, 2)])],
        ),
        lambda r: True,
        "only the subset of today's results",
    ),
    "J10RxTwoBlockedWakeAndFrame": (
        dict(
            side="rx",
            mode="tok",
            threads=[W(), W(), X("kick", after=[B(0, 1)]), X("pub", after=[A(0, 1)])],
        ),
        lambda r: True,
        "only the subset of today's results",
    ),
    "J11TxWakeThenGetAfterTheWaiterLeft": (
        dict(
            side="tx",
            mode="edge",
            threads=[W(), X("kick", after=[B(0)]), W(after=[D(0)])],
        ),
        lambda r: r == {(("E",), ("T",))},
        "the later get is not ended by the earlier wake",
    ),
    # unified sessions: no mode, LW2 never runs, lkick has no effect
    "UnifiedIgnoresLkick": (
        dict(side="rx", mode=None, threads=[W(), X("kick", after=[B(0)])]),
        lambda r: r == {(("T",),)},
        "a session without a mode sleeps on to its deadline",
    ),
}
STRICT_EXEMPT = {
    "TxTwoBlockedGetsOneWake",
    "UnifiedIgnoresLkick",
    "J9TxThreeBlockedOneWakeOneFrame",
}

MUTANTS = {  # name: (the cases that must break, what the mutant does)
    "tok_fast_path": (
        ["FastPathGetDoesNotStealTheWakeOfABlockedGet"],
        "LW2 also before the first T4: a fast-path get spends the token",
    ),
    "spend_on_return": (
        [
            "FastPathGetDoesNotStealTheWakeOfABlockedGet",
            "WakePostedDuringAConversionEndsTheNextGet",
        ],
        "a get that returns a frame clears the token (355fc356e, 2e1c23738)",
    ),
    "tok_load_store": (
        ["OneWakeEndsOneOfTwoBlockedGets"],
        "the spend is a load, then a store of 0 (no xchg)",
    ),
    "kick_no_event": (
        ["WakeBlockEndsTheWait", "St20pWakeEndsBlockedGetFrame"],
        "LW1 without EVENT(s, lane 0)",
    ),
    "kick_before_t2": (
        ["RxWakeRacingASpuriousWake"],
        "LW2 reads lkick before T2's load of ec",
    ),
    "no_final_attempt": (
        ["RxFrameReadyTogetherWithTheWake"],
        "a spent token returns NULL without the attempt",
    ),
    "tok_after_t4": (
        ["RxWakeThenFrame"],
        "RX LW2 after T4 on later passes: a frame claimed beside a token keeps it",
    ),
    "edge_before_t4": (
        ["TxFrameReadyTogetherWithTheWake"],
        "TX LW2 before T4: NULL although the frame is free",
    ),
    "edge_as_token": (
        ["TxWakeWithNoWaiterDoesNothing"],
        "TX keeps a token: a wake with no waiter ends a later get",
    ),
    "notify_sets_token": (
        ["StaleFrameWakeDoesNotSkipTheWait"],
        "a frame's EVENT leaves a token (the defect 1a89339c4 fixed)",
    ),
    "edge_reload": (
        ["St20pWakeEndsBlockedGetFrame"],
        "judge: EDGE re-takes k0 on every pass (a snapshot per sleep): a wake while asleep is lost",
    ),
    "tok_skip_first": (
        ["WakePostedBeforeWaitIsNotLost"],
        "judge, DP shape: no LW2 on WT's first pass after DP: a wake posted before the get is lost",
    ),
    "dp_spends": (
        ["FastPathGetDoesNotStealTheWakeOfABlockedGet"],
        "DP shape only: DP's successful attempt clears the token (355fc356e)",
    ),
}


def check_case(name, mut=(), quiet=False):
    case, prop, what = CASES[name]
    n, res, bad = explore(case, "core", mut)
    _, leg, lbad = explore(case, "legacy")
    _, legs, _ = explore(case, "legacy", spurious=True)
    probs = sorted(bad)
    if not prop(res):
        probs.append("PROPERTY")
    if case["mode"] is not None and not res <= leg and name not in STRICT_EXEMPT:
        probs.append("NOT_IN_TODAY")
    if case["mode"] is not None and not res <= legs:
        probs.append("NOT_IN_TODAY_POSIX")
    if case["mode"] is not None and name not in STRICT_EXEMPT and not prop(leg):
        probs.append(
            "TODAY_FAILS_THE_PIN"
        )  # the legacy model must pass what main's tests pin
    if not quiet:
        if case["mode"] is None:
            leg = set()
        extra = ""
        if leg and not res <= leg:
            extra = "  extra vs today (no spurious wakeups): %s" % sorted(res - leg)
        print(
            "%-52s %6d states  core %-26s today %-26s %s%s"
            % (
                name,
                n,
                fmt(res),
                fmt(leg),
                "ok" if not probs else "FAIL " + ",".join(probs),
                extra,
            )
        )
    return probs, res, leg, legs


def fmt(res):
    return "|".join(
        (
            "".join("".join(g) for g in r)
            if len(r) == 1
            else ",".join("".join(g) for g in r)
        )
        for r in sorted(res)
    )


def main(argv):
    if argv[:1] == [
        "--any-deadline"
    ]:  # safety only: LEAK, STUCK, results among today's
        ANYDL[0] = True
        argv = argv[1:]
        fail = 0
        for name, (case, prop, what) in CASES.items():
            n, res, bad = explore(case, "core", ())
            _, legs, _ = explore(case, "legacy", spurious=True)
            probs = sorted(bad)
            if case["mode"] is not None and not res <= legs:
                probs.append("NOT_IN_TODAY_POSIX %s" % sorted(res - legs))
            fail |= bool(probs)
            print(
                "%-52s %6d states  %s"
                % (name, n, "ok" if not probs else "FAIL " + ",".join(probs))
            )
        print("any-deadline", "PASS" if not fail else "FAIL")
        return 1 if fail else 0
    if argv[:1] == ["--wt-only"]:
        DPFIRST[0] = False
        argv = argv[1:]
    if argv[:1] == ["--trace"]:
        explore(CASES[argv[1]][0], "core", tuple(argv[2:]), trace=True)
        return 0
    t0 = time.time()
    fail = 0
    print(
        "results: F a frame, E NULL early, T NULL at the timeout; one letter per get, ',' between threads"
    )
    for name in CASES:
        probs, *_ = check_case(name)
        fail |= bool(probs)
    for m, (cases, what) in MUTANTS.items():
        if (m in ("dp_spends", "tok_skip_first")) != DPFIRST[0] and m in (
            "dp_spends",
            "tok_fast_path",
            "tok_skip_first",
        ):
            print("%-18s %-70s %s" % (m, what, "n/a in this shape"))
            continue
        hits = []
        for c in cases:
            probs, res, leg, _ = check_case(c, (m,), quiet=True)
            if probs:
                hits.append("%s:%s(core %s)" % (c, ",".join(probs), fmt(res)))
        fail |= len(hits) < len(cases)
        print(
            "%-18s %-70s %s"
            % (m, what, "killed by " + " ".join(hits) if hits else "NOT KILLED")
        )
    print("%.1f s, %s" % (time.time() - t0, "PASS" if not fail else "FAIL"))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
