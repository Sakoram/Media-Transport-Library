#!/usr/bin/env python3
"""Analyze a pcap for ST 2110 frame timing using tshark.

Usage: python3 analyze_pcap.py <pcap_file> [rtp_port]

Default RTP port: 20000

Outputs:
- Per-frame: RTP delta, inter-frame gap, elapsed time, pkt count
- pkt_ts_vs_rtp_ts drift per frame
- Overall statistics
"""

import subprocess
import sys

def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <pcap> [rtp_port=20000]")
        sys.exit(1)

    pcap = sys.argv[1]
    port = sys.argv[2] if len(sys.argv) > 2 else "20000"

    # Extract all packets
    cmd = [
        "tshark", "-r", pcap,
        "-d", f"udp.port=={port},rtp",
        "-T", "fields",
        "-e", "frame.time_epoch",
        "-e", "rtp.timestamp",
        "-e", "rtp.seq",
        "-e", "rtp.marker",
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, text=True, stderr=subprocess.DEVNULL)
    lines = result.stdout.strip().split('\n')

    if not lines or not lines[0]:
        print("No RTP packets found")
        sys.exit(1)

    # Parse packets
    packets = []
    for line in lines:
        parts = line.split('\t')
        if len(parts) < 3:
            continue
        t = float(parts[0])
        rtp = int(parts[1])
        seq = int(parts[2])
        marker = int(parts[3]) if len(parts) > 3 and parts[3] else 0
        packets.append((t, rtp, seq, marker))

    print(f"Total packets: {len(packets)}")
    print()

    # Find frame boundaries (RTP timestamp changes)
    first_t = packets[0][0]
    first_rtp = packets[0][1]
    prev_rtp = packets[0][1]
    prev_t = packets[0][0]
    pkt_count = 1
    frame_num = 0
    frames = []

    for i in range(1, len(packets)):
        t, rtp, seq, marker = packets[i]
        if rtp != prev_rtp:
            frame_num += 1
            delta_rtp = rtp - prev_rtp
            gap_us = (t - prev_t) * 1e6
            elapsed = t - first_t
            wire_elapsed_ms = elapsed * 1000
            rtp_elapsed_ms = (rtp - first_rtp) / 90.0  # 90kHz clock
            pkt_ts_vs_rtp_ms = wire_elapsed_ms - rtp_elapsed_ms

            frames.append({
                'num': frame_num,
                'rtp_delta': delta_rtp,
                'gap_us': gap_us,
                'elapsed_s': elapsed,
                'pkts': pkt_count,
                'pkt_ts_vs_rtp_ms': pkt_ts_vs_rtp_ms,
                'wire_ms': wire_elapsed_ms,
                'rtp_ms': rtp_elapsed_ms,
            })
            pkt_count = 0

        pkt_count += 1
        prev_rtp = rtp
        prev_t = t

    print(f"{'Frame':>5} {'RTP_Δ':>7} {'Gap(µs)':>10} {'Elapsed(s)':>11} "
          f"{'Pkts':>5} {'pkt_vs_rtp(ms)':>15}")
    print("-" * 65)

    rtp_deltas = set()
    gaps = []
    for f in frames:
        rtp_deltas.add(f['rtp_delta'])
        gaps.append(f['gap_us'])
        print(f"{f['num']:5d} {f['rtp_delta']:7d} {f['gap_us']:10.1f} "
              f"{f['elapsed_s']:11.6f} {f['pkts']:5d} {f['pkt_ts_vs_rtp_ms']:15.3f}")

    print()
    print(f"--- Summary ---")
    print(f"  Frames: {len(frames)}")
    print(f"  RTP deltas seen: {sorted(rtp_deltas)}")
    print(f"  Expected (30fps): 3000,  (15fps): 6000")
    if gaps:
        print(f"  Inter-frame gap: min={min(gaps):.1f}µs  max={max(gaps):.1f}µs  "
              f"avg={sum(gaps)/len(gaps):.1f}µs")
    if frames:
        drifts = [f['pkt_ts_vs_rtp_ms'] for f in frames]
        print(f"  pkt_ts_vs_rtp: min={min(drifts):.3f}ms  max={max(drifts):.3f}ms")
        print(f"  pkt_ts_vs_rtp drift rate: "
              f"{(drifts[-1] - drifts[0]) / len(drifts):.4f} ms/frame")


if __name__ == '__main__':
    main()
