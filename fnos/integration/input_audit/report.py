"""Human-readable rendering of an fnos-input-audit JSON document."""

from __future__ import annotations

from typing import Any


def _fmt_bytes(value: int | None) -> str:
    if value is None:
        return "?"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    n = float(value)
    for unit in units:
        if n < 1024 or unit == units[-1]:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{value} B"


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        out.append("| " + " | ".join("" if v is None else str(v).replace("|", "\\|") for v in row) + " |")
    return "\n".join(out)


def render(doc: dict) -> str:
    inp = doc.get("input", {})
    off = inp.get("official", {})
    img = doc.get("image", {})
    root = doc.get("rootfs", {})
    elf = root.get("elf", {})
    sysd = root.get("systemd", {})
    boot = root.get("boot", {})
    bootpart = root.get("boot_partition", {})
    scripts = root.get("scripts", {})
    net = root.get("network", {})
    storage = root.get("storage", {})
    modules = root.get("modules", {})
    kc = doc.get("kernel_compare", {}).get("config", {})
    km = doc.get("kernel_compare", {}).get("modules", {})
    verdict = doc.get("verdict", {})
    findings = doc.get("findings", [])
    p0 = [f for f in findings if f["severity"] == "P0"]
    p1 = [f for f in findings if f["severity"] == "P1"]
    p2 = [f for f in findings if f["severity"] == "P2"]

    lines: list[str] = []
    lines.append("# fnOS ARM 输入只读审计（2026-11-06）")
    lines.append("")
    lines.append(f"- schema：`{doc.get('schema')}`；工具：`{doc.get('tool', {}).get('name')} {doc.get('tool', {}).get('version')}`")
    lines.append(f"- 生成时间：{doc.get('generated_at')}；模式：`{doc.get('mode')}`")
    lines.append(f"- 结论：**静态审计完成，未在 X5 上启动、未执行镜像内任何脚本/服务/安装器，不能据此宣称 fnOS 可用。**")
    lines.append("")
    lines.append("## 1. 输入与来源")
    lines.append("")
    lines.append(_table(
        ["项目", "实测值"],
        [
            ["文件", f"`{inp.get('path')}`"],
            ["压缩", inp.get("compression")],
            ["压缩文件大小", _fmt_bytes(inp.get("size"))],
            ["压缩文件 SHA-256（本次计算）", f"`{inp.get('sha256')}`"],
            ["解压大小", _fmt_bytes((inp.get("decompressed") or {}).get("size"))],
            ["原始 URL", off.get("source_url") or "未提供"],
            ["官方校验类型/值", f"{off.get('checksum_type') or '无'} / `{off.get('checksum') or '无'}`"],
            ["官方校验是否通过", "未提供官方校验值" if not off.get("checksum") else off.get("checksum_verified")],
        ],
    ))
    lines.append("")
    lines.append("> 官方页面提供 MD5 时，本工具用原厂 MD5 判定“官方校验通过”；SHA-256 是本项目自行计算，不冒充官方哈希。")
    lines.append("")
    lines.append("## 2. 分区与根文件系统")
    lines.append("")
    lines.append(f"- 分区表：`{img.get('table')}`；扇区：{img.get('sector_size')} B；分区前原始区域：{_fmt_bytes(img.get('pre_partition_bytes'))}")
    if img.get("meta", {}).get("header_crc_ok") is not None:
        lines.append(f"- GPT CRC：header={img['meta'].get('header_crc_ok')}，entries={img['meta'].get('entries_crc_ok')}")
    rows = []
    for p in img.get("partitions", []):
        fs = p.get("filesystem", {})
        rows.append([
            p.get("index"),
            p.get("name") or "",
            p.get("type"),
            _fmt_bytes(p.get("start_bytes")),
            _fmt_bytes(p.get("size_bytes")),
            fs.get("type"),
            fs.get("label") or "",
            fs.get("uuid") or "",
            p.get("unique_guid") or "",
        ])
    if rows:
        lines.append("")
        lines.append(_table(["#", "名称", "类型", "起始", "大小", "文件系统", "label", "文件系统 UUID", "unique partition GUID"], rows))
    lines.append("")
    lines.append(_table(
        ["根文件系统项", "实测值"],
        [
            ["分区", root.get("partition_index")],
            ["类型/后端", f"{root.get('fstype')} / {root.get('backend')}"],
            ["UUID/label", f"{root.get('uuid')} / {root.get('label')}"],
            ["unique partition GUID", root.get("unique_guid")],
            ["OS", ", ".join(f"{k}={v}" for k, v in (root.get("os_release") or {}).items() if k in ("NAME", "VERSION", "ID", "VERSION_ID", "PRETTY_NAME"))],
            ["Debian", root.get("debian_version")],
            ["hostname", root.get("hostname")],
            ["init", f"{sysd.get('init', {}).get('init_target')}；systemd={sysd.get('init', {}).get('is_systemd')}"],
            ["包数量（dpkg installed）", (root.get("packages") or {}).get("count")],
            ["内核模块", f"{modules.get('count')} 个 .ko；modules.dep {modules.get('modules_dep_lines')} 行；release {modules.get('kernelrelease')}"],
            ["独立 /boot 分区", (f"partition {bootpart.get('partition_index')} / {bootpart.get('fstype')}；config={bootpart.get('config_name')}" if bootpart.get('partition_index') else "未发现")],
            ["fnOS 内核", (modules.get('versions') or [None])[0]],
        ],
    ))
    ident = root.get("identity_checks") or {}
    if ident.get("checked"):
        lines.append("")
        lines.append("### 2.1 fstab UUID 与分区 UUID")
        lines.append("")
        lines.append(_table(
            ["挂载点", "fstab UUID", "分区 UUID", "一致"],
            [[c["mount"], c["fstab_uuid"], c["partition_uuid"], c["fstab_uuid"].lower() == (c["partition_uuid"] or "").lower()] for c in ident["checked"]],
        ))
        if ident.get("mismatches"):
            lines.append("")
            lines.append("> **不一致**：源镜像 fstab 的 UUID 与分区超级块 UUID 不同；组装前必须确认 `root=PARTUUID` 与首启是否重写 fstab，不能直接假定任一方。")
    lines.append("")
    lines.append("## 3. AArch64 ELF、loader 与 glibc")
    lines.append("")
    loader = elf.get("loader") or {}
    libc = elf.get("libc") or {}
    lines.append(_table(
        ["项目", "实测值"],
        [
            ["loader 路径", loader.get("path")],
            ["loader 符号链接", loader.get("symlink")],
            ["loader 机器", (loader.get("elf") or {}).get("machine_name")],
            ["libc 路径/链接", f"{libc.get('path')} -> {libc.get('symlink')}"],
            ["libc banner", libc.get("banner")],
            ["关键二进制最高 GLIBC 要求", elf.get("max_glibc_requirement")],
            ["关键 ELF 样本数", elf.get("sample_count")],
            ["非 AArch64 样本", len(elf.get("foreign") or [])],
        ],
    ))
    if elf.get("samples"):
        lines.append("")
        lines.append(_table(
            ["ELF", "架构", "interpreter", "NEEDED（前 4 项）"],
            [
                [s["path"], s["machine_name"], s.get("interpreter") or "", ", ".join((s.get("needed") or [])[:4])]
                for s in elf["samples"]
            ],
        ))
    lines.append("")
    lines.append("## 4. systemd 包与服务")
    lines.append("")
    key = (root.get("packages") or {}).get("key", {})
    lines.append(_table(
        ["关键包", "版本"],
        [[k, v or "未安装/未记录"] for k, v in key.items()],
    ))
    lines.append("")
    enabled = sysd.get("enabled") or []
    lines.append(f"- 已启用单元（/etc/systemd/system 链接）：**{len(enabled)}**")
    if enabled:
        lines.append("- 列表：" + "、".join(f"`{e['unit']}`" for e in enabled[:40]))
    custom = sysd.get("custom_units") or []
    lines.append(f"- 自定义单元：**{len(custom)}**")
    if custom:
        lines.append("")
        lines.append(_table(
            ["单元", "Description", "ExecStart"],
            [[c["path"], c.get("description") or "", ("; ".join(c.get("exec_start") or []))[:160]] for c in custom[:20]],
        ))
    lines.append("")
    lines.append("## 5. 启动、扩容与更新路径")
    lines.append("")
    lines.append(_table(
        ["项目", "实测/判断"],
        [
            ["/boot 文件数", len(boot.get("files") or [])],
            ["initramfs", len(boot.get("initramfs") or [])],
            ["boot 配置", ", ".join((boot.get("configs") or {}).keys())],
            ["扩容脚本", len(scripts.get("resize_scripts") or [])],
            ["更新判断", scripts.get("assessment")],
            ["更新关键词命中", len(scripts.get("update_references") or [])],
            ["boot/SPI/NAND 关键词命中", len(scripts.get("boot_references") or [])],
            ["更新二进制判断", (scripts.get("update_binaries") or {}).get("assessment")],
            ["boot 分区文件", ", ".join(f.get("name", "") for f in (bootpart.get("files") or [])[:12])],
            ["fnEnv", bootpart.get("fn_env")],
        ],
    ))
    if scripts.get("boot_references"):
        lines.append("")
        lines.append(_table(
            ["文件:行", "关键词", "文本"],
            [[f"{r['path']}:{r['line']}", r["keyword"], r["text"][:120]] for r in scripts["boot_references"][:15]],
        ))
    lines.append("")
    lines.append("## 6. 网络与板型识别")
    lines.append("")
    lines.append(_table(
        ["项目", "实测值"],
        [
            ["网络管理器", ", ".join(net.get("managers") or []) or "未识别"],
            ["NIC 相关模块", ", ".join(net.get("nic_modules") or []) or "未识别"],
            ["板型身份文件", ", ".join(b.get("path") for b in (net.get("board_identity") or [])) or "未发现"],
        ],
    ))
    if net.get("files", {}).get("/etc/network/interfaces"):
        lines.append("")
        lines.append("`/etc/network/interfaces` 前 20 行：")
        lines.append("")
        lines.append("```text")
        lines.extend(net["files"]["/etc/network/interfaces"].splitlines()[:20])
        lines.append("```")
    lines.append("")
    lines.append("## 7. SMB 与存储依赖")
    lines.append("")
    samba = storage.get("samba", {})
    lines.append(_table(
        ["项目", "实测值"],
        [
            ["Samba 包", samba.get("installed")],
            ["smbd/nmbd", f"{samba.get('binaries', {}).get('/usr/sbin/smbd')} / {samba.get('binaries', {}).get('/usr/sbin/nmbd')}"],
            ["smb.conf", samba.get("config_present")],
            ["共享名", ", ".join(samba.get("shares") or []) or "无"],
            ["Samba VFS", ", ".join(samba.get("vfs_objects") or []) or "无"],
            ["缺失 VFS", ", ".join(samba.get("vfs_missing") or []) or "无"],
            ["NFS server", storage.get("nfs", {}).get("installed")],
            ["存储栈", storage.get("storage_stack")],
            ["fstab 条目", len(storage.get("fstab") or [])],
            ["mount 单元", ", ".join(storage.get("mount_units") or []) or "无"],
            ["udev 规则数", len(storage.get("udev_rules") or [])],
        ],
    ))
    if storage.get("fstab"):
        lines.append("")
        lines.append(_table(
            ["设备", "挂载点", "类型", "选项"],
            [[f["device"], f["mount"], f["type"], f["options"]] for f in storage["fstab"][:15]],
        ))
    lines.append("")
    lines.append("## 8. 与 6.18 同次 config / modules 对照")
    lines.append("")
    if kc.get("performed"):
        lines.append(f"- config：`{kc.get('config_path')}`，SHA-256 `{kc.get('config_sha256')}`")
        missing = kc.get("missing") or []
        lines.append(f"- 检查 {len(kc.get('checks') or [])} 项，缺失 **{len(missing)}** 项")
        if missing:
            lines.append("")
            lines.append(_table(
                ["X5 缺失符号", "用途", "级别", "X5 值", "fnOS 值"],
                [[c["symbol"], c["needed_for"], c["severity_if_missing"], c["value"], c.get("fnos_value")] for c in missing],
            ))
        delta = kc.get("fnos_delta") or []
        if delta:
            lines.append("")
            lines.append(f"- fnOS 启用但 X5 缺失：**{len(delta)}** 项（已计入 findings）")
            lines.append(_table(
                ["符号", "fnOS", "X5", "用途"],
                [[c["symbol"], c.get("fnos_value"), c["value"], c["needed_for"]] for c in delta],
            ))
    else:
        lines.append("- 未提供 `--kernel-config`，未做 config 对照。")
    lines.append("")
    if km.get("performed"):
        lines.append(
            f"- 模块清单：X5 {km.get('x5_count')}；fnOS {km.get('fnos_count')}；交集 {len(km.get('intersection') or [])}；"
            f"fnOS-only {len(km.get('fnos_only') or [])}；X5-only {km.get('x5_only_count')}"
        )
        if km.get("fnos_only_generic"):
            lines.append(f"- fnOS-only 通用名（需逐项映射）：{', '.join(km['fnos_only_generic'][:30])}")
    else:
        lines.append("- 未提供 `--kernel-modules`，未做模块清单对照。")
    lines.append("")
    lines.append("## 9. P0 / P1 / P2")
    lines.append("")
    asm = doc.get("assembly_interface", {})
    curated = [
        ("P0 组装依赖（必须补齐）", asm.get("p0_dependencies") or []),
        ("P1 交付前", asm.get("p1_before_delivery") or []),
        ("P2 可后置", asm.get("p2_deferred") or []),
        ("实机确认项", [{"id": f"hw-{i+1}", "summary": x} for i, x in enumerate(asm.get("hardware_confirmations") or [])]),
    ]
    for label, items in curated:
        lines.append(f"### {label}")
        lines.append("")
        if not items:
            lines.append("（无）")
        else:
            lines.append(_table(["ID", "内容", "证据/边界"], [[x.get("id"), x.get("summary"), x.get("evidence", "")] for x in items]))
        lines.append("")
    lines.append("### 审计自动发现（按严重级）")
    lines.append("")
    for label, items in (("P0（组装/启动前必须解决）", p0), ("P1（交付候选前）", p1), ("P2（可后置）", p2)):
        lines.append(f"### {label}")
        lines.append("")
        if not items:
            lines.append("（本级别无发现）")
        else:
            lines.append(_table(["ID", "领域", "结论", "证据/动作"], [[f["id"], f["area"], f["summary"], (f.get("action") or "")[:180]] for f in items]))
        lines.append("")
    lines.append("## 10. X5 启动链 + 用户态最小组装接口")
    lines.append("")
    asm = doc.get("assembly_interface", {})
    lines.append(f"原则：{asm.get('principle')}")
    lines.append("")
    lines.append("```json")
    import json as _json

    lines.append(_json.dumps({"kernel": asm.get("kernel"), "boot": asm.get("boot"), "userspace": asm.get("userspace"), "update_boundary": asm.get("update_boundary")}, ensure_ascii=False, indent=2))
    lines.append("```")
    lines.append("")
    lines.append("门禁：")
    for gate in asm.get("gates", []):
        lines.append(f"- {gate}")
    lines.append("")
    lines.append("## 11. 缺项、未测与复验")
    lines.append("")
    gaps = doc.get("gaps") or []
    if gaps:
        lines.append(_table(["ID", "领域", "缺项"], [[g["id"], g["area"], g["summary"]] for g in gaps]))
    else:
        lines.append("（本次未记录额外缺项）")
    lines.append("")
    lines.append("本次明确未做：")
    lines.append("")
    lines.append("- 未 mount 镜像、未执行镜像内脚本/服务/安装器、未 sudo 安装全局依赖、未写盘、未上板、未操作 CP5/串口/UMS。")
    lines.append("- 静态审计只证明“文件与依赖长什么样”，不证明 fnOS 能在 X5 启动、联网、SMB 或升级。")
    lines.append("- 实机项：X5 U-Boot 加载 6.18 Image/DTB、根分区 UUID 挂载、网卡 PHY、USB/UAS、SMB 往返、首启扩容、升级回退。")
    lines.append("")
    lines.append("复验命令见 `fnos/integration/input_audit/README.md`。")
    lines.append("")
    return "\n".join(lines) + "\n"
