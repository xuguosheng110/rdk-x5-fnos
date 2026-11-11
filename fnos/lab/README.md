# fnos.lab — 可离线验证的板卡执行器

本目录实现 **检查、计划、硬件无关模拟、文件替身执行**，以及默认关闭的真实 CP5 USB/串口/写盘后端接口。默认路径不会切换电源、打开串口、写 `/dev` 或访问云端；真实执行必须同时满足环境门槛、真实计划、计划绑定授权令牌、`--confirm-board`、root 和统一板卡锁。`simulate` 的状态与事件仍全部是模拟数据，不能当作真实烧录证据。

当前明确未完成/未验证：

- 统一锁协议、CP5 USB、文件替身写盘/回读和 runner 已实现并有离线跨进程测试；真实 CP5/旧 flash 的**上板**互锁尚未实测；
- 真实 macOS 写盘后端代码存在，但本批未对任何 `/dev` 执行写入；真实 UMS 串口命令自动化未实现（串口只做被动监听）；
- 未通过 R09 的连续 3 轮真实烧录验收。

## 1. 快速开始

在仓库根目录运行（`fnos` 目录是 Python namespace package，不需要额外安装）：

```sh
python3 -m fnos.lab --help
python3 -m fnos.lab inspect --fixture fnos/tests/fixtures/lab-host-valid.json
head -c 1048576 /dev/zero > /tmp/lab-image.img
python3 -m fnos.lab plan \
  --board x5-lab-01 \
  --image /tmp/lab-image.img \
  --target /dev/disk4 \
  --expect-serial X5TEST0001 \
  --fixture fnos/tests/fixtures/lab-host-valid.json \
  --out /tmp/lab-plan.json
python3 -m fnos.lab simulate \
  --plan /tmp/lab-plan.json \
  --state-dir /tmp/fnos-lab-state \
  --run-id run-demo-01
python3 -m fnos.lab status \
  --state-dir /tmp/fnos-lab-state \
  --run run-demo-01
```

测试：

```sh
python3 -m unittest -v fnos.tests.test_lab
```

## 2. 安全边界

| 动作 | 本批次默认 | 显式门槛后的代码路径 |
| --- | --- | --- |
| 真实块设备写入/回读 | 不触发 | `run --backends real` + root + 环境门槛 + token |
| CP5/电源 API 调用 | 不触发 | `run --backends real`，或人工 `usb-* --board` |
| 打开串口 | 不触发 | real runner 先建立只读监听 |
| SSH/远程写入 | 不实现 | 不实现 |
| `sudo`/格式化/升级 | 不要求、不执行 | 真实写盘由 root 运行，脚本不调用 sudo |
| 读取 `~/.config/mijia-cp5/auth.json` 等凭据 | 不读取 | 仅由既有 CP5 CLI 在 usb-* 内部使用，不复制/打印 |
| 日志中的秘密 | 字段白名单，事件只写已知身份/阶段/哈希，不写环境变量或凭据 | 同左 |

`inspect` 在真实主机上只运行只读的 `diskutil list/info -plist`，并枚举工具和串口设备名。它**不会**打开串口、发送电源命令或连接 SSH。`plan` 只读镜像并写计划文件。`simulate` 只读写指定的私有状态目录。

## 3. inspect

```sh
python3 -m fnos.lab inspect                 # 真实主机，只读
python3 -m fnos.lab inspect --fixture FILE  # 离线 fixture，不碰主机
python3 -m fnos.lab inspect --format text
```

输出区分：

- `disks[]`：发现的磁盘，每项有 `identity_status`；`verified` 只表示身份字段齐全，不表示可写；
- `verified_identity[]`：身份字段通过校验的设备；
- `verified_writable[]`：本批次恒为空；
- `write_eligible`：恒为 `false`；
- `hardware_actions`：恒为未打开串口、未调用电源、未 SSH、未写盘。

`identity_status` 还包括 `rejected_read_only`、`unknown_layout`（`Internal`/`WholeDisk` 缺失）、`insufficient_identity`（序列号/型号/容量缺失）等；任何非 `verified` 都不会被 `plan` 接受。

真实模式会把整盘条目的**子分区/APFS 容器挂载**汇总到 `mount_points`，不会只看整盘 `MountPoint`；`Internal`/`WholeDisk` 缺失时标记为 `unknown_layout`，不会默认成“已核验外部整盘”。真实模式会读取 `fnos/integration/hardware/board-bindings.json`；模板初始 `reviewed=false`、串口为空、USB 映射未确认，因此真实 `plan` 会拒绝，直到完成只读预检和人工绑定。这是有意 fail-closed。

## 4. plan

```sh
python3 -m fnos.lab plan \
  --board BOARD_ID \
  --image /absolute/path/candidate.img \
  --target /dev/disk4 \
  --expect-serial DISK_SERIAL \
  --out /absolute/path/plan.json
```

必须同时满足：

- 镜像路径绝对、普通文件、非符号链接，有完整 SHA-256 与字节数；
- `board_id` 在评审过的 inventory 中存在；
- 电源 backend 受支持、channel 明确、`mapping_verified=true`、`wired=true`；
- 串口端口已绑定；
- 目标由 `--target` 与 `--expect-serial` 双重绑定；序列号必须唯一，不能只靠 `diskN`；
- 目标是整盘、非内部/系统盘、未挂载、非只读、非虚拟盘，容量 ≥ 镜像 + 保留空间；
- 计划输出必须是**新路径**：绝对、非符号链接、父目录存在、不与输入镜像/fixture 相同或经 `..`/符号链接父目录别名；已存在的文件（包括旧计划或无关文件）一律拒绝，绝不覆盖；
- 计划以 `mkstemp` + `fsync` + 无覆盖 `os.link` 原子落位，权限 `0600`。

拒绝示例（均为非零退出，不产生计划、不改变输入字节）：

- 内部系统盘、目标歧义、序列号缺失/不匹配、容量不足、电源通道未绑定、计划输出是符号链接、计划输出等于镜像/fixture、计划输出已存在。

计划 schema 为 `fnos-lab-plan/v1`，带 `integrity.payload_sha256`。**计划不是写盘授权**：

fixture/模拟计划：

```json
"execution": {
  "mode": "simulation-only",
  "real_backend_wired": false,
  "write_eligible": false,
  "authorization": "none"
}
```

真实计划：

```json
"execution": {
  "mode": "hardware-required",
  "real_backend_wired": true,
  "write_eligible": false,
  "authorization": "required"
}
```

两种计划的 `write_eligible` 都是 false；真实执行另需 §5.1 的门槛和 token。fixture 计划的 `simulated` 必须是 `true`；即使手工把字段改成 `false` 并重新签名，`simulate` 也会拒绝。

## 5. 单一板卡锁

唯一入口：

```python
from fnos.lab.locks import board_lock

with board_lock(state_root, board_id):
    ...  # power/serial/flash/boot/verify 都应在这一把锁内
```

实现要点：

- 锁文件路径由 `board_id` 的 SHA-256 派生，避免路径穿越；
- `os.open(..., O_NOFOLLOW | O_NONBLOCK)` 后用 `fstat` 确认是普通文件；符号链接、目录、FIFO 等异常文件直接 `Refused`，不会挂起；
- 获取后 `flock(LOCK_EX | LOCK_NB)`；同一 board 的第二个操作立即拒绝，不排队；不同 board 可并行；
- 锁文件权限 `0600`，进程崩溃时 `flock` 自动释放；cleanup 错误不会覆盖原始异常；
- **不删除锁文件**。删除会制造两个 inode 的锁竞争窗口；文件内残留元数据只用于诊断。

### 跨工具接入（本批已实现）

- CP5 的 USB 组入口（`cp5.py usb-status|usb-on|usb-off --board`）使用同一 `sha256(board_id)[:32]` 路径；
- `fnos/integration/hardware/legacy_flash_adapter.py` 不自己持锁，而是把同一绝对路径交给旧 `flash_macos.py`；旧写盘进程成为唯一持锁者，避免父持锁再调用子进程自抢锁；
- `fnos.lab run` 在自己的进程内调用 CP5/串口/写盘后端函数；若通过子进程调用 CP5，则用继承 fd（`FNOS_BOARD_LOCK_FD`）让子进程**采纳**父锁，不重新抢锁；
- 已接入路径：`fnos-lab-run`、`mijia-cp5-usb`、`legacy-flash-adapter`；
- 未接入/阻断：`legacy-flash-command`（仓库直接 dd 脚本）由脚本顶部实际拒绝，状态 `blocked_by_guard`；外部旧 `legacy-flash-direct`、`ha-power` 标记 `not_integrated`，工作区代码无法阻断，不能宣称已禁用；`raw-device-write`、米家 App、人工拔线标记为 `outside_file_lock`，文件锁无法阻止；
- 接入清单和状态可用 `fnos/lab/registry.py` 或预检脚本查看。

真实上板互锁尚未实测；手机 App 与人工拔线仍无法由文件锁阻止。真实 `run` 还要求 `--state-dir` 与共享锁根一致，否则拒绝，避免 CP5/flash 使用另一把锁。

## 5.1 文件替身真实执行

`run` 使用同一状态机，但通过注入后端运行：

```sh
python3 -m fnos.lab run --plan /tmp/lab-plan.json \
  --state-dir /tmp/fnos-lab-state --run-id run-fake-01 --backends fake
python3 -m fnos.lab status --state-dir /tmp/fnos-lab-state --run run-fake-01
python3 -m fnos.lab cancel --state-dir /tmp/fnos-lab-state --run run-fake-01
```

`--backends fake` 只写状态目录内的普通文件（`fake-target.img`），从不打开 `/dev`，不联网。真实后端：

```sh
export FNOS_LAB_ENABLE_REAL_BACKENDS=1        # 默认关闭
python3 -m fnos.lab authorize --plan /abs/real-plan.json \
  --out /abs/authorization.json --confirm-board x5-lab-01 \
  --authorize-real-hardware
sudo -E python3 -m fnos.lab run --plan /abs/real-plan.json \
  --state-dir "$HOME/.local/state/fnos-lab" --backends real \
  --auth /abs/authorization.json --confirm-board x5-lab-01 --authorize-write
```

真实门槛缺一不可：环境变量、`simulated=false` 计划、`--backends real`、token、`--confirm-board`、`--authorize-write`、root，以及同一把锁。`execution.write_eligible=true`、`authorized=true` 等计划内布尔字段不能替代 token。真实串口只建立被动监听；UMS 命令输入和真实 boot_id/rootfs 校验不在本批实现，因此真实 run 在 `validating` 显式失败 `post_boot_validation_not_implemented`，不会 `run_passed`。

boot 证据使用冷启动前记录的 serial cursor，只接受动作后新增字节；旧日志、仅 kernel banner、超时、kernel 版本不匹配都不会通过。fake 只有在注入完整 fake 记录（kernel/login/boot_id/rootfs）时才可 `passed`，`status.validation.source=fake` 明确标记为替身证据。

所有 OFF 前先检查 live safe-to-cut 证据；真实模式没有该证据实现，直接 fail closed，不发生 OFF。fake 通过显式 `cut_safe=True` 注入；OFF 确认后至少保持 5 秒并记录 `power_hold_*`，保持期取消仍先恢复 ON。继承 fd 采纳必须通过“独立 probe 失败 + 对 fd flock 成功 + 再次 probe 失败”，复制 metadata/offset 的普通 fd 会被拒绝。

`--allow-planned-fault --planned-fault-at writing|reading` 仅用于 fake + fixture 计划，记录为 `planned_power_fault`，与 `cancel`（`interruption.kind=cancel`、不切电）和正常关机（`normal_shutdown` 记录）分开。写入中的取消会等待本次完整写入完成；回读中的取消在块边界停止，均不宣称 verified、不切电、不冷启动。

## 6. simulate

```sh
python3 -m fnos.lab simulate \
  --plan /tmp/lab-plan.json \
  --state-dir /tmp/fnos-lab-state \
  --run-id run-demo-01
```

状态目录可用 `--state-dir` 或 `FNOS_LAB_STATE_DIR` 指定；建议始终使用临时/私有目录，避免碰用户全局状态。每次运行生成：

```text
<state-dir>/locks/board-<hash>.lock
<state-dir>/runs/<run_id>/state.json
<state-dir>/runs/<run_id>/events.jsonl
```

`runs/<run_id>` 用原子 `mkdir` 占用：不同 board/进程即使同时使用同一 `run_id`，也只有最多一个能创建成功，另一个收到 `Refused`；不会覆盖既有状态。

正常阶段：

```text
preflight -> serial_capture -> power_cycle -> ums_ready
          -> writing -> written -> reading -> verified
          -> power_cycle(cold boot) -> booting -> validating -> passed
```

串口预监听发生在**任何 power_action 之前**，避免错失 U-Boot 窗口；阶段跳转由 `model.TRANSITIONS` 显式校验，非法跳转（如 `preflight -> booting`）会失败，不能任意改阶段。

策略：

- `writing`、`written`、`reading`、`booting`、`validating` 阶段禁止 power off；注入的 off 会被拒绝并记录 `power_off_refused`。
- 写入前、回读前重新校验计划完整性和镜像 SHA-256/字节数；计划或镜像变化即失败。
- 回读哈希不一致 -> `failed`，保持供电，不进入 boot，不自动重试。
- `--cancel-at preflight|writing|reading`：在安全边界停止。写入中取消会等写完后停止，介质标记 `incomplete`，不回读、不启动；回读中取消不宣称 verified；不隐式续写。
- `--crash-at writing|reading`：持久化阶段后以退出码 70 模拟进程死亡；`status` 检测到 pid 不存在后标记 `interrupted`，不自动恢复。
- `--fault hash_mismatch|cloud_unknown|recovery_failed|power_off_at_writing|power_off_at_reading`：分别模拟回读差异、云状态未知、恢复上电失败、写/读阶段被注入 off。
- 云状态未知：不盲重试 off；只尝试一次明确 on，失败则要求人工恢复。
- 同一 `run_id` 已存在时拒绝，绝不从中间偏移续写。

退出码：

| 退出码 | 含义 |
| --- | --- |
| 0 | 成功 / `passed` |
| 1 | 模拟失败 |
| 2 | 前置条件拒绝 |
| 3 | 取消（安全边界） |
| 70 | 模拟进程中断（仅 `--crash-at`） |

## 7. status

```sh
python3 -m fnos.lab status --state-dir /tmp/fnos-lab-state --run run-demo-01
python3 -m fnos.lab status ... --verify-image
```

报告：阶段/状态、电源、介质写入/回读/verified/incomplete、失败原因、计划完整性、镜像状态、`resume_allowed`（恒为 false）、下一步。

若 `state.json` 记录为 `running` 但 pid 已不存在，`status` 会把状态改为 `interrupted` 并追加 `interrupted_detected` 事件；它不会调用写盘、电源或串口。`--verify-image` 在 mtime 变化时重新计算 SHA-256。

## 8. fixture 格式

fixture 根必须包含 `"schema": "fnos-lab-fixture/v1"` 和 `"simulated": true`。示例见 `fnos/tests/fixtures/lab-host-valid.json`。字段白名单：

```json
{
  "schema": "fnos-lab-fixture/v1",
  "simulated": true,
  "host": {
    "tools": [{"name": "diskutil", "path": "/usr/sbin/diskutil"}],
    "serial_ports": ["/dev/cu.usbserial-1340"]
  },
  "disks": [
    {
      "device": "/dev/disk4",
      "size_bytes": 31293702144,
      "model": "USB 3.0 SSD",
      "serial": "X5TEST0001",
      "bus": "USB",
      "internal": false,
      "removable": true,
      "whole_disk": true,
      "mount_points": []
    }
  ],
  "boards": {
    "x5-lab-01": {
      "model": "RDK X5",
      "power": {
        "backend": "simulated-cp5",
        "channel": "s1",
        "mapping_verified": true,
        "wired": true
      },
      "serial": {"port": "/dev/cu.usbserial-1340", "baud": 115200},
      "allowed_target_serials": ["X5TEST0001"]
    }
  }
}
```

fixture 中未列入白名单的字段会被忽略，不会被写入计划或日志。**fixture 永远不能生成真实写盘资格。**

## 9. 与真实 R09 的区别

本目录的 `passed` 只证明模拟状态机在该输入下按策略运行。它不证明：

- 真实 U 盘/系统卡写入与全量回读；
- U-Boot/UMS 实际交互；
- CP5 云状态、真实断电与恢复；
- 冷启动、fnOS 用户态、SMB/Web 验收；
- R09 连续 3 轮真实烧录。

真实闭环仍需要后续实现并单独验收。

## 10. 真实后端文档与测试

- 实现范围、测试命令、真板未测项和上板门槛见 `fnos/docs/hardware-backends-20261106.md`；
- 板卡绑定模板：`fnos/integration/hardware/board-bindings.json`；
- 只读预检：`python3 fnos/integration/hardware/preflight.py --board x5-lab-01 ...`；
- 旧 flash 适配器：`python3 fnos/integration/hardware/legacy_flash_adapter.py ...`（不自己持锁，子进程持同一锁）；
- 离线测试：`python3 -m unittest -v fnos.tests.test_lab fnos.tests.test_hardware_backends`。

## 11. 真实会话/身份/目标/备份通道（2026-11-06）

- `uart_session.py`：串口所有权、U-Boot/Linux 状态、nonce 命令边界、倒计时单空格中断（真实未执行）。
- `safe_cut.py`：绑定当前 session/TTL 的 safe-cut 证据，切电前重验；真实未切电。
- `runtime_identity.py`：本轮 Linux 命令采集 kernelrelease/boot_id/root UUID/model/fnOS；banner 不算身份。
- `candidate.py`：候选 manifest 镜像/内核/fnOS/root UUID 固定与比对。
- `target_mapping.py`：U-Boot mmc/CID 与宿主整盘多证据唯一映射；无序列号需审阅记录。
- `backup.py` / `backup-plan`：只生成非执行备份计划，不覆盖恢复副本。

真实只读诊断结论见 `fnos/docs/board-diagnostic-20261106.md`：当前板卡在 U-Boot 自动 UMS 循环中复位，未进入 Linux/U-Boot 提示符。实现与测试范围见 `fnos/docs/hardware-runtime-channels-20261106.md`。

```sh
python3 -m unittest -v fnos.tests.test_lab fnos.tests.test_hardware_backends fnos.tests.test_runtime_diagnostics
python3 -m unittest discover -s fnos/tests -p 'test_*.py'
```

## 12. R1–R4 修订（2026-11-06）

- `uart_session.py`：`CommandReceipt` 注册表、terminal epoch、真实回显/NUL/CRLF 解析、倒计时时间界、power-on/状态变化失效、精确 U-Boot 只读命令。
- `safe_cut.py`：证据只引用当前 session receipt；`CutEvidenceGate.assert_valid` 重新查 registry，切电前重验。
- `runtime_identity.py`：Debian 基础层与 fnOS trim 产品层分离；真实 assembly manifest 需要显式 product adapter。
- `live.py` + `serial.SessionSerialCapture`：CLI 与离线端到端测试共用的单一串口会话、UMS 启动/停止、冷启准备。
- `target_mapping.py`：精确容量、plan/flash 交叉校验、物理端口身份排他。
- 实际 `fnos-x5-assembly/v1` 候选需 `--product-version 1.2.0701`（或 manifest `product.trim_version`）adapter；实际 assembly 默认 `accepted=false`，未附独立验收记录前真实 run 在串口/电源/写盘前拒绝。

修订报告：`fnos/docs/runtime-review-fix-20261106.md`；消费方 contract 说明：`fnos/docs/image-hardware-contract-consumer-20261106.md`。
