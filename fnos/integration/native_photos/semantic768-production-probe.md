# 原生生产预算筛查

2026-11-10 11:27:53 真板原生 `img2vec` stdout 明确为 `cpu_usage=50.0`、4/8 CPU `[0, 1, 2, 3]`、`num_threads_onnx:4`、原生 Medium ONNX。原文 742 字节，SHA256 `f880d0d86f0a56396c002e7377be6003b3c28b56a93dac815fe0f1d36bdce40d`，随包保存为 `production-budget.log`。

服务器原版 `ai_manager` 的 Go `SrvImplImg2Vec.ApplyBootParam` CPU 分支也印证该行为：0x94323c 调用 `ComTrimResMonApi.GetCPU`，0x943244–0x943250 将返回 CPU 数除2；零时最少1。结果填入 `--num_threads_onnx`。0x943444–0x943468 追加 `--cpu_usage 50`。因此包装器未显式传参时的3线程默认值不是线上实际预算。

本次只做快速回归筛查：原28张的固定索引 `[0,5,10,15,20,25]`，3次预热/方、3轮交替配对，共36个计时请求。两服务先继承0–3亲和，再传`cpu_usage100`，避免再减半；原CPU明确4个ONNX线程。严格保留r6的模型、worker、预处理、768维和精度门槛。复用34张预处理oracle时仍核验其真实1线程记录；新的4线程CPU每次返回都与r6同图原CPU向量逐坐标比较（余弦≥0.999999、最大绝对差≤0.00002），不冒充完整重跑34张oracle。

用r8包code及既有准确依赖运行：

```text
/usr/bin/python3.11 -m fnos.integration.native_photos.semantic768_production_probe
  --work-dir /tmp/fnos-semantic768-native-r8/production-probe-r1
  --native-executable /usr/trim/lib/ai_manager/com.trim.img2vec/cp311/img2vec.bin
  --native-onnx /vol1/@sysappmeta/ai-manager/models/img2vec-medium-model-1.0.0/img2vec.onnx
  --native-rpc-dir /usr/trim/lib/ai_manager/com.trim.img2vec/cp311
  --worker /var/lib/fnos-x5-validation/semantic768-fc2-exit-20261110-r1/semantic768-worker
  --model /var/lib/fnos-x5-validation/semantic768-fc2-exit-20261110-r1/native_cn_image.bin
  --manifest /tmp/fnos-semantic768-native-r1/fixtures/manifest.json
  --queries-manifest /tmp/fnos-semantic768-native-r8/queries-zh-consistency.json
  --benchmark-report /tmp/fnos-semantic768-native-r6/benchmark-r1/benchmark.json
  --verified-oracle /tmp/fnos-semantic768-native-r5/oracle-reuse.json
  --production-log /tmp/fnos-semantic768-native-r8/production-budget.log
```

主线程只需把`benchmark-report`和`verified-oracle`改为实际已完成的证据路径，其余实际路径按需核验。产出`probe.json`逐请求进度、原生启动receipt和CPU/BPU日志。数值通过但BPU慢时，程序完成筛查、明确推荐`retain_native_cpu_default`；退出0表示测量与数值检查完成，不表示速度门槛通过。六张小样本始终将完整性能样本和产品验收标为false。即便小样本更快也不能直接默认启用。

r8同时按真实txt2vec日志修正独立文本receipt：文本服务确实不输出`model_format`，因此只对文本要求其真实输出的`onnx_model/num_threads/sp/edition/device:CPU`；Popen原始命令仍严格要求ONNX，原ELF/模型/分词器SHA与peer PID绑定不变。图像日志仍要求`model_format:ONNX`。旧r6图像结果保留；新的文本试验用r8和新work-dir重跑即可。
