# X5 + fnOS candidate image assembly (offline, final-media verified)

This package builds a **candidate** whole-disk image only. It does not mount a
host block device, does not use sudo, and does not write physical media.

## Current contract

- manifest schema: `fnos-x5-assembly/v2`
- release path: `fnos-x5-candidate-r3.img`
- final verification: all critical checks read the **final `.img` p2** through
  private-image `fuse2fs` at offset `272629760`
- source components are recorded separately from final components; p1/p2 region
  hashes are compared to the produced components.

## A1: raw initrd load

The adapted X5 `boot.cmd` sets explicit X5 load addresses recovered from the
real `uboot.img` defaults:

```text
kernel_addr_r=0x85800000
fdt_addr_r=0x84000000
ramdisk_addr_r=0xc0000000
```

It loads `initrd.img-6.18.0`, saves `initrd_size=0x${filesize}` (U-Boot
`filesize` is hex without 0x; `test` uses base-0 parsing), rejects
empty/zero/>512 MiB, and passes `address:size` to `booti`:

```text
setenv initrd_load_addr "${ramdisk_addr_r}:${initrd_size}"
booti ${kernel_addr_r} ${initrd_load_addr} ${fdt_addr_r}
```

FDT/kernel/initrd load failures, empty size, zero size, and >512 MiB **exit**
instead of falling back to `-`. The assembly parses the selected final DTB's
enabled reserved-memory and verifies FDT/kernel/initrd ranges do not overlap
log/CMA/firewall/ADSP/ramoops, and stay within DRAM. The r2 initrd is at
`0xc0000000..0xd20b810a`.

## A2: default-deny boot-affecting units

The builder masks, by `/etc/systemd/system/<unit> -> /dev/null`:

```text
resize-rootfs.service
updatemgr.service
apt-daily.timer
apt-daily-upgrade.timer
unattended-upgrades.service
auto_update_miniboot.service
```

Original `/etc` unit files are preserved under `/etc/fnos-x5/disabled-units/`.
There is no allow file. Core fnOS services (`smbd`, `nmbd`, `trim_nginx`,
`trim_http_cgi`, `docker`, `containerd`, `nfs-server`) remain enabled.
Final verification uses `systemctl --root=<final-p2-mount> is-enabled` and
requires `masked` for every masked unit.

## A3: identity contract

The image is **Debian 12 userspace** (`/etc/os-release` `ID=debian`,
`VERSION_ID=12`) with fnOS `trim` 1.2.0701 from `/usr/trim/etc/version` and
`/var/lib/dpkg/status`. The manifest exposes both facts; it does not fabricate
`ID=fnos`.

## A4: final-media verification and repro

Run under **fakeroot** with private `btrfs-progs`, `u-boot-tools`, and
`fuse2fs` in PATH. Use a `/tmp` work directory for p2 (tmpfs), and write the
final image to `/data`:

```sh
B=/srv/fnos-build/assembly-20261106
A=/srv/fnos-build/artifacts/recovered-fix19-d255-20261105
SRC=/srv/fnos-build/fnos-input-audit-20261106/work/audit/rootfs-p2.btrfs
cd "$B/tool"
fakeroot -- env \
  PATH=/tmp/btrfs-tool/root/usr/bin:/tmp/btrfs-tool/root/usr/sbin:/tmp/uboot-tool/root/usr/bin:/tmp/fuse2fs-tool/root/usr/bin:$PATH \
  PYTHONPATH="$B/tool" \
  python3 -m fnos.integration.image_assembly.assemble \
  --source-rootfs "$SRC" \
  --x5-artifacts "$A" \
  --boot-cmd "$B/input/boot.cmd" \
  --mbr "$B/input/mbr.bin" \
  --work /tmp/fnos-r2-work \
  --output "$B/artifacts/fnos-x5-candidate-r2.img" \
  --rootfs-size 12G \
  --rootfs-uuid c2d3e4f5-a6b7-4c8d-9e0f-1a2b3c4d5e6f \
  --source-image-sha256 6807ba7ed52d0b8292b029423cfb667b373491fbc62138393351759a54c21e60 \
  --release r2
```

The first r1 attempt (missing `boot.scr`) is preserved under
`artifacts/failed/fnos-x5-candidate-r1-missing-bootscr.img` with its log; it was
not overwritten. r2 and the failed r3 keyerror attempt are also preserved.

## r3 boot-asset revision

`rebuild_boot.py` is the replayable path for r3. It copies the verified r2
image, regenerates only `boot.cmd`/`boot.scr`, writes them into the copied p2
through private-image `fuse2fs`, and runs final p2 readback:

```sh
cd "$B/tool"
PATH=/tmp/uboot-tool/root/usr/bin:/tmp/fuse2fs-tool/root/usr/bin:$PATH PYTHONPATH="$B/tool" python3 -m fnos.integration.image_assembly.rebuild_boot   --base-image "$B/artifacts/fnos-x5-candidate-r2.img"   --base-manifest "$B/artifacts/fnos-x5-candidate-r2.img.manifest.json"   --boot-cmd "$B/input/boot.cmd"   --output "$B/artifacts/fnos-x5-candidate-r3.img"   --work /tmp/fnos-r3-work --release r3   --live-bdinfo "$B/input/uboot-memory-evidence-20261106.json"   --live-numeric "$B/input/uboot-numeric-probes-20261106.json"
```

The success branches use `echo`, not the live-unsupported `:` builtin.

## Household storage and acceleration runtime (2026-11-09)

Build the home bridge on the designated build server, then pass the resulting
AArch64 library with `--home-acl-library`. `--runtime-sources` adds the pinned
Wi-Fi/multimedia/DNN runtime; `--bpu-module` is the separately reviewed X5
compatibility module. These are image assembly inputs, not commands for NAS
users to run after installation.

```sh
aarch64-linux-gnu-gcc -O2 -Wall -Wextra -Werror -fPIC -shared \
  fnos/integration/acceleration/home_acl.c \
  fnos/integration/acceleration/home_trash.c \
  -ldl -o libfnos_home_acl.so
```

The storage option stages the library, service configuration and native
application environment adapter. Final-media verification checks their actual
bytes, root ownership and modes. The application adapter is pinned to the
reviewed fnOS ARM application-center binary; verify that input when selecting a
new base image. See `../../docs/home-storage-policy-20261109.md` for native
workflow evidence and explicit limits; the historical "Not tested" list below
describes the original r3 image assembly, not the later live integration.

## Tests

- `tests/test_assemble.py`: MBR + boot.cmd `address:size` / fail-closed.
- Input audit tests remain 30/30.
- Final verification mounts the final p2 read-only and checks actual files,
  p1/p2 region hashes, OS/trim facts, masks, boot ranges, AArch64, initramfs.

## Not tested

Real X5 boot, SPI Miniboot/U-Boot, network, SMB, upgrade, firmware/BPU/GPU, and
physical media write. Static acceptance is pending Codex re-review.

## 独立 ARM64 加速模块（2026-11-12）

`product_inputs.py` 在完整镜像内放置同一份 in-place `bundle.json` /
`compat.json`（不复制大模型树）；`product_bootstrap.py --repair-runtime` 同时
检查相册和影视的已知原生哈希。独立 `.fpk` 由
`fnos/integration/acceleration/build_module.py` 从已组装 root 收集同一批文件，
安装/升级/卸载入口见
[`fnos/integration/acceleration/module/README.md`](../acceleration/module/README.md)。
当前 r24 的真板测试范围见 [仓库验收记录](../../../TESTED.md)。
