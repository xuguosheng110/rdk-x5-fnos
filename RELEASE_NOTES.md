# v0.1.0 — RDK X5 fnOS 家庭共享版

首个公开 Release，基于已完成真板验证的 product-r14 完整系统镜像。无需手工安装存储、转码或 BPU 相册补丁；按原生界面完成首次账号和存储初始化即可使用。

包含原生 H264/HEVC 硬件编解码、官方 BPU 人脸检测/识别、Wi-Fi、家庭共享存储及 WebDAV 持久缓存。烧录完整回读、冷启动、原生备份、应用升级及跨 UID 容器读写已验证。

## 下载

GitHub：下载 `merge-image.py`，在空目录执行 `python3 merge-image.py --download`，自动下载全部 29 个小分卷并逐卷校验，再合并校验完整镜像。也可手动下载所有 `.part*` 后执行 `python3 merge-image.py`。解压 `.img.xz` 后烧录整盘镜像；分卷不可直接烧录。

百度网盘：稍后补充链接；完整包文件名为 `rdk-x5-fnos-v0.1.0.img.xz`。

## 已知限制

家庭共享不实现逐用户 Windows ACL v2 隔离，保留“权限未适配”提示，请勿执行该转换。独立外接 U 盘未验收，尚待实物补测。缩放和中文语义搜索仍为 CPU；10-bit/HDR 等输入的软件回退保留。系统通用自动升级已禁用以保护 X5 启动链。不是官方厂商发行版。

详见 README、TESTED.md、release.json 和校验文件。
