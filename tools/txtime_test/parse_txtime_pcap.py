#!/usr/bin/env python3
"""Parse txtime_test packets from a pcap capture.

Usage:
    tcpdump -i <iface> -w capture.pcap udp port 12345
    python3 parse_txtime_pcap.py capture.pcap

Decodes the self-describing payload and prints a table showing
which packets arrived, their intended launch time, and relative
arrival timing (no clock sync needed).
"""

import struct
import sys
from collections import defaultdict

MAGIC = 0x54585431  # "TXT1"
# C struct txtime_payload:
#   uint32_t magic, seq, test_id, pad;
#   uint64_t phc_at_send; int64_t offset_ns; uint64_t launch_time;
PAYLOAD_FMT = ">IIIIQqQ"
PAYLOAD_SIZE = struct.calcsize(PAYLOAD_FMT)  # 40 bytes

TEST_NAMES = {
    0: "Sanity (no txtime flag)",
    1: "Baseline +1ms future",
    2: "All past -1ms",
    3: "Alternating +1ms / -1ms",
    4: "All past -50ms",
    5: "All past -500ms",
    6: "Alternating +1ms / -500ms",
    7: "Offset = 0 (PHC now)",
    8: "Far future +1s",
    9: "Alternating +1s / -1ms",
    10: "Graduated +1ms to -999ms",
    11: "MTL-style stream (100pkts, 7936ns step)",
}

try:
    from scapy.all import rdpcap, UDP
except ImportError:
    print("Install scapy: pip install scapy", file=sys.stderr)
    sys.exit(1)


def decode_payload(raw: bytes):
    if len(raw) < PAYLOAD_SIZE:
        return None
    magic, seq, test_id, pad, phc, offset, launch = struct.unpack(
        PAYLOAD_FMT, raw[:PAYLOAD_SIZE]
    )
    if magic != MAGIC:
        return None
    return {
        "seq": seq,
        "test_id": test_id,
        "phc": phc,
        "offset_ns": offset,
        "launch": launch,
    }


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <pcap_file>", file=sys.stderr)
        sys.exit(1)

    packets = rdpcap(sys.argv[1])

    results = []
    t0 = None

    for pkt in packets:
        if not pkt.haslayer(UDP):
            continue
        udp = pkt[UDP]
        if udp.dport != 12345 and udp.sport != 54321:
            continue
        raw = bytes(udp.payload)
        info = decode_payload(raw)
        if info is None:
            continue

        ts = float(pkt.time)
        if t0 is None:
            t0 = ts

        info["arrival_s"] = ts - t0
        results.append(info)

    if not results:
        print("No txtime_test packets found in capture.")
        return

    # Group by test_id
    by_test = defaultdict(list)
    for r in results:
        by_test[r["test_id"]].append(r)

    # Per-test analysis
    for tid in sorted(by_test.keys()):
        pkts = by_test[tid]
        name = TEST_NAMES.get(tid, f"Unknown test {tid}")
        print(f"\n{'='*72}")
        print(f"TEST {tid}: {name}  ({len(pkts)} packets)")
        print(f"{'='*72}")

        # Header
        print(f"{'seq':>5}  {'offset_ms':>10}  {'launch_nsec':>14}  "
              f"{'>>7(19bit)':>10}  {'arrival_ms':>12}  {'intra_us':>10}")
        print("-" * 72)

        first_arrival = pkts[0]["arrival_s"]
        for r in pkts:
            nsec = r["launch"] % 1_000_000_000
            shifted = (nsec >> 7) & 0x7FFFF
            offset_ms = r["offset_ns"] / 1e6
            intra_us = (r["arrival_s"] - first_arrival) * 1e6
            seq_label = "recov" if r["seq"] == 0xFFFF else str(r["seq"])
            print(f"{seq_label:>5}  {offset_ms:>+10.1f}  {nsec:>14d}  "
                  f"{shifted:>10d}  {r['arrival_s']*1000:>12.3f}  "
                  f"{intra_us:>10.1f}")

        # Timing summary for this test
        data_pkts = [r for r in pkts if r["seq"] != 0xFFFF]
        if data_pkts:
            arrivals = [r["arrival_s"] for r in data_pkts]
            spread_us = (max(arrivals) - min(arrivals)) * 1e6
            phc = data_pkts[0]["phc"]
            phc_sec = phc / 1e9
            first_arr = min(arrivals)
            print(f"  >> Burst spread: {spread_us:.1f} us  "
                  f"| PHC@send: {phc/1e9:.6f}s  "
                  f"| All offsets: {set(r['offset_ns'] for r in data_pkts)}")

    # Inter-packet gap analysis for Test 11 (MTL-style stream)
    if 11 in by_test:
        pkts11 = [r for r in by_test[11] if r["seq"] != 0xFFFF]
        pkts11.sort(key=lambda r: r["seq"])
        if len(pkts11) > 1:
            print(f"\n{'='*90}")
            print(f"INTER-PACKET GAP ANALYSIS — Test 11 ({len(pkts11)} packets)")
            print(f"{'='*90}")
            print(f"  Planned step: 7936 ns (62 × 128ns)")
            print()

            # Compute actual gaps (wire arrival) and planned gaps (launch time)
            actual_gaps_ns = []
            planned_gaps_ns = []
            for i in range(1, len(pkts11)):
                actual_ns = (pkts11[i]["arrival_s"] - pkts11[i-1]["arrival_s"]) * 1e9
                planned_ns = pkts11[i]["launch"] - pkts11[i-1]["launch"]
                actual_gaps_ns.append(actual_ns)
                planned_gaps_ns.append(planned_ns)

            print(f"  {'Gap#':>5} {'Planned(ns)':>12} {'Actual(ns)':>12} "
                  f"{'Error(ns)':>12} {'Error%':>8}")
            print(f"  {'-'*54}")

            errors = []
            for i, (planned, actual) in enumerate(zip(planned_gaps_ns, actual_gaps_ns)):
                err_ns = actual - planned
                err_pct = (err_ns / planned * 100) if planned != 0 else 0
                errors.append(abs(err_ns))
                flag = "" if abs(err_ns) < 1000 else (" *" if abs(err_ns) < 5000 else " **")
                # Print first 20, last 5, and any with error >5us
                if i < 20 or i >= len(planned_gaps_ns) - 5 or abs(err_ns) > 5000:
                    print(f"  {i:>5} {planned:>12.0f} {actual:>12.0f} "
                          f"{err_ns:>+12.0f} {err_pct:>+7.1f}%{flag}")
                elif i == 20:
                    print(f"  {'...':>5} {'(gaps within tolerance omitted)':>40}")

            # Statistics
            import math
            mean_actual = sum(actual_gaps_ns) / len(actual_gaps_ns)
            mean_planned = sum(planned_gaps_ns) / len(planned_gaps_ns)
            mean_err = sum(errors) / len(errors)
            max_err = max(errors)
            within_1us = sum(1 for e in errors if e < 1000)
            within_5us = sum(1 for e in errors if e < 5000)
            total_span_planned = sum(planned_gaps_ns)
            total_span_actual = (pkts11[-1]["arrival_s"] - pkts11[0]["arrival_s"]) * 1e9

            # Pearson correlation
            n = len(actual_gaps_ns)
            mean_a = mean_actual
            mean_p = mean_planned
            sum_ap = sum((a - mean_a) * (p - mean_p) for a, p in zip(actual_gaps_ns, planned_gaps_ns))
            sum_aa = sum((a - mean_a) ** 2 for a in actual_gaps_ns)
            sum_pp = sum((p - mean_p) ** 2 for p in planned_gaps_ns)
            denom = math.sqrt(sum_aa * sum_pp) if sum_aa > 0 and sum_pp > 0 else 0
            r_val = sum_ap / denom if denom > 0 else 0
            r2 = r_val ** 2

            print(f"\n  --- Gap Statistics ---")
            print(f"  Packets                : {len(pkts11)}")
            print(f"  Gaps analyzed          : {len(actual_gaps_ns)}")
            print(f"  Planned step (ns)      : {mean_planned:.0f}")
            print(f"  Mean actual gap (ns)   : {mean_actual:.0f}")
            print(f"  Mean |error| (ns)      : {mean_err:.0f}")
            print(f"  Max |error| (ns)       : {max_err:.0f}")
            print(f"  Within 1 µs            : {within_1us}/{n} ({within_1us/n*100:.0f}%)")
            print(f"  Within 5 µs            : {within_5us}/{n} ({within_5us/n*100:.0f}%)")
            print(f"  Total span planned (µs): {total_span_planned/1000:.1f}")
            print(f"  Total span actual (µs) : {total_span_actual/1000:.1f}")
            print(f"  Span error (µs)        : {(total_span_actual - total_span_planned)/1000:+.1f}")
            print(f"  Pearson r              : {r_val:.6f}")
            print(f"  R²                     : {r2:.6f}")
            if r2 > 0.99:
                print(f"  Verdict                : HW SCHEDULING ACTIVE (R²≥0.99)")
            elif r2 > 0.5:
                print(f"  Verdict                : PARTIAL SCHEDULING (0.5<R²<0.99)")
            else:
                print(f"  Verdict                : NO SCHEDULING (R²<0.5, all gaps ~equal)")

    # Inter-packet gap analysis for all tests (burst spread check)
    for tid in sorted(by_test.keys()):
        if tid == 11:
            continue  # already analyzed above
        pkts = [r for r in by_test[tid] if r["seq"] != 0xFFFF]
        pkts.sort(key=lambda r: r["seq"])
        if len(pkts) > 1:
            gaps_ns = [(pkts[i]["arrival_s"] - pkts[i-1]["arrival_s"]) * 1e9
                       for i in range(1, len(pkts))]
            mean_gap = sum(gaps_ns) / len(gaps_ns)
            max_gap = max(gaps_ns)
            min_gap = min(gaps_ns)
            # For uniform-offset tests, all planned gaps are 0 (same launch time)
            offsets = set(r["offset_ns"] for r in pkts)
            if len(offsets) == 1:
                # All same offset: planned gap = 128ns (inter-packet step in build)
                planned_gap = 128  # ns per packet
            else:
                planned_gap = None  # varying offsets, gaps depend on offset pattern

    # Global summary
    print(f"\n{'#'*72}")
    print(f"SUMMARY: {len(results)} total packets, "
          f"{len(by_test)} test groups")
    print(f"{'#'*72}")
    for tid in sorted(by_test.keys()):
        pkts = by_test[tid]
        data_pkts = [r for r in pkts if r["seq"] != 0xFFFF]
        recov = [r for r in pkts if r["seq"] == 0xFFFF]
        name = TEST_NAMES.get(tid, f"?")
        offsets = sorted(set(r["offset_ns"] for r in data_pkts))
        off_str = ", ".join(f"{o/1e6:+.0f}ms" for o in offsets)
        arrivals = [r["arrival_s"] for r in data_pkts]
        spread_us = (max(arrivals) - min(arrivals)) * 1e6 if len(arrivals) > 1 else 0
        print(f"  Test {tid:>2}: {len(data_pkts):>2} data + {len(recov)} recov  "
              f"spread={spread_us:>8.1f}us  offsets=[{off_str}]  {name}")


if __name__ == "__main__":
    main()
