# 硬件接入适配入口

本目录只放**非凭据**的板卡绑定、只读预检和旧工具适配入口。真实执行门槛、锁协议和测试说明见 `fnos/docs/hardware-backends-20261106.md`。

| 文件 | 作用 | 是否产生硬件副作用 |
| --- | --- | --- |
| `board-bindings.json` | `board_id`、串口、CP5 USB 通道、目标序列号白名单、旧入口禁用状态 | 否 |
| `preflight.py` | 只读 `diskutil`/`stat`/镜像哈希预检，打印 ready 检查表 | 否（不打开串口、不联网、不写盘） |
| `legacy_flash_adapter.py` | 把规范 board 锁路径传给旧 `flash_macos.py`；适配器本身不持锁 | 计划默认否；`--write` 才可能写盘 |

`board-bindings.json` 初始为未审阅模板：`reviewed=false`、`serial.port` 为空、`mapping_verified=false`。必须完成只读预检和用户物理确认后再填写；不得写入任何 token、密码或认证文件内容。

`fnos.lab.registry` 还显式列出两个未接入的旧烧录入口：`rdk-x5-usb-flash` 直接调用（`legacy-flash-direct`）和仓库 `scripts/flash-rdk-x5-sd.command`（`legacy-flash-command`）。仓库 `scripts/flash-rdk-x5-sd.command` 已在脚本顶部实际拒绝（`blocked_by_guard`）；外部 `rdk-x5-usb-flash` 直接调用标记 `not_integrated`，工作区代码无法阻断，不能宣称已禁用。只应使用本适配器或 `fnos.lab run` 的受控路径。

## 运行时通道

- 串口命令会话、live safe-cut、runtime identity、候选 manifest、U-Boot/宿主目标映射和备份计划分别位于 `fnos/lab/uart_session.py`、`safe_cut.py`、`runtime_identity.py`、`candidate.py`、`target_mapping.py`、`backup.py`。
- `python3 -m fnos.lab backup-plan ...` 只生成非执行计划，不打开源设备、不写目标。
- 新副作用路径尚未上板执行；真实只读诊断见 `fnos/docs/board-diagnostic-20261106.md`，离线测试范围见 `fnos/docs/hardware-runtime-channels-20261106.md`。
