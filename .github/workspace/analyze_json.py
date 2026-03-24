#!/usr/bin/env python3
"""Analyze EBU LIST JSON output for ST 2110-21 compliance.

Usage: python3 analyze_json.py <path_to_json>

Prints compliance summary, Cinst/VRX histograms, pkt_ts_vs_rtp_ts range,
inter_frame_rtp_ts_delta, and key stream parameters.
"""

import json
import sys

def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <ebu_list_json>")
        sys.exit(1)

    with open(sys.argv[1]) as f:
        d = json.load(f)

    s = d['streams'][0]
    a = s['analyses']
    g = s['global_video_analysis']
    ms = s['media_specific']
    st = s['statistics']

    print(f"=== COMPLIANCE: {g.get('compliance')} ===")
    print()

    # Per-analysis results
    for k in ['packet_ts_vs_rtp_ts', 'inter_frame_rtp_ts_delta',
              '2110_21_cinst', '2110_21_vrx', 'rtp_sequence']:
        v = a.get(k, {})
        r = v.get('result', '?')
        det = v.get('details', {})
        rng = det.get('range', {}) if isinstance(det, dict) else {}
        limit = det.get('limit', {}) if isinstance(det, dict) else {}
        print(f"  {k}: {r}")
        if rng:
            print(f"    range: min={rng.get('min')} max={rng.get('max')} avg={rng.get('avg')}")
        if limit:
            print(f"    limit: {limit}")

    print(f"\n--- Stream Info ---")
    print(f"  rate={ms.get('rate')} fps  frames={st.get('frame_count')}  "
          f"pkts={st.get('packet_count')}  dropped={st.get('dropped_packet_count')}")
    print(f"  {ms.get('width')}x{ms.get('height')} {ms.get('scan_type')} "
          f"{ms.get('sampling')} {ms.get('color_depth')}bit")
    print(f"  pkts/frame={ms.get('packets_per_frame')}  schedule={ms.get('schedule')}")
    print(f"  tro: min={ms.get('min_tro_ns')} avg={ms.get('avg_tro_ns')} "
          f"max={ms.get('max_tro_ns')} default={ms.get('tro_default_ns')}")

    # Cinst histogram summary
    cinst_h = g['cinst']['histogram']
    print(f"\n--- Cinst ---")
    print(f"  range: {cinst_h[0][0]} to {cinst_h[-1][0]}")
    print(f"  narrow cmax=4, wide cmax=16")
    # Show distribution in bands
    bands = [(0, 0), (1, 4), (5, 8), (9, 16), (17, 100), (101, 1000), (1001, 99999)]
    for lo, hi in bands:
        pct = sum(p for c, p in cinst_h if lo <= c <= hi)
        if pct > 0:
            label = f"{lo}" if lo == hi else f"{lo}-{hi}"
            print(f"    Cinst [{label:>9}]: {pct:.2f}%")

    # VRX histogram summary
    vrx_h = g['vrx']['histogram']
    print(f"\n--- VRX ---")
    print(f"  range: {vrx_h[0][0]} to {vrx_h[-1][0]}")
    print(f"  narrow limit: [-8, 8]  wide limit: [-720, 720]")
    vrx_in_narrow = sum(p for v, p in vrx_h if -8 <= v <= 8)
    vrx_in_wide = sum(p for v, p in vrx_h if -720 <= v <= 720)
    vrx_neg = sum(p for v, p in vrx_h if v < -8)
    vrx_pos = sum(p for v, p in vrx_h if v > 8)
    print(f"    within narrow [-8,8]:   {vrx_in_narrow:.2f}%")
    print(f"    within wide [-720,720]: {vrx_in_wide:.2f}%")
    print(f"    below -8: {vrx_neg:.2f}%")
    print(f"    above +8: {vrx_pos:.2f}%")

    # Show major VRX bands (>5%)
    major = [(v, p) for v, p in vrx_h if p > 5.0]
    if major:
        print(f"  Major VRX bands (>5%):")
        for v, p in major:
            print(f"    VRX={v}: {p:.1f}%")


if __name__ == '__main__':
    main()
