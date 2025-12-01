/* SPDX-License-Identifier: BSD-3-Clause
 * Copyright(c) 2025 Intel Corporation
 */

#include "core/constants.hpp"
#include "core/test_fixture.hpp"

TEST_F(NoCtxTest, st40p_basic_loopback) {
  ctx->para.ptp_get_time_fn = NoCtxTest::FakePtpClockNow;
  ctx->para.log_level = MTL_LOG_LEVEL_INFO;

  ctx->handle = mtl_init(&ctx->para);
  ASSERT_TRUE(ctx->handle != nullptr);

  auto bundle = createSt40pHandlerBundle(/*createTx=*/true, /*createRx=*/true,
                                         /*strategyFactory=*/nullptr);
  auto* handler = bundle.handler;
  ASSERT_NE(handler, nullptr);

  handler->startSession();
  sleepUntilFailure(5);
  handler->stopSession();

  EXPECT_GT(handler->txFrames(), 0u) << "st40p did not transmit any frames";
  EXPECT_EQ(handler->txFrames(), handler->rxFrames())
      << "st40p TX/RX frame count mismatch";
}
