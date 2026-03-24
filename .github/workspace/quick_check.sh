#!/bin/bash
# Quick analysis of a pcap+json pair for ST 2110-21 compliance
# Usage: ./quick_check.sh <base_name>
# Expects: <base_name>.pcap and <base_name>.json in the same directory
#
# Example: ./quick_check.sh /home/labrat/mkasiew/dumps/mypcap-tsn-monday-cleaned

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE="${1:?Usage: $0 <base_name_without_extension>}"

echo "=== PCAP Analysis ==="
if [ -f "${BASE}.pcap" ]; then
    python3 "$SCRIPT_DIR/analyze_pcap.py" "${BASE}.pcap" 2>/dev/null | tail -20
else
    echo "No pcap found: ${BASE}.pcap"
fi

echo ""
echo "=== JSON Analysis ==="
if [ -f "${BASE}.json" ]; then
    python3 "$SCRIPT_DIR/analyze_json.py" "${BASE}.json"
else
    echo "No json found: ${BASE}.json"
fi
