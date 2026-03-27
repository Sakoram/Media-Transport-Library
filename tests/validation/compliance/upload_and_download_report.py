import argparse
import json
from pathlib import Path

from compliance_client import PcapComplianceClient


def parse_args():
    parser = argparse.ArgumentParser(
        description="Upload a PCAP to EBU LIST and save the JSON report."
    )
    parser.add_argument("--pcap", required=True, help="Path to the PCAP file.")
    parser.add_argument("--ip", required=True, help="EBU LIST server IP or hostname.")
    parser.add_argument("--user", required=True, help="EBU LIST username.")
    parser.add_argument("--password", required=True, help="EBU LIST password.")
    parser.add_argument(
        "--output",
        help="Path to write the JSON report. Defaults to <pcap>.json.",
    )
    parser.add_argument(
        "--proxy",
        help="Optional proxy URL used for http/https/ftp requests.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=60,
        help="How many 1-second polling attempts to wait for the report.",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Delete the uploaded PCAP from EBU LIST after downloading the report.",
    )
    return parser.parse_args()


def build_proxies(proxy):
    if not proxy:
        return None
    return {"http": proxy, "https": proxy, "ftp": proxy}


def resolve_output_path(pcap_path, output_path):
    if output_path:
        return Path(output_path)
    pcap = Path(pcap_path)
    return pcap.with_suffix(".json")


def main():
    args = parse_args()
    output_path = resolve_output_path(args.pcap, args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    client = PcapComplianceClient(
        ebu_ip=args.ip,
        user=args.user,
        password=args.password,
        pcap_file=args.pcap,
        proxies=build_proxies(args.proxy),
    )
    pcap_id = client.upload_pcap()
    report = client.download_report(retries=args.retries)
    if not report:
        raise RuntimeError(
            f"Report for PCAP {pcap_id} was not ready after {args.retries} attempts."
        )

    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")

    if args.delete:
        client.delete_pcap(pcap_id)

    print(f">>>UUID: {pcap_id}")
    print(f">>>JSON: {output_path}")


if __name__ == "__main__":
    main()