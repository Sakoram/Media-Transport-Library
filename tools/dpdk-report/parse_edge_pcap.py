#!/usr/bin/env python3
"""
Parse txtime_edge_test pcap capture.

Extract self-describing payloads, correlate intended launch time
with actual wire arrival, group by test_id, and show timing analysis.

Usage:
  python3 parse_edge_pcap.py /tmp/txtime_edge.pcap
"""

import struct
import sys
from collections import defaultdict

try:
    import dpkt
except ImportError:
    print("Install dpkt:  pip3 install dpkt")
    sys.exit(1)

MAGIC = 0xE830CAFE
UDP_PORT = 12345

# struct test_payload { u32 magic; u16 test_id; u16 seq;
#                       i64 offset_ns; u64 phc_at_send; u64 launch_time; }
PAYLOAD_FMT = ">IHHqQQ"
PAYLOAD_SIZE = struct.calcsize(PAYLOAD_FMT)

TEST_NAMES = {
    0: "future +1ms",     1: "future +5ms",     2: "future +10ms",
    3: "future +30ms",    4: "future +50ms",    5: "future +60ms",
    6: "future +65ms",    7: "future +67ms",    8: "future +67.1ms",
    9: "future +68ms",   10: "future +70ms",   11: "future +80ms",
   12: "future +100ms",  13: "future +134ms",
   14: "past -1ms",      15: "past -5ms",      16: "past -10ms",
   17: "past -50ms",     18: "past -67ms",     19: "past -100ms",
   20: "past -500ms",    21: "past -999ms",
   22: "dynfield=0",     23: "future +1s",     24: "future +2s",
   25: "future +10s",    26: "past -1s",
   27: "future +128ns",  28: "future +1us",    29: "future +10us",
   30: "future +100us",  31: "mixed ±1ms",
}


def parse_pcap(path):
    packets = []
    with open(path, "rb") as f:
        pcap = dpkt.pcap.Reader(f)

        for ts, buf in pcap:
            try:
                eth = dpkt.ethernet.Ethernet(buf)
                if not isinstance(eth.data, dpkt.ip.IP):
                    continue
                ip = eth.data
                if not isinstance(ip.data, dpkt.udp.UDP):
                    continue
                udp = ip.data
                if udp.dport != UDP_PORT and udp.sport != UDP_PORT:
                    continue
                payload = bytes(udp.data)
                if len(payload) < PAYLOAD_SIZE:
                    continue
                fields = struct.unpack(PAYLOAD_FMT, payload[:PAYLOAD_SIZE])
                magic, test_id, seq, offset_ns, phc_at_send, launch_time = fields
                if magic != MAGIC:
                    continue
                # ts is float or Decimal seconds from pcap
                wire_ns = int(float(ts) * 1e9)
                packets.append({
                    "wire_ns": wire_ns,
                    "test_id": test_id,
                    "seq": seq,
                    "offset_ns": offset_ns,
                    "phc_at_send": phc_at_send,
                    "launch_time": launch_time,
                })
            except Exception:
                continue
    return packets


def analyze(packets):
    by_test = defaultdict(list)
    for p in packets:
        by_test[p["test_id"]].append(p)

    print(f"\n{'='*90}")
    print(f"  E830 TXTIME Edge-Case Pcap Analysis — {len(packets)} packets, "
          f"{len(by_test)} tests")
    print(f"{'='*90}\n")

    print(f"{'T#':>3} {'Name':<22} {'Cnt':>3} {'Offset':>12} "
          f"{'WireDelay':>12} {'SchedErr':>12} {'Spread':>10} {'Verdict'}")
    print("-" * 90)

    for tid in sorted(by_test.keys()):
        pkts = sorted(by_test[tid], key=lambda p: p["seq"])
        name = TEST_NAMES.get(tid, f"test_{tid}")
        cnt = len(pkts)
        offset = pkts[0]["offset_ns"]

        # Wire delay = wire_ns - phc_at_send  (how long after PHC read did it arrive)
        wire_delays = [p["wire_ns"] - p["phc_at_send"] for p in pkts]
        # Scheduling error = wire_ns - launch_time (how close to intended launch)
        sched_errs = [p["wire_ns"] - p["launch_time"] for p in pkts]
        # Spread = max wire_ns - min wire_ns within the burst
        spread = max(p["wire_ns"] for p in pkts) - min(p["wire_ns"] for p in pkts)

        avg_delay = sum(wire_delays) / len(wire_delays)
        avg_sched = sum(sched_errs) / len(sched_errs)

        # Verdict:
        # If offset > 0 (future): scheduling error should be near 0
        # If offset <= 0 (past): packet sent immediately, sched_err = |offset| + small
        if offset > 500000:  # > 0.5ms future
            verdict = "SCHEDULED" if abs(avg_sched) < 5000000 else "IMMEDIATE"
        elif offset <= 0:
            verdict = "IMMEDIATE"
        else:
            verdict = "MICRO"

        print(f"T{tid:02d} {name:<22} {cnt:>3} {offset/1e6:>+10.3f}ms "
              f"{avg_delay/1e6:>10.3f}ms {avg_sched/1e6:>10.3f}ms "
              f"{spread/1e3:>8.1f}us {verdict}")

    # Detailed per-packet dump
    print(f"\n{'='*90}")
    print("  Per-Packet Detail")
    print(f"{'='*90}\n")

    for tid in sorted(by_test.keys()):
        pkts = sorted(by_test[tid], key=lambda p: p["seq"])
        name = TEST_NAMES.get(tid, f"test_{tid}")
        print(f"--- T{tid:02d}: {name} ---")
        print(f"  {'Seq':>3} {'WireTime':>22} {'LaunchTime':>22} "
              f"{'WireDelay':>12} {'SchedErr':>12}")

        for p in pkts:
            wire_s = p["wire_ns"] / 1e9
            launch_s = p["launch_time"] / 1e9
            wire_delay = (p["wire_ns"] - p["phc_at_send"]) / 1e6
            sched_err = (p["wire_ns"] - p["launch_time"]) / 1e6
            print(f"  {p['seq']:>3} {wire_s:>22.9f} {launch_s:>22.9f} "
                  f"{wire_delay:>+10.3f}ms {sched_err:>+10.3f}ms")
        print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <pcap_file>")
        sys.exit(1)
    pkts = parse_pcap(sys.argv[1])
    if not pkts:
        print("No matching packets found in pcap.")
        sys.exit(1)
    analyze(pkts)
