# RAM 救援烧录入口（需主线程独立审查，尚未上板）

用途是从已批准的服务器镜像写入已确认的 **X5 SD 卡**，然后完整回读。它不负责 U-Boot、切电、重启或产品功能验收，也不向任何数据盘写入。

实现为 Mac 控制器 `ram_deploy.py` 和另外上传到救援 `/run` 的静态 AArch64 `ram_deploy_helper.c`。已冻结的 initramfs 不修改。生产助手只有固定 `/dev/mmcblk1`，精确绑定 CID `035344534b333247854bf6868b01a200`、容量 `31914983424` 字节；没有测试/普通文件后端开关。模拟后端必须另用 `-DRAM_DEPLOY_TEST` 编译，绝不能上传该二进制。

已构建的生产文件：

```text
build-server:/srv/fnos-build/ram-deploy-20261110-r1/ram-deploy-helper-aarch64
bytes: 852072
SHA256: afd3a217d64546777f498a7c2f1800ed3895cddf51dba22442b088670131853d
```

当前控制器修订是 **r2**，证据为 `ram_deploy_offline_acceptance_r2.json`；服务器冻结目录是 `ram-deploy-20261110-r2`。生产 C 源码和上面的静态助手 SHA 均未变化。r2 的服务器 Linux 30 项、AArch64 QEMU 30 项通过；没有真实块设备 ioctl、救援启动或烧录通过结论。旧 `ram_deploy_offline_acceptance.json` 和服务器 r1 目录仅保留历史，不能作为当前控制器的通过证据。

r2 修复了独立审核发现的 P1：证据磁盘空间不足或 stdout BrokenPipe 曾可能使异常日志再次抛错，跳过远端终止确认。现在所有诊断输出均不能抛出至控制流程，`execute` 的第一项 `finally` 操作强制确认 writer terminal；未知状态下即使查询/日志再次异常，也继续持有物理锁。诊断不完整不产生最终部署 PASS；明确非0、非255的 SSH 远端退出会直接记录失败，不再无限查询可能损坏的状态文件。新增6项有针对性的故障回归。

编译只在指定服务器进行：

```sh
aarch64-linux-gnu-gcc -static -O2 -Wall -Wextra -Werror \
  fnos/integration/hardware/ram_deploy_helper.c -o ram-deploy-helper-aarch64
gcc -O2 -Wall -Wextra -Werror -DRAM_DEPLOY_TEST \
  fnos/integration/hardware/ram_deploy_helper.c -o helper-test
RAM_DEPLOY_TEST_BINARY="$PWD/helper-test" PYTHONDONTWRITEBYTECODE=1 \
  python3 -m unittest fnos.tests.test_ram_deploy -v
```

执行前，主线程先按 `rescue-stage-20261110.json` 核对三文件 SHA，临时 U-Boot `ext4load mmc 1:2` / `booti` 进入 RAM，不 `saveenv`。通过串口记录新的 rescue boot ID、DHCP 地址、Dropbear2222 host key，并在独立 `known_hosts` 中固定这个 host key。不要覆盖正常 fnOS 的 SSH 信任记录。救援根必须为 ramfs/tmpfs；SD、外盘、MD/LVM 均不能挂载，swap 必须为空，SD 和分区不能有 holders。先读取实际 `/sys/block/mmcblk1/dev`，不要猜 minor 编号。

镜像最终批准记录使用独立文件，初始模板应保持 `accepted:false`，不能把旧 r3/r10 离线检查直接转换成批准。主线程验收通过后填写实际值，记录该 JSON 自身 SHA，再调用控制器。下面仅为字段说明，不是可运行批准：

```json
{
  "schema": "fnos-ram-deploy-approval/v1",
  "accepted": false,
  "review_status": "pending_independent_review",
  "reviewer": "Codex independent acceptance",
  "server": "guosheng@192.0.2.10",
  "image": {"path": "<最终完整镜像的服务器绝对路径>", "bytes": 0, "sha256": "<最终完整镜像SHA>"},
  "target": {"board_id": "x5-lab-01", "device": "/dev/mmcblk1", "cid": "035344534b333247854bf6868b01a200", "bytes": 31914983424, "major": 179, "minor": 0},
  "rescue_boot_id": "<本次救援boot_id>",
  "rescue_host": "<串口确认的IPv4>",
  "known_hosts_sha256": "<串口确认的独立known_hosts文件SHA>",
  "helper_sha256": "afd3a217d64546777f498a7c2f1800ed3895cddf51dba22442b088670131853d",
  "selective_backup_review_sha256": "<selective-backup-independent-review-20261110.json文件SHA>"
}
```

批准时 `review_status` 必须是 `independently_approved_for_board_deploy`。关键文件备份已经 PASS，不再要求整卡备份。控制器绑定独立备份验收文件的 SHA、明确 `backup_mode=selective_backup`、完整性检查和私有本地 `key-files.tar.gz` 的大小/SHA/权限。它不会把“空卡跳过”证据当作关键备份成功，也不复制私有文件进镜像。

从服务器取生产 helper 到 Mac 私有工作目录并核对上述 SHA。以下调用默认只检查本地批准/备份/二进制/host-key 文件，**零网络和板卡操作**：

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m fnos.integration.hardware.ram_deploy \
  --approval /private/approved-deploy.json \
  --approval-sha256 '<该JSON的独立SHA>' \
  --helper /private/ram-deploy-helper-aarch64 \
  --backup-review fnos/logs/native-product-20261109/selective-backup-independent-review-20261110.json \
  --known-hosts /private/rescue-known-hosts \
  --rescue-host '<本次救援IPv4>' \
  --evidence-dir /private/new-deploy-evidence
```

主线程独立审查工具并完成救援验证后，在相同参数后加 `--execute` 才会运行。若主线程已经持有统一物理锁，使用 `--lock-fd <held.fd>`，通过 `pass_fds=held.pass_fds` 启动，并使用相同 `--lock-root`；工具验证继承的锁，不重复获取，不释放父锁。否则控制器自己持有 `/home/your-user/.local/state/fnos-lab` 下 `x5-lab-01` 锁。父线程应一直保持该锁到冷启动和验收，不能同时派出串口/电源/磁盘任务。

真实传输顺序：

1. 用严格 host-key 校验连接端口2222；核对 rescue boot ID 和根为 RAM；在 `/run/fnos-ram-deploy-<token>` 新建私有目录、上传 helper 并核对 SHA。
2. 服务器打开镜像一次，通过同一 FD 完整计算 SHA、检查长度和稳定的 inode/mtime/ctime，再发出 `SOURCE_VERIFIED`；后续也从该 FD 传输。
3. 助手再次核对 RAM 根、boot ID、无块设备挂载、无 swap/holders、SD CID/容量/major:minor。以 `O_EXCL|O_NOFOLLOW` 打开 SD，再以 `fstat`、`BLKGETSIZE64`、`BLKSSZGET` 和再次 sysfs 核对绑定该 FD；sda 不是可选目标。
4. 只写批准的镜像长度；输入过短、多余字节、短写、错误 SHA 均失败。SHA 使用救援既有可信 BusyBox `sha256sum` 子进程，不包含自行编写的密码学代码。
5. 同一 FD 执行 `fsync` 和 `BLKFLSBUF`，丢弃块设备缓存，再完整读取镜像长度计算 SHA；短读、错误 SHA、身份变化均失败。镜像以外的 SD 尾部保持不变。
6. 控制器同时要求 helper 退出码、唯一 PASS 收据、token、写入/回读字节数及 SHA 一致。成功只记为 `PASS_write_and_full_readback_only_not_booted`。工具从不自动重启。

失败时保持供电。连接中断不等于远端写盘结束；状态未知时控制器保留物理锁并反复检查远端 token/锁状态，直到确认助手已退出。SIGINT/SIGTERM 会停止继续送入镜像，等待远端结束后才退出。不要 `kill -9` 控制器或关闭 Mac；掉电/强制杀死控制进程无法由文件锁防护。若网络持续不可达，需要主线程在同一父锁下通过串口证明助手已经停止，再处理恢复，不能凭超时切电。任何失败或未完整回读都不应自动启动该 SD。

写盘通过后由主线程另行完成 s2 冷启动、factory identity、Web/SMB/Wi-Fi、原生应用自动安装、存储路径操作、人脸和媒体完整功能验证。使用 s2 明确通道，禁止调用旧 `Cp5CliClient.read_usb/set_usb` 作为 X5 供电。工具通过不等于 fnOS 产品通过。
