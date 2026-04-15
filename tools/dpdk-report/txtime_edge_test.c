/* SPDX-License-Identifier: BSD-3-Clause
 * Copyright(c) 2026 Intel Corporation
 *
 * E830 TXTIME edge-case test with self-describing UDP packets.
 * Each packet carries its test parameters in the payload so a
 * pcap capture on the RX side can fully reconstruct timing.
 *
 * Build:  ninja -C builddir
 * Run:    sudo ./builddir/txtime_edge_test -l 0-1 -a 0000:ca:00.0 --iova-mode=pa
 *
 * Capture (on RX NIC, e.g. eth1):
 *   sudo tcpdump -i eth1 --time-stamp-precision=nano -nn \
 *        udp port 12345 -w /tmp/txtime_edge.pcap
 */

#define _DEFAULT_SOURCE
#include <inttypes.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#include <rte_cycles.h>
#include <rte_eal.h>
#include <rte_ethdev.h>
#include <rte_ether.h>
#include <rte_ip.h>
#include <rte_mbuf.h>
#include <rte_mbuf_dyn.h>
#include <rte_udp.h>

#define NUM_MBUFS  4096
#define MBUF_CACHE 256
#define TX_DESC    512
#define RX_DESC    128
#define UDP_PORT   12345
#define WAIT_SEC   3

/* ---- self-describing payload ---- */
#define PAYLOAD_MAGIC 0xE830CAFE

struct test_payload {
    uint32_t magic;
    uint16_t test_id;
    uint16_t seq;
    int64_t  offset_ns;     /* requested offset from PHC */
    uint64_t phc_at_send;   /* PHC reading just before tx_burst */
    uint64_t launch_time;   /* dynfield value written to mbuf */
} __attribute__((packed));

/* ---- globals ---- */
static int ts_offset = -1;
static uint64_t ts_mask;
static uint16_t g_port;
static struct rte_mempool *g_pool;
static struct rte_ether_addr g_src_mac;

/* Broadcast dest — arrives everywhere without ARP */
static struct rte_ether_addr g_dst_mac = {
    .addr_bytes = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff}
};

/* ------------------------------------------------------------------ */
static uint64_t read_phc(void)
{
    struct timespec ts;
    rte_eth_timesync_read_time(g_port, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + ts.tv_nsec;
}

static int port_init(void)
{
    struct rte_eth_dev_info info;
    struct rte_eth_conf conf;
    struct rte_eth_txconf txconf;
    int ret;

    memset(&conf, 0, sizeof(conf));
    ret = rte_eth_dev_info_get(g_port, &info);
    if (ret < 0) return ret;

    if (!(info.tx_offload_capa & RTE_ETH_TX_OFFLOAD_SEND_ON_TIMESTAMP)) {
        printf("FATAL: No SEND_ON_TIMESTAMP capability\n");
        return -1;
    }

    conf.txmode.offloads = RTE_ETH_TX_OFFLOAD_SEND_ON_TIMESTAMP;
    ret = rte_eth_dev_configure(g_port, 1, 1, &conf);
    if (ret < 0) return ret;

    ret = rte_eth_rx_queue_setup(g_port, 0, RX_DESC,
                                  rte_eth_dev_socket_id(g_port), NULL, g_pool);
    if (ret < 0) return ret;

    txconf = info.default_txconf;
    txconf.offloads = conf.txmode.offloads;
    ret = rte_eth_tx_queue_setup(g_port, 0, TX_DESC,
                                  rte_eth_dev_socket_id(g_port), &txconf);
    if (ret < 0) return ret;

    ret = rte_eth_dev_start(g_port);
    if (ret < 0) return ret;

    rte_eth_timesync_enable(g_port);
    rte_eth_macaddr_get(g_port, &g_src_mac);

    /* wait for PHC */
    for (int i = 0; i < 100; i++) {
        if (read_phc() > 1000000000ULL) break;
        usleep(50000);
    }
    return 0;
}

/* ------------------------------------------------------------------ */

/*
 * Build one UDP packet with self-describing payload.
 * Returns the mbuf (caller must free on failure).
 */
static struct rte_mbuf *build_pkt(uint16_t test_id, uint16_t seq,
                                   int64_t offset_ns, uint64_t phc_now)
{
    struct rte_mbuf *m = rte_pktmbuf_alloc(g_pool);
    if (!m) return NULL;

    /* Ethernet header */
    struct rte_ether_hdr *eth = (struct rte_ether_hdr *)
        rte_pktmbuf_append(m, sizeof(struct rte_ether_hdr));
    rte_ether_addr_copy(&g_dst_mac, &eth->dst_addr);
    rte_ether_addr_copy(&g_src_mac, &eth->src_addr);
    eth->ether_type = rte_cpu_to_be_16(RTE_ETHER_TYPE_IPV4);

    /* IPv4 header */
    struct rte_ipv4_hdr *ip = (struct rte_ipv4_hdr *)
        rte_pktmbuf_append(m, sizeof(struct rte_ipv4_hdr));
    memset(ip, 0, sizeof(*ip));
    ip->version_ihl = 0x45;
    ip->time_to_live = 64;
    ip->next_proto_id = IPPROTO_UDP;
    ip->src_addr = rte_cpu_to_be_32(0x0A000001); /* 10.0.0.1 */
    ip->dst_addr = rte_cpu_to_be_32(0xFFFFFFFF); /* 255.255.255.255 */

    /* UDP header */
    struct rte_udp_hdr *udp = (struct rte_udp_hdr *)
        rte_pktmbuf_append(m, sizeof(struct rte_udp_hdr));
    udp->src_port = rte_cpu_to_be_16(UDP_PORT);
    udp->dst_port = rte_cpu_to_be_16(UDP_PORT);

    /* Payload */
    struct test_payload *p = (struct test_payload *)
        rte_pktmbuf_append(m, sizeof(struct test_payload));

    uint64_t launch = (uint64_t)((int64_t)phc_now + offset_ns);

    p->magic       = rte_cpu_to_be_32(PAYLOAD_MAGIC);
    p->test_id     = rte_cpu_to_be_16(test_id);
    p->seq         = rte_cpu_to_be_16(seq);
    p->offset_ns   = rte_cpu_to_be_64((uint64_t)offset_ns);
    p->phc_at_send = rte_cpu_to_be_64(phc_now);
    p->launch_time = rte_cpu_to_be_64(launch);

    /* Fix lengths */
    uint16_t payload_len = sizeof(struct rte_udp_hdr) + sizeof(struct test_payload);
    udp->dgram_len = rte_cpu_to_be_16(payload_len);
    udp->dgram_cksum = 0;
    ip->total_length = rte_cpu_to_be_16(sizeof(struct rte_ipv4_hdr) + payload_len);
    ip->hdr_checksum = 0;
    ip->hdr_checksum = rte_ipv4_cksum(ip);

    /* Set TXTIME dynfield */
    m->ol_flags |= ts_mask;
    *RTE_MBUF_DYNFIELD(m, ts_offset, uint64_t *) = launch;

    return m;
}

/* ------------------------------------------------------------------ */

struct test_case {
    const char *name;
    int64_t offset_ns;
    int count;
};

static void reset_queue(void)
{
    rte_eth_dev_tx_queue_stop(g_port, 0);
    rte_eth_dev_tx_queue_start(g_port, 0);
    rte_eth_stats_reset(g_port);
    usleep(100000);
}

/* Send `count` packets for a test case, return opackets delta */
static uint64_t run_test(uint16_t test_id, const struct test_case *tc)
{
    struct rte_eth_stats s0, s1;
    struct rte_mbuf *pkts[64];
    int count = tc->count > 64 ? 64 : tc->count;

    rte_eth_stats_get(g_port, &s0);

    uint64_t phc_now = read_phc();

    for (int i = 0; i < count; i++) {
        pkts[i] = build_pkt(test_id, i, tc->offset_ns, phc_now);
        if (!pkts[i]) {
            printf("  alloc fail at %d\n", i);
            for (int j = 0; j < i; j++) rte_pktmbuf_free(pkts[j]);
            return 0;
        }
    }

    uint16_t sent = rte_eth_tx_burst(g_port, 0, pkts, count);
    for (int i = sent; i < count; i++) rte_pktmbuf_free(pkts[i]);

    /* Wait and poll */
    for (int s = 1; s <= WAIT_SEC; s++) {
        sleep(1);
        rte_eth_stats_get(g_port, &s1);
        uint64_t delta = s1.opackets - s0.opackets;
        printf("    +%ds opkts=%" PRIu64 " oerr=%" PRIu64 "\n",
               s, delta, s1.oerrors - s0.oerrors);
    }
    rte_eth_stats_get(g_port, &s1);
    return s1.opackets - s0.opackets;
}

/* ------------------------------------------------------------------ */

int main(int argc, char *argv[])
{
    struct rte_eth_link link;
    int ret;

    ret = rte_eal_init(argc, argv);
    if (ret < 0) rte_exit(EXIT_FAILURE, "EAL init failed\n");
    if (rte_eth_dev_count_avail() == 0)
        rte_exit(EXIT_FAILURE, "No ports\n");

    ts_offset = rte_mbuf_dynfield_register(
        &(struct rte_mbuf_dynfield){
            .name = RTE_MBUF_DYNFIELD_TIMESTAMP_NAME,
            .size = sizeof(uint64_t),
            .align = __alignof__(uint64_t),
        });
    if (ts_offset < 0) rte_exit(EXIT_FAILURE, "dynfield register failed\n");

    int bit = rte_mbuf_dynflag_register(
        &(struct rte_mbuf_dynflag){
            .name = RTE_MBUF_DYNFLAG_TX_TIMESTAMP_NAME,
        });
    if (bit < 0) rte_exit(EXIT_FAILURE, "dynflag register failed\n");
    ts_mask = 1ULL << bit;

    g_port = 0;
    g_pool = rte_pktmbuf_pool_create("POOL", NUM_MBUFS, MBUF_CACHE, 0,
                                      RTE_MBUF_DEFAULT_BUF_SIZE,
                                      rte_eth_dev_socket_id(g_port));
    if (!g_pool) rte_exit(EXIT_FAILURE, "pool create failed\n");

    ret = port_init();
    if (ret < 0) rte_exit(EXIT_FAILURE, "port_init: %d\n", ret);

    printf("Waiting for link...\n");
    for (int i = 0; i < 30; i++) {
        (void)rte_eth_link_get_nowait(g_port, &link);
        if (link.link_status) break;
        sleep(1);
    }
    if (!link.link_status) rte_exit(EXIT_FAILURE, "Link never came up\n");
    printf("Link UP at %u Mbps\n\n", link.link_speed);

    uint64_t phc = read_phc();
    printf("PHC = %" PRIu64 " ns  (%u.%09u)\n\n",
           phc, (unsigned)(phc / 1000000000ULL),
           (unsigned)(phc % 1000000000ULL));

    rte_eth_stats_reset(g_port);
    sleep(1);

    /* ============================================================ */
    /*  Test matrix — comprehensive edge cases                      */
    /* ============================================================ */

    static const struct test_case tests[] = {
        /* --- GROUP 1: Near future (should all transmit, with scheduling) --- */
        { "future +1ms",            1000000LL,    5 },   /* T0 */
        { "future +5ms",            5000000LL,    5 },   /* T1 */
        { "future +10ms",          10000000LL,    5 },   /* T2 */
        { "future +30ms",          30000000LL,    5 },   /* T3 */
        { "future +50ms",          50000000LL,    5 },   /* T4 */

        /* --- GROUP 2: Window boundary (19-bit, 128ns → ~67ms) --- */
        { "future +60ms",          60000000LL,    5 },   /* T5 */
        { "future +65ms",          65000000LL,    5 },   /* T6 */
        { "future +67ms",          67000000LL,    5 },   /* T7  = ~window edge */
        { "future +67.1ms",        67100000LL,    5 },   /* T8 */
        { "future +68ms",          68000000LL,    5 },   /* T9 */
        { "future +70ms",          70000000LL,    5 },   /* T10 */
        { "future +80ms",          80000000LL,    5 },   /* T11 */
        { "future +100ms",        100000000LL,    5 },   /* T12 */
        { "future +134ms",        134000000LL,    5 },   /* T13 = 2× window */

        /* --- GROUP 3: Past timestamps --- */
        { "past -1ms",             -1000000LL,    5 },   /* T14 */
        { "past -5ms",             -5000000LL,    5 },   /* T15 */
        { "past -10ms",           -10000000LL,    5 },   /* T16 */
        { "past -50ms",           -50000000LL,    5 },   /* T17 */
        { "past -67ms",           -67000000LL,    5 },   /* T18 = window edge back */
        { "past -100ms",         -100000000LL,    5 },   /* T19 */
        { "past -500ms",         -500000000LL,    5 },   /* T20 */
        { "past -999ms",         -999000000LL,    5 },   /* T21 */

        /* --- GROUP 4: Extreme values --- */
        { "dynfield=0 (epoch)",             0,    5 },   /* T22: offset rewritten below */
        { "future +1s (wraps sub-s)",  1000000000LL, 5 }, /* T23 */
        { "future +2s",           2000000000LL,   5 },   /* T24 */
        { "future +10s",         10000000000LL,   5 },   /* T25 */
        { "past   -1s",         -1000000000LL,    5 },   /* T26: exactly 1s ago */

        /* --- GROUP 5: Micro-offsets (sub-window-resolution) --- */
        { "future +128ns (1 tick)",      128LL,   5 },   /* T27 */
        { "future +1us",               1000LL,    5 },   /* T28 */
        { "future +10us",             10000LL,    5 },   /* T29 */
        { "future +100us",           100000LL,    5 },   /* T30 */

        /* --- GROUP 6: Mixed burst (alternating past/future in single burst) --- */
        /* These will all use +5ms offset; we send them as a group */
        { "mixed alternating +1ms/-1ms",  0LL,    10},   /* T31: special handling */
    };

    int n_tests = sizeof(tests) / sizeof(tests[0]);
    int total = 0, passed = 0;

    printf("===========================================================\n");
    printf("  E830 TXTIME Edge-Case Test — %d tests, UDP port %d\n", n_tests, UDP_PORT);
    printf("  Capture with: sudo tcpdump -i <rx_if> --time-stamp-precision=nano \\\n");
    printf("                     udp port %d -w /tmp/txtime_edge.pcap\n", UDP_PORT);
    printf("===========================================================\n\n");

    for (int t = 0; t < n_tests; t++) {
        const struct test_case *tc = &tests[t];

        printf("--- T%02d: %-35s ---\n", t, tc->name);

        /* Reset queue before each test to prevent cascading failures */
        reset_queue();

        uint64_t phc_now = read_phc();
        uint64_t opkts;

        if (t == 22) {
            /* Special: dynfield=0 — build packets manually with launch_time=0 */
            struct rte_eth_stats s0, s1;
            rte_eth_stats_get(g_port, &s0);
            for (int i = 0; i < 5; i++) {
                struct rte_mbuf *m = build_pkt(t, i, 0, phc_now);
                if (!m) break;
                /* Override: set dynfield to literal 0 */
                *RTE_MBUF_DYNFIELD(m, ts_offset, uint64_t *) = 0;
                uint16_t s = rte_eth_tx_burst(g_port, 0, &m, 1);
                if (s == 0) rte_pktmbuf_free(m);
            }
            for (int s = 1; s <= WAIT_SEC; s++) {
                sleep(1);
                rte_eth_stats_get(g_port, &s1);
                printf("    +%ds opkts=%" PRIu64 " oerr=%" PRIu64 "\n",
                       s, s1.opackets - s0.opackets, s1.oerrors - s0.oerrors);
            }
            rte_eth_stats_get(g_port, &s1);
            opkts = s1.opackets - s0.opackets;
        } else if (t == 31) {
            /* Special: alternating +1ms/-1ms in one burst */
            struct rte_eth_stats s0, s1;
            struct rte_mbuf *pkts[10];
            rte_eth_stats_get(g_port, &s0);
            phc_now = read_phc();
            for (int i = 0; i < 10; i++) {
                int64_t off = (i % 2 == 0) ? 1000000LL : -1000000LL;
                pkts[i] = build_pkt(t, i, off, phc_now);
                if (!pkts[i]) {
                    for (int j = 0; j < i; j++) rte_pktmbuf_free(pkts[j]);
                    opkts = 0;
                    goto print_result;
                }
            }
            uint16_t sent = rte_eth_tx_burst(g_port, 0, pkts, 10);
            for (int i = sent; i < 10; i++) rte_pktmbuf_free(pkts[i]);
            for (int s = 1; s <= WAIT_SEC; s++) {
                sleep(1);
                rte_eth_stats_get(g_port, &s1);
                printf("    +%ds opkts=%" PRIu64 " oerr=%" PRIu64 "\n",
                       s, s1.opackets - s0.opackets, s1.oerrors - s0.oerrors);
            }
            rte_eth_stats_get(g_port, &s1);
            opkts = s1.opackets - s0.opackets;
        } else {
            opkts = run_test(t, tc);
        }

print_result:
        printf("    >> T%02d %-35s offset=%+" PRId64 "ns  opkts=%" PRIu64 " %s\n\n",
               t, tc->name, tc->offset_ns, opkts,
               opkts >= (uint64_t)tc->count ? "PASS" : (opkts > 0 ? "PARTIAL" : "BLOCKED"));
        total++;
        if (opkts >= (uint64_t)tc->count) passed++;
    }

    printf("===========================================================\n");
    printf("  SUMMARY: %d / %d tests transmitted all packets\n", passed, total);
    printf("===========================================================\n");

    rte_eth_dev_stop(g_port);
    rte_eth_dev_close(g_port);
    rte_eal_cleanup();
    return 0;
}
