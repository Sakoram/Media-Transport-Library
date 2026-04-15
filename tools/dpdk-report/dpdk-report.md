
h3. ice: E830 SEND_ON_TIMESTAMP offload does not transmit packets (opackets=0)

h4. Summary

When {{RTE_ETH_TX_OFFLOAD_SEND_ON_TIMESTAMP}} is enabled on an Intel E830 NIC, packets submitted via {{rte_eth_tx_burst()}} are accepted but never transmitted — {{opackets}} stays 0. Without the offload, the same port transmits normally. The same hardware works via the kernel ice driver with ETF qdisc HW offload.

Removing one register write in {{ice_tx_queue_start()}} fixes the issue. Patch attached.

h4. Environment

* DPDK version: {{26.03.0}}
* OS: {{Linux 5.15.0-73-generic x86_64}}
* Compiler: {{gcc 11.4.0}}
* NIC: Intel E830 100G (PCI ID {{8086:12d2}})
* NIC firmware: {{1.20 0x80017ef4 1.3909.0}}
* Kernel ice driver (reference): ice-2.5.4

h4. Steps to reproduce

{noformat}
# Bind E830 port to vfio-pci
dpdk-devbind.py -b vfio-pci 0000:ca:00.0

# Build and run the attached reproducer
meson setup builddir && ninja -C builddir
sudo ./builddir/txtime_repro -l 0-1 -a ca:00.0 --iova-mode=pa
{noformat}

The reproducer sends packets with and without the timestamp flag on the same port.

h5. Actual result

{noformat}
--- Test A: send 3 packets WITHOUT timestamp flag ---
tx_burst: 3 / 3
opackets: 3
OK: port transmits without timestamp flag

--- Test B: send 10 packets WITH timestamp flag ---
tx_burst: 10 / 10
  +1s: opackets=0  oerrors=0
  +5s: opackets=0  oerrors=0

FAIL: opackets=0 — launch-time packets never transmitted
{noformat}

h5. Expected result

{{opackets=10}} after Test B. With the attached patch applied, this is the observed result.

h4. Observations

In {{ice_tx_queue_start()}} ({{drivers/net/intel/ice/ice_rxtx.c}}), the TXTIME branch writes 0 to the TXTIME doorbell during queue initialization:

{noformat}
txq->qtx_tail = hw->hw_addr + E830_GLQTX_TXTIME_DBELL_LSB(txq->reg_idx);

/* Init the Tx time tail register*/
ICE_PCI_REG_WRITE(txq->qtx_tail, 0);       /* <--- problematic write */

err = ice_aq_set_txtimeq(hw, txq->reg_idx, 1, ts_elem, ts_buf_len, NULL);
{noformat}

Observed behavior:
* After this write, PQM MDD event 29 is raised (read from {{E830_GL_MDET_TX_PQM}} and its FIFO). All subsequent TXTIME transmissions produce {{opackets=0}}.
* Removing this write: no MDD event, packets transmit normally with correct launch-time pacing.
* The kernel ice driver does not write to the TXTIME doorbell during queue setup — verified via mmiotrace.
* The regular TX tail ({{QTX_COMM_DBELL}}) init to 0 in the else branch is not affected.

h4. Fix

Remove {{ICE_PCI_REG_WRITE(txq->qtx_tail, 0)}} from the TXTIME branch of {{ice_tx_queue_start()}}. Keep the pointer assignment.

h4. Regression

N — SEND_ON_TIMESTAMP on E830 has never worked in DPDK.

h4. Attachments

* {{txtime_repro.c}} — Minimal reproducer
* {{meson.build}} — Build file
* {{0001-ice-e830-do-not-write-TXTIME-doorbell-during-queue-s.patch}} — Fix (applies to 26.03)