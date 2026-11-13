# RDK X5 独立 ARM64 加速模块（fnOS `.fpk`）

状态：**r6 `0.3.1` 真实载荷包已生成，并在已有配置的 X5 上通过应用中心
原生升级；新板干净安装、重启和生命周期验收仍未完成，未发布。**
本页是安装、兼容版本、更新行为、回滚和剩余工作的唯一入口；不要把源码
`tar.gz` 当成可安装包。

## 1. 它是什么

r6 真实包（`fnos-x5-accel` 构建 `0.3.1`，公开系统版本 `0.3.0`）的
SHA-256 为
`f6401b83d1b9919cf89fff9b479e3581005e7a82fcefe3c78029a5285e0d73a9`，
1,301,626,413 字节。它是一个可通过飞牛网页“应用中心 → 手动安装”安装的
ARM64 加速模块：

- 相册：官方 BPU 人脸检测/106 点关键点/FaceID128 的 worker、模型和原生入口适配；
- 影视：已验 VPU 编解码适配库、启动器和 `mediasrv` drop-in；
- 运行时：镜像同一份离线 Python 3.11 ARM 依赖、模型缓存和 AI 默认项修复；
- 升级保护：相册/影视重装或已知兼容更新后，按原生二进制哈希恢复适配；
- 事务：重复安装、低空间、中途失败、升级、卸载回滚。

它**不是**：内核、驱动、固件、完整系统镜像、相册/影视应用安装器，也不是
用户照片/索引/媒体数据的备份工具。模块只写本页列出的受审系统路径；不改
`/var/apps/*/var`、照片目录、数据库记录、人物分组或应用停用状态。

## 2. 官方格式依据

已按飞牛官方开发文档确认并验证：

| 项目 | 依据 |
|---|---|
| 打包工具 | [`fnpack`](https://developer.fnnas.com/docs/cli/fnpack/) 1.2.3；`fnpack build` 生成 `.fpk`，打包前检查 `manifest`、`config/privilege`、`config/resource`、图标、`app/`、`cmd/`、`wizard/` |
| 包容器 | 实际生成物为 gzip/tar `.fpk`，含 `manifest`、`cmd/*`、`config/*`、图标和 `app.tgz`；`app.tgz` 内是应用 target 文件 |
| 应用框架/生命周期 | [`应用框架`](https://developer.fnnas.com/docs/core-concepts/framework/)：`install_init`/`install_callback`、`upgrade_init`/`upgrade_callback`、`uninstall_init`/`uninstall_callback`、`main` |
| Manifest | [`Manifest`](https://developer.fnnas.com/docs/core-concepts/manifest/)：`platform=arm`、`os_min_version`、`os_max_version`、`ctl_stop` |
| 权限 | [`应用权限`](https://developer.fnnas.com/docs/core-concepts/privilege/)：仅生命周期需要特权准备时使用 `run-as=root`；本模块无常驻网络进程 |
| 本地安装 | [`appcenter-cli`](https://developer.fnnas.com/docs/cli/appcentercli/) 与 [`测试应用`](https://developer.fnnas.com/docs/quick-started/test-application/)：应用中心手动安装或 `appcenter-cli install-fpk` |

本模块的 `platform=arm`（不写 `all`），因为载荷是 AArch64 原生 worker/库；
`os_min_version` 与 `os_max_version` 先固定为已验收的 `1.2.0701`，不承诺未测试
的未来版本。真实包必须用官方 `fnpack` 生成，构建脚本会拒绝把源码归档作为包
成员或把 `source.tar.gz` 放进 `.fpk`。

## 3. 已实现的兼容检查

清单文件：`compat.json`。只有闭列表命中的原生哈希才允许覆盖；否则保留原生
可用路径并在状态中显示 `unknown_native`，不做猜测性补丁。

| 组件 | 已固定值 |
|---|---|
| 平台/内核 | `aarch64`；`6.18.0`；不替换内核或驱动 |
| BPU 前置 | `bpu_framework`、`bpu_cores`、`bpu_hw_io_x5` 模块；`/dev/bpu_core0`；`/usr/lib/hbbpu/libdnn.so` |
| 相册 | `trim.photos 0.9.14`，`trim-photos` SHA256 `d0916570…6fe976` |
| 相册 worker | 已验收大 ROI 检测 worker SHA256 `71461897…78cae1`（旧 `05831b…` 不再使用） |
| 相册 AI 入口 | `face_det.bin` SHA256 `b7fe842b…1860c4`；AI Manager `47ea984c…d094f` |
| 影视 | `trim.media 0.9.8-1`；`mediasrv.service` SHA256 `ef43c271…3bd15a`；原生 `libavcodec.so.62` SHA256 `cd8067ed…708bc` |
| 模型/Python | 与完整镜像相同的 FaceID128 模型、`photos-python` 离线依赖；逐文件 SHA256 由 `bundle.json` 记录 |

拦截结果写入：

- `/var/lib/fnos-x5/accel/state.json`：模块文件清单、备份路径、事务状态；
- `/var/lib/fnos-x5/accel/module-health.json`：用户可读的 `status`、`skipped`、`recovery`；
- `product_bootstrap --repair-runtime` 的 `/var/lib/fnos-x5/product-bootstrap/runtime-health.json`：相册/影视分别的兼容状态。

未知版本不会删除、覆盖或重编译原生二进制；不会为了“通过”降低版本检查。

## 4. 安装步骤（验收通过后发布）

前置：已使用本项目匹配的完整镜像启动；内核 `6.18.0`；已在飞牛网页创建或挂载
数据卷；已通过应用中心安装相册和/或影视。无需 SSH 复制文件或手改服务。

1. 在构建服务器生成真实 `.fpk`（见第 6 节），把 `fnos-x5-accel.fpk` 和
   `module-package.json` 一起下载到本机。
2. 打开 `http://板卡IP:5666/` → 应用中心 → 手动安装 → 选择
   `fnos-x5-accel.fpk`。
3. 选择安装位置（数据卷）并确认。安装过程只调用本模块的 root 生命周期脚本，
   不需要用户执行命令。
4. 安装完成后在应用中心查看模块状态；在
   `/var/lib/fnos-x5/accel/module-health.json` 可看到逐项结果。
5. 若数据卷尚未就绪，模块显示 `installed_waiting_for_native_data_volume`；
   运行时定时器会在卷就绪后继续，不会改用系统盘。
6. 若相册/影视尚未安装，模块显示 `waiting_for_native_apps`；修复工具、bundle
   指针和 timer 已持久化，后装应用后定时器自动继续，不需要重装本模块或 SSH。
7. 安装器只有在 `systemctl enable --now fnos-x5-accel-runtime.timer` 且
   `is-active` 成功时才报告自动修复已启用；否则状态为
   `installed_repair_required`，给出可执行恢复命令。

卸载：应用中心卸载模块。`uninstall_init` 只恢复本模块修改过的系统文件；用户
照片、索引、数据库、人物分组、媒体和停用偏好不删除。

## 5. 更新/重装/回滚行为

- **重复安装**：已安装且哈希一致时返回 `already_installed`，不重写文件、不重启应用；
  运行时文件的 `create`/`replace` ownership 会保留，不会在第二次安装时退化成
  `leave`，卸载仍能删除模块新增文件或恢复镜像原文件。
- **低空间**：在第一次写入前计算“新增 + 备份 + 64 MiB 余量”；不足直接失败，
  不改任何文件。
- **中途失败**：每个目标先备份到
  `/var/lib/fnos-x5/accel/backup/<txid>/`；失败按逆序恢复，半成品删除，状态
  `failed`，不会把半安装当成功。
- **升级**：保留首次安装时的原生备份；新版本只替换仍属于本模块的旧文件。升级
  使用独立的 `runtime-transaction.json` / `pointer-transaction.json` 快照，失败或
  断电恢复升级前 worker/wrapper 和 `compat.json`，不会把首次卸载备份当作升级快照。
- **相册/影视更新**：仅当新二进制哈希在 `compat.json` 中才恢复适配；未知版本
  保留原生 CPU/软件路径，记录 `unknown_native` 和恢复入口。相册重装后重新
  创建 venv、模型缓存和 AI 默认项，但不覆盖已有用户选项。
- **卷/路径迁移**：每次从 `appcenter-cli default-volume` 或应用 `meta` 链接重新
  解析 `/volN`，旧路径不写死；等待真实可写挂载（含超级块 `emergency_ro` 检查）。
- **停用偏好**：只有安装/更新前处于 `running` 的应用才可能被重载；`stopped`
  应用保持停止。
- **防循环重启**：重载失败写入 `restart_policy`（3 次上限、指数退避到 1 小时），
  定时器不会每 5 分钟重复重启；用户可执行
  `python3 /usr/lib/fnos-x5/product_bootstrap.py --reset-reload` 清除退避后重试。
- **卸载回滚**：只删除本模块创建的文件，或恢复本模块备份的原生文件。若安装后
  供应商又替换了同一文件，检测到哈希变化就保留该文件并标记
  `uninstalled_with_preserved_changes`，不盲目覆盖。

## 6. 真实打包所需的已存在载荷

以下路径来自当前构建服务器（`<BUILD_SERVER>`，根目录
`<FNOS_BUILD_ROOT>`）。真实构建只读取这些已存在载荷，不在 Mac
编译板端二进制。

| 用途 | 已存在位置 |
|---|---|
| 相册/影视安装包与模型 | `<FNOS_BUILD_ROOT>/integration-20261109/product-inputs-20261110` |
| 相册原生 worker/模型/Python 资产 | `<FNOS_BUILD_ROOT>/delivery-v030-20261112/photos-assets`（7875 文件；worker 已更新为 `714618…`） |
| 旧只读基线（不修改） | `<FNOS_BUILD_ROOT>/integration-20261109/photos-assets-r2-official` |
| 影视 VPU 适配库 | `<FNOS_BUILD_ROOT>/native-media-review-20261110-r24/libfnos_hobot_avcodec-r24.so` |
| X5 运行库/固件源 | `<FNOS_BUILD_ROOT>/integration-20261109/source` |
| BPU 兼容模块 | `<FNOS_BUILD_ROOT>/integration-20261109/bpu_hw_io_x5.ko` |
| 已组装离线 root（推荐） | 任一次与本模块同版本的 `assembly-product-*/work/rootfs` |

推荐从已组装 root 收集，保证模块与完整镜像逐文件同源：

```sh
# 在生产构建服务器执行；不在 Mac 编译板端二进制
python3 fnos/integration/acceleration/build_module.py \
  --source-root /path/to/reviewed-rootfs \
  --product-inputs /path/to/reviewed-product-inputs \
  --icon /path/to/ICON.PNG --icon-256 /path/to/ICON_256.PNG \
  --profile runtime --version 0.3.1 --fnpack /usr/local/bin/fnpack \
  --output /path/to/accel-module-output
```

`--source-root` 必须已经有 `native_photos`/`native_media` 的受审产物；脚本会
核对每个输入哈希，不使用源码 tar 替代。

无二进制格式检查（可在 Mac）：

```sh
python3 fnos/integration/acceleration/build_module.py \
  --format-test --fnpack /path/to/fnpack-1.2.3-darwin-arm64 \
  --output /tmp/fnos-module-format
```

`--format-test` 只验证官方 `.fpk` 容器/生命周期与“无板端二进制”，报告
`payload_complete=false`；不得作为可交付包。release 构建会执行完整必需载荷
校验（相册 worker/模型/Python 包/原生入口、影视 adapter/dropin、compat 字段），
并在 `app.tgz` 内再次校验。r6 选择 `runtime`，按
`module/runtime-model-inventory.json` 收集 6 个模型或配置文件。
BPU 模型随加速载荷收集。完整归档的 `full` 配置另有 16 个文件。

## 7. 离线回归

```sh
python3 -m unittest \
  fnos.tests.test_product_accel \
  fnos.tests.test_runtime_profile \
  fnos.tests.test_native_photos_staging -v
```

这组用例覆盖载荷裁剪、许可证保留及安装事务。应用中心安装和重启仍需真板验收。

## 8. 交付状态与剩余缺口（不得省略）

- [x] 在构建服务器用真实 `--source-root` 生成带 AArch64 worker/库/模型的
      r6 `0.3.1` `.fpk`，记录 SHA256 和成员清单。
- [x] 已有配置 X5 通过应用中心原生升级；原图、人物分组、AI 设置和两个应用
      保留，升级后服务成功状态有记录。
- [ ] r20 新板网页手动安装、重复安装、低空间、中途失败、升级、卸载回滚。
- [ ] 相册/影视应用重装、已知兼容更新、外置卷迁移后自动恢复。
- [ ] 重启后原生相册/影视基本功能和 BPU/VPU 路径 smoke；不重复算法精度 benchmark。
- [ ] 未知原生版本、驱动缺失、`emergency_ro`、卸载时供应商已改文件的可读状态验证。
- [ ] 完整镜像与独立包共用同一份 `bundle.json`/`compat.json` 的最终镜像读回。
- [ ] 验收后同步 GitHub Release、百度网盘、`SHA256SUMS` 和本页；不承诺未测试版本。

## 9. 代码入口

- `fnos/integration/acceleration/build_module.py`：`.fpk` 构建与格式校验。
- `fnos/integration/acceleration/module/app/module_install.py`：生命周期安装/升级/卸载。
- `fnos/integration/acceleration/module/template/cmd/`：`fnpack` 生命周期脚本。
- `fnos/integration/acceleration/module/compat.json`：闭列兼容清单。
- `fnos/integration/image_assembly/product_accel.py`：共享覆盖层、事务、兼容状态。
- `fnos/integration/image_assembly/product_bootstrap.py`：首次安装和运行时恢复。
- `fnos/integration/image_assembly/product_inputs.py`：完整镜像内同一份 in-place bundle。

## 10. 安全边界

- 不写用户照片/媒体目录，不删原照片，不改人物分组，不重建索引。
- 不执行网络下载、`pip install`、远程脚本或任意用户输入命令。
- 不替换内核/驱动；平台或驱动不满足时直接阻止安装。
- 不把源码归档、开发目录或调试脚本打包成可安装 `.fpk`。
- 模块生命周期需要 root 才能写 `/usr/lib/fnos-x5` 等受审路径；没有常驻
  网络服务，安装完成后不保留提权进程。
