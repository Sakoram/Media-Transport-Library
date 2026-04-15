project('txtime_repro', 'c',
  default_options: ['c_std=c11', 'warning_level=2'])

dpdk_dep = dependency('libdpdk', required: true)

executable('txtime_repro',
  'txtime_repro.c',
  dependencies: [dpdk_dep],
  install: false)

executable('txtime_timestamp_test',
  'txtime_timestamp_test.c',
  dependencies: [dpdk_dep],
  install: false)

executable('txtime_edge_test',
  'txtime_edge_test.c',
  dependencies: [dpdk_dep],
  install: false)
