# Linux 6.18 kernel pin (v0.3.0 draft)

| Item | Value |
| --- | --- |
| Kernel release | `6.18.0` |
| r24 config SHA-256 | `bee5f9981b13f72ea85374630a5545c0da58c776b51e57308bad36baac5ebf20` |
| v0.2.0 release config SHA-256 | `bee5f9981b13f72ea85374630a5545c0da58c776b51e57308bad36baac5ebf20` |
| r24 kernel Image SHA-256 | `1429ef8ffa19d3136d2c848c0ea1639b743ad31783b7bea722b840072cfd2cbb` |
| v0.2.0 kernel source archive SHA-256 | `be478464d0ad66150bd8325858ef9f53b9f92528b88b5091ffe422f5c4d9dea3` |
| r24 underlying build source | `fnos-storage-timeoutfix-20261111` (llbitmap timeout reference-resurrection change) |

The verified v0.2.0 source archive records the exact kernel Image and config used by r16 and r24. The archive SHA-256, embedded build identity and config bytes were verified. The matching source archive can accompany the r24 candidate.

The download instructions in the [v0.2.0 release](https://github.com/xuguosheng110/rdk-x5-fnos/releases/tag/v0.2.0)
link to the matching `rdk-x5-linux-6.18-storage-r16-source.tar.gz`. The
archive carries the kernel source and config under its applicable upstream licenses.

[D-Robotics kernel repository](https://github.com/D-Robotics/kernel) is an upstream
reference. The archive above pins the exact build used for this fnOS candidate.
