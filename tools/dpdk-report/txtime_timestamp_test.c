/* SPDX-License-Identifier: BSD-3-Clause
 * Copyright(c) 2026 Intel Corporation
 *
 * Prove three claims about the E830 TXTIME engine:
 *
 *   CLAIM 1: Near-future timestamps (PHC + 1..50 ms) transmit successfully.
 *   CLAIM 2: Bad timestamps (dynfield=0, far-past) poison the queue --
 *            the queue stops transmitting ALL packets, even valid ones.
 *   CLAIM 3: A TX-queue stop/start cycle recovers the poisoned queue.
 *
 * Test sequence (each step depends on the previous):
 *
 *   Step A  -- baseline:      PHC+1ms   -> expect PASS   (proves TXTIME works)
 *   Step B  -- still works:   PHC+5ms   -> expect PASS   (queue still healthy)
 *   Step C  -- POISON:        dynfield=0-> expect FAIL   (bad ts poisons queue)
 *   Step D  -- stuck check:   PHC+1ms   -> expect FAIL   (queue is stuck)
 *   Step E  -- RESET queue    (tx_queue_stop + tx_queue_start)
 *   Step F  -- recovery:      PHC+1ms   -> expect PASS   (queue recovered)
 *   Step G  -- window sweep:  PHC+10/30/50/67/80 ms -> find the window edge
 *
 * Build:
 *   meson setup builddir && ninja -C builddir
 *
 * Run (E830 bound to vfio-pci):
 *   sudo ./builddir/txtime_timestamp_test -l 0-1 -a 0000:ca:00.0 --iova-mode=pa
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
#define PKT_LEN    64
#define BURST      5
#define WAIT_SEC   2

static int ts_offset = -1;
static uint64_t ts_mask;
static uint16_t g_port;
static struct rte_mempool *g_pool;

/* ------------------------------------------------------------------ */

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
if (ret < 0) return ret;

if (!(info.tx_offload_capa & RTE_ETH_TX_OFFLOAD_SEND_ON_TIMESTAMP)) {
printf("FATAL: Port %u does not support SEND_ON_TIMESTAMP\n", port);
return -1;
}

conf.txmode.offloads = RTE_ETH_TX_OFFLOAD_SEND_ON_TIMESTAMP;
ret = rte_eth_dev_configure(port, 1, 1, &conf);
if (ret < 0) return ret;

ret = rte_eth_rx_queue_setup(port, 0, RX_DESC,
rte_eth_dev_socket_id(port), NULL, pool);
if (ret < 0) return ret;

txconf = info.default_txconf;
txconf.offloads = conf.txmode.offloads;
ret = rte_eth_tx_queue_setup(port, 0, TX_DESC,
rte_eth_dev_socket_id(port), &txconf);
if (ret < 0) return ret;

ret = rte_eth_dev_start(port);
if (ret < 0) return ret;

rte_eth_timesync_enable(port);

/* Let PHC stabilize */
for (int i = 0; i < 100; i++) {
if (read_phc(port) > 1000000000ULL)
break;
usleep(50000);
}
return 0;
}

/* ------------------------------------------------------------------ */

/*
 * Send BURST packets with dynfield = ts_val.
 * Returns opackets delta after WAIT_SEC seconds.
 */
static uint64_t send_pkts(uint64_t ts_val, const char *label)
{
struct rte_mbuf *pkts[BURST];
struct rte_eth_stats s0, s1;
uint16_t sent;

rte_eth_stats_get(g_port, &s0);

for (int i = 0; i < BURST; i++) {
pkts[i] = rte_pktmbuf_alloc(g_pool);
if (!pkts[i]) {
printf("    alloc fail\n");
for (int j = 0; j < i; j++)
rte_pktmbuf_free(pkts[j]);
return 0;
}
memset(rte_pktmbuf_append(pkts[i], PKT_LEN), 0xAA, PKT_LEN);
pkts[i]->ol_flags |= ts_mask;
*RTE_MBUF_DYNFIELD(pkts[i], ts_offset, uint64_t *) =
ts_val + (uint64_t)i * 128;
}

sent = rte_eth_tx_burst(g_port, 0, pkts, BURST);
for (int i = sent; i < BURST; i++)
rte_pktmbuf_free(pkts[i]);

/* Poll opackets */
for (int s = 1; s <= WAIT_SEC; s++) {
sleep(1);
rte_eth_stats_get(g_port, &s1);
printf("    +%ds opkts=%"PRIu64" obytes=%"PRIu64" oerrs=%"PRIu64"\n",
       s, s1.opackets - s0.opackets, s1.obytes - s0.obytes, s1.oerrors - s0.oerrors);
}

rte_eth_stats_get(g_port, &s1);
uint64_t delta = s1.opackets - s0.opackets;

uint64_t phc_sub = read_phc(g_port) % 1000000000ULL;
uint64_t ts_sub  = ts_val % 1000000000ULL;
printf("    %s: ts_val=%" PRIu64 " sub_s=%"PRIu64
       "ns PHC_sub=%"PRIu64"ns  burst=%u delta=%" PRIu64 "\n",
       label, ts_val, ts_sub, phc_sub, sent, delta);

return delta;
}

static void reset_queue(void)
{
int r1 = rte_eth_dev_tx_queue_stop(g_port, 0);
int r2 = rte_eth_dev_tx_queue_start(g_port, 0);
rte_eth_stats_reset(g_port);
printf("    queue reset: stop=%d start=%d\n", r1, r2);
usleep(100000);
}

/* ------------------------------------------------------------------ */

static int check(const char *tag, uint64_t got, int expect_nonzero)
{
int ok = expect_nonzero ? (got >= BURST) : (got == 0);
printf("  >> %s: %" PRIu64 " packets -- %s (expected %s)\n\n",
       tag, got,
       ok ? "OK" : "**UNEXPECTED**",
       expect_nonzero ? ">= 5" : "0");
return ok ? 1 : 0;
}

int main(int argc, char *argv[])
{
struct rte_eth_link link;
int ret;
int passed = 0, total = 0;
uint64_t phc;

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

g_port = 0;
g_pool = rte_pktmbuf_pool_create("POOL", NUM_MBUFS, MBUF_CACHE, 0,
RTE_MBUF_DEFAULT_BUF_SIZE,
rte_eth_dev_socket_id(g_port));
if (!g_pool)
rte_exit(EXIT_FAILURE, "pool create failed\n");

ret = port_init(g_port, g_pool);
if (ret < 0)
rte_exit(EXIT_FAILURE, "port_init: %d\n", ret);

printf("Waiting for link...\n");
for (int i = 0; i < 30; i++) {
ret = rte_eth_link_get_nowait(g_port, &link);
(void)ret;
if (link.link_status) break;
sleep(1);
}
if (!link.link_status)
rte_exit(EXIT_FAILURE, "Link never came up\n");
printf("Link UP at %u Mbps\n", link.link_speed);

phc = read_phc(g_port);
printf("PHC = %" PRIu64 " ns  (sub-second = %" PRIu64 " ms)\n\n",
       phc, (phc % 1000000000ULL) / 1000000);

rte_eth_stats_reset(g_port);
sleep(1);

/* ============================================================ */
printf("============================================================\n");
printf("CLAIM 1: Valid near-future timestamps transmit successfully\n");
printf("============================================================\n\n");

printf("  Step A: PHC + 1 ms\n");
phc = read_phc(g_port);
total++; passed += check("Step A", send_pkts(phc + 1000000, "PHC+1ms"), 1);

printf("  Step B: PHC + 5 ms  (queue still healthy)\n");
phc = read_phc(g_port);
total++; passed += check("Step B", send_pkts(phc + 5000000, "PHC+5ms"), 1);

/* ============================================================ */
printf("============================================================\n");
printf("CLAIM 2: dynfield=0 poisons the queue\n");
printf("============================================================\n\n");

printf("  Step C: send dynfield=0  (far past -> should fail)\n");
total++; passed += check("Step C", send_pkts(0, "dynfield=0"), 0);

printf("  Step D: PHC + 1 ms  (valid ts, but queue should be stuck)\n");
phc = read_phc(g_port);
total++; passed += check("Step D", send_pkts(phc + 1000000, "PHC+1ms STUCK?"), 0);

/* ============================================================ */
printf("============================================================\n");
printf("CLAIM 3: TX queue reset recovers the poisoned queue\n");
printf("============================================================\n\n");

printf("  Step E: resetting TX queue 0\n");
reset_queue();

printf("  Step F: PHC + 1 ms  (after reset -> should work again)\n");
phc = read_phc(g_port);
total++; passed += check("Step F", send_pkts(phc + 1000000, "PHC+1ms RECOVERED"), 1);

/* ============================================================ */
printf("============================================================\n");
printf("BONUS: TXTIME scheduling window sweep\n");
printf("============================================================\n\n");

static const int64_t offsets_ms[] = {1, 5, 10, 20, 30, 50, 60, 67, 80, 100};
int n_offsets = (int)(sizeof(offsets_ms) / sizeof(offsets_ms[0]));

for (int i = 0; i < n_offsets; i++) {
/* Reset queue before each probe so a failure does not
 * cascade into the next test. */
reset_queue();
int64_t off_ns = offsets_ms[i] * 1000000LL;
phc = read_phc(g_port);
uint64_t ts_val = phc + off_ns;

printf("  PHC + %3" PRId64 " ms:\n", offsets_ms[i]);
uint64_t delta = send_pkts(ts_val, "window");
total++;
if (delta >= BURST) {
printf("  >> PHC+%" PRId64 "ms: TRANSMIT  (%"PRIu64" pkts)\n\n",
       offsets_ms[i], delta);
passed++;
} else {
printf("  >> PHC+%" PRId64 "ms: BLOCKED   (0 pkts)\n\n",
       offsets_ms[i]);
}
}

/* ============================================================ */
printf("============================================================\n");
printf("SUMMARY: %d / %d checks passed\n", passed, total);
printf("============================================================\n");
printf("\nExpected results if hypothesis is correct:\n");
printf("  Step A: PASS  (valid ts works)\n");
printf("  Step B: PASS  (still works)\n");
printf("  Step C: FAIL  (bad ts doesn't transmit)\n");
printf("  Step D: FAIL  (queue poisoned -- even valid ts blocked)\n");
printf("  Step F: PASS  (queue reset recovers it)\n");
printf("  Window: PASS up to ~67ms, then BLOCKED\n");

rte_eth_dev_stop(g_port);
rte_eth_dev_close(g_port);
rte_eal_cleanup();
return 0;
}
