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
  <a href="#常见问题">常见问题</a>
</p>

> [!TIP]
> **[下载 v0.2.0 完整镜像](https://pan.baidu.com/s/1F5t3ehuAREo-Sy2zhoMIWw?pwd=ugjr)** · 提取码 `ugjr` · 约 3.80 GB
>
> 选择 `rdk-x5-fnos-v0.2.0.img.xz`。同一目录提供 `SHA256SUMS`、内核源码和安装说明。 [GitHub Release](https://github.com/xuguosheng110/rdk-x5-fnos/releases/tag/v0.2.0)

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

中文语义搜索和视频缩放使用 CPU。10-bit、HDR、4:4:4 等未支持格式保留软件回退。跨设备兼容性和长期可靠性见 [开发计划](TODO.md)。

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

## 开发与贡献

欢迎补充设备兼容性实测和提交改进。请先阅读 [测试指南](TESTING.md)、[真板结果](TESTED.md) 和 [开发计划](TODO.md)。

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
