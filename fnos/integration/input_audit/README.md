# fnOS ARM 输入只读审计工具

本目录实现 **只读** 的 fnOS ARM 镜像依赖审计：解压到私有工作目录、解析 GPT/MBR、识别根文件系统、通过 `debugfs` / `unsquashfs` 读取文件，不 mount、不执行镜像内脚本/服务/安装器、不使用 sudo、不写块设备。

输出两份文件：

- `audit.json`：机读证据，schema 为 `fnos-input-audit/v1`；
- `audit.md`：简明中文报告（可选）。

> 静态审计只回答“镜像里有什么依赖”。它 **不能** 证明 fnOS 能在 RDK X5 启动、联网、SMB 或升级；这些必须由实机启动/读写/升级测试证明。

## 1. 快速开始

在仓库根目录：

```sh
python3 -m fnos.integration.input_audit audit \
  --image /path/to/fnos_arm.img.gz \
  --official-md5 <官方页面 MD5> \
  --source-url 'https://.../official.img.gz' \
  --work-dir /path/to/private/work \
  --out-json /path/to/audit.json \
  --out-md /path/to/audit.md \
  --kernel-config /path/to/config-6.18.0 \
  --kernel-image /path/to/Image-6.18.0 \
  --dtb /path/to/x5-rdk.dtb \
  --kernel-modules /path/to/modules-root
```

测试：

```sh
python3 -m unittest -v fnos.integration.input_audit.tests.test_audit
```

在没有 `debugfs`/`mke2fs`/`mkfs.btrfs`/`blkid` 的主机上，相应后端/UUID 测试会明确 `skipped`；其余解析/边界测试仍运行。服务器上可用 `FNOS_AUDIT_REAL_PARTITIONS=<含 rootfs-p1.ext4/rootfs-p2.btrfs 的目录>` 启用真实分区与 `blkid -p` 交叉核对。`--rootfs-dir` 只用于目录 fixture，不是真实镜像审计。

## 2. 安全边界

| 动作 | 本工具 |
| --- | --- |
| 解压 | 只在 `--work-dir` 写解压副本 |
| 分区/文件系统解析 | 纯 Python 读取魔数；GPT CRC 校验 |
| ext4 文件读取 | `debugfs -R`，对私有分区副本只读 |
| squashfs | `unsquashfs -l/-cat`（不 mount） |
| btrfs | `btrfs restore` 到私有目录（需 PATH 中有 `btrfs`；非 mount） |
| xfs/LUKS | 目前明确记为 gap，不猜测、不 mount |
| 镜像内脚本/服务/安装器 | **从不执行** |
| `mount`/`losetup`/`sudo`/`systemctl` | 主机工具 allow-list 明确拒绝 |
| 块设备写入 | 不实现 |
| 凭据 | 不读取、不落盘 |

`ToolRunner` 只允许 `debugfs`、`dumpe2fs`、`unsquashfs`、`btrfs`、`zstd`；调用记录写入 `safety.host_tools_used`。遇到缺失工具时输出 `gaps[]` 或以非零退出，不静默 `sudo` 安装。

## 3. JSON 主要字段

```text
schema, tool, generated_at, mode
input.{path,size,sha256,compression,decompressed,official}
image.{table,partitions[],meta}
rootfs.{fstype,partition_index,uuid,os_release,debian_version,hostname}
rootfs.elf.{samples[],foreign[],loader,libc,max_glibc_requirement}
rootfs.packages.{manager,count,key,installed}
rootfs.systemd.{enabled[],custom_units[],unit_files,init}
rootfs.boot.{files[],configs,initramfs}
rootfs.scripts.{resize_scripts[],boot_references[],update_references[],assessment}
rootfs.modules.{versions,kernelrelease,count,notable,vermagic,paths}
rootfs.network.{managers,nic_modules,board_identity}
rootfs.storage.{samba,nfs,packages,fstab,mount_units,storage_stack}
kernel_compare.config.{performed,checks[],missing[]}
kernel_compare.modules.{performed,intersection,fnos_only,fnos_only_generic}
findings[]  # severity P0/P1/P2/INFO
gaps[]
verdict.{static_audit,fnos_bootable_on_x5,statement}
assembly_interface.{kernel,boot,userspace,update_boundary,gates}
```

`input.official.checksum_verified` 只有在提供了原厂 MD5 且实际 MD5 一致时才为 `true`；SHA-256 是本项目计算，不冒充官方哈希。

## 4. 当前后端与明确缺项

| 根文件系统 | 状态 | 说明 |
| --- | --- | --- |
| ext4 | 已实现并有服务器测试 | `debugfs` 读私有分区副本；无 mount |
| btrfs | 已实现（真实 1.2.0701 镜像验证） | 需要 PATH 中有 `btrfs`；用 `btrfs restore -i -S` 还原到私有目录，非 mount、不恢复 owner |
| squashfs | 已实现（基础） | `unsquashfs` 列表/`-cat`；权限/大小字段有限 |
| xfs | **未实现** | 输出 gap |
| LUKS | **未实现** | 识别为加密，不做解密 |
| ISO9660 | 未实现 | 需要 `xorriso`/ISO 解析后端；不冒充支持 |

遇到未实现后端时，工具仍会输出分区表、文件系统类型和 `gaps[]`，并以非零状态提醒。

## 4.1 UUID 解析约定

- ext4 `s_uuid`、btrfs `fsid` 按超级块中的 **16 字节 canonical UUID** 解析；
- GPT 分区/磁盘 GUID 按 UEFI **mixed-endian** 解析，不能与文件系统 UUID 共用同一函数；
- 测试用不对称 UUID `00112233-4455-6677-8899-aabbccddeeff`，并在服务器上对真实 ext4/btrfs 分区与 `blkid -p` 交叉核对（`FNOS_AUDIT_REAL_PARTITIONS`）。

## 5. 规则解释

- **P0**：组装/首次启动前必须解决；例如根文件系统缺失、非 AArch64 ELF、loader/libc 缺失、X5 6.18 缺少观察到的必需内核符号、更新路径可能写 boot/SPI/NAND。
- **P1**：交付候选前解决；例如首启扩容脚本假设、SMB 配置缺失、更新路径未验证。
- **P2**：可后置；例如容器/加密/RAID 可选能力、BPU/VPU/GPU。
- `confidence=observed` 表示来自镜像文本/二进制；`engineering` 表示按 X5 启动链要求推导，仍须实机验证。

## 6. 已知限制

1. `debugfs` 需要先把根分区复制到 `--work-dir`；btrfs 还需要 `btrfs restore` 的还原树。大镜像会占用与根分区等量或更多的临时空间，工具会预估并要求足够空间。
2. ELF 只扫描关键二进制样本，不是全量 census；`rootfs.elf.note` 会说明。
3. 服务/更新/扩容判断是**有界文本扫描**，不是执行/跟踪；`rootfs.scripts.coverage` 会说明扫描范围。
4. 模块 vermagic 只抽样小模块；模块清单对比是名字级，不等价于 ABI/符号兼容。
5. GPT 按 512 B 扇区解析；4K 原生扇区镜像会显式报错/列为 gap，不猜测。
6. btrfs 后端依赖宿主 `btrfs` 二进制。本批用 `apt-get download btrfs-progs=6.17.1-1build1` + `dpkg-deb -x` 的私有副本，没有全局安装；如果 PATH 中没有 `btrfs`，会输出 `ToolMissing`/gap，不会改用 mount。
7. 目录 fixture 模式（`--rootfs-dir`）仅用于测试解析器与报告，不构成真实镜像证据。
