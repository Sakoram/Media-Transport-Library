#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

#define BAR_SIZE (128 * 1024 * 1024)

/* E830 TXTIME registers */
#define TS_CFG 0x002D3100
#define CNTX_CTL 0x002D3204
#define CNTX_DATA(i) (0x002D3104 + (i)*4)
#define CNTX_STAT 0x002D3208
#define FETCH_PROF 0x002D3500
#define WRR_CREDITS 0x002D320C
#define WRR_WEIGHTS 0x002D3210
#define OUTST_REQ 0x002D3214
#define GL_MDET_PQM 0x002D2E00
#define GL_MDET_PQM_FIFO 0x002D4B00
#define QTX_COMM_HEAD(q) (0x000E4000 + (q)*4)

static volatile uint32_t* bar;
static uint32_t rd(uint32_t r) {
  return bar[r / 4];
}

static void dump_ctx(int q) {
  bar[CNTX_CTL / 4] = (q & 0x7FF) | (1 << 19);
  usleep(100);
  uint32_t d[7];
  for (int i = 0; i < 7; i++) d[i] = rd(CNTX_DATA(i));
  uint32_t ena = (d[3] >> 9) & 1;
  uint32_t db32 = (d[3] >> 10) & 1;
  uint32_t res = (d[3] >> 11) & 0xF;
  uint32_t round = (d[3] >> 15) & 0x3;
  uint32_t pslot = (d[3] >> 17) & 0x7;
  uint32_t merge = (d[3] >> 20) & 1;
  uint32_t fprof = (d[3] >> 21) & 0xF;
  printf("  ctx[q=%d]: data={0x%08x 0x%08x 0x%08x 0x%08x 0x%08x 0x%08x 0x%08x}\n", q,
         d[0], d[1], d[2], d[3], d[4], d[5], d[6]);
  printf("    ena=%u db32=%u res=%u round=%u pslot=%u merge=%u fprof=%u\n", ena, db32,
         res, round, pslot, merge, fprof);
  printf("    int_q_state: d3[31:30]=0x%x d4=0x%08x d5=0x%08x d6[5:0]=0x%x\n",
         (d[3] >> 30) & 3, d[4], d[5], d[6] & 0x3F);
}

int main(int argc, char** argv) {
  const char* pci = argc > 1 ? argv[1] : "0000:ca:00.0";
  char path[256];
  snprintf(path, sizeof(path), "/sys/bus/pci/devices/%s/resource0", pci);
  int fd = open(path, O_RDWR | O_SYNC);
  if (fd < 0) {
    perror(path);
    return 1;
  }
  bar = mmap(NULL, BAR_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
  close(fd);
  if (bar == MAP_FAILED) {
    perror("mmap");
    return 1;
  }

  const char* label = argc > 2 ? argv[2] : "REGISTER DUMP";
  printf("=== TXTIME HW Register Dump: %s ===\n", label);

  uint32_t ts = rd(TS_CFG);
  printf("TS_CFG     = 0x%08x (ENABLE=%u STORAGE=%u PIPE_LAT=%u)\n", ts, ts & 1,
         (ts >> 2) & 7, (ts >> 5) & 0x1FFF);

  uint32_t fp = rd(FETCH_PROF);
  printf("FETCH_PROF = 0x%08x (FETCH=%u THRESH=%u)\n", fp, fp & 0x1FF, (fp >> 9) & 0x7F);

  printf("WRR_CRED   = 0x%08x  WRR_WT = 0x%08x\n", rd(WRR_CREDITS), rd(WRR_WEIGHTS));

  uint32_t outst = rd(OUTST_REQ);
  printf("OUTST_REQ  = 0x%08x (THRESHOLD=%u SNAPSHOT=%u)\n", outst, outst & 0x3FF,
         (outst >> 10) & 0x3FF);

  uint32_t mdet = rd(GL_MDET_PQM);
  uint32_t fifo = rd(GL_MDET_PQM_FIFO);
  printf("MDET_PQM   = 0x%08x (VALID=%u)  FIFO = 0x%08x (MAL=%u VALID=%u CNT=%u)\n", mdet,
         (mdet >> 31) & 1, fifo, (fifo >> 15) & 0x1F, (fifo >> 21) & 1,
         (fifo >> 24) & 0xFF);

  for (int q = 0; q < 8; q++) dump_ctx(q);

  for (int q = 0; q < 4; q++) printf("  HEAD[%d] = 0x%08x\n", q, rd(QTX_COMM_HEAD(q)));

  printf("=== End ===\n");
  munmap((void*)bar, BAR_SIZE);
  return 0;
}
