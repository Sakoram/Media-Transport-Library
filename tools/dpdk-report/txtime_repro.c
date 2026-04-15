/* SPDX-License-Identifier: BSD-3-Clause
 * Copyright(c) 2024 Intel Corporation
 *
 * Minimal reproducer: E830 RTE_ETH_TX_OFFLOAD_SEND_ON_TIMESTAMP gives
 * opackets=0.  Packets are accepted by rte_eth_tx_burst() but the NIC
 * never transmits them.
 *
 * The same hardware works via the kernel ice driver (ETF qdisc + HW
 * offload), proving the NIC supports launch-time scheduling.
 *
 * Build:
 *   meson setup builddir && ninja -C builddir
 *
 * Run (E830 bound to vfio-pci):
 *   sudo ./builddir/txtime_repro -l 0-1 -a 0000:ca:00.0 --iova-mode=pa
 */

#define _DEFAULT_SOURCE
#include <inttypes.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#include <rte_cycles.h>
#include <rte_eal.h>
#include <rte_ethdev.h>
#include <rte_mbuf.h>
#include <rte_mbuf_dyn.h>

#define NUM_MBUFS  2048
#define MBUF_CACHE 256
#define TX_DESC    512
#define RX_DESC    128
#define NUM_PKTS   10
#define PKT_LEN    64

static int ts_offset = -1;
static uint64_t ts_mask;

static uint64_t read_phc(uint16_t port)
{
	struct timespec ts;

	rte_eth_timesync_read_time(port, &ts);
	return (uint64_t)ts.tv_sec * 1000000000ULL + ts.tv_nsec;
}

static int port_init(uint16_t port, struct rte_mempool *pool)
{
	struct rte_eth_dev_info info;
	struct rte_eth_conf conf;
	struct rte_eth_txconf txconf;
	int ret;

	memset(&conf, 0, sizeof(conf));

	ret = rte_eth_dev_info_get(port, &info);
	if (ret < 0)
		return ret;

	if (!(info.tx_offload_capa & RTE_ETH_TX_OFFLOAD_SEND_ON_TIMESTAMP)) {
		printf("Port %u: SEND_ON_TIMESTAMP not supported\n", port);
		return -1;
	}
	printf("Port %u: SEND_ON_TIMESTAMP supported (E830)\n", port);

	conf.txmode.offloads = RTE_ETH_TX_OFFLOAD_SEND_ON_TIMESTAMP;

	ret = rte_eth_dev_configure(port, 1, 1, &conf);
	if (ret < 0)
		return ret;

	ret = rte_eth_rx_queue_setup(port, 0, RX_DESC,
			rte_eth_dev_socket_id(port), NULL, pool);
	if (ret < 0)
		return ret;

	txconf = info.default_txconf;
	txconf.offloads = conf.txmode.offloads;
	ret = rte_eth_tx_queue_setup(port, 0, TX_DESC,
			rte_eth_dev_socket_id(port), &txconf);
	if (ret < 0)
		return ret;

	ret = rte_eth_dev_start(port);
	if (ret < 0)
		return ret;

	rte_eth_timesync_enable(port);

	/* Wait for PHC to produce a non-zero reading. */
	for (int i = 0; i < 50; i++) {
		if (read_phc(port) != 0)
			break;
		usleep(10000);
	}

	return 0;
}

/*
 * Test A — sanity: send packets WITHOUT the timestamp flag.
 * Proves the port can transmit at all.
 */
static int test_without_txtime(uint16_t port, struct rte_mempool *pool)
{
	struct rte_mbuf *pkts[3];
	struct rte_eth_stats st;

	printf("\n--- Test A: send 3 packets WITHOUT timestamp flag ---\n");
	rte_eth_stats_reset(port);

	for (int i = 0; i < 3; i++) {
		pkts[i] = rte_pktmbuf_alloc(pool);
		if (!pkts[i])
			return -1;
		memset(rte_pktmbuf_append(pkts[i], PKT_LEN), 0, PKT_LEN);
		/* Do NOT set ts_mask — regular TX path */
	}

	uint16_t sent = rte_eth_tx_burst(port, 0, pkts, 3);
	printf("tx_burst: %u / 3\n", sent);
	for (int i = sent; i < 3; i++)
		rte_pktmbuf_free(pkts[i]);

	sleep(2);
	rte_eth_stats_get(port, &st);
	printf("opackets: %" PRIu64 "\n", st.opackets);

	if (st.opackets == 0) {
		printf("FAIL: port cannot transmit at all\n");
		return -1;
	}
	printf("OK: port transmits without timestamp flag\n");
	return 0;
}

/*
 * Test B — the bug: send packets WITH the timestamp flag.
 * Sets launch_time = PHC + 10 ms (well within the 67 ms HW window).
 */
static int test_with_txtime(uint16_t port, struct rte_mempool *pool)
{
	struct rte_mbuf *pkts[NUM_PKTS];
	struct rte_eth_stats st;
	uint64_t phc, launch;

	printf("\n--- Test B: send %d packets WITH timestamp flag ---\n",
			NUM_PKTS);
	rte_eth_stats_reset(port);

	phc = read_phc(port);
	launch = ((phc + 10000000ULL) >> 7) << 7; /* PHC + 10 ms, 128 ns align */

	printf("PHC:         %" PRIu64 " ns\n", phc);
	printf("Launch time: %" PRIu64 " ns  (PHC + 10 ms)\n", launch);

	for (int i = 0; i < NUM_PKTS; i++) {
		pkts[i] = rte_pktmbuf_alloc(pool);
		if (!pkts[i])
			return -1;
		memset(rte_pktmbuf_append(pkts[i], PKT_LEN), 0, PKT_LEN);

		pkts[i]->ol_flags |= ts_mask;
		*RTE_MBUF_DYNFIELD(pkts[i], ts_offset, uint64_t *) =
				launch + (uint64_t)i * 1024;
	}

	uint16_t sent = rte_eth_tx_burst(port, 0, pkts, NUM_PKTS);
	printf("tx_burst: %u / %d\n", sent, NUM_PKTS);
	for (int i = sent; i < NUM_PKTS; i++)
		rte_pktmbuf_free(pkts[i]);

	for (int s = 1; s <= 5; s++) {
		sleep(1);
		rte_eth_stats_get(port, &st);
		printf("  +%ds: opackets=%" PRIu64 "  oerrors=%" PRIu64 "\n",
				s, st.opackets, st.oerrors);
	}

	rte_eth_stats_get(port, &st);
	return st.opackets > 0 ? 0 : -1;
}

int main(int argc, char *argv[])
{
	struct rte_mempool *pool;
	struct rte_eth_link link;
	uint16_t port = 0;
	int ret, result;

	ret = rte_eal_init(argc, argv);
	if (ret < 0)
		rte_exit(EXIT_FAILURE, "EAL init failed\n");

	if (rte_eth_dev_count_avail() == 0)
		rte_exit(EXIT_FAILURE, "No ports\n");

	ts_offset = rte_mbuf_dynfield_register(
		&(struct rte_mbuf_dynfield){
			.name = RTE_MBUF_DYNFIELD_TIMESTAMP_NAME,
			.size = sizeof(uint64_t),
			.align = __alignof__(uint64_t),
		});
	if (ts_offset < 0)
		rte_exit(EXIT_FAILURE, "dynfield register failed\n");

	int bit = rte_mbuf_dynflag_register(
		&(struct rte_mbuf_dynflag){
			.name = RTE_MBUF_DYNFLAG_TX_TIMESTAMP_NAME,
		});
	if (bit < 0)
		rte_exit(EXIT_FAILURE, "dynflag register failed\n");
	ts_mask = 1ULL << bit;

	pool = rte_pktmbuf_pool_create("POOL", NUM_MBUFS, MBUF_CACHE, 0,
			RTE_MBUF_DEFAULT_BUF_SIZE,
			rte_eth_dev_socket_id(port));
	if (!pool)
		rte_exit(EXIT_FAILURE, "pool create failed\n");

	ret = port_init(port, pool);
	if (ret < 0)
		rte_exit(EXIT_FAILURE, "port_init: %d\n", ret);

	printf("Waiting for link...\n");
	for (int i = 0; i < 30; i++) {
		ret = rte_eth_link_get_nowait(port, &link);
		(void)ret;
		if (link.link_status)
			break;
		sleep(1);
	}
	if (!link.link_status)
		rte_exit(EXIT_FAILURE, "Link never came up\n");
	printf("Link UP at %u Mbps\n\n", link.link_speed);
	sleep(1);

	/* Test A: sanity — prove port can TX */
	ret = test_without_txtime(port, pool);
	if (ret < 0)
		goto out;

	/* Test B: the bug — SEND_ON_TIMESTAMP gives opackets=0 */
	result = test_with_txtime(port, pool);

	printf("\n=== RESULT ===\n");
	if (result == 0)
		printf("PASS: launch-time packets transmitted\n");
	else
		printf("FAIL: opackets=0 — launch-time packets never "
		       "transmitted\n");

out:
	rte_eth_dev_stop(port);
	rte_eth_dev_close(port);
	rte_eal_cleanup();
	return result == 0 ? 0 : 1;
}
