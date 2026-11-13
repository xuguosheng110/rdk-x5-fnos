# Linux 6.18 kernel builds for RDK X5 fnOS

## Published v0.3.0 source baseline

| Item | Value |
| --- | --- |
| Kernel release | `6.18.0` |
| r24 config SHA-256 | `bee5f9981b13f72ea85374630a5545c0da58c776b51e57308bad36baac5ebf20` |
| v0.2.0 release config SHA-256 | `bee5f9981b13f72ea85374630a5545c0da58c776b51e57308bad36baac5ebf20` |
| r24 kernel Image SHA-256 | `1429ef8ffa19d3136d2c848c0ea1639b743ad31783b7bea722b840072cfd2cbb` |
| v0.2.0 kernel source archive SHA-256 | `be478464d0ad66150bd8325858ef9f53b9f92528b88b5091ffe422f5c4d9dea3` |
| r24 underlying build source | `fnos-storage-timeoutfix-20261111` (llbitmap timeout reference-resurrection change) |

The verified v0.2.0 source archive records the exact kernel Image and config used by r16 and r24. The archive SHA-256, embedded build identity and config bytes were verified. r25 preserves the same kernel Image and configuration. The matching source archive covers both r24 and r25.

The download instructions in the [v0.2.0 release](https://github.com/xuguosheng110/rdk-x5-fnos/releases/tag/v0.2.0)
link to the matching `rdk-x5-linux-6.18-storage-r16-source.tar.gz`. The
archive carries the kernel source and config under its applicable upstream licenses.

[D-Robotics X5 kernel repository](https://github.com/D-Robotics/x5-kernel) is an upstream
reference. The archive above pins the exact build used for this fnOS candidate.


## r26 candidate

r26 enables `CONFIG_FHANDLE=y` for the native directory monitor. It uses the
same Linux 6.18.0 source snapshot as the published v0.3.0 baseline. The r26
source archive pairs that snapshot with the exact r26 config, compiler versions,
and build identity.

| File | SHA-256 |
| --- | --- |
| Image | `044539fbf2d6760b389661b221ab1fe918e70860de0671d3dedb9309a2d0d741` |
| config-6.18.0 | `04bde2575e6a878ab34ec44aab6719e087622ef2a99808eadc0fbbbad87c013d` |
| System.map-6.18.0 | `e50a57497a66b4987608cb32a72330b28ac319d6514d36b63696b7496a3d37a1` |
| rdk-x5-linux-6.18-r26-source.tar.gz | `8eafd1d48d55b3cdcba7df643db099091d872b9aa9d152557c99bfe34a37a72c` |

The kernel, 14 device trees and 940 in-tree modules were built on Linux.
The bundled BPU I/O module is versioned separately. The r26 image has passed
50 image checks. Native SMB, first boot and application regression remain
subject to X5 hardware acceptance.
