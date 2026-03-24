#!/usr/bin/env python3
"""Simulate steady-state TSN pacing timing.

Models the builder/transmitter lockstep to predict:
- time_to_tx per frame
- frame cycle time
- whether epoch advance fires
- expected fps

Usage: python3 timing_model.py [total_pkts] [fps] [bulk]
Defaults: 4115 pkts, 30fps, bulk=4
"""

import sys

def main():
    total_pkts = int(sys.argv[1]) if len(sys.argv) > 1 else 4115
    fps = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0
    bulk = int(sys.argv[3]) if len(sys.argv) > 3 else 4

    frame_time = 1e9 / fps  # ns
    reactive = 1080.0 / 1125.0
    trs = frame_time * reactive / total_pkts
    tr_offset = frame_time * 43.0 / 1125.0
    vrx = 8  # narrow default
    vrx_compensation = bulk - 1
    effective_vrx = vrx - vrx_compensation

    # Bresenham
    ticks_base = int(trs / 128.0)
    total_ticks = round(trs * total_pkts / 128.0)
    ticks_extra = total_ticks - ticks_base * total_pkts
    frame_span = total_ticks * 128  # ns, exact

    # Ring
    ring_size = 512
    while ring_size > total_pkts:
        ring_size //= 2

    # transmission_start_time = epoch * frame_time + tr_offset - vrx * trs
    start_offset = tr_offset - effective_vrx * trs  # offset within epoch

    # Scheduler overhead per packet (empirical)
    overhead_per_pkt_bulk1 = 1500  # ns
    overhead_per_pkt = overhead_per_pkt_bulk1 / bulk

    # Effective trs with overhead
    effective_trs = trs + overhead_per_pkt

    # Builder is blocked by ring backpressure after filling ring_size slots.
    # From that point, builder produces 1 pkt per effective_trs (locked to transmitter drain).
    # Total builder wall time from tsc_cursor:
    #   Phase 1: fill ring (ring_size pkts) - takes ~1ms at CPU speed, but
    #            transmitter also drains during this time
    #   Phase 2: lockstep (total_pkts - ring_size) pkts at effective_trs each
    # Simplified: builder finishes when transmitter has sent (total_pkts - ring_size) pkts
    lockstep_pkts = total_pkts - ring_size
    builder_wall_time = lockstep_pkts * effective_trs  # ns from tsc_cursor

    # Frame cycle: time_to_tx_N + builder_wall_time_N = time until sync_pacing(N+1)
    # At sync_pacing(N+1): calc returns cur_epochs+1, start_time = (cur_epochs+1)*ft + offset
    # time_to_tx(N+1) = start_time(N+1) - cur_tai(N+1)
    # cur_tai(N+1) = cur_tai(N) + time_to_tx(N) + builder_wall_time
    # start_time(N+1) = start_time(N) + frame_time
    # time_to_tx(N+1) = start_time(N) + frame_time - (cur_tai(N) + time_to_tx(N) + builder_wall_time)
    #                  = time_to_tx(N) + frame_time - time_to_tx(N) - builder_wall_time
    #                  = frame_time - builder_wall_time
    steady_state_time_to_tx = frame_time - builder_wall_time
    steady_state_cycle = frame_time  # if time_to_tx > 0

    advance_fires = steady_state_time_to_tx <= 0

    if advance_fires:
        # Advance adds +1 epoch → time_to_tx += frame_time
        adjusted_time_to_tx = steady_state_time_to_tx + frame_time
        actual_cycle = adjusted_time_to_tx + builder_wall_time
        effective_fps = 1e9 / actual_cycle
        advance_mode = "EVERY FRAME (2× frame rate!)"
    else:
        adjusted_time_to_tx = steady_state_time_to_tx
        actual_cycle = adjusted_time_to_tx + builder_wall_time
        effective_fps = 1e9 / actual_cycle
        advance_mode = "bootstrap only"

    print(f"=== TSN Pacing Timing Model ===")
    print(f"")
    print(f"--- Parameters ---")
    print(f"  total_pkts:    {total_pkts}")
    print(f"  fps:           {fps}")
    print(f"  bulk:          {bulk}")
    print(f"  frame_time:    {frame_time/1e6:.3f} ms")
    print(f"  trs:           {trs:.2f} ns")
    print(f"  tr_offset:     {tr_offset/1e6:.3f} ms")
    print(f"  vrx (raw):     {vrx}")
    print(f"  vrx (comp.):   {effective_vrx} (after bulk-1={vrx_compensation} compensation)")
    print(f"  ring_size:     {ring_size}")
    print(f"")
    print(f"--- Bresenham ---")
    print(f"  ticks_base:    {ticks_base} ({ticks_base*128} ns)")
    print(f"  ticks_extra:   {ticks_extra}/{total_pkts}")
    print(f"  total_ticks:   {total_ticks}")
    print(f"  frame_span:    {frame_span/1e6:.3f} ms")
    print(f"")
    print(f"--- Overhead Model ---")
    print(f"  overhead/pkt:  {overhead_per_pkt:.0f} ns (from {overhead_per_pkt_bulk1} ns at bulk=1)")
    print(f"  effective_trs: {effective_trs:.0f} ns")
    print(f"")
    print(f"--- Steady State ---")
    print(f"  lockstep_pkts: {lockstep_pkts}")
    print(f"  builder_wall:  {builder_wall_time/1e6:.3f} ms")
    print(f"  time_to_tx:    {steady_state_time_to_tx/1e6:.3f} ms {'⚠ NEGATIVE' if advance_fires else '✓'}")
    print(f"  advance mode:  {advance_mode}")
    print(f"  actual cycle:  {actual_cycle/1e6:.3f} ms")
    print(f"  effective fps: {effective_fps:.1f}")
    print(f"  margin:        {steady_state_time_to_tx/1e6:.3f} ms")
    print(f"")

    # Safety analysis
    print(f"--- Safety Margin ---")
    max_safe_overhead = (frame_time / lockstep_pkts - trs)
    max_safe_bulk1_overhead = max_safe_overhead * bulk
    print(f"  Max overhead/pkt before advance fires: {max_safe_overhead:.0f} ns")
    print(f"  Equivalent bulk=1 overhead limit:      {max_safe_bulk1_overhead:.0f} ns")
    print(f"")

    # Cinst prediction
    cinst_peak = bulk - 1  # worst case: bulk pkts arrive, trs scheduled 1
    print(f"--- Cinst Prediction ---")
    print(f"  Peak Cinst:    {cinst_peak} (bulk={bulk}, target=1/trs)")
    print(f"  Narrow cmax:   4 → {'PASS' if cinst_peak <= 4 else 'FAIL'}")
    print(f"  Wide cmax:     16 → {'PASS' if cinst_peak <= 16 else 'FAIL'}")

    # What the while advance would do at bootstrap
    print(f"")
    print(f"--- Bootstrap (first frame) ---")
    # At bootstrap, cur_tai is random within current epoch
    # Worst case: cur_tai just past transmission_start → time_to_tx ≈ -frame_time + start_offset
    worst_bootstrap = -frame_time + start_offset
    advances_needed = 0
    t = worst_bootstrap
    while t <= 0:
        t += frame_time
        advances_needed += 1
    print(f"  Worst-case time_to_tx: {worst_bootstrap/1e6:.3f} ms")
    print(f"  Advances needed:       {advances_needed}")
    print(f"  After advance:         {t/1e6:.3f} ms")


if __name__ == '__main__':
    main()
