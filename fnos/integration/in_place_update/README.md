# RDK X5 v0.3.2 → v0.3.3 原地更新

仅支持与更新包内精确内核/模块摘要匹配的构建（fnOS ARM 1.2.0701、Linux 6.18.0）。当前 PR4 RC1 完整镜像已内置配置，无需再次安装更新包；新候选的真板更新与回滚尚待验收。原始社区 PR 的 v0.3.2 内核与本候选不同，不能只凭版本号混用。r27/v0.3.1 缺少本事务基线，也不能直接使用该包升级。更新包包含 AIC8800 Wi-Fi 模块、应用兼容脚本、Wi-Fi 与 CPU 性能配置，以及版本标记。系统分区中的 `/var/lib/fnos-x5/update-v0.3.3/` 保存原模块和脚本，用于回滚。同一更新包可重复执行。升级或回滚过程中中断后，使用同一个包重试 `apply` 或执行 `rollback`。

当前候选更新包名为 `fnos-x5-update-v0.3.3-current-kernel.pyz`。下载后先核对 [release.json](../../../release.json) 的 `in_place_update` 与 `wifi_driver_hotfix`，只有内核与基线均匹配才运行。以下构建命令需要同一轮审核后的模块和 `wifi-target.json`：

```sh
python3 -m fnos.integration.in_place_update.build fnos-x5-update-v0.3.3-current-kernel.pyz \
  --wifi-module aic8800_fdrv.ko --wifi-target wifi-target.json
sha256sum fnos-x5-update-v0.3.3-current-kernel.pyz
```

维护精确匹配的系统时，将包传到 X5，和 [release.json](../../../release.json) 中的 `in_place_update.sha256` 核对。把包放在系统分区，方便应用更新后再次使用。

```sh
sudo install -o root -g root -m 0600 fnos-x5-update-v0.3.3-current-kernel.pyz /var/lib/fnos-x5/
sudo python3 /var/lib/fnos-x5/fnos-x5-update-v0.3.3-current-kernel.pyz status
sudo python3 /var/lib/fnos-x5/fnos-x5-update-v0.3.3-current-kernel.pyz apply
sudo reboot
```

成功后命令输出 `v0.3.3`。重启会加载新模块并禁用 AIC8800 省电，Wi-Fi 继续启用。CPU 使用 `performance` governor，在内核允许的频率范围内请求最高频率，温控仍然生效。可以核对加载的模块和性能设置。

```sh
cat /sys/module/aic8800_fdrv/srcversion
cat /sys/module/aic8800_fdrv/parameters/ps_on
cat /sys/devices/system/cpu/cpufreq/policy*/scaling_governor
```

已经装过早期 v0.3.3 包的设备可继续执行 `apply` 补齐性能配置。包会拒绝覆盖相同路径下内容不同的本地配置。回滚移除包管理的性能配置和 CPU 服务启用链接，命令输出 `v0.3.2`。重启加载原模块并恢复系统默认的频率策略。原模块可能复现已知的 Wi-Fi 启动异常。

```sh
sudo python3 /var/lib/fnos-x5/fnos-x5-update-v0.3.3-current-kernel.pyz rollback
sudo reboot
```

## 完整镜像与其他内核构建

v0.3.3 完整镜像也通过同一个更新事务部署：原版应用兼容脚本、原版 Wi-Fi
模块先保存为回滚基线，再安装修复模块、禁用 Wi-Fi 省电、启用 CPU 性能服务，
最后写入版本标记。成品镜像验证会检查这组文件、性能配置和回滚基线；缺少
修复模块或对应内核校验清单时，组装器会拒绝产出标记为 v0.3.3 的镜像。
普通用户直接安装完整镜像，无需通过 SSH 补装这些配置。

不同内核构建即使都显示 `6.18.0`，也不能混用模块。原始 PR 的更新目标继续
保留；为另一套已审核构建制作更新包时，应将相应的 `wifi-target.json` 同时
交给镜像组装器和更新包构建器：

```sh
python3 -m fnos.integration.in_place_update.build update.pyz \
  --wifi-module aic8800_fdrv.ko --wifi-target wifi-target.json
```

该清单使用 `fnos-x5-wifi-target/v1` 格式，包含 `kernel_image_sha256`、
`base_driver_sha256` 和 `driver_sha256`，分别绑定精确内核、原版模块及修复模块。
这些摘要必须来自同一轮受控构建。更新包内嵌该清单，在写文件前核对本机内核，
不会因为内核版本字符串相同而覆盖不匹配的驱动。完整镜像和相应更新包必须
使用同一清单。未提供该参数时，构建器仍只接受本文原始 PR 的已审核模块。
