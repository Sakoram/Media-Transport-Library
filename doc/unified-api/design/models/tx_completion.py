#!/usr/bin/env python3
"""TX frame completion on main's tv_frame_free_cb (task E1a, D-248), one sh_info per frame.

Standard library only; run with `python3 -I tx_completion.py`. Exhaustive DFS over interleavings,
sequential consistency, in the style of final_model.py.

The design (D-248, engine.md §2.2, §5): E1a = MF1's claim (a refcnt CAS 1 -> 2 in
tv_frame_free_cb and in legacy recovery's frame loop, then a subtraction of 2), MF7 (the
builder's sh_info reference from pick to build end), the recovery verdict for core sessions
(verdict hook, no publish, only software-held references dropped, the builder's MF7 reference
among them), one sh_info per frame; the binding
names the unit by frame_idx through its own record written at its pick (MS1: frame == slot), reads
main's done_* meta, and claims with the completion CAS.

What it covers beyond one frame and an atomic recovery:
  * two frames (= two slots) and three units, so the application reaps and re-submits one slot
    while the other frame is still in flight;
  * recovery is NOT atomic: it holds the scheduler token S (the builder and the transmitter wait),
    but every step of it (one verdict per frame, each ring drop, the other leg's pad flush, the
    builder reset) interleaves with foreign free callbacks (TSQ, E1b's cleanup) and the app;
  * two legs (ST 2022-7): the P queue hangs or not, the R leg's descriptors are freed by the pad
    flush recovery pushes (st_tx_video_session.c:4303-4307) or later;
  * the verdict is the core's CAS on ENGINE|gen, named through the binding's record of the frame;
  * a legacy teardown in the middle of a frame (tv_uinit_hw's pad flush) with MF7;
  * the MS2b shape (rebind): an engine frame bound to a different slot at each pick, where
    refcnt 0 comes before the binding's notify, so the binding must gate the re-pick itself.

Checks: every unit completes at most once, with its own meta; DROPPED exactly when its verdict was
recorded, and every unit recovery took packets from (ring drop, builder reset) is DROPPED; no frame
is picked while its sh_info counts or while a callback of its previous use has not published; at
the end every unit without a hung descriptor completed and the app got all units back.
"""

import sys

F = 2  # frames == slots (OI-59 (b))


def freeze(d):
    return tuple(sorted(d.items()))


def tset(t, i, v):
    t = list(t)
    t[i] = v
    return tuple(t)


def cp(s, **kw):
    n = dict(s)
    n.update(kw)
    return n


def explore(init, succ, check, final, limit=3_000_000):
    start = freeze(init)
    parent = {start: None}
    stack = [start]
    while stack:
        k = stack.pop()
        s = dict(k)
        e = check(s)
        if e:
            return e, tr(parent, k), len(parent)
        moves = list(succ(s))
        if not moves:
            e = final(s)
            if e:
                return e, tr(parent, k), len(parent)
        for lab, n in moves:
            nk = freeze(n)
            if nk not in parent:
                parent[nk] = (k, lab)
                stack.append(nk)
                if len(parent) > limit:
                    raise RuntimeError("state limit")
    return None, None, len(parent)


def tr(parent, k):
    out = []
    while parent[k] is not None:
        k, lab = parent[k]
        out.append(lab)
    return list(reversed(out))


DEFAULT = dict(
    mode="core",  # "core" (the binding) or "legacy" (notify names frame_idx, app re-arms)
    units=3,
    legs=1,
    recovery=True,
    verdict="first",  # "first": step 1 before every drop; "late": where today's loop is
    verdict_cas=True,  # the verdict holds only on ENGINE|gen; False: a plain store
    drop_bref=True,  # recovery drops the builder's MF7 reference when it resets the builder
    claim=True,  # MF1's refcnt CAS in the callback and legacy recovery
    core_name="record",  # "record": the binding's record of the frame; "slotword": the live gen
    core_meta="done",  # "done": main's snapshot; "live": frame->tv_meta
    teardown=False,  # legacy: destroy at any point (tv_uinit_hw's pad flush frees all)
    teardown_drop=True,  # teardown drops the builder's MF7 reference
    rebind=False,  # MS2b shape: a unit's slot is its own and the binding binds any free
    # frame to it at the pick (mtl_tx_send_slot, attached memory)
    gate="refcnt",  # with rebind: "refcnt" hands a frame out at refcnt 0; "record" only
    # once the binding's notify of the frame's previous use has returned
    early_check=True,  # False: report what an early re-pick breaks instead of the re-pick
)


def init(cfg):
    return dict(
        slot=tuple(("F", 0) for _ in range(nslots(cfg))),
        verdict=(0,) * nslots(cfg),
        sub=1,
        reap=1,
        pickc=1,
        bbusy=(0,) * F,
        refcnt=(0,) * F,
        sh=(0,) * F,
        live=(0,) * F,
        done=(0,) * F,
        rec=(0,) * F,
        bref=(0,) * F,
        b=("idle",),
        mb=(),
        cbs=(),
        res=(),
        gap=(),
        early="",
        rstate="no" if cfg["recovery"] else "off",
        hung=0,
        td="no" if cfg["teardown"] else "off",
    )


def fr(g):
    return (g - 1) % F


def nslots(cfg):
    return cfg["units"] if cfg["rebind"] else F


def sl(cfg, g):
    """The slot of unit g: the frame's in MS1 (OI-59 (b)), its own with rebind."""
    return g - 1 if cfg["rebind"] else fr(g)


def drop(s, f, n=1):
    sh = tset(s["sh"], f, s["sh"][f] - n)
    s = cp(s, sh=sh)
    if sh[f] == 0:
        s = cp(s, cbs=s["cbs"] + ((f, 0, 0),))
    return s


def s_free(s):
    """The scheduler token: recovery holds it from its first to its last step."""
    return s["rstate"] in ("no", "off", "done")


def succ(s, cfg):
    legacy = cfg["mode"] == "legacy"
    # application: submit the next unit when its slot is free (acquire + submit)
    g = s["sub"]
    if g <= cfg["units"] and s["slot"][sl(cfg, g)][0] == "F" and s["td"] != "done":
        yield "app submit u%d" % g, cp(
            s, slot=tset(s["slot"], sl(cfg, g), ("Q", g)), sub=g + 1
        )
    # application: reap in order
    g = s["reap"]
    if g <= cfg["units"] and s["slot"][sl(cfg, g)] == ("P", g):
        yield "app reap u%d" % g, cp(
            s, slot=tset(s["slot"], sl(cfg, g), ("F", g)), reap=g + 1
        )
    b = s["b"]
    if s_free(s) and s["td"] != "done":
        # builder pick: the binding hands out the entry at the pick cursor if QUEUED
        g = s["pickc"]
        frames = [fr(g)]
        if cfg["rebind"]:
            frames = [
                x for x in range(F) if cfg["gate"] == "refcnt" or not s["bbusy"][x]
            ]
        ready = (
            b == ("idle",) and g <= cfg["units"] and s["slot"][sl(cfg, g)] == ("Q", g)
        )
        for f in frames if ready else ():
            if s["refcnt"][f] != 0:
                continue
            early = s["early"]
            if s["sh"][f] != 0 or any(
                mf == f and st in "RNH" for (mf, _, _, st) in s["mb"]
            ):
                early = (
                    early
                    or "u%d picked while a reference of the frame's previous use is live"
                    % g
                )
            if any(cf == f and pc >= 3 for (cf, pc, _) in s["cbs"]):
                early = (
                    early
                    or "u%d picked before the binding published the frame's previous use"
                    % g
                )
            n = cp(
                s,
                slot=tset(s["slot"], sl(cfg, g), ("E", g)),
                rec=tset(s["rec"], f, g),
                bbusy=tset(s["bbusy"], f, 1),
                live=tset(s["live"], f, g),
                refcnt=tset(s["refcnt"], f, 1),
                sh=tset(s["sh"], f, s["sh"][f] + 1),
                bref=tset(s["bref"], f, 1),
                b=("build", f, g, 0),
                pickc=g + 1,
                early=early,
            )
            yield "pick u%d on f%d" % (g, f), n
        if b[0] == "build":
            _, f, g, i = b
            if i < cfg["legs"]:
                yield "attach u%d leg%d" % (g, i), cp(
                    s,
                    mb=s["mb"] + ((f, g, i, "R"),),
                    sh=tset(s["sh"], f, s["sh"][f] + 1),
                    b=("build", f, g, i + 1),
                )
            else:
                n = drop(cp(s, b=("idle",), bref=tset(s["bref"], f, 0)), f)
                yield "build end u%d" % g, n
        for j, (f, g, leg, st) in enumerate(s["mb"]):
            if st == "R":
                yield "burst u%d leg%d" % (g, leg), cp(
                    s, mb=tset(s["mb"], j, (f, g, leg, "N"))
                )
    # NIC completion freed by any context (own transmitter, TSQ foreign scheduler, E1b cleanup)
    for j, (f, g, leg, st) in enumerate(s["mb"]):
        if st == "N":
            yield "free u%d leg%d" % (g, leg), drop(
                cp(s, mb=tset(s["mb"], j, (f, g, leg, "F"))), f
            )
    # free callbacks: main's tv_frame_free_cb with MF1's claim, then the binding
    for ci, (f, pc, named) in enumerate(s["cbs"]):

        def upd(n, pc2, nm=named, _ci=ci, _f=f):
            if pc2 is None:
                return cp(n, cbs=n["cbs"][:_ci] + n["cbs"][_ci + 1 :])
            return cp(n, cbs=tset(n["cbs"], _ci, (_f, pc2, nm)))

        rc = s["refcnt"][f]
        if pc == 0:
            if cfg["claim"]:
                if rc == 1:
                    yield "cb f%d claim 1->2" % f, upd(
                        cp(s, refcnt=tset(s["refcnt"], f, 2)), 1
                    )
                else:
                    yield "cb f%d claim fails (%d)" % (f, rc), upd(s, None)
            else:
                if rc == 1:
                    yield "cb f%d refcnt==1" % f, upd(s, 1)
                else:
                    yield "cb f%d refcnt!=1 return" % f, upd(s, None)
        elif pc == 1:
            yield "cb f%d snapshot" % f, upd(
                cp(s, done=tset(s["done"], f, s["live"][f])), 2
            )
        elif pc == 2:
            n = cp(s, refcnt=tset(s["refcnt"], f, rc - (2 if cfg["claim"] else 1)))
            yield "cb f%d release" % f, upd(n, 3)
        elif legacy:
            if (
                pc == 3
            ):  # legacy notify: the app names frame_idx; the unit is the frame's use
                g = s["done"][f]
                n = cp(s, res=s["res"] + ((g, s["done"][f], "OK"),))
                if s["slot"][f] in (("E", g), ("C", g)):
                    n = cp(n, slot=tset(n["slot"], f, ("P", g)))
                yield "cb f%d notify u%d" % (f, g), upd(n, None)
        else:
            if pc == 3:  # binding names the unit
                g = s["rec"][f] if cfg["core_name"] == "record" else s["slot"][f][1]
                yield "cb f%d bind names u%d" % (f, g), upd(s, 4, g)
            elif pc == 4:  # completion CAS
                if s["slot"][sl(cfg, named)] == ("E", named):
                    yield "cb f%d claim u%d" % (f, named), upd(
                        cp(s, slot=tset(s["slot"], sl(cfg, named), ("C", named))), 5
                    )
                else:
                    yield "cb f%d claim u%d lost" % (f, named), upd(s, None)
            elif pc == 5:  # result
                meta = s["done"][f] if cfg["core_meta"] == "done" else s["live"][f]
                st = "DROPPED" if s["verdict"][sl(cfg, named)] in (named, -1) else "OK"
                yield "cb f%d result u%d m%d %s" % (f, named, meta, st), upd(
                    cp(s, res=s["res"] + ((named, meta, st),)), 6
                )
            elif pc == 6:
                yield "cb f%d publish u%d" % (f, named), upd(
                    cp(
                        s,
                        slot=tset(s["slot"], sl(cfg, named), ("P", named)),
                        bbusy=tset(s["bbusy"], f, 0),
                    ),
                    None,
                )
    # recovery: once, on the transmitter (holds S), every step interleaving with the rest
    rs = s["rstate"]
    if rs == "no" and s_free(s) and s["td"] != "done":
        for hung in (0, 1):
            first = "v" if cfg["verdict"] == "first" else "d"
            yield "R start%s" % (" (P queue hung)" if hung else ""), cp(
                s, rstate=(first, 0), hung=hung
            )
    if isinstance(rs, tuple):
        yield from rec_step(s, cfg, rs)
    # legacy teardown: tv_uinit_hw pushes pads until every descriptor is freed
    if s["td"] == "no" and s_free(s):
        n = cp(s, td="done")
        if b[0] == "build":
            _, f, g, i = b
            n = cp(n, b=("idle",))
            if cfg["teardown_drop"] and n["bref"][f]:
                n = drop(cp(n, bref=tset(n["bref"], f, 0)), f)
        for j, (f, g, leg, st) in enumerate(n["mb"]):
            if st in "RN":
                n = drop(cp(n, mb=tset(n["mb"], j, (f, g, leg, "F"))), f)
        yield "teardown", n


def rec_step(s, cfg, rs):
    legacy = cfg["mode"] == "legacy"
    order = ["v", "d", "x", "b"] if cfg["verdict"] == "first" else ["d", "x", "b", "v"]
    phase, i = rs[0], rs[1]
    nxt = (
        order[order.index(phase) + 1] if order.index(phase) + 1 < len(order) else "done"
    )

    def adv(n, label):
        return label, cp(n, rstate=(nxt, 0) if nxt != "done" else "done")

    if phase == "v":  # step 1 (core) / the frame loop (legacy), one frame per step
        if i == F:
            yield adv(s, "R frames done")
            return
        f = i
        n = cp(s, rstate=("v", i + 1))
        if legacy:
            ok = s["refcnt"][f] == 1 if cfg["claim"] else s["refcnt"][f] != 0
            if ok:
                g = s["live"][f]
                n = cp(
                    n,
                    res=n["res"] + ((g, g, "OK"),),
                    refcnt=tset(n["refcnt"], f, s["refcnt"][f]),
                    sh=tset(n["sh"], f, 0),
                )
                if cfg["claim"]:
                    n = cp(n, refcnt=tset(n["refcnt"], f, 0))  # CAS 1->2, notify, sub 2
                else:
                    n = cp(n, refcnt=tset(n["refcnt"], f, s["refcnt"][f] - 1))
                if n["slot"][f] in (("E", g), ("C", g)):
                    n = cp(n, slot=tset(n["slot"], f, ("P", g)))
                yield "R legacy completes f%d u%d" % (f, g), n
            else:
                yield "R legacy skips f%d" % f, n
            return
        # core: the loop reads refcnt, then calls the verdict hook (two steps: a foreign callback
        # can finish the use and the app re-submit the slot in between)
        if len(rs) == 2:
            if s["refcnt"][f] != 0:
                yield "R f%d in flight" % f, cp(s, rstate=("v", i, 1))
            else:
                yield "R verdict f%d idle" % f, n
            return
        g = s["rec"][f]  # the hook: the binding names the frame's unit from its record
        if s["slot"][sl(cfg, g)] == ("E", g):
            yield "R verdict f%d u%d" % (f, g), cp(
                n, verdict=tset(n["verdict"], sl(cfg, g), g)
            )
        elif not cfg[
            "verdict_cas"
        ]:  # a plain store lands on whatever unit the slot holds next
            yield "R verdict f%d u%d stored on slot %s" % (
                f,
                g,
                s["slot"][sl(cfg, g)],
            ), cp(n, verdict=tset(n["verdict"], sl(cfg, g), -1))
        else:
            yield "R verdict f%d u%d fails (slot %s)" % (f, g, s["slot"][sl(cfg, g)]), n
        return
    if (
        phase == "d"
    ):  # step 2: the session rings and the transmitter's in-flight packets
        for j, (f, g, leg, st) in enumerate(s["mb"]):
            if st == "R":
                n = drop(
                    cp(s, mb=tset(s["mb"], j, (f, g, leg, "F")), gap=s["gap"] + (g,)), f
                )
                yield "R drop ring u%d leg%d" % (g, leg), n
                return
        n = s
        if s[
            "hung"
        ]:  # the P queue's descriptors stay referenced until MS3's stop/start
            for j, (f, g, leg, st) in enumerate(n["mb"]):
                if st == "N" and leg == 0:
                    n = cp(n, mb=tset(n["mb"], j, (f, g, leg, "H")))
        yield adv(n, "R rings empty")
        return
    if (
        phase == "x"
    ):  # the other legs' pad flush (:4303-4307): their descriptors are freed
        for j, (f, g, leg, st) in enumerate(s["mb"]):
            if st == "N" and leg > 0:
                yield "R flush leg%d u%d" % (leg, g), drop(
                    cp(s, mb=tset(s["mb"], j, (f, g, leg, "F"))), f
                )
                return
        yield adv(s, "R flush done")
        return
    if phase == "b":  # the builder restarts at WAIT_FRAME
        n = s
        b = s["b"]
        if b[0] == "build":
            _, f, g, i = b
            n = cp(n, b=("idle",), gap=n["gap"] + (g,))
            if legacy:
                n = cp(
                    n, bref=tset(n["bref"], f, 0)
                )  # legacy: sh_info = 0 dropped it already
            elif cfg["drop_bref"] and n["bref"][f]:
                n = drop(cp(n, bref=tset(n["bref"], f, 0)), f)
        yield adv(n, "R builder reset")
        return


def check(s, cfg):
    gens = [r[0] for r in s["res"]]
    for g in set(gens):
        if gens.count(g) > 1:
            return "DOUBLE: u%d completed twice" % g
    if (
        cfg["mode"] == "legacy"
    ):  # SF-39 only: legacy recovery's sh_info = 0 (SF-41) stays open
        return None
    for g, meta, st in s["res"]:
        if meta != g:
            return "META: u%d reported with m%d" % (g, meta)
        if st == "DROPPED" and s["verdict"][sl(cfg, g)] != g:
            return "VERDICT: u%d DROPPED without its verdict" % g
        if g in s["gap"] and st != "DROPPED":
            return "VERDICT: u%d lost packets to recovery but reported %s" % (g, st)
    if s["early"] and cfg["early_check"]:
        return "REPICK: " + s["early"]
    if any(x < 0 for x in s["sh"]):
        return "sh_info count below zero"
    return None


def final(s, cfg):
    hung = {g for (_, g, _, st) in s["mb"] if st == "H"}
    done = {r[0] for r in s["res"]}
    if s["td"] == "done":
        lost = [g for g in range(1, s["pickc"]) if g not in done]
        return "LOST at teardown: %s" % lost if lost else None
    if cfg["mode"] == "legacy":
        return None
    for g in range(1, s["pickc"]):
        if g not in done and g not in hung:
            return "LOST: u%d never completed" % g
    if not hung and s["reap"] != cfg["units"] + 1:
        return "STUCK: reap at u%d, slots %s" % (s["reap"], s["slot"])
    return None


def run(name, cfg, expect):
    e, t, n = explore(
        init(cfg),
        lambda s: succ(s, cfg),
        lambda s: check(s, cfg),
        lambda s: final(s, cfg),
    )
    got = "-" if e is None else e.split(":")[0]
    ok = got == expect
    print(
        "%-4s %-58s %8d states  expect %-8s got %s"
        % ("ok" if ok else "BAD", name, n, expect, "-" if e is None else e)
    )
    if e is not None and not ok or (e is not None and "-v" in sys.argv):
        print("       trace: " + " ; ".join(t))
    return ok


def main():
    def c(**kw):
        return dict(DEFAULT, **kw)

    cases = [
        ("core, the pick, 1 leg", c(), "-"),
        ("core, the pick, 2 legs", c(legs=2), "-"),
        (
            "core, the pick, 2 legs, binding reads live tv_meta",
            c(legs=2, core_meta="live"),
            "-",
        ),
        (
            "core, the pick, 2 legs, binding names by the slot word",
            c(legs=2, core_name="slotword"),
            "-",
        ),
        (
            "core, the pick, 2 legs, no claim CAS in the callback",
            c(legs=2, claim=False),
            "-",
        ),
        (
            "MS2b shape: frames re-bound, handed out at refcnt 0",
            c(rebind=True),
            "REPICK",
        ),
        (
            "  same, what the early re-pick breaks",
            c(rebind=True, early_check=False),
            "META",
        ),
        (
            "MS2b shape: frames re-bound, handed out after the notify",
            c(rebind=True, gate="record"),
            "-",
        ),
        (
            "mutant: verdict where today's frame loop is (after drops)",
            c(legs=2, verdict="late"),
            "VERDICT",
        ),
        ("mutant: verdict as a plain store", c(legs=2, verdict_cas=False), "VERDICT"),
        (
            "mutant: recovery keeps the builder's MF7 reference",
            c(drop_bref=False),
            "LOST",
        ),
        ("legacy, MF1's claim, 1 leg", c(mode="legacy"), "-"),
        ("legacy, MF1's claim, 2 legs", c(mode="legacy", legs=2), "-"),
        (
            "legacy at NEW (no claim), 3 units (SF-39 with SF-41)",
            c(mode="legacy", claim=False),
            "DOUBLE",
        ),
        (
            "legacy at NEW (no claim), 1 unit: SF-39 alone",
            c(mode="legacy", claim=False, units=1, legs=2),
            "DOUBLE",
        ),
        ("legacy, MF1's claim, 1 unit, 2 legs", c(mode="legacy", units=1, legs=2), "-"),
        (
            "legacy teardown mid-frame, MF7 dropped at teardown",
            c(mode="legacy", recovery=False, teardown=True),
            "-",
        ),
        (
            "mutant: teardown keeps the builder's MF7 reference",
            c(mode="legacy", recovery=False, teardown=True, teardown_drop=False),
            "LOST at teardown",
        ),
    ]
    ok = all([run(*x) for x in cases])
    print("all expectations met" if ok else "SOME EXPECTATION NOT MET")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
