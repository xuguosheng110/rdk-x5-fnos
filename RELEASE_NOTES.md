# v0.2.0 — RDK X5 U 盘 Basic 存储修复

这一版修复了部分 RDK X5 上在飞牛界面创建 Basic 存储空间时的失败。GitHub 提供 X5 适配源码、Linux 6.18 内核源码和构建配置、修复补丁与校验文件。

## 更新

- Linux 6.18.0 #5 启用 `CONFIG_MD_LLBITMAP=y`，让飞牛原生 `mdadm` 可以为 Basic 卷设置 lockless bitmap。
- 回移上游 llbitmap 超时恢复补丁，等待超时后恢复内核引用计数。组装器会检查必需的 MD、RAID1 和 llbitmap 配置。
- r16 沿用 v0.1.0 的应用和运行时构建输入。该版本的变化集中在存储内核及组装校验。

## 验证范围

- 同一 #5 内核在真板通过 120 GB U 盘的原生 Basic/Btrfs 创建。128 MiB 测试文件写入回读成功，冷启动自动挂载后 SHA-256 一致。
- r16 完整 SD 镜像通过写入、全量回读、首启、首次设置和已有 Basic 卷导入。同一 #5 内核的冷启动测试也验证了 2 TB 外置 ext4 盘的复挂和只读抽样。
- 公开源码回归 162 项通过、1 项跳过，组装回归 10 项通过。超时恢复用例验证了模拟超时和正常完成两条分支。

全新 r16 镜像上的新建卷、双盘 ext4 RAID1 和真实超时故障注入待实测。早期 16 GB U 盘的掉线原因仍待定位。家庭共享权限提示及 v0.1.0 的应用兼容范围见 [真板测试记录](https://github.com/xuguosheng110/rdk-x5-fnos/blob/v0.2.0/TESTED.md)。

## 下载与构建

从 [百度网盘](https://pan.baidu.com/s/1F5t3ehuAREo-Sy2zhoMIWw?pwd=ugjr) 下载 `rdk-x5-fnos-v0.2.0.img.xz`、对应内核源码 `rdk-x5-linux-6.18-storage-r16-source.tar.gz`、`SHA256SUMS` 和镜像安装说明。提取码 `ugjr`。从 [v0.2.0 GitHub Release](https://github.com/xuguosheng110/rdk-x5-fnos/releases/tag/v0.2.0) 获取适配源码 ZIP、校验值和真板结果。

使用镜像时先校验 XZ 文件，再解压并写入 RDK X5 的系统 SD 卡。自行组装需要依法取得的 fnOS ARM 用户态及对应厂商运行库。

将下载文件放在同一目录后核对校验值。内核归档中的 `kernel-build-reference/` 包含实际构建配置、编译器版本和产物哈希。源码已经包含超时恢复补丁。

```sh
sha256sum -c --ignore-missing SHA256SUMS
tar -xzf rdk-x5-linux-6.18-storage-r16-source.tar.gz
src="$PWD/fnos-storage-timeoutfix-20261111"
out="$PWD/kernel-build"
mkdir -p "$out"
cp kernel-build-reference/config-6.18.0 "$out/.config"
make -C "$src" O="$out" ARCH=arm64 LLVM=1 CROSS_COMPILE=aarch64-linux-gnu- olddefconfig
make -C "$src" O="$out" ARCH=arm64 LLVM=1 CROSS_COMPILE=aarch64-linux-gnu- -j"$(nproc)" Image dtbs modules
```

部署时配套使用同次构建的 Image、DTB、内核模块和 initramfs。镜像组装入口与输入格式见 [组装文档](https://github.com/xuguosheng110/rdk-x5-fnos/blob/v0.2.0/fnos/integration/image_assembly/README.md)。
