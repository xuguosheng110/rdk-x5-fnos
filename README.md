<h1 align="center">
  <img src="docs/assets/readme-banner.jpg" alt="RDK X5 × fnOS，配有 RDK X5 实物与飞牛 Logo" width="100%">
</h1>

<p align="center">
  <strong>用 RDK X5 搭建家庭 NAS</strong><br>
  文件共享 · 智能相册 · 影音服务 · BPU / VPU 硬件加速
</p>

<p align="center">
  <a href="https://github.com/xuguosheng110/rdk-x5-fnos/releases/tag/v0.2.0"><img alt="版本 v0.2.0" src="https://img.shields.io/badge/release-v0.2.0-0891b2?style=flat-square"></a>
  <img alt="平台 RDK X5 ARM64" src="https://img.shields.io/badge/platform-RDK%20X5%20ARM64-263554?style=flat-square">
  <img alt="Linux 6.18.0" src="https://img.shields.io/badge/Linux-6.18.0-6d4aff?style=flat-square">
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="#v020-更新">版本更新</a> ·
  <a href="#功能与测试">功能与测试</a> ·
  <a href="#应用安装与硬件加速">安装与加速</a> ·
  <a href="#开发计划与交付清单">TODO</a> ·
  <a href="#常见问题">常见问题</a>
</p>

> [!TIP]
> **[下载 v0.2.0 完整镜像](https://pan.baidu.com/s/1F5t3ehuAREo-Sy2zhoMIWw?pwd=ugjr)** · 提取码 `ugjr` · 约 3.80 GB
>
> 选择 `rdk-x5-fnos-v0.2.0.img.xz`。同一目录提供 `SHA256SUMS`、内核源码和安装说明。 [GitHub Release](https://github.com/xuguosheng110/rdk-x5-fnos/releases/tag/v0.2.0)

> [!NOTE]
> **[v0.3.0 适配源码版](https://github.com/xuguosheng110/rdk-x5-fnos/releases/tag/v0.3.0)** 已发布，包含适配源码、对应 Linux 6.18 源码、验收说明和校验清单。完整镜像与含第三方载荷的加速包仍待确认再分发许可。下方快速开始对应 v0.2.0 镜像。

## 快速开始

准备 **RDK X5、至少 32 GB 的系统 SD 卡、独立 USB 数据盘**。首次启动建议接有线网络。使用符合 RDK 官方要求的电源，多盘或机械硬盘可使用独立供电。

1. **下载并校验。** 从上方入口下载 `rdk-x5-fnos-v0.2.0.img.xz` 与 `SHA256SUMS`，核对镜像的 SHA-256。Linux 下可以在下载目录运行 `sha256sum -c --ignore-missing SHA256SUMS`。
2. **解压并烧录。** 解压 XZ 得到 `.img`，用整盘镜像烧录工具写入 X5 的**系统 SD 卡**。烧录会覆盖所选盘，请按容量和设备类型核对目标。
3. **打开飞牛桌面。** 在路由器设备列表找到板卡 IP，访问 `http://板卡IP:5666/`，按引导创建管理员账号。
4. **准备数据与应用。** 已有飞牛数据卷选择挂载，空白盘可创建存储空间。选择媒体与应用位置，等待所需模型下载。Wi-Fi 可在系统设置中配置。

<details>
<summary><strong>进阶烧录 · 通过 U-Boot 映射 SD 卡</strong></summary>

通过串口进入 RDK X5 的 U-Boot 后运行 `ums 0 mmc 1`。在电脑上核对映射设备为系统 SD 卡，再写入整盘镜像。本项目的部署验证覆盖 SD 卡启动。更多接线与供电说明见 [RDK 官方文档](https://developer.d-robotics.cc/rdk_doc/)。

</details>

<details>
<summary><strong>源码包与历史版本</strong></summary>

- [v0.2.0 GitHub Release](https://github.com/xuguosheng110/rdk-x5-fnos/releases/tag/v0.2.0) 提供适配源码 ZIP、版本说明和校验资料。对应 Linux 6.18 内核源码及构建配置在新版网盘目录内。
- [v0.1.0 家庭共享版](https://github.com/xuguosheng110/rdk-x5-fnos/releases/tag/v0.1.0) 保留首发镜像、源码和当时的验收记录。

</details>

## v0.2.0 更新

- 修复部分 RDK X5 在飞牛界面创建 U 盘 **Basic** 存储空间时报错。Linux 6.18.0 #5 启用 `CONFIG_MD_LLBITMAP`，支持原生 `mdadm` 使用的 lockless bitmap。
- 回移 llbitmap 超时恢复补丁，在等待超时后恢复内核引用。镜像组装器增加所需 MD、RAID1 和 llbitmap 配置检查。
- 同一内核在 120 GB U 盘通过 Basic/Btrfs 创建、128 MiB 文件读写和冷启动自动挂载。完整 r16 镜像通过烧录回读、首启和已有 Basic 卷导入。

**待验证**：全新 r16 上的新建卷、双盘 ext4 RAID1 和真实超时故障注入。16 GB 测试盘的间歇掉线仍待定位。 [版本说明](RELEASE_NOTES.md) · [存储测试记录](docs/storage-create-validation.md)

## 功能与测试

项目提供 RDK X5 的启动、网络、存储和 BPU / VPU 适配。以下应用结果来自 **v0.1.0 真板验收**。v0.2.0 沿用了应用和运行时构建输入，本轮真板重点为存储。 [测试结果](TESTED.md)

| 功能 | 已验证范围 |
| --- | --- |
| 网络与文件 | 有线 Web / SMB、原生 Wi-Fi 冷启动重连、文件传输和回收站恢复 |
| 相册 · BPU | 官方人脸检测、106 点关键点、FaceID128 与人物分组 |
| 影视 · VPU | 渐进式 8-bit、4:2:0 SDR H.264 / HEVC 样本硬件解码与编码 |
| 备份与容器 | 原生备份、应用升级、非 root 应用和容器跨 UID 共享读写 |
| WebDAV | 数据卷缓存、冷启动挂载、服务恢复后续传及远端校验 |

中文语义搜索和视频缩放使用 CPU。10-bit、HDR、4:4:4 等未支持格式保留软件回退。跨设备兼容性和长期可靠性见本页[开发计划与交付清单](#开发计划与交付清单)。

## 应用安装与硬件加速

目标体验是：刷入镜像 → 网页创建管理员 → 创建或挂载外置存储空间 → 选择应用安装位置 → 安装相册、影视 → 自动接入硬件加速。普通用户不应需要 SSH、串口、手工复制模型或修改服务配置。

**当前状态：上述完整流程仍在新板验收中。已有板卡的加速测试通过，不代表所有新安装、应用升级和重新安装路径都已通过。独立加速安装包尚未发布。**

| 组件 | 部署位置与时机 | 用户需要做什么 |
| --- | --- | --- |
| X5 内核驱动、固件及 BPU/VPU 运行库 | 随匹配版本的系统镜像部署 | 烧录本项目完整镜像 |
| 相册、影视安装资源 | 镜像可预置离线资源；实际安装等待数据卷就绪 | 在飞牛界面创建或挂载存储空间，选择应用位置 |
| 应用数据、索引及数据卷模型 | 按飞牛选定的数据卷部署；程序文件遵循原生安装包规则 | 在应用中选择照片、视频目录 |
| 加速适配与兼容性检查 | 系统自动衔接应用及 X5 运行库 | 查看加速状态和错误提示，完整免命令行路径待验收 |

仅插入 USB 盘还不等于已有可用的应用安装空间。没有数据卷时，安装流程应等待，不能退回系统卡存放应用数据。已有数据的磁盘优先使用原生挂载/导入流程；创建存储空间可能清空所选磁盘，请按型号、容量核对。

相册已测硬件路径为 BPU 人脸检测、106 点关键点和 FaceID128。中文语义搜索仍使用 CPU。影视已测 H.264/HEVC 指定样本的 VPU 解码和编码，缩放使用 CPU；不支持的格式保留软件回退。应用成功安装或播放成功，均不能单独证明硬件加速生效。

### 下载包与版本对应

GitHub Release 和百度网盘应提供一致的版本目录、文件名、校验值及安装说明。受平台文件大小限制，Release 可以链接到网盘中的完整镜像，但必须明确下载入口。

| 交付物 | 当前情况 / 要求 |
| --- | --- |
| 完整系统镜像 `.img.xz` | v0.2.0 下载入口见本页顶部 |
| `SHA256SUMS`、版本说明、安装手册 | 与对应镜像一起提供；用户安装步骤以本页为统一入口 |
| 适配源码、Linux 6.18 源码及配置 | 保留对应版本的源码与构建资料入口 |
| 独立 ARM64 加速组件包 | **待实现、验收和发布**；用于已有系统升级，不能替代内核和固件 |
| 组件版本与兼容性清单 | 待完善：列明系统、内核、应用、运行库和模型版本及回滚方式 |

后续独立包必须经过安装、重启、应用升级和卸载恢复测试后，才能加入 Release 与网盘正式下载目录。当前不要把源码归档当成可直接安装的加速包。

## 常见问题

<details>
<summary><strong>找不到板卡 IP，或无法打开桌面？</strong></summary>

接上网线，在路由器的 DHCP 或设备列表中找到板卡 IP，再访问 `http://板卡IP:5666/`。首次安装按页面引导设置账号和密码。重装后请重新确认 IP。

</details>

<details>
<summary><strong>怎样从 Windows 或 macOS 访问共享文件？</strong></summary>

在飞牛文件管理中创建共享文件夹并启用 SMB。Windows 在资源管理器地址栏输入 `\\板卡IP`。macOS 在 Finder 的「连接服务器」中输入 `smb://板卡IP`。

</details>

<details>
<summary><strong>存储页显示「权限未适配」怎么办？</strong></summary>

该提示属于家庭共享策略的已知限制。已测文件、相册、影视、备份和应用共享路径可用。家庭共享使用共享读写策略，逐用户 Windows ACL v2 隔离不在支持范围内。请勿执行权限转换。

</details>

<details>
<summary><strong>创建存储失败或 U 盘掉线，如何反馈？</strong></summary>

记录镜像版本、板卡型号、U 盘型号与容量、分区情况和完整错误信息。提交前去掉密码、令牌和私人文件名。有数据的盘先备份并保留现场。 [提交问题](https://github.com/xuguosheng110/rdk-x5-fnos/issues)

</details>

<details>
<summary><strong>系统如何更新？</strong></summary>

升级前备份数据和配置，使用本项目经过验证的镜像。为保留 X5 的启动链，通用 fnOS 自动更新与不匹配的启动固件更新已禁用。请勿直接替换通用内核或启动固件。

</details>

## 开发计划与交付清单

更新时间：2026-11-12。勾选仅表示列出的范围已完成。历史真板结果与当前新板验收分开记录。

### 当前交付重点：外置盘上的应用与加速

- [x] 将安装说明、加速模块、限制和 TODO 汇总到本 README。
- [ ] 新板仅通过网页完成存储创建、选择应用位置和相册/影视安装。
- [ ] 验证无数据盘时等待、数据卷就绪后部署及失败重试。
- [ ] 验证相册真实任务调用 BPU，影视真实任务调用 VPU，并显示回退原因。
- [ ] 验证重启、应用更新、卸载重装后的适配与索引兼容性。
- [ ] 制作并验收独立 ARM64 加速组件升级包及回滚入口。
- [ ] 将验收后的镜像、组件包、安装说明、源码和校验文件同步到 Release 与百度网盘。

### 当前最高优先级：新板首次创建存储空间

- [x] 确认当前板两个 USB 数据卷已识别、挂载（Btrfs / ext4）。
- [x] 两卷各 1 MiB root 文件写入、fsync、SHA-256 回读通过，测试文件已移除。
- [x] 新板原生“创建空间”失败已复现：mdadm 请求 llbitmap，内核缺少 CONFIG_MD_LLBITMAP。
- [x] 增加镜像构建配置门禁，拒绝缺少原生创建能力的内核。
- [x] 启用 CONFIG_MD_LLBITMAP 后，替换板原生 Basic/Btrfs（120 GB）和 Basic/ext4（16 GB）创建成功。
- [x] 120 GB 新建 Btrfs 卷以普通用户写入 1 GiB，完整回读及三遍请求清除文件缓存后的回读哈希一致。
- [ ] 解决 16 GB 盘创建后在原生读写检测中的 USB 超时掉线；创建成功不等于稳定性通过。
- [x] 120 GB 新建卷完成冷启动自动挂载与文件哈希复验；16 GB 稳定性仍未通过。
- [x] 内核 #5 在第二块 120 GB 盘通过原生创建与冷启动自动挂载，2 TB 盘原有 ext4 自动挂载及只读抽样一致。
- [x] storage-r16 完整镜像通过离线校验、SD 写入回读、首启及已有 Basic 卷导入。
- [ ] 复验全新 r16 系统上新建空白盘、双盘 ext4 RAID1 和热插拔。
- [x] 将内核修复纳入 r16 组装并校验必需配置。
- [ ] 在空白测试盘上验证原生创建、文件共享、相册/影视/应用目录选择。
- [ ] 冷启动重新挂载与读写复验，再在第二块板上复验。
- [x] 发布 v0.2.0 适配源码、内核源码归档和校验值。

两个已有卷的读写通过，不能替代空白盘原生创建验收。v0.2.0 已包含该内核修复，完整新板安装流程的剩余验收项见上方。另有 USB 掉线问题独立排查；16 GB 测试盘在 Mac 两遍整盘读取一致，Mac 写入因用户主动拔盘中断，不算坏盘证据；X5 原生检测在约 34 MiB 处掉线，原因仍待隔离。

### 用户体验与可靠性

- [x] v0.1.0：SD 镜像烧录、完整回读和受控冷启动。
- [x] 已测范围的 Wi-Fi、SMB、BPU 人脸、H.264/HEVC 硬件编解码。
- [ ] 多品牌 U 盘/移动硬盘、不同 USB 桥接芯片兼容矩阵。
- [ ] 意外断电、长时间读写、容量耗尽、恢复和升级回滚测试。
- [ ] 进一步缩小硬件转码软件回退范围；单独验证 10-bit/HDR 等能力。
- [ ] 在不绕过原生完整性保护的前提下，与上游对齐家庭共享权限提示。

### 开源与发布

- [x] 面向使用者的 README、使用入口、官方帮助及论坛链接。
- [x] 原创适配代码许可与第三方许可边界说明。
- [ ] 核实飞牛完整系统再分发的明确许可或书面授权；个人使用许可不能替代分发许可。
- [x] 发布 r16 对应的 X5 Linux 6.18 源码快照、补丁、配置与构建步骤。
- [ ] 核实内核快照与上游 X5 BSP 的精确提交对应关系。
- [ ] 固定 BSP、运行库、模型版本及授权清单，完善可复现构建与 SBOM。
- [ ] 文档/源码归档与后续 Release、网盘文件同步，避免主分支与旧版本归档混淆。

## 开发与贡献

欢迎补充设备兼容性实测和提交改进。请先阅读 [测试指南](TESTING.md)、[真板结果](TESTED.md) 和 [本页开发计划](#开发计划与交付清单)。

<details>
<summary><strong>源码导航与构建说明</strong></summary>

- [镜像组装](fnos/integration/image_assembly/README.md) 包含离线组装、原生服务适配与首次启动准备。
- [原生影视](fnos/integration/native_media) 与 [原生相册](fnos/integration/native_photos) 包含 VPU 编解码与 BPU 人脸适配。
- [共享与加速](fnos/integration/acceleration) 包含共享存储策略及辅助代码。
- [内核补丁](fnos/patches)、[离线测试](fnos/tests) 与 [真板工具](fnos/integration/hardware) 供开发者使用。

从源码组装时，按 [内核构建说明](RELEASE_NOTES.md#下载与构建) 和组装器要求准备同版本 fnOS ARM 用户态、RDK BSP、厂商运行库和模型。源码归档提供 r16 实际内核树、配置及编译器版本。

</details>

[飞牛帮助中心](https://help.fnnas.com/) · [飞牛论坛](https://club.fnnas.com/) · [RDK 社区](https://forum.d-robotics.cc/) · [RDK BSP](https://github.com/D-Robotics/x5-rdk-gen) · [问题反馈](https://github.com/xuguosheng110/rdk-x5-fnos/issues)

这是社区适配项目。独立适配代码采用 [MIT License](LICENSE)，内核、fnOS 用户态、厂商运行库及模型各自适用其许可。 [第三方许可说明](THIRD_PARTY.md)

封面素材来自 [Switch Science 的 RDK X5 产品页](https://www.switch-science.com/products/10500) 和 [飞牛官网](https://fnnas.com/)。
