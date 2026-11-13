# v0.3.0 source map (draft)

## r24 integration overlay

The r24 board candidate retains the reviewed r22 boot and kernel chain. The image assembly and 0.3.4 module build used the following overlay files. Each file was independently matched to the source on the build server.

| Path | SHA-256 |
| --- | --- |
| `fnos/integration/native_media/quality_switch.js` | `d3c45d684ff8bc10e9529675a00108c669b94cefc812e6dd52cfdc3ce67ec786` |
| `fnos/integration/image_assembly/media_web.py` | `86a38f187fb5a0781775712b186464495016e3758becd5dedcd7fd1090578e2a` |
| `fnos/integration/image_assembly/native_media.py` | `b6849837808fa8ace184b1ee455ee5bdbfe20ec3ae755ca03f5d83e87343c94e` |
| `fnos/integration/image_assembly/product_inputs.py` | `05817ca6aef7997ac8b087959a83eac1f975dac83c17a0a6c24ffe9fd30004bd` |
| `fnos/integration/image_assembly/product_accel.py` | `56f7003ed4051692464aa95fe97661cf90eac89502bf5a79c9bad961f899bb09` |
| `fnos/integration/image_assembly/product_bootstrap.py` | `3ab9260d1b82b53d828e20ea16bc53e3a80c4bcba41b60296293ed2c069c3dfb` |
| `fnos/integration/acceleration/build_module.py` | `b2f4f929908943766b4172584c9f0eb95e0d6db88a61a26c7a7bdec8af769a6c` |
| `fnos/integration/acceleration/module/app/module_install.py` | `b3deea76aed2d54c0e82585621626eaf6cd1c9537df9a509e22f2d0ccdae2581` |

The earlier source staging references below describe the r20 history.


This is a source-export staging tree, not a release. It is based on the public
repository commit `2c22437` and overlays the accepted r20/r6 product source.
The public repository working tree is not modified.

## Inputs

| Input | Identity |
| --- | --- |
| Public source base | `deliverables/github-rdk-x5-fnos` commit `2c22437` |
| r20 source snapshot | `source-r20-r6payload`; 423 pin rows, 191 real paths after excluding 232 AppleDouble files; tree SHA-256 `52b4afef4b0aabe250813dadf5cd3c1e2df532447cc1230f82905812d9dca266` |
| r20 pin file | `source-r20-r6payload.pins.json`; SHA-256 `1a050e345bfc9b51c760fdda99879011fd6496a4fcfc91f8fd04604f66f627bb` |
| r20 change record | `source-r20-r6payload.changes.json`; SHA-256 `40bc7107fa038bc87e228f08db816d3efffb8cec5dbe6bc4c76c82f2f4f7f4f9` |
| r6 FPK | 1,301,626,413 bytes; SHA-256 `f6401b83d1b9919cf89fff9b479e3581005e7a82fcefe3c78029a5285e0d73a9` |
| r6 actual module build source | `source-r10/fnos/integration/acceleration/build_module.py`; SHA-256 `3a3b346bbb12812e0eabb1bfef09767343f368af4d9ab83960db9b10dab83c8f` (build version `0.3.1`) |
| r20 image assembly | 300 exported files including source-map/release docs; image candidate SHA below |

## Accepted source overlays

The following 9 files are the r20 product source that differs from the public
base. They replace the public copies in this staging tree:

| Path | SHA-256 |
| --- | --- |
| `fnos/integration/acceleration/face_detection_worker.c` | `82ca13d6db7525b56e0bb1eff329324275134f30f55b472901a22e6a9d0f8718` |
| `fnos/integration/image_assembly/README.md` | `8ec28f3603988b224c9378290d4c0e6d01f370b3c5040a5d9c55e6d123b205d7` |
| `fnos/integration/image_assembly/assemble.py` | `fc9d9103925a2dfebce9150558d23e276a69ef58e8f4f4cc8e3351fd1cc986bd` |
| `fnos/integration/image_assembly/native_photos.py` | `22ccc747dfdcb5f2b668ec377881e8b8d39eaf8347789703846485d36e4f401b` |
| `fnos/integration/image_assembly/product_ai_defaults.py` | `2ca992d70b72f17e4b41b302946377c8e6f58227f260e0a9ac7b1e2e6e7acb0d` |
| `fnos/integration/image_assembly/product_bootstrap.py` | `30ebf4dd237d160fa8d461d80312a50cee0209d7d985470510faa5b5b6618c8f` |
| `fnos/integration/image_assembly/product_inputs.py` | `cfcb6b71b5b0016da8a9a1a3743516bf763426dc9c045c4e4b5f0dd8ecbf8cb6` |
| `fnos/integration/native_photos/backend.py` | `f6f72c8626287d4490bb767165c0ae8d4be907b43dd735eb5678b0bf2382158f` |
| `fnos/tests/test_product_bootstrap.py` | `07e8371a2574adc8b7e7e63634a8e24e68ea2cd3470cbce4b12adc819dfdba57` |

The following 20 files are added because they are actual module/product source
or their tests, not experimental work:

| Path | SHA-256 |
| --- | --- |
| `fnos/integration/acceleration/build_module.py` | `3a3b346bbb12812e0eabb1bfef09767343f368af4d9ab83960db9b10dab83c8f` |
| `fnos/integration/acceleration/module/README.md` | `6f0483591d0128550bfeaf081f8cad4ad6859de3aa5ed6fab0d69b31cf7f8a9f` (r6 status + sanitized build paths) |
| `fnos/integration/acceleration/module/app/module_install.py` | `2379738afb70eca0704e89f364e9a9f5bd38850d6028410a904fc573e487a6c0` |
| `fnos/integration/acceleration/module/compat.json` | `2a6021675c8fd662cd32ec2ab2096cc60fc622b48120e9cc2614440ce237f6ab` |
| `fnos/integration/acceleration/module/model-inventory.json` | `e01ec056e4c2d26bffde9c9c50a2c828b05030daac238e56e7fa379e4fa1f9a8` |
| `fnos/integration/acceleration/module/runtime-model-inventory.json` | `2de2a1cb0540d5edbee7316ee7d6291bd788c8e84570b45735a8910d5197c0ea` |
| `fnos/integration/acceleration/module/template/cmd/config_callback` | `c87c7a722d59a266b300850637a1cb77b46cc67c5add8a66aa9e79e198955808` |
| `fnos/integration/acceleration/module/template/cmd/config_init` | `c87c7a722d59a266b300850637a1cb77b46cc67c5add8a66aa9e79e198955808` |
| `fnos/integration/acceleration/module/template/cmd/install_callback` | `c53070f6864456d88cf35204e316eb41229baa18d60d326ce3649a4cf5622f09` |
| `fnos/integration/acceleration/module/template/cmd/install_init` | `24ac1b2da0584282662bd2e9107a01dfefc8f4fdbc7d85d48533d8f06364b298` |
| `fnos/integration/acceleration/module/template/cmd/main` | `21d72dfc1fe2770118bc5d7fab2aa2b3edb0f324749a1f649e663d1feb79d30c` |
| `fnos/integration/acceleration/module/template/cmd/uninstall_callback` | `9d910100d9b003b609f0f5067b4891347d1471fbcf688c24513081cda3e44b13` |
| `fnos/integration/acceleration/module/template/cmd/uninstall_init` | `364757c06cd3c7ef8e32350eab12469263b2c8213179db969b32e04ef743d2ba` |
| `fnos/integration/acceleration/module/template/cmd/upgrade_callback` | `d6419fbaebae6cb5cfc3d8a65018444bf219fa05b37726256f48eddce7a438aa` |
| `fnos/integration/acceleration/module/template/cmd/upgrade_init` | `8781ddf2deebb3d901f1fa2485e6b876f1b994e6f8486f2f9dc976642309b477` |
| `fnos/integration/image_assembly/product_accel.py` | `7412ac5886494052f615b109ef5ab27b9c5bbab96ae8bb2b74126b6bdbc41bcb` |
| `fnos/integration/image_assembly/runtime_profile.py` | `799872ed34475beebf10f8ce5b2c22c4764d78436640576418ab0c89084a1383` |
| `fnos/tests/test_native_photos_staging.py` | `0a99a74c91848cf3b85b2d478b55a121d184a692ebbb86ec744b8f8d61ed4985` |
| `fnos/tests/test_product_accel.py` | `7229a5dc225cc9dac0079d358a1408c6f6883f0bc884e9892dd4a28705eac66f` |
| `fnos/tests/test_runtime_profile.py` | `f79d704714646d98f0d0689111a1aee301a58c8db6d98d5440b837bf7376bdb5` |

`module/README.md` was also updated from the stale "no package built" status
to the actual r6 0.3.1 package/upgrade state. This is a documentation change;
the module implementation is unchanged from the accepted source.

## Public-path sanitization

The export does not contain the build-root literal, the build-host IP, or local
user paths. The following public-base files had those literals replaced with
`/srv/fnos-build`, `build-server`, or `<LOCAL_USER>` placeholders. No logic was
changed, but these are publication edits, not byte-identical copies of the
public HEAD; record them in the final source review.

```text
fnos/integration/hardware/deploy_r3.command
fnos/integration/hardware/deploy_r3.py
fnos/integration/hardware/deploy_r3_root.sh
fnos/integration/hardware/flash_r2.py
fnos/integration/hardware/ram_deploy.py
fnos/integration/hardware/ram_deploy_README.md
fnos/integration/hardware/ram_deploy_helper.c
fnos/integration/hardware/sd_backup.py
fnos/integration/image_assembly/README.md
fnos/integration/image_assembly/build_pci_kernel.py
fnos/integration/image_assembly/native_semantic768_README.md
fnos/integration/image_assembly/rebuild_boot.py
fnos/tests/test_native_media_launcher.py
fnos/tests/test_ram_deploy.py
fnos/tests/test_remote_exec.py
fnos/tests/test_semantic_conversion.py
```

## Excluded from the public source export

These are intentionally not copied; none are accepted product source:

- 37 `fnos/integration/native_photos/semantic_stride_diagnostics/*` files:
  experimental layout/quantization diagnostics; not the default product route.
- `fnos/integration/image_assembly/semantic768_staging_offline_acceptance.json`
  and `..._r2.json`: internal offline acceptance receipts.
- 4 dangling `fnos/integration/input_audit/tests/fixtures/rootfs/...`
  symlinks: fixture-only links with absent targets.
- `fnos/integration/native_media/runtime_av_duration_probe.py`: manual
  measurement utility with host-specific `/var/tmp` paths, not product code.
- 232 AppleDouble `._*` metadata entries from the server snapshot.
- User photos, private keys, credentials, remote access configuration,
  coordination logs, failed/unused models and build outputs.

## Known input difference requiring review

1. **Module build source version.** r20 source has
   `fnos/integration/acceleration/build_module.py` with build version
   `0.3.0.1`; the actual r6 FPK was built from
   `source-r10` with build version `0.3.1`. The staging tree uses the r10
   file. This is intentional and exact; do not replace it with the r20 copy.
2. **Image assembly product_accel.py.** The r20 copy includes the r18
   bundle-directory permission fix (`_bundle_dir`, `enforce_bundle_dirs`) and is
   the source used for the image. The r10 copy is older and must not be used for
   image assembly. The staging tree uses r20.
3. **Module documentation.** The draft's regression command uses the three
   included test modules from the 22-test Linux run. The build example selects
   the same runtime profile and module version as r6.
4. **Kernel source archive.** The v0.2.0 archive hash, embedded kernel build
   identity and configuration match the kernel carried forward in r20. The
   verified archive is a separate candidate asset.

## Kernel/config pin

- r20 config SHA-256: `bee5f9981b13f72ea85374630a5545c0da58c776b51e57308bad36baac5ebf20`
- v0.2.0 release config SHA-256: same value
- r20 `Image-6.18.0` SHA-256: `1429ef8ffa19d3136d2c848c0ea1639b743ad31783b7bea722b840072cfd2cbb`
- Kernel source/config archive is a separate release asset; it must not be
  omitted from the final GitHub/网盘 asset set.

## Review status

- Source tree draft: 300 files; per-file/tree pin is `SOURCE-PIN.json`.
- 9 overlays + 20 additions reconciled against the r20 pin file.
- Linux source regression: 179 tests, 7 skipped (`public-source-tests-linux.log`);
  module-focused: 22 passed (`module-source-tests-linux.log`). These are source
  tests, not board acceptance.
- r20 boot image and r6 FPK are referenced by SHA; neither is copied into this
  source staging tree.
- Real-board acceptance remains pending; see `README.md` and `TESTED.md`.
