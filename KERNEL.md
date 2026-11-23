# Linux 6.18 kernel builds for RDK X5 fnOS

## v0.3.3 matching source bundle

The [v0.3.3 Release](https://github.com/xuguosheng110/rdk-x5-fnos/releases/tag/v0.3.3)
provides `rdk-x5-linux-6.18-pr4-source.zip` (270,076,035 bytes), SHA-256
`17bc6cbd2760515c5a7fcf316e81e084990c6c7b06ef3361633c7a00003b06ae`.
It contains the complete X5 kernel source archive, the two exact additional Wi-Fi
patches, matching config, target manifest and build notes. Kernel Image remains
`044539fb…d741`; config remains `04bde257…13d`. Original COPYING/LICENSES remain
inside the kernel source archive.

Reference baseline: [upstream Linux v6.18](https://github.com/torvalds/linux/tree/v6.18).
The X5-specific source snapshot, configuration and appended patches are provided
in the matching bundle above; adaptation Python source is in this repository.

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

## v0.3.3 AIC8800 SDIO Wi-Fi 修复

在 v0.3.2 和 v0.3.3 使用的 Linux 6.18.0 X5 构建上，应用
[`0010`](patches/rdk-x5-linux-6.18/0010-wifi-aic8800-balance-command-queue-lock.patch) 和
[`0011`](patches/rdk-x5-linux-6.18/0011-wifi-aic8800-honor-d80-sdio-clock-limit.patch)。
前一补丁保证固件命令队列进入故障状态后仍成对加锁和解锁。后一补丁让 D80 的 SDIO 初始化遵循 BSP 提供的时钟上限，同时保留已协商的较低速率。

本次使用的 Image 摘要为 `608dbb9fe29e49ac1f10ca009f9004ff069c2c28260695806a2a1fc67a3312f0`，
构建目录的 `.config` 摘要为 `8948b71418f4c69f346a8ac57548097500c07770e2ea0b9d2472781aa52b6d3a`。
在匹配该内核的源码与输出目录中应用补丁，并重新构建模块。用完整的模块构建保留对 `cfg80211` 和 `aic8800_bsp` 导出符号的校验。

```sh
patch -d "$kernel_source" -p1 < "$repo/patches/rdk-x5-linux-6.18/0010-wifi-aic8800-balance-command-queue-lock.patch"
patch -d "$kernel_source" -p1 < "$repo/patches/rdk-x5-linux-6.18/0011-wifi-aic8800-honor-d80-sdio-clock-limit.patch"
make -C "$kernel_source" O="$kernel_output" ARCH=arm64 LLVM=1 modules
llvm-strip --strip-debug -o aic8800_fdrv.ko \
  "$kernel_output/drivers/net/wireless/aic8800/aic8800_fdrv/aic8800_fdrv.ko"
```

安装前核对模块的 `vermagic`、依赖和 [release.json](release.json) 中的摘要。使用构建结果制作[原地更新包](fnos/integration/in_place_update/README.md)，包会备份并替换驱动，回滚时还原。重启后用 `cat /sys/module/aic8800_fdrv/srcversion` 核对加载的版本。
