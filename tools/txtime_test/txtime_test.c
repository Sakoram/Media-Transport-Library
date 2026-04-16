#define _POSIX_C_SOURCE 200809L
#define _DEFAULT_SOURCE
/* SPDX-License-Identifier: BSD-3-Clause
 * Copyright(c) 2024 Intel Corporation
 *
 * Minimal DPDK tool to test E830 TX launch time (txtime) behavior.
 * Runs a full test suite in a single invocation (avoids 20s link-up
 * wait per test).  Each packet payload is self-describing so the
 * capture side needs no clock synchronization.
 *
 * Usage:
 *   sudo ./txtime_test -a 0000:xx:00.0 [-- --dst-mac aa:bb:cc:dd:ee:ff]
 */

#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <inttypes.h>
#include <rte_common.h>
#include <rte_cycles.h>
#include <rte_eal.h>
#include <rte_ethdev.h>
#include <rte_ether.h>
#include <rte_ip.h>
#include <rte_launch.h>
#include <rte_lcore.h>
#include <rte_malloc.h>
#include <rte_mbuf.h>
#include <rte_mbuf_dyn.h>
#include <rte_udp.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#define MEMPOOL_SIZE 2048
#define MEMPOOL_CACHE 256
#define TX_DESC 512
#define UDP_SRC_PORT 54321
#define UDP_DST_PORT 12345
#define PKT_MAGIC 0x54585431 /* "TXT1" */

/* Self-describing payload embedded in every packet. */
struct txtime_payload {
  uint32_t magic;
  uint32_t seq;     /* seq within this test */
  uint32_t test_id; /* which test case */
  uint32_t pad;
  uint64_t phc_at_send; /* PHC reading before burst (ns) */
  int64_t offset_ns;    /* offset applied to this packet  */
  uint64_t launch_time; /* phc_at_send + offset_ns        */
};

/* --- globals ------------------------------------------------------- */
static int ts_dynfield_offset = -1;
static uint64_t ts_dynflag_mask;
static uint16_t g_port;
static struct rte_mempool* g_pool;
/* Default dst MAC = eth0 (E810 port 0, physically looped to E830 port 0) */
static struct rte_ether_addr g_dst_mac = {
    .addr_bytes = {0xb4, 0x96, 0x91, 0xae, 0x67, 0x00}};
static bool g_dst_mac_set = true;

/* Stream test parameters */
static uint32_t g_stream_count = 100;
static uint32_t g_stream_step_ns = 7936; /* 62 * 128ns ~ 8us */
static uint32_t g_stream_offset_ms = 5;
static uint32_t g_stream_bulk = 4;

/* BAR0 mmap for direct register access */
static volatile uint32_t* g_bar0 = NULL;

static uint32_t bar_rd32(uint32_t reg) {
  if (!g_bar0) return 0xdeadbeef;
  return g_bar0[reg / 4];
}

static void bar_wr32(uint32_t reg, uint32_t val) {
  if (!g_bar0) return;
  g_bar0[reg / 4] = val;
  __sync_synchronize();
}

/* E830 TXTIME register addresses */
#define E830_GLTXTIME_TS_CFG 0x002D3100
#define E830_GLTXTIME_QTX_CNTX_CTL 0x002D3204
#define E830_GLTXTIME_QTX_CNTX_DATA(i) (0x002D3104 + (i)*4)
#define E830_GLTXTIME_QTX_CNTX_STAT 0x002D3208
#define E830_GLTXTIME_FETCH_PROFILE_0_0 0x002D3500
#define E830_GLTXTIME_DBL_COMP_WRR_CREDITS 0x002D320C
#define E830_GLTXTIME_DBL_COMP_WRR_WEIGHTS 0x002D3210
#define E830_GLTXTIME_OUTST_REQ_CNTL 0x002D3214
#define E830_GL_MDET_TX_PQM_REG 0x002D2E00
#define E830_GL_MDET_TX_PQM_FIFO_REG 0x002D4B00
#define E830_QTX_COMM_HEAD(q) (0x000E4000 + (q)*4)
#define E830_GLTSYN_TIME_L(t) (0x000888D0 + (t)*4)
#define E830_GLTSYN_TIME_H(t) (0x000888D4 + (t)*4)

static void dump_txtime_context(int queue_idx) {
  /* Latch context: CMD_EXEC=1 (bit 19), CMD=0 (read), QUEUE_ID */
  bar_wr32(E830_GLTXTIME_QTX_CNTX_CTL, (queue_idx & 0x7FF) | (1 << 19));
  usleep(100);
  uint32_t stat = bar_rd32(E830_GLTXTIME_QTX_CNTX_STAT);
  if (stat & 1) usleep(200);

  uint32_t d[7];
  for (int i = 0; i < 7; i++) d[i] = bar_rd32(E830_GLTXTIME_QTX_CNTX_DATA(i));

  uint32_t txtime_ena = (d[3] >> 9) & 1;
  uint32_t drbell32 = (d[3] >> 10) & 1;
  uint32_t ts_res = (d[3] >> 11) & 0xF;
  uint32_t ts_round = (d[3] >> 15) & 0x3;
  uint32_t ts_pacing_slot = (d[3] >> 17) & 0x7;
  uint32_t merging_ena = (d[3] >> 20) & 1;
  uint32_t ts_fetch_prof = (d[3] >> 21) & 0xF;

  printf("  TXTIME ctx[q=%d]: data={0x%08x 0x%08x 0x%08x 0x%08x 0x%08x 0x%08x 0x%08x}\n",
         queue_idx, d[0], d[1], d[2], d[3], d[4], d[5], d[6]);
  printf(
      "    txtime_ena=%u drbell32=%u ts_res=%u ts_round=%u pacing_slot=%u merging=%u "
      "fetch_prof=%u\n",
      txtime_ena, drbell32, ts_res, ts_round, ts_pacing_slot, merging_ena, ts_fetch_prof);
  printf("    int_q_state: d3[31:30]=0x%x d4=0x%08x d5=0x%08x d6[5:0]=0x%x\n",
         (d[3] >> 30) & 3, d[4], d[5], d[6] & 0x3F);
}

static void dump_txtime_regs(const char* label) {
  if (!g_bar0) {
    printf("BAR0 not mapped, skipping register dump\n");
    return;
  }

  printf("\n=== TXTIME HW Register Dump: %s ===\n", label);

  /* Global TXTIME config */
  uint32_t ts_cfg = bar_rd32(E830_GLTXTIME_TS_CFG);
  printf(
      "E830_GLTXTIME_TS_CFG      = 0x%08x (ENABLE=%u STORAGE_MODE=%u PIPE_LATENCY=%u)\n",
      ts_cfg, ts_cfg & 1, (ts_cfg >> 2) & 7, (ts_cfg >> 5) & 0x1FFF);

  /* Fetch profile */
  uint32_t fp = bar_rd32(E830_GLTXTIME_FETCH_PROFILE_0_0);
  printf("FETCH_PROFILE[0,0]        = 0x%08x (FETCH_TS_DESC=%u FIFO_THRESH=%u)\n", fp,
         fp & 0x1FF, (fp >> 9) & 0x7F);

  /* WRR */
  uint32_t cred = bar_rd32(E830_GLTXTIME_DBL_COMP_WRR_CREDITS);
  uint32_t wt = bar_rd32(E830_GLTXTIME_DBL_COMP_WRR_WEIGHTS);
  printf("WRR_CREDITS               = 0x%08x (DBL=%u COMP=%u)\n", cred, cred & 0xFF,
         (cred >> 8) & 0xFF);
  printf("WRR_WEIGHTS               = 0x%08x (DBL=%u COMP=%u)\n", wt, wt & 0x3F,
         (wt >> 6) & 0x3F);

  /* Outstanding requests */
  uint32_t outst = bar_rd32(E830_GLTXTIME_OUTST_REQ_CNTL);
  printf("OUTST_REQ_CNTL            = 0x%08x (THRESHOLD=%u SNAPSHOT=%u)\n", outst,
         outst & 0x3FF, (outst >> 10) & 0x3FF);

  /* MDD */
  uint32_t mdet = bar_rd32(E830_GL_MDET_TX_PQM_REG);
  uint32_t fifo = bar_rd32(E830_GL_MDET_TX_PQM_FIFO_REG);
  printf("GL_MDET_TX_PQM            = 0x%08x (VALID=%u)\n", mdet, (mdet >> 31) & 1);
  printf("GL_MDET_TX_PQM_FIFO       = 0x%08x (MAL_TYPE=%u VALID=%u EVT_CNT=%u)\n", fifo,
         (fifo >> 15) & 0x1F, (fifo >> 21) & 1, (fifo >> 24) & 0xFF);

  /* Per-queue contexts for q=0,1,2,3 */
  for (int q = 0; q < 4; q++) {
    dump_txtime_context(q);
  }

  /* QTX_COMM_HEAD for q=0,1 */
  for (int q = 0; q < 2; q++) {
    uint32_t head = bar_rd32(E830_QTX_COMM_HEAD(q));
    printf("  QTX_COMM_HEAD[%d]        = 0x%08x\n", q, head);
  }

  printf("=== End Register Dump ===\n\n");
}

/* ------------------------------------------------------------------ */
static uint64_t timespec_to_ns(const struct timespec* ts) {
  return (uint64_t)ts->tv_sec * 1000000000ULL + (uint64_t)ts->tv_nsec;
}

static int parse_mac(const char* str, struct rte_ether_addr* addr) {
  unsigned int b[6];
  if (sscanf(str, "%x:%x:%x:%x:%x:%x", &b[0], &b[1], &b[2], &b[3], &b[4], &b[5]) != 6)
    return -1;
  for (int i = 0; i < 6; i++) addr->addr_bytes[i] = (uint8_t)b[i];
  return 0;
}

static int parse_app_args(int argc, char** argv) {
  static struct option long_opts[] = {{"dst-mac", required_argument, NULL, 'd'},
                                      {"stream-count", required_argument, NULL, 'c'},
                                      {"stream-step-ns", required_argument, NULL, 's'},
                                      {"stream-offset-ms", required_argument, NULL, 'o'},
                                      {"stream-bulk", required_argument, NULL, 'b'},
                                      {NULL, 0, NULL, 0}};
  int opt;
  optind = 0;
  while ((opt = getopt_long(argc, argv, "d:c:s:o:b:", long_opts, NULL)) != -1) {
    switch (opt) {
      case 'd':
        if (parse_mac(optarg, &g_dst_mac) < 0) {
          fprintf(stderr, "bad MAC: %s\n", optarg);
          return -1;
        }
        g_dst_mac_set = true;
        break;
      case 'c':
        g_stream_count = (uint32_t)atoi(optarg);
        break;
      case 's':
        g_stream_step_ns = (uint32_t)atoi(optarg);
        break;
      case 'o':
        g_stream_offset_ms = (uint32_t)atoi(optarg);
        break;
      case 'b':
        g_stream_bulk = (uint32_t)atoi(optarg);
        break;
    }
  }
  return 0;
}

/* ------------------------------------------------------------------ */
static int port_init(uint16_t port, struct rte_mempool* pool) {
  struct rte_eth_conf port_conf;
  struct rte_eth_dev_info dev_info;
  int ret;

  memset(&port_conf, 0, sizeof(port_conf));
  ret = rte_eth_dev_info_get(port, &dev_info);
  if (ret < 0) return ret;

  if (!(dev_info.tx_offload_capa & RTE_ETH_TX_OFFLOAD_SEND_ON_TIMESTAMP)) {
    fprintf(stderr, "Port %u: no SEND_ON_TIMESTAMP — not E830?\n", port);
    return -ENOTSUP;
  }

  port_conf.txmode.offloads = RTE_ETH_TX_OFFLOAD_SEND_ON_TIMESTAMP;
  port_conf.rxmode.offloads = RTE_ETH_RX_OFFLOAD_TIMESTAMP;
  ret = rte_eth_dev_configure(port, 1 /* need >=1 rxq */, 1, &port_conf);
  if (ret < 0) return ret;

  /* RX queue (required for port to function) */
  ret = rte_eth_rx_queue_setup(port, 0, 128, rte_eth_dev_socket_id(port), NULL, pool);
  if (ret < 0) {
    fprintf(stderr, "rx_queue_setup failed: %d\n", ret);
    return ret;
  }

  struct rte_eth_txconf txconf = dev_info.default_txconf;
  txconf.offloads = port_conf.txmode.offloads;
  ret = rte_eth_tx_queue_setup(port, 0, TX_DESC, rte_eth_dev_socket_id(port), &txconf);
  if (ret < 0) return ret;

  ret = rte_eth_dev_start(port);
  if (ret < 0) return ret;

  /* Full stop→start cycle to replicate kernel's VSI close→rebuild→open.
   * The kernel disables all queues (AQ 0x0C31) before re-creating them
   * (AQ 0x0C30 + 0x0C35). Without this, FW may have stale queue state
   * that prevents the TXTIME engine from triggering data DMA.
   */
  printf("=== Performing stop→start cycle (kernel VSI rebuild equivalent) ===\n");
  ret = rte_eth_dev_stop(port);
  printf("dev_stop ret=%d\n", ret);
  ret = rte_eth_dev_start(port);
  printf("dev_start ret=%d\n", ret);
  if (ret < 0) return ret;

  /* PHC init with retry — matches MTL dev_start_timesync() pattern.
   * The PHC may need a few attempts before returning valid (non-zero) time. */
  {
    int max_retry = 100;
    int i;
    for (i = 0; i < max_retry; i++) {
      ret = rte_eth_timesync_enable(port);
      if (ret < 0) {
        fprintf(stderr, "WARNING: timesync_enable failed: %d\n", ret);
        break;
      }
      struct timespec ts;
      ret = rte_eth_timesync_read_time(port, &ts);
      if (ret < 0) {
        fprintf(stderr, "WARNING: timesync_read_time failed: %d\n", ret);
        break;
      }
      if (ts.tv_sec || ts.tv_nsec) {
        printf("PHC validated: %ld.%09ld (retry %d)\n", (long)ts.tv_sec, (long)ts.tv_nsec,
               i);
        break;
      }
      usleep(10000); /* 10ms between retries */
    }
    if (i >= max_retry)
      fprintf(stderr, "WARNING: PHC still reads 0 after %d retries\n", max_retry);
  }

  rte_eth_promiscuous_enable(port);
  return 0;
}

/* ------------------------------------------------------------------ */
static uint64_t read_phc(void) {
  struct timespec ts;
  rte_eth_timesync_read_time(g_port, &ts);
  return timespec_to_ns(&ts);
}

/* ------------------------------------------------------------------ */
static struct rte_mbuf* build_pkt(uint32_t test_id, uint32_t seq, uint64_t phc_ns,
                                  int64_t offset) {
  struct rte_mbuf* m = rte_pktmbuf_alloc(g_pool);
  if (!m) return NULL;

  uint16_t payload_len = sizeof(struct txtime_payload);
  uint16_t pkt_len = sizeof(struct rte_ether_hdr) + sizeof(struct rte_ipv4_hdr) +
                     sizeof(struct rte_udp_hdr) + payload_len;

  char* p = rte_pktmbuf_append(m, pkt_len);
  if (!p) {
    rte_pktmbuf_free(m);
    return NULL;
  }

  /* Ethernet */
  struct rte_ether_hdr* eth = (struct rte_ether_hdr*)p;
  if (g_dst_mac_set)
    rte_ether_addr_copy(&g_dst_mac, &eth->dst_addr);
  else
    memset(&eth->dst_addr, 0xff, sizeof(eth->dst_addr));
  rte_eth_macaddr_get(g_port, &eth->src_addr);
  eth->ether_type = rte_cpu_to_be_16(RTE_ETHER_TYPE_IPV4);

  /* IPv4 */
  struct rte_ipv4_hdr* ip = (struct rte_ipv4_hdr*)(p + sizeof(struct rte_ether_hdr));
  memset(ip, 0, sizeof(*ip));
  ip->version_ihl = 0x45;
  ip->total_length = rte_cpu_to_be_16(pkt_len - sizeof(struct rte_ether_hdr));
  ip->time_to_live = 64;
  ip->next_proto_id = IPPROTO_UDP;
  ip->src_addr = rte_cpu_to_be_32(0x0a000001);
  ip->dst_addr = rte_cpu_to_be_32(0x0a000002);
  ip->hdr_checksum = 0;
  ip->hdr_checksum = rte_ipv4_cksum(ip);

  /* UDP */
  struct rte_udp_hdr* udp =
      (struct rte_udp_hdr*)((char*)ip + sizeof(struct rte_ipv4_hdr));
  udp->src_port = rte_cpu_to_be_16(UDP_SRC_PORT);
  udp->dst_port = rte_cpu_to_be_16(UDP_DST_PORT);
  udp->dgram_len = rte_cpu_to_be_16(sizeof(struct rte_udp_hdr) + payload_len);
  udp->dgram_cksum = 0;

  /* Payload */
  uint64_t launch = (uint64_t)((int64_t)phc_ns + offset);
  struct txtime_payload* pl =
      (struct txtime_payload*)((char*)udp + sizeof(struct rte_udp_hdr));
  pl->magic = rte_cpu_to_be_32(PKT_MAGIC);
  pl->seq = rte_cpu_to_be_32(seq);
  pl->test_id = rte_cpu_to_be_32(test_id);
  pl->pad = 0;
  pl->phc_at_send = rte_cpu_to_be_64(phc_ns);
  pl->offset_ns = rte_cpu_to_be_64((uint64_t)offset);
  pl->launch_time = rte_cpu_to_be_64(launch);

  /* Set TX timestamp dynfield */
  m->ol_flags |= ts_dynflag_mask;
  *RTE_MBUF_DYNFIELD(m, ts_dynfield_offset, uint64_t*) = launch;

  return m;
}

/* ------------------------------------------------------------------ */
/* Run one test case. */
static void run_test(uint32_t test_id, const char* name, uint16_t count,
                     const int64_t* offsets) {
  printf("\n");
  printf("========================================\n");
  printf("TEST %u: %s\n", test_id, name);
  printf("========================================\n");

  /* Reset stats baseline */
  rte_eth_stats_reset(g_port);

  uint64_t phc_ns = read_phc();
  printf("PHC now       : %" PRIu64 " ns  (%u.%09u)\n", phc_ns,
         (unsigned)(phc_ns / 1000000000ULL), (unsigned)(phc_ns % 1000000000ULL));

  /* Build packets */
  struct rte_mbuf* pkts[512];
  for (uint16_t i = 0; i < count; i++) {
    pkts[i] = build_pkt(test_id, i, phc_ns, offsets[i]);
    if (!pkts[i]) {
      fprintf(stderr, "  build_pkt failed at %u\n", i);
      return;
    }
    uint64_t lt = (uint64_t)((int64_t)phc_ns + offsets[i]);
    printf("  pkt[%2u] off=%+14" PRId64 "  launch_nsec=%09u  >>7=%u\n", i, offsets[i],
           (unsigned)(lt % 1000000000ULL), (unsigned)((lt % 1000000000ULL) >> 7));
  }

  /* Send */
  struct timespec wb, wa;
  clock_gettime(CLOCK_MONOTONIC, &wb);
  uint16_t sent = rte_eth_tx_burst(g_port, 0, pkts, count);
  clock_gettime(CLOCK_MONOTONIC, &wa);
  uint64_t elapsed_us = (timespec_to_ns(&wa) - timespec_to_ns(&wb)) / 1000;

  printf("tx_burst      : %u / %u  (wall %" PRIu64 " us)\n", sent, count, elapsed_us);

  for (uint16_t i = sent; i < count; i++) rte_pktmbuf_free(pkts[i]);

  /* Wait 3s, print stats each second */
  for (int s = 1; s <= 3; s++) {
    sleep(1);
    struct rte_eth_stats st;
    rte_eth_stats_get(g_port, &st);
    uint64_t phc = read_phc();
    printf("  +%ds: opkts=%" PRIu64 "  obytes=%" PRIu64 "  oerr=%" PRIu64
           "  PHC=%u.%09u\n",
           s, st.opackets, st.obytes, st.oerrors, (unsigned)(phc / 1000000000ULL),
           (unsigned)(phc % 1000000000ULL));
  }

  /* Recovery: 1 pkt at +1ms */
  phc_ns = read_phc();
  struct rte_mbuf* rec = build_pkt(test_id, 0xFFFF, phc_ns, 1000000);
  if (rec) {
    uint16_t rs = rte_eth_tx_burst(g_port, 0, &rec, 1);
    if (rs == 0) rte_pktmbuf_free(rec);
    usleep(200000);
    struct rte_eth_stats st;
    rte_eth_stats_get(g_port, &st);
    printf("Recovery      : sent=%u  opkts_total=%" PRIu64 "  oerr=%" PRIu64 "\n", rs,
           st.opackets, st.oerrors);
  }
}

/* ------------------------------------------------------------------ */
static void fill_offsets(int64_t* arr, uint16_t n, int64_t val) {
  for (uint16_t i = 0; i < n; i++) arr[i] = val;
}

static void fill_alternating(int64_t* arr, uint16_t n, int64_t even_val,
                             int64_t odd_val) {
  for (uint16_t i = 0; i < n; i++) arr[i] = (i & 1) ? odd_val : even_val;
}

/* Align nanosecond value UP to 128ns boundary (HW timestamp resolution). */
static inline uint64_t align128(uint64_t ns) {
  return ((ns + 127) >> 7) << 7;
}

static uint64_t mono_ns(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return timespec_to_ns(&ts);
}

/* ------------------------------------------------------------------ */
/* Test 11: MTL-style stream — per-packet timestamps, TSC-gated bulk
 * submission, TS-only doorbell.  Replicates what MTL does for video TX.
 */
static void run_stream_test(void) {
  uint32_t count = g_stream_count;
  uint32_t step_ns = g_stream_step_ns;
  uint32_t offset_ns = g_stream_offset_ms * 1000000u;
  uint32_t bulk = g_stream_bulk;

  if (count > MEMPOOL_SIZE - 64) {
    fprintf(stderr, "stream-count %u too large for mempool (%d)\n", count, MEMPOOL_SIZE);
    return;
  }
  if (bulk == 0) bulk = 1;

  printf("\n");
  printf("========================================\n");
  printf("TEST 11: MTL-style stream (TS-only doorbell)\n");
  printf("  count=%u  step=%uns  offset=%ums  bulk=%u\n", count, step_ns,
         g_stream_offset_ms, bulk);
  printf("  total span: %u us\n", (count * step_ns) / 1000);
  printf("========================================\n");

  rte_eth_stats_reset(g_port);

  /* Read PHC and compute all launch times (128ns aligned) */
  uint64_t phc_base = read_phc();
  uint64_t first_launch = align128(phc_base + offset_ns);

  printf("PHC base      : %" PRIu64 " ns  (%u.%09u)\n", phc_base,
         (unsigned)(phc_base / 1000000000ULL), (unsigned)(phc_base % 1000000000ULL));
  printf("First launch  : %" PRIu64 " ns  (PHC + %u ms)\n", first_launch,
         g_stream_offset_ms);
  printf("Last launch   : %" PRIu64 " ns  (PHC + %u ms + %u us)\n",
         first_launch + (uint64_t)(count - 1) * step_ns, g_stream_offset_ms,
         ((count - 1) * step_ns) / 1000);

  /* Pre-build all packets */
  struct rte_mbuf** pkts = malloc(sizeof(struct rte_mbuf*) * count);
  if (!pkts) {
    fprintf(stderr, "malloc failed\n");
    return;
  }

  for (uint32_t i = 0; i < count; i++) {
    uint64_t launch = first_launch + (uint64_t)i * step_ns;
    int64_t offset = (int64_t)(launch - phc_base);
    pkts[i] = build_pkt(11, i, phc_base, offset);
    if (!pkts[i]) {
      fprintf(stderr, "  build_pkt failed at %u\n", i);
      for (uint32_t j = 0; j < i; j++) rte_pktmbuf_free(pkts[j]);
      free(pkts);
      return;
    }
  }

  /* Capture wall-clock baseline to correlate with PHC */
  uint64_t wall_base = mono_ns();
  uint64_t phc_at_wall_base = read_phc();

  printf("\nSubmitting %u packets in bulks of %u (TSC-gated)...\n", count, bulk);

  uint32_t total_sent = 0;
  uint32_t burst_zero_count = 0;
  uint32_t stall_events = 0;
  bool in_stall = false;
  uint64_t stall_start = 0;
  uint64_t worst_stall_ns = 0;

  for (uint32_t base = 0; base < count; base += bulk) {
    uint32_t this_bulk = count - base;
    if (this_bulk > bulk) this_bulk = bulk;

    /* TSC gating: wait until ~500us before this bulk's first launch time.
     * Convert PHC target to wall clock using the PHC-wall offset. */
    uint64_t target_launch = first_launch + (uint64_t)base * step_ns;
    int64_t phc_ahead = (int64_t)(target_launch - phc_at_wall_base);
    uint64_t target_wall;
    if (phc_ahead > 500000)
      target_wall = wall_base + (uint64_t)(phc_ahead - 500000);
    else
      target_wall = wall_base; /* already past gate time */

    /* Busy-wait until gate time */
    uint64_t now;
    while ((now = mono_ns()) < target_wall) {
      /* spin */
    }

    /* Submit bulk */
    uint16_t sent = rte_eth_tx_burst(g_port, 0, &pkts[base], this_bulk);
    total_sent += sent;

    if (sent == 0) {
      burst_zero_count++;
      if (!in_stall) {
        in_stall = true;
        stall_start = now;
        stall_events++;
      }
    } else {
      if (in_stall) {
        uint64_t d = now - stall_start;
        if (d > worst_stall_ns) worst_stall_ns = d;
        printf("  STALL ended: pkt[%u] after %" PRIu64 " us\n", base, d / 1000);
        in_stall = false;
      }
    }

    /* Free unsent packets */
    for (uint16_t i = sent; i < this_bulk; i++) rte_pktmbuf_free(pkts[base + i]);

    /* Per-bulk log (every 10th bulk or first/last) */
    if (base == 0 || base + bulk >= count || (base / bulk) % 10 == 0) {
      uint64_t phc_now = read_phc();
      int64_t delta_phc = (int64_t)target_launch - (int64_t)phc_now;
      printf("  bulk[%3u..%3u] sent=%u/%u  PHC_delta=%+" PRId64 " us  wall=%" PRIu64
             " us\n",
             base, base + this_bulk - 1, sent, this_bulk, delta_phc / 1000,
             (now - wall_base) / 1000);
    }
  }

  if (in_stall) {
    uint64_t d = mono_ns() - stall_start;
    if (d > worst_stall_ns) worst_stall_ns = d;
    printf("  STALL ongoing at end: %" PRIu64 " us\n", d / 1000);
  }

  printf("\nStream submission complete.\n");
  printf("  total_sent (accepted by tx_burst) : %u / %u\n", total_sent, count);
  printf("  burst_zero_count                  : %u\n", burst_zero_count);
  printf("  stall_events                      : %u\n", stall_events);
  printf("  worst_stall                       : %" PRIu64 " us\n", worst_stall_ns / 1000);

  /* Wait for HW to transmit, print stats each second */
  printf("\nWaiting for HW transmission...\n");
  for (int s = 1; s <= 5; s++) {
    sleep(1);
    struct rte_eth_stats st;
    rte_eth_stats_get(g_port, &st);
    uint64_t phc = read_phc();
    printf("  +%ds: opkts=%" PRIu64 "  obytes=%" PRIu64 "  oerr=%" PRIu64
           "  PHC=%u.%09u\n",
           s, st.opackets, st.obytes, st.oerrors, (unsigned)(phc / 1000000000ULL),
           (unsigned)(phc % 1000000000ULL));
  }

  struct rte_eth_stats final_st;
  rte_eth_stats_get(g_port, &final_st);
  printf("\nFINAL: opkts=%" PRIu64 "  obytes=%" PRIu64 "  oerr=%" PRIu64 "\n",
         final_st.opackets, final_st.obytes, final_st.oerrors);
  if (final_st.opackets > 0)
    printf(">>> TX TIME SCHEDULING IS ACTIVE — packets transmitted! <<<\n");
  else
    printf(">>> opackets=0 — TX time scheduling did not engage <<<\n");

  free(pkts);
}

/* ------------------------------------------------------------------ */
/* Test 12: Hold-time test — definitively prove whether TXTIME engine
 * holds packets until launch time.
 *
 * Sends 1 packet at PHC+50ms, then polls opackets every 100µs.
 * If HW scheduling is active: opackets stays 0 for ~50ms, then goes to 1.
 * If HW scheduling is NOT active: opackets goes to 1 within <1ms.
 */
static void run_hold_test(void) {
  printf("\n");
  printf("========================================\n");
  printf("TEST 12: Hold-time test (+50ms, poll opackets)\n");
  printf("========================================\n");

  rte_eth_stats_reset(g_port);

  uint64_t phc_at_send = read_phc();
  int64_t offset_ns = 50000000LL; /* +50ms */
  uint64_t launch = align128(phc_at_send + offset_ns);

  printf("PHC at send   : %u.%09u\n", (unsigned)(phc_at_send / 1000000000ULL),
         (unsigned)(phc_at_send % 1000000000ULL));
  printf("Launch time   : %u.%09u (+50ms)\n", (unsigned)(launch / 1000000000ULL),
         (unsigned)(launch % 1000000000ULL));
  uint32_t tstamp_19 = (uint32_t)((launch % 1000000000ULL) >> 7) & 0x7FFFF;
  printf("19-bit tstamp : %u (0x%05x)\n", tstamp_19, tstamp_19);

  struct rte_mbuf* pkt = build_pkt(12, 0, phc_at_send, offset_ns);
  if (!pkt) {
    printf("build_pkt failed\n");
    return;
  }

  uint16_t sent = rte_eth_tx_burst(g_port, 0, &pkt, 1);
  uint64_t submit_wall = mono_ns();
  printf("tx_burst      : %u / 1\n", sent);
  if (sent == 0) {
    printf("  tx_burst returned 0 — aborting hold test\n");
    rte_pktmbuf_free(pkt);
    return;
  }

  /* Poll opackets every 100µs for 200ms */
  printf("\nPolling opackets (100us interval, 200ms window):\n");
  printf("  %8s  %8s  %8s  %s\n", "wall_us", "opkts", "PHC_delta", "status");

  uint64_t first_tx_wall = 0;
  int logged = 0;
  for (int i = 0; i < 2000; i++) {
    /* busy-wait 100µs */
    uint64_t target = submit_wall + (uint64_t)(i + 1) * 100000ULL;
    while (mono_ns() < target) { /* spin */
    }

    struct rte_eth_stats st;
    rte_eth_stats_get(g_port, &st);
    uint64_t wall_elapsed_us = (mono_ns() - submit_wall) / 1000;
    uint64_t phc_now = read_phc();
    int64_t phc_delta_us = ((int64_t)phc_now - (int64_t)launch) / 1000;

    /* Log at key intervals or when opackets first changes */
    bool should_log = (i < 5) || (i % 100 == 0) || (i == 1999) ||
                      (st.opackets > 0 && first_tx_wall == 0);

    if (st.opackets > 0 && first_tx_wall == 0) {
      first_tx_wall = wall_elapsed_us;
    }

    if (should_log && logged < 40) {
      const char* status = "";
      if (st.opackets > 0 && first_tx_wall == wall_elapsed_us)
        status = " <<<< TRANSMITTED";
      printf("  %8" PRIu64 "  %8" PRIu64 "  %+8" PRId64 "  %s\n", wall_elapsed_us,
             st.opackets, phc_delta_us, status);
      logged++;
    }
  }

  printf("\n--- Hold Test Result ---\n");
  if (first_tx_wall == 0) {
    printf("  opackets never incremented in 200ms — packet stuck!\n");
  } else if (first_tx_wall < 1000) {
    printf("  Transmitted at %" PRIu64 " us — NO HOLD (immediate TX)\n", first_tx_wall);
    printf("  Expected: ~50000 us if HW scheduling active\n");
    printf("  Verdict: SCHEDULING NOT ACTIVE\n");
  } else if (first_tx_wall >= 40000 && first_tx_wall <= 60000) {
    printf("  Transmitted at %" PRIu64 " us — HELD ~50ms!\n", first_tx_wall);
    printf("  Verdict: HW SCHEDULING ACTIVE\n");
  } else {
    printf("  Transmitted at %" PRIu64 " us — unexpected timing\n", first_tx_wall);
  }

  /* Recovery packet to keep queue clean */
  struct rte_mbuf* recov = build_pkt(12, 0xFFFF, read_phc(), 1000000LL);
  if (recov) {
    rte_eth_tx_burst(g_port, 0, &recov, 1);
  }
  rte_delay_ms(1000);
}

/* ------------------------------------------------------------------ */
int main(int argc, char** argv) {
  int ret;

  /* Force PA IOVA mode — VA mode causes DMAR faults on this platform. */
  int new_argc = argc + 1;
  char** new_argv = malloc(sizeof(char*) * (new_argc + 1));
  if (!new_argv) {
    fprintf(stderr, "malloc failed\n");
    return 1;
  }
  new_argv[0] = argv[0];
  new_argv[1] = "--iova-mode=pa";
  for (int i = 1; i < argc; i++) new_argv[i + 1] = argv[i];
  new_argv[new_argc] = NULL;

  ret = rte_eal_init(new_argc, new_argv);
  free(new_argv);
  if (ret < 0) rte_exit(EXIT_FAILURE, "EAL init failed\n");
  /* ret counts args consumed from new_argv which has 1 extra (--iova-mode=pa).
   * Adjust to skip only the original EAL args in argv. */
  argc -= (ret - 1);
  argv += (ret - 1);

  if (parse_app_args(argc, argv) < 0) rte_exit(EXIT_FAILURE, "Bad arguments\n");

  /* Register dynfield/dynflag */
  {
    int off = rte_mbuf_dynfield_register(&(struct rte_mbuf_dynfield){
        .name = RTE_MBUF_DYNFIELD_TIMESTAMP_NAME,
        .size = sizeof(uint64_t),
        .align = __alignof__(uint64_t),
    });
    if (off < 0) rte_exit(EXIT_FAILURE, "dynfield register: %d\n", off);
    ts_dynfield_offset = off;
  }
  {
    int bit = rte_mbuf_dynflag_register(&(struct rte_mbuf_dynflag){
        .name = RTE_MBUF_DYNFLAG_TX_TIMESTAMP_NAME,
    });
    if (bit < 0) rte_exit(EXIT_FAILURE, "dynflag register: %d\n", bit);
    ts_dynflag_mask = 1ULL << bit;
  }

  printf("Dynfield: offset=%d  flag_bit=%d  flag_mask=0x%" PRIx64 "\n",
         ts_dynfield_offset,
         (int)(ts_dynflag_mask ? __builtin_ctzll(ts_dynflag_mask) : -1), ts_dynflag_mask);

  uint16_t nb_ports = rte_eth_dev_count_avail();
  if (nb_ports == 0) rte_exit(EXIT_FAILURE, "No ports\n");
  g_port = 0;

  g_pool =
      rte_pktmbuf_pool_create("POOL", MEMPOOL_SIZE, MEMPOOL_CACHE, 0,
                              RTE_MBUF_DEFAULT_BUF_SIZE, rte_eth_dev_socket_id(g_port));
  if (!g_pool) rte_exit(EXIT_FAILURE, "mempool failed\n");

  ret = port_init(g_port, g_pool);
  if (ret < 0) rte_exit(EXIT_FAILURE, "port_init: %d\n", ret);

  /* Map BAR0 for register access via sysfs resource0 */
  {
    struct rte_eth_dev_info di;
    memset(&di, 0, sizeof(di));
    rte_eth_dev_info_get(g_port, &di);
    const char* name = di.device ? rte_dev_name(di.device) : NULL;
    printf("PCI device: %s\n", name ? name : "(null)");
    if (name) {
      char path[256];
      snprintf(path, sizeof(path), "/sys/bus/pci/devices/%s/resource0", name);
      int fd = open(path, O_RDWR | O_SYNC);
      if (fd >= 0) {
        /* Get BAR0 size from resource file */
        char res_path[256];
        snprintf(res_path, sizeof(res_path), "/sys/bus/pci/devices/%s/resource", name);
        FILE* rf = fopen(res_path, "r");
        size_t bar_len = 0;
        if (rf) {
          unsigned long long start, end, flags;
          if (fscanf(rf, "%llx %llx %llx", &start, &end, &flags) == 3)
            bar_len = (size_t)(end - start + 1);
          fclose(rf);
        }
        if (bar_len == 0) bar_len = 128 * 1024 * 1024; /* 128MB default */
        void* map = mmap(NULL, bar_len, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
        close(fd);
        if (map != MAP_FAILED) {
          g_bar0 = (volatile uint32_t*)map;
          printf("BAR0 mapped: %p (len=%zu)\n", map, bar_len);
        } else {
          printf("BAR0 mmap failed: %s\n", strerror(errno));
        }
      } else {
        printf("BAR0 open(%s) failed: %s\n", path, strerror(errno));
      }
    }
  }

  /* Wait for link */
  printf("Waiting for link (up to 30s)...\n");
  struct rte_eth_link link;
  for (int w = 0; w < 30; w++) {
    memset(&link, 0, sizeof(link));
    ret = rte_eth_link_get_nowait(g_port, &link);
    (void)ret;
    if (link.link_status) break;
    printf("  +%ds: still DOWN\n", w + 1);
    sleep(1);
  }
  printf("Link: %s  speed %u Mbps\n", link.link_status ? "UP" : "DOWN", link.link_speed);
  if (!link.link_status) {
    fprintf(stderr, "ABORT: link never came up\n");
    goto cleanup;
  }

  /* Let link settle */
  sleep(1);

  dump_txtime_regs("AFTER INIT (before tests)");

  printf("\n");
  printf("############################################################\n");
  printf("#  E830 TX Time (Launch Time) Test Suite                   #\n");
  printf("#  Each test: send 10 pkts, wait 3s, try recovery pkt     #\n");
  printf("############################################################\n");

  /* ================================================================
   * Test 0: Sanity — send WITHOUT txtime to verify port works at all
   * ================================================================ */
  {
    printf("\n");
    printf("========================================\n");
    printf("TEST 0: Sanity — send 3 pkts WITHOUT txtime flag\n");
    printf("========================================\n");
    rte_eth_stats_reset(g_port);

    struct rte_mbuf* sanity[3];
    /* Even though we clear the txtime ol_flag, the PMD unconditionally
     * reads the dynfield for all pkts on a txtime queue.  Write a valid
     * future timestamp so we don't stall the HW scheduler with tstamp=0. */
    uint64_t sanity_phc = read_phc();
    for (int i = 0; i < 3; i++) {
      sanity[i] = build_pkt(0, i, sanity_phc, +1000000LL); /* +1ms future */
      if (!sanity[i]) {
        fprintf(stderr, "  build_pkt failed\n");
        goto cleanup;
      }
      /* CLEAR the txtime flag — send as normal packet (dynfield still set) */
      sanity[i]->ol_flags &= ~ts_dynflag_mask;
    }
    uint16_t s0 = rte_eth_tx_burst(g_port, 0, sanity, 3);
    printf("tx_burst (no txtime): %u / 3\n", s0);
    for (int i = s0; i < 3; i++) rte_pktmbuf_free(sanity[i]);
    sleep(2);
    struct rte_eth_stats st0;
    rte_eth_stats_get(g_port, &st0);
    printf("After 2s: opkts=%" PRIu64 "  obytes=%" PRIu64 "  oerr=%" PRIu64 "\n",
           st0.opackets, st0.obytes, st0.oerrors);
    if (st0.opackets == 0)
      printf("WARNING: even non-txtime packets show 0 — port TX may be broken\n");
    else
      printf("OK: non-txtime packets transmitted successfully\n");
  }

  enum { N = 10 };
  int64_t offsets[N];

  /* Test 1 */
  fill_offsets(offsets, N, +1000000LL);
  run_test(1, "Baseline: all future +1ms", N, offsets);

  /* Test 2 */
  fill_offsets(offsets, N, -1000000LL);
  run_test(2, "All past -1ms", N, offsets);

  /* Test 3 */
  fill_alternating(offsets, N, +1000000LL, -1000000LL);
  run_test(3, "Alternating +1ms / -1ms", N, offsets);

  /* Test 4 */
  fill_offsets(offsets, N, -50000000LL);
  run_test(4, "All past -50ms (within 67ms window)", N, offsets);

  /* Test 5 */
  fill_offsets(offsets, N, -500000000LL);
  run_test(5, "All past -500ms (outside 67ms window)", N, offsets);

  /* Test 6 */
  fill_alternating(offsets, N, +1000000LL, -500000000LL);
  run_test(6, "Alternating +1ms / -500ms", N, offsets);

  /* Test 7 */
  fill_offsets(offsets, N, 0);
  run_test(7, "Offset = 0 (launch = PHC now)", N, offsets);

  /* Test 8 */
  fill_offsets(offsets, N, +1000000000LL);
  run_test(8, "Far future +1s (nsec wraps to same value)", N, offsets);

  /* Test 9 */
  fill_alternating(offsets, N, +1000000000LL, -1000000LL);
  run_test(9, "Alternating +1s / -1ms", N, offsets);

  /* Test 10: Graduated */
  {
    int64_t graduated[] = {
        +1000000LL,   /* +1ms    */
        -1000000LL,   /* -1ms    */
        -10000000LL,  /* -10ms   */
        -30000000LL,  /* -30ms   */
        -50000000LL,  /* -50ms   */
        -67000000LL,  /* -67ms (~HW window edge) */
        -100000000LL, /* -100ms  */
        -250000000LL, /* -250ms  */
        -500000000LL, /* -500ms  */
        -999000000LL, /* -999ms  */
    };
    run_test(10, "Graduated: +1ms to -999ms", N, graduated);
  }

  /* Test 11: MTL-style stream with TSC-gated submission */
  run_stream_test();

  /* Test 12: Hold-time test */
  run_hold_test();

  dump_txtime_regs("AFTER ALL TESTS");

cleanup:
  printf("\n--- All tests complete ---\n");
  rte_eth_dev_stop(g_port);
  rte_eth_dev_close(g_port);
  rte_eal_cleanup();
  return 0;
}
