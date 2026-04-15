/* SPDX-License-Identifier: BSD-3-Clause
 * Copyright(c) 2024 Intel Corporation
 *
 * Kernel SO_TXTIME test for E830 launch-time scheduling.
 * Uses the kernel ice driver path: SO_TXTIME + ETF qdisc + HW offload.
 *
 * Build:
 *   gcc -O2 -Wall -o kernel_txtime_test kernel_txtime_test.c
 *
 * Prerequisites (run setup_kernel_txtime.sh first):
 *   1. ice driver loaded, interface up with IP
 *   2. ETF qdisc configured with offload
 *
 * Usage:
 *   sudo ./kernel_txtime_test [options]
 *     --iface eth2          Network interface (default: eth2)
 *     --dst-ip 10.0.0.2     Destination IP (default: 10.0.0.2)
 *     --count 100           Number of packets (default: 100)
 *     --offset-ms 50        Future offset from CLOCK_TAI (default: 50)
 */

#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <getopt.h>
#include <inttypes.h>
#include <linux/errqueue.h>
#include <linux/net_tstamp.h>
#include <net/if.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#ifndef SO_TXTIME
#define SO_TXTIME 61
#endif
#ifndef SCM_TXTIME
#define SCM_TXTIME SO_TXTIME
#endif

#define UDP_SRC_PORT 54321
#define UDP_DST_PORT 12345
#define PKT_MAGIC 0x54585431 /* "TXT1" — same as DPDK test */

/* Self-describing payload (matches DPDK txtime_test format) */
struct txtime_payload {
  uint32_t magic;
  uint32_t seq;
  uint32_t test_id;
  uint32_t pad;
  uint64_t phc_at_send;
  int64_t  offset_ns;
  uint64_t launch_time;
};

/* Variable-spacing signature pattern (in nanoseconds).
 * Each value is the gap BEFORE the next packet.  Repeats cyclically.
 * Pattern chosen so that wire-rate transmission (~670ns for 82B @ 100G)
 * could never reproduce these large, variable gaps.
 *
 * Signature: 10us, 100us, 10us, 200us, 10us, 50us, 10us, 500us  (8-element cycle)
 * Mean ~111us, but the shape is unmistakable.
 */
static const uint32_t SIG_PATTERN[] = {
  10000, 100000, 10000, 200000, 10000, 50000, 10000, 500000,
};
#define SIG_LEN (sizeof(SIG_PATTERN) / sizeof(SIG_PATTERN[0]))

/* Options */
static const char *g_iface = "eth2";
static const char *g_dst_ip = "10.0.0.2";
static uint32_t g_count = 100;
static uint32_t g_offset_ms = 50;

static uint64_t timespec_to_ns(const struct timespec *ts) {
  return (uint64_t)ts->tv_sec * 1000000000ULL + (uint64_t)ts->tv_nsec;
}

static uint64_t clock_tai_ns(void) {
  struct timespec ts;
  clock_gettime(CLOCK_TAI, &ts);
  return timespec_to_ns(&ts);
}

static uint64_t clock_mono_ns(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return timespec_to_ns(&ts);
}

static inline uint64_t align128(uint64_t ns) {
  return ((ns + 127) >> 7) << 7;
}

/* Drain error queue — report any SO_TXTIME errors */
static void drain_errqueue(int fd) {
  char ctrl[256];
  char buf[64];
  int count = 0;

  while (1) {
    struct iovec iov = { .iov_base = buf, .iov_len = sizeof(buf) };
    struct msghdr msg = {
      .msg_iov = &iov, .msg_iovlen = 1,
      .msg_control = ctrl, .msg_controllen = sizeof(ctrl),
    };

    int ret = recvmsg(fd, &msg, MSG_ERRQUEUE | MSG_DONTWAIT);
    if (ret < 0) break;
    count++;

    struct cmsghdr *cmsg;
    for (cmsg = CMSG_FIRSTHDR(&msg); cmsg; cmsg = CMSG_NXTHDR(&msg, cmsg)) {
      if (cmsg->cmsg_level == SOL_IP && cmsg->cmsg_type == IP_RECVERR) {
        struct sock_extended_err *serr =
          (struct sock_extended_err *)CMSG_DATA(cmsg);
        if (count <= 10)
          printf("  ERRQUEUE[%d]: ee_errno=%d ee_origin=%d ee_type=%d "
                 "ee_code=%d ee_info=%u\n",
                 count, serr->ee_errno, serr->ee_origin,
                 serr->ee_type, serr->ee_code, serr->ee_info);
      }
    }
  }
  if (count > 10)
    printf("  ... (%d more errors)\n", count - 10);
  if (count == 0)
    printf("  (none)\n");
}

/* Send one packet with SO_TXTIME via UDP sendmsg */
static int send_txtime_pkt(int fd, const struct sockaddr_in *dst,
                           uint32_t seq, uint64_t tai_base,
                           uint64_t launch_ns) {
  struct txtime_payload pl = {
    .magic = htonl(PKT_MAGIC),
    .seq = htonl(seq),
    .test_id = htonl(99),  /* test_id=99 for kernel test */
    .pad = 0,
    .phc_at_send = htobe64(tai_base),
    .offset_ns = htobe64((uint64_t)(int64_t)(launch_ns - tai_base)),
    .launch_time = htobe64(launch_ns),
  };

  struct iovec iov = { .iov_base = &pl, .iov_len = sizeof(pl) };
  char ctrl[CMSG_SPACE(sizeof(uint64_t))];
  memset(ctrl, 0, sizeof(ctrl));

  struct msghdr msg = {
    .msg_name = (void *)dst,
    .msg_namelen = sizeof(*dst),
    .msg_iov = &iov,
    .msg_iovlen = 1,
    .msg_control = ctrl,
    .msg_controllen = sizeof(ctrl),
  };

  struct cmsghdr *cmsg = CMSG_FIRSTHDR(&msg);
  cmsg->cmsg_level = SOL_SOCKET;
  cmsg->cmsg_type = SCM_TXTIME;
  cmsg->cmsg_len = CMSG_LEN(sizeof(uint64_t));
  *(uint64_t *)CMSG_DATA(cmsg) = launch_ns;

  return sendmsg(fd, &msg, 0);
}

static void parse_args(int argc, char **argv) {
  static struct option opts[] = {
    {"iface",     required_argument, NULL, 'i'},
    {"dst-ip",    required_argument, NULL, 'I'},
    {"count",     required_argument, NULL, 'c'},
    {"offset-ms", required_argument, NULL, 'o'},
    {NULL, 0, NULL, 0},
  };
  int opt;
  while ((opt = getopt_long(argc, argv, "i:I:c:o:", opts, NULL)) != -1) {
    switch (opt) {
    case 'i': g_iface = optarg; break;
    case 'I': g_dst_ip = optarg; break;
    case 'c': g_count = (uint32_t)atoi(optarg); break;
    case 'o': g_offset_ms = (uint32_t)atoi(optarg); break;
    }
  }
}

int main(int argc, char **argv) {
  parse_args(argc, argv);

  int ifindex = (int)if_nametoindex(g_iface);
  if (ifindex == 0) {
    fprintf(stderr, "Interface %s not found\n", g_iface);
    return 1;
  }

  /* Pre-compute per-packet gaps from the variable signature */
  uint64_t *gap_ns = calloc(g_count, sizeof(uint64_t));
  if (!gap_ns) { perror("calloc"); return 1; }

  uint64_t total_span_ns = 0;
  for (uint32_t i = 0; i < g_count; i++) {
    if (i == 0) {
      gap_ns[i] = 0;  /* first packet has no gap before it */
    } else {
      /* Pick gap from repeating signature, align to 128ns */
      uint32_t raw = SIG_PATTERN[(i - 1) % SIG_LEN];
      gap_ns[i] = align128(raw);
      total_span_ns += gap_ns[i];
    }
  }

  printf("========================================\n");
  printf("Kernel SO_TXTIME Variable-Spacing Test\n");
  printf("  iface=%s  ifindex=%d\n", g_iface, ifindex);
  printf("  dst_ip=%s\n", g_dst_ip);
  printf("  count=%u  offset=%ums\n", g_count, g_offset_ms);
  printf("  total span: %" PRIu64 " us\n", total_span_ns / 1000);
  printf("  signature cycle (%zu elements):\n", SIG_LEN);
  for (size_t j = 0; j < SIG_LEN; j++)
    printf("    gap[%zu] = %u ns (%u us)\n", j, SIG_PATTERN[j],
           SIG_PATTERN[j] / 1000);
  printf("========================================\n\n");

  /* Create UDP socket */
  int fd = socket(AF_INET, SOCK_DGRAM, 0);
  if (fd < 0) { perror("socket"); return 1; }

  /* Bind to interface */
  struct ifreq ifr;
  memset(&ifr, 0, sizeof(ifr));
  snprintf(ifr.ifr_name, IFNAMSIZ, "%s", g_iface);
  if (setsockopt(fd, SOL_SOCKET, SO_BINDTODEVICE, &ifr, sizeof(ifr)) < 0) {
    perror("SO_BINDTODEVICE");
    close(fd);
    return 1;
  }

  /* Set socket priority to 3 so packets route to TC 0 (ETF qdisc).
   * mqprio map: priority 3 → TC 0 (where ETF is attached).
   */
  int prio = 3;
  if (setsockopt(fd, SOL_SOCKET, SO_PRIORITY, &prio, sizeof(prio)) < 0) {
    perror("SO_PRIORITY");
  }

  /* Enable SO_TXTIME */
  struct sock_txtime so_txtime = {
    .clockid = CLOCK_TAI,
    .flags = SOF_TXTIME_REPORT_ERRORS,
  };
  if (setsockopt(fd, SOL_SOCKET, SO_TXTIME, &so_txtime, sizeof(so_txtime)) < 0) {
    perror("setsockopt SO_TXTIME");
    fprintf(stderr, "Hint: Is ETF qdisc configured on %s?\n"
                    "  Run: sudo ./setup_kernel_txtime.sh %s\n",
            g_iface, g_iface);
    close(fd);
    return 1;
  }
  printf("SO_TXTIME enabled (CLOCK_TAI, report_errors)\n");

  /* Get source info */
  if (ioctl(fd, SIOCGIFHWADDR, &ifr) == 0) {
    uint8_t *m = (uint8_t *)ifr.ifr_hwaddr.sa_data;
    printf("Source MAC: %02x:%02x:%02x:%02x:%02x:%02x\n",
           m[0], m[1], m[2], m[3], m[4], m[5]);
  }

  struct sockaddr_in dst_addr = {
    .sin_family = AF_INET,
    .sin_port = htons(UDP_DST_PORT),
    .sin_addr.s_addr = inet_addr(g_dst_ip),
  };

  /* Read CLOCK_TAI and compute launch times */
  uint64_t tai_base = clock_tai_ns();
  uint32_t offset_ns = g_offset_ms * 1000000u;
  uint64_t first_launch = align128(tai_base + offset_ns);

  /* Compute absolute launch times from gaps */
  uint64_t *launch_times = calloc(g_count, sizeof(uint64_t));
  if (!launch_times) { perror("calloc"); free(gap_ns); return 1; }
  launch_times[0] = first_launch;
  for (uint32_t i = 1; i < g_count; i++)
    launch_times[i] = launch_times[i - 1] + gap_ns[i];

  printf("\nCLOCK_TAI    : %" PRIu64 " ns  (%u.%09u)\n",
         tai_base, (unsigned)(tai_base / 1000000000ULL),
         (unsigned)(tai_base % 1000000000ULL));
  printf("First launch : %" PRIu64 " ns  (TAI + %u ms)\n",
         first_launch, g_offset_ms);
  printf("Last launch  : %" PRIu64 " ns  (TAI + %u ms + %" PRIu64 " us)\n",
         launch_times[g_count - 1], g_offset_ms, total_span_ns / 1000);

  /* Print first 20 planned gaps for reference */
  printf("\nPlanned gaps (first %u):\n", g_count < 20 ? g_count : 20);
  for (uint32_t i = 1; i < g_count && i < 20; i++)
    printf("  gap[%2u->%2u] = %6" PRIu64 " ns (%3" PRIu64 " us)\n",
           i - 1, i, gap_ns[i], gap_ns[i] / 1000);

  printf("\nSending %u packets...\n", g_count);

  uint64_t wall_start = clock_mono_ns();
  uint32_t sent_ok = 0;
  uint32_t send_err = 0;

  for (uint32_t i = 0; i < g_count; i++) {
    uint64_t launch = launch_times[i];
    int ret = send_txtime_pkt(fd, &dst_addr, i, tai_base, launch);

    if (ret > 0) {
      sent_ok++;
    } else {
      send_err++;
      if (send_err <= 5)
        fprintf(stderr, "  sendmsg[%u] failed: %s (errno=%d)\n",
                i, strerror(errno), errno);
    }

    /* Log every 10th packet and first/last */
    if (i == 0 || i == g_count - 1 || (i % 10 == 0)) {
      uint64_t tai_now = clock_tai_ns();
      int64_t delta = (int64_t)launch - (int64_t)tai_now;
      printf("  pkt[%4u] launch_delta=%+" PRId64 " us  gap=%" PRIu64 " us  %s\n",
             i, delta / 1000, gap_ns[i] / 1000, ret > 0 ? "OK" : "FAIL");
    }
  }

  uint64_t wall_elapsed = clock_mono_ns() - wall_start;

  /* Brief sleep then drain error queue */
  usleep(200000);
  printf("\nError queue:\n");
  drain_errqueue(fd);

  printf("\nResults:\n");
  printf("  sent_ok   : %u / %u\n", sent_ok, g_count);
  printf("  send_err  : %u\n", send_err);
  printf("  wall_time : %" PRIu64 " us\n", wall_elapsed / 1000);

  /* Wait for HW to transmit */
  printf("\nWaiting 3s for HW transmission...\n");
  for (int s = 1; s <= 3; s++) {
    sleep(1);
    uint64_t tai_now = clock_tai_ns();
    printf("  +%ds: TAI=%u.%09u\n", s,
           (unsigned)(tai_now / 1000000000ULL),
           (unsigned)(tai_now % 1000000000ULL));
  }

  printf("\nDone. Check captures on receiver:\n");
  printf("  sudo tcpdump -i eth1 udp port %d\n", UDP_DST_PORT);

  free(gap_ns);
  free(launch_times);
  close(fd);
  return 0;
}
