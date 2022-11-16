/* SPDX-License-Identifier: BSD-3-Clause
 * Copyright(c) 2022 Intel Corporation
 */

#include "log.h"
#include "tests.h"

static int test_dma_cnt(struct st_tests_context* ctx) {
  st_handle handle = ctx->handle;
  struct st_stats stats;
  int ret;

  ret = st_get_stats(handle, &stats);
  if (ret < 0) return ret;

  return stats.dma_dev_cnt;
}

static void test_dma_create_one(struct st_tests_context* ctx) {
  st_handle handle = ctx->handle;
  int base_cnt = test_dma_cnt(ctx), cnt, ret;
  st_udma_handle dma;

  dma = st_udma_create(handle, 128, ST_PORT_P);
  ASSERT_TRUE(dma != NULL);
  cnt = test_dma_cnt(ctx);
  EXPECT_EQ(base_cnt + 1, cnt);
  ret = st_udma_free(dma);
  EXPECT_GE(ret, 0);
  cnt = test_dma_cnt(ctx);
  EXPECT_EQ(base_cnt, cnt);
}

static void test_dma_create_max(struct st_tests_context* ctx) {
  st_handle handle = ctx->handle;
  int base_cnt = test_dma_cnt(ctx), cnt, ret, dma_idx;
  st_udma_handle dma[ST_DMA_DEV_MAX];

  dma_idx = 0;
  while (true) {
    dma[dma_idx] = st_udma_create(handle, 128, ST_PORT_P);
    if (!dma[dma_idx]) break;
    dma_idx++;
    cnt = test_dma_cnt(ctx);
    EXPECT_EQ(base_cnt + dma_idx, cnt);
  }

  for (int i = 0; i < dma_idx; i++) {
    ret = st_udma_free(dma[i]);
    EXPECT_GE(ret, 0);
    cnt = test_dma_cnt(ctx);
    EXPECT_EQ(base_cnt + dma_idx - i - 1, cnt);
  }
}

TEST(Dma, create) {
  struct st_tests_context* ctx = st_test_ctx();

  if (!st_test_dma_available(ctx)) return;

  test_dma_create_one(ctx);
}

TEST(Dma, create_max) {
  struct st_tests_context* ctx = st_test_ctx();

  if (!st_test_dma_available(ctx)) return;

  test_dma_create_max(ctx);
}

TEST(Dma, create_multi) {
  struct st_tests_context* ctx = st_test_ctx();

  if (!st_test_dma_available(ctx)) return;

  for (int i = 0; i < 10; i++) test_dma_create_one(ctx);
}

static void _test_dma_copy(st_handle st, st_udma_handle dma, uint32_t off, uint32_t len) {
  void *dst = NULL, *src = NULL;
  st_iova_t dst_iova, src_iova;
  int ret;

  dst = st_hp_malloc(st, len, ST_PORT_P);
  ASSERT_TRUE(dst != NULL);
  dst_iova = st_hp_virt2iova(st, dst);
  src = st_hp_malloc(st, len, ST_PORT_P);
  ASSERT_TRUE(src != NULL);
  src_iova = st_hp_virt2iova(st, src);
  st_test_rand_data((uint8_t*)src, len, 0);

  ret = st_udma_copy(dma, dst_iova + off, src_iova + off, len - off);
  EXPECT_GE(ret, 0);
  ret = st_udma_submit(dma);

  uint16_t nb_dq = 0;
  while (nb_dq < 1) {
    nb_dq = st_udma_completed(dma, 32);
  }

  ret = memcmp((uint8_t*)src + off, (uint8_t*)dst + off, len - off);
  EXPECT_EQ(ret, 0);

  st_hp_free(st, dst);
  st_hp_free(st, src);
}

static void test_dma_copy(struct st_tests_context* ctx, uint32_t off, uint32_t len) {
  st_handle st = ctx->handle;
  st_udma_handle dma;
  int ret;

  dma = st_udma_create(st, 128, ST_PORT_P);
  ASSERT_TRUE(dma != NULL);

  _test_dma_copy(st, dma, off, len);

  ret = st_udma_free(dma);
  EXPECT_GE(ret, 0);
}

static void test_dma_copy_sanity(struct st_tests_context* ctx) {
  st_handle st = ctx->handle;
  st_udma_handle dma;
  int ret;

  dma = st_udma_create(st, 128, ST_PORT_P);
  ASSERT_TRUE(dma != NULL);

  for (int len = 1; len < 1024; len += 7) {
    _test_dma_copy(st, dma, 0, len);
  }

  for (int off = 1; off < 1024; off += 7) {
    _test_dma_copy(st, dma, off, 1024);
  }

  ret = st_udma_free(dma);
  EXPECT_GE(ret, 0);
}

static void test_dma_copy_fill_async(struct st_tests_context* ctx, bool fill) {
  st_handle st = ctx->handle;
  st_udma_handle dma;
  int ret;
  uint16_t nb_desc = 1024;
  int nb_elements = nb_desc * 8, element_size = 1260;
  int fb_size = element_size * nb_elements;
  int fb_dst_iova_off = 0, fb_src_iova_off = 0;
  uint8_t pattern = 0xa5;

  dma = st_udma_create(st, nb_desc, ST_PORT_P);
  ASSERT_TRUE(dma != NULL);

  void *fb_dst = NULL, *fb_src = NULL;
  st_iova_t fb_dst_iova, fb_src_iova;
  unsigned char fb_dst_shas[SHA256_DIGEST_LENGTH];
  unsigned char fb_src_shas[SHA256_DIGEST_LENGTH];

  /* allocate fb dst and src(with random data) */
  fb_dst = st_hp_malloc(st, fb_size, ST_PORT_P);
  ASSERT_TRUE(fb_dst != NULL);
  fb_dst_iova = st_hp_virt2iova(st, fb_dst);
  fb_src = st_hp_malloc(st, fb_size, ST_PORT_P);
  ASSERT_TRUE(fb_src != NULL);
  fb_src_iova = st_hp_virt2iova(st, fb_src);
  if (fill)
    memset(fb_src, pattern, fb_size);
  else
    st_test_rand_data((uint8_t*)fb_src, fb_size, 0);
  SHA256((unsigned char*)fb_src, fb_size, fb_src_shas);

  while (fb_dst_iova_off < fb_size) {
    /* try to copy */
    while (fb_src_iova_off < fb_size) {
      if (fill)
        ret = st_udma_fill_u8(dma, fb_dst_iova + fb_src_iova_off, pattern, element_size);
      else
        ret = st_udma_copy(dma, fb_dst_iova + fb_src_iova_off,
                           fb_src_iova + fb_src_iova_off, element_size);
      if (ret < 0) break;
      fb_src_iova_off += element_size;
    }
    /* submit */
    st_udma_submit(dma);
    /* check complete */
    uint16_t nb_dq = st_udma_completed(dma, 32);
    fb_dst_iova_off += element_size * nb_dq;
  }

  /* all copy completed, check sha */
  SHA256((unsigned char*)fb_dst, fb_size, fb_dst_shas);
  ret = memcmp(fb_dst_shas, fb_src_shas, SHA256_DIGEST_LENGTH);
  EXPECT_EQ(ret, 0);

  st_hp_free(st, fb_dst);
  st_hp_free(st, fb_src);

  ret = st_udma_free(dma);
  EXPECT_GE(ret, 0);
}

TEST(Dma, copy) {
  struct st_tests_context* ctx = st_test_ctx();

  if (!st_test_dma_available(ctx)) return;

  test_dma_copy(ctx, 0, 1024);
  test_dma_copy(ctx, 128, 1024 * 4);
}

TEST(Dma, copy_odd) {
  struct st_tests_context* ctx = st_test_ctx();

  if (!st_test_dma_available(ctx)) return;

  test_dma_copy(ctx, 33, 1024);
  test_dma_copy(ctx, 33, 1024 - 33);
}

TEST(Dma, copy_sanity) {
  struct st_tests_context* ctx = st_test_ctx();

  if (!st_test_dma_available(ctx)) return;

  test_dma_copy_sanity(ctx);
}

TEST(Dma, copy_async) {
  struct st_tests_context* ctx = st_test_ctx();

  if (!st_test_dma_available(ctx)) return;

  test_dma_copy_fill_async(ctx, false);
}

static void _test_dma_fill(st_handle st, st_udma_handle dma, uint32_t off, uint32_t len,
                           uint8_t pattern) {
  void *dst = NULL, *src = NULL;
  st_iova_t dst_iova;
  int ret;

  dst = st_hp_malloc(st, len, ST_PORT_P);
  ASSERT_TRUE(dst != NULL);
  dst_iova = st_hp_virt2iova(st, dst);
  src = st_hp_malloc(st, len, ST_PORT_P);
  ASSERT_TRUE(src != NULL);
  memset(src, pattern, len);

  ret = st_udma_fill_u8(dma, dst_iova + off, pattern, len - off);
  EXPECT_GE(ret, 0);
  ret = st_udma_submit(dma);

  uint16_t nb_dq = 0;
  while (nb_dq < 1) {
    nb_dq = st_udma_completed(dma, 32);
  }

  ret = memcmp((uint8_t*)src + off, (uint8_t*)dst + off, len - off);
  EXPECT_EQ(ret, 0);

  st_hp_free(st, dst);
  st_hp_free(st, src);
}

static void test_dma_fill(struct st_tests_context* ctx, uint32_t off, uint32_t len,
                          uint8_t pattern) {
  st_handle st = ctx->handle;
  st_udma_handle dma;
  int ret;

  dma = st_udma_create(st, 128, ST_PORT_P);
  ASSERT_TRUE(dma != NULL);

  _test_dma_fill(st, dma, off, len, pattern);

  ret = st_udma_free(dma);
  EXPECT_GE(ret, 0);
}

static void test_dma_fill_sanity(struct st_tests_context* ctx) {
  st_handle st = ctx->handle;
  st_udma_handle dma;
  int ret;

  dma = st_udma_create(st, 128, ST_PORT_P);
  ASSERT_TRUE(dma != NULL);

  for (int len = 1; len < 1024; len += 7) {
    _test_dma_fill(st, dma, 0, len, uint8_t(rand()));
  }

  for (int off = 1; off < 1024; off += 7) {
    _test_dma_fill(st, dma, off, 1024, uint8_t(rand()));
  }

  ret = st_udma_free(dma);
  EXPECT_GE(ret, 0);
}

TEST(Dma, fill) {
  struct st_tests_context* ctx = st_test_ctx();

  if (!st_test_dma_available(ctx)) return;

  test_dma_fill(ctx, 0, 1024, 0xa5);
  test_dma_fill(ctx, 128, 1024 * 4, 0x5a);
}

TEST(Dma, fill_odd) {
  struct st_tests_context* ctx = st_test_ctx();

  if (!st_test_dma_available(ctx)) return;

  test_dma_fill(ctx, 33, 1024, 0x5a);
  test_dma_fill(ctx, 33, 1024 - 33, 0xa5);
}

TEST(Dma, fill_sanity) {
  struct st_tests_context* ctx = st_test_ctx();

  if (!st_test_dma_available(ctx)) return;

  test_dma_fill_sanity(ctx);
}

TEST(Dma, fill_async) {
  struct st_tests_context* ctx = st_test_ctx();

  if (!st_test_dma_available(ctx)) return;

  test_dma_copy_fill_async(ctx, true);
}

static void _test_dma_map(st_handle st, const void* vaddr, size_t size,
                          bool expect_succ) {
  st_iova_t iova = st_dma_map(st, vaddr, size);
  if (expect_succ) {
    EXPECT_TRUE(iova != ST_BAD_IOVA);
    int ret = st_dma_unmap(st, vaddr, iova, size);
    EXPECT_TRUE(ret >= 0);
  } else
    EXPECT_FALSE(iova != ST_BAD_IOVA);
}

static void test_dma_map(struct st_tests_context* ctx, size_t size) {
  auto st = ctx->handle;
  size_t pg_sz = st_page_size(st);
  /* 2 more pages to hold the head and tail */
  uint8_t* p = (uint8_t*)malloc(size + 2 * pg_sz);
  ASSERT_TRUE(p != NULL);

  uint8_t* align = (uint8_t*)ST_ALIGN((uint64_t)p, pg_sz);
  _test_dma_map(st, align, size, true);

  free(p);
}

TEST(Dma, map) {
  struct st_tests_context* ctx = st_test_ctx();
  auto st = ctx->handle;

  test_dma_map(ctx, 64 * st_page_size(st));
  test_dma_map(ctx, 512 * st_page_size(st));
}

TEST(Dma, map_fail) {
  struct st_tests_context* ctx = st_test_ctx();
  auto st = ctx->handle;
  size_t pg_sz = st_page_size(st);
  uint8_t* p = (uint8_t*)malloc(pg_sz / 2);

  _test_dma_map(st, p, pg_sz / 2, false);

  free(p);
}

static void test_dma_remap(struct st_tests_context* ctx, size_t size) {
  auto st = ctx->handle;
  size_t pg_sz = st_page_size(st);
  /* 2 more pages to hold the head and tail */
  uint8_t* p = (uint8_t*)malloc(size + 2 * pg_sz);
  ASSERT_TRUE(p != NULL);

  uint8_t* align = (uint8_t*)ST_ALIGN((uint64_t)p, pg_sz);
  st_iova_t iova = st_dma_map(st, align, size);
  EXPECT_TRUE(iova != ST_BAD_IOVA);

  st_iova_t bad_iova = st_dma_map(st, align, size);
  EXPECT_TRUE(bad_iova == ST_BAD_IOVA);
  bad_iova = st_dma_map(st, align + pg_sz, size - pg_sz);
  EXPECT_TRUE(bad_iova == ST_BAD_IOVA);
  bad_iova = st_dma_map(st, align - pg_sz, size);
  EXPECT_TRUE(bad_iova == ST_BAD_IOVA);

  int ret = st_dma_unmap(st, align, iova, size - pg_sz);
  EXPECT_TRUE(ret < 0);
  ret = st_dma_unmap(st, align + pg_sz, iova + pg_sz, size - pg_sz);
  EXPECT_TRUE(ret < 0);

  ret = st_dma_unmap(st, align, iova, size);
  EXPECT_TRUE(ret >= 0);

  ret = st_dma_unmap(st, align, iova, size);
  EXPECT_TRUE(ret < 0);

  free(p);
}

TEST(Dma, map_remap) {
  struct st_tests_context* ctx = st_test_ctx();
  auto st = ctx->handle;

  test_dma_remap(ctx, 64 * st_page_size(st));
}

static void test_dma_map_copy(st_handle st, st_udma_handle dma, size_t copy_size) {
  void *dst = NULL, *src = NULL;
  size_t pg_sz = st_page_size(st);
  /* 2 more pages to hold the head and tail */
  size_t size = copy_size + 2 * pg_sz;

  dst = (uint8_t*)malloc(size);
  if (!dst) return;
  src = (uint8_t*)malloc(size);
  if (!src) {
    free(dst);
    return;
  }
  st_test_rand_data((uint8_t*)src, size, 0);

  void* src_align = (void*)ST_ALIGN((uint64_t)src, pg_sz);
  void* dst_align = (void*)ST_ALIGN((uint64_t)dst, pg_sz);
  st_iova_t src_iova = st_dma_map(st, src_align, copy_size);
  st_iova_t dst_iova = st_dma_map(st, dst_align, copy_size);
  ASSERT_TRUE(src_iova != ST_BAD_IOVA);
  ASSERT_TRUE(dst_iova != ST_BAD_IOVA);

  int ret = st_udma_copy(dma, dst_iova, src_iova, copy_size);
  EXPECT_GE(ret, 0);
  ret = st_udma_submit(dma);
  uint16_t nb_dq = 0;
  while (nb_dq < 1) {
    nb_dq = st_udma_completed(dma, 32);
  }

  ret = memcmp(src_align, dst_align, copy_size);
  EXPECT_EQ(ret, 0);

  ret = st_dma_unmap(st, src_align, src_iova, copy_size);
  EXPECT_TRUE(ret >= 0);
  ret = st_dma_unmap(st, dst_align, dst_iova, copy_size);
  EXPECT_TRUE(ret >= 0);

  free(dst);
  free(src);
}

TEST(Dma, map_copy) {
  struct st_tests_context* ctx = st_test_ctx();
  auto st = ctx->handle;

  if (!st_test_dma_available(ctx)) return;

  st_udma_handle dma = st_udma_create(st, 128, ST_PORT_P);
  ASSERT_TRUE(dma != NULL);

  test_dma_map_copy(st, dma, 64 * st_page_size(st));

  int ret = st_udma_free(dma);
  EXPECT_GE(ret, 0);
}

static void test_dma_mem_alloc_free(struct st_tests_context* ctx, size_t size) {
  auto st = ctx->handle;
  st_dma_mem_handle dma_mem = st_dma_mem_alloc(st, size);
  ASSERT_TRUE(dma_mem != NULL);
  ASSERT_TRUE(st_dma_mem_addr(dma_mem) != NULL);
  ASSERT_TRUE(st_dma_mem_iova(dma_mem) != 0 && st_dma_mem_iova(dma_mem) != ST_BAD_IOVA);

  st_dma_mem_free(st, dma_mem);
}

TEST(Dma, mem_alloc_free) {
  struct st_tests_context* ctx = st_test_ctx();

  test_dma_mem_alloc_free(ctx, 111);
  test_dma_mem_alloc_free(ctx, 2222);
  test_dma_mem_alloc_free(ctx, 33333);
  test_dma_mem_alloc_free(ctx, 444444);
}