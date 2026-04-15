# SPDX-License-Identifier: BSD-3-Clause
project('txtime_test', 'c',
  default_options: ['c_std=c11', 'warning_level=2'])

dpdk_dep = dependency('libdpdk', required: true)

executable('txtime_test',
  'txtime_test.c',
  dependencies: [dpdk_dep],
  install: false)

# Standalone register dump tool (no DPDK dependency)
executable('read_txtime_regs',
  'read_txtime_regs.c',
  install: false)
