# Ideas & Next Steps

## Current Status (After Fix 9)
- All 11 fixes applied
- Build passes, installed
- **NOT YET TESTED** — needs pcap capture and EBU LIST analysis

## Expected Results
- Cinst: 0-3 (bulk=4 → 4 packets burst, 3 above ideal → peak Cinst = 3, within narrow cmax=4)
- VRX: Should be within [-8, 8] if Bresenham pacing and TSC gate work correctly
- RTP delta: 3000 (30fps, one epoch per frame)
- pkt_ts_vs_rtp_ts: Should be small and stable (no cumulative drift)

## If Still Failing

### Cinst > 4
- Could mean bulk=4 with NIC wire-rate burst exceeds expectations
- Try bulk=2 as compromise between overhead and burst size
- Check if NIC submits all 4 packets at first pkt's LaunchTime

### VRX out of range
- Check tro_default_ns vs actual — EBU LIST may compute VRX with wrong tr_offset
- Check if 128ns quantization jitter (±64ns per packet) accumulates
- Look at per-frame VRX pattern: should be flat, not banded

### pkt_ts_vs_rtp_ts growing
- Indicates builder is consistently late: time_to_tx clamped to 0 on some frames
- Check `epoch_drop` stat in MTL output
- Possible fix: add small headroom to `transmission_start_time` for TSN

## Longer-Term Ideas

### 1. NIC-Level Pacing Confirmation
- Use `ethtool --show-priv-flags` or ice debugfs to confirm LaunchTime is actually active
- Try capturing on a mirror port to see actual wire timestamps vs programmed LaunchTime
- Test with `testpmd` to isolate NIC behavior from MTL software

### 2. Adaptive Bulk Size
- Monitor `time_to_tx_ns` over frames
- If consistently negative (margin < 1ms): reduce bulk
- If consistently positive (margin > 5ms): could increase bulk

### 3. Better Epoch Advance
- Instead of flat threshold, track history of `time_to_tx_ns`
- Advance only if multiple consecutive frames show negative time_to_tx
- This would handle transient scheduler delays without firing too aggressively

### 4. Hardware PTP vs No-PTP
- Current approach works without `--ptp` by using free-running NIC clock
- If PTP is available, ptp_time_cursor should be more accurate
- Test with `--ptp` to see if compliance improves further

### 5. Other Resolutions/Framerates
- 1080p60: trs ≈ 3888ns, total_pkts ≈ 4115, frame_time ≈ 16.67ms
  - drain with bulk=4: ~14.8ms (ok, 1.9ms margin)
  - drain with bulk=1: ~16.8ms (FAIL, would trigger epoch doubling)
- 4K (3840x2160): trs ≈ 1944ns, total_pkts ≈ 16460, frame_time ≈ 33.33ms
  - drain with bulk=4: ~31.2ms (tight! only 2.1ms margin)
  - May need bulk=8 or ring tuning for 4K

### 6. Formal Compliance Testing
- Need 15+ minutes of capture for proper ST 2110-21 compliance
- Current tests are 42-160 frames (1-5 seconds) — too short
- Must verify no epoch drops or timing anomalies in long runs

## Quick Test Checklist
```bash
# Analyze pcap
python3 .github/workspace/analyze_pcap.py /tmp/test.pcap

# Run through EBU LIST, then analyze JSON
python3 .github/workspace/analyze_json.py /tmp/test-results.json
```
