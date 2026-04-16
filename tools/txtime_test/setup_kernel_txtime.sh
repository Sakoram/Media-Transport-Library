#!/bin/bash
# SPDX-License-Identifier: BSD-3-Clause
# Setup E830 kernel TX time (ETF qdisc with HW offload)
#
# Usage:
#   sudo ./setup_kernel_txtime.sh [interface] [ip/mask]
#   sudo ./setup_kernel_txtime.sh eth2 10.0.0.1/24
#
# This configures the ETF (Earliest TxTime First) qdisc with hardware
# offload on the specified interface. The ice driver will call
# ice_offload_txtime() which sets up the txtime queue on hardware.

set -e

IFACE="${1:-eth2}"
IP="${2:-10.0.0.1/24}"

echo "=== E830 Kernel TX Time Setup ==="
echo "Interface: $IFACE"
echo "IP:        $IP"
echo ""

# Check interface exists
if ! ip link show "$IFACE" &>/dev/null; then
	echo "ERROR: Interface $IFACE not found"
	exit 1
fi

# Check it's using ice driver
DRIVER=$(basename "$(readlink /sys/class/net/$IFACE/device/driver 2>/dev/null)" 2>/dev/null)
echo "Driver: $DRIVER"
if [ "$DRIVER" != "ice" ]; then
	echo "WARNING: $IFACE is not using ice driver (got: $DRIVER)"
fi

# Bring up interface and assign IP
echo ""
echo "--- Bringing up interface ---"
ip addr flush dev "$IFACE" 2>/dev/null || true
ip addr add "$IP" dev "$IFACE" 2>/dev/null || true
ip link set "$IFACE" up
sleep 1

# Remove existing qdisc
echo ""
echo "--- Configuring ETF qdisc ---"
tc qdisc del dev "$IFACE" root 2>/dev/null || true

# Try direct ETF qdisc first (simplest approach)
if tc qdisc add dev "$IFACE" root etf clockid CLOCK_TAI delta 500000 offload 2>/dev/null; then
	echo "ETF qdisc added (direct root)"
else
	echo "Direct ETF failed, trying with mqprio parent..."
	# Fallback: use mqprio as root, ETF as child
	tc qdisc add dev "$IFACE" root handle 100: mqprio \
		num_tc 1 map 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 \
		queues 1@0 hw 0
	tc qdisc add dev "$IFACE" parent 100:1 etf \
		clockid CLOCK_TAI delta 500000 offload
	echo "ETF qdisc added (under mqprio)"
fi

# Verify
echo ""
echo "--- Qdisc configuration ---"
tc qdisc show dev "$IFACE"

# Check dmesg for txtime messages
echo ""
echo "--- Recent dmesg (txtime/TxTime) ---"
dmesg | grep -i "txtime\|TxTime\|launch.time" | tail -10

echo ""
echo "=== Setup complete ==="
echo ""
echo "Run the test:"
echo "  gcc -O2 -Wall -o kernel_txtime_test kernel_txtime_test.c"
echo "  sudo ./kernel_txtime_test --iface $IFACE --dst-ip 10.0.0.2"
echo ""
echo "Capture on receiver:"
echo "  sudo tcpdump -i eth0 -w /tmp/kernel_txtime.pcap udp port 12345"
