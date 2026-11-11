# 原生中文文本独立核对

r7 仅增加 `semantic768_run_text_validation.py` 和其测试。r6 的模型、预处理、入口、服务、计时及 receipt 验证文件保持逐字节一致；当前进行中的 r6 测速无需更新。

图像测速结束后，由主线程保持硬件互锁并使用与 r6 相同的 Python 3.11 及依赖目录执行：

```text
/usr/bin/python3.11 -m fnos.integration.native_photos.semantic768_run_text_validation
  --work-dir /tmp/fnos-semantic768-native-r7/text-r1
  --native-executable /usr/trim/lib/ai_manager/com.trim.txt2vec/cp311/txt2vec.bin
  --native-text-model /vol1/@sysappmeta/ai-manager/models/txt2vec-medium-model-1.0.0/txt2vec.onnx
  --native-tokenizer /vol1/@sysappmeta/ai-manager/models/txt2vec-medium-model-1.0.0/sp.model
  --benchmark-report <已完成的r6测速目录>/benchmark.json
  --queries-manifest /tmp/fnos-semantic768-native-r7/queries-zh-consistency.json
  --cpus 7 --threads 1
```

PYTHONPATH 首项是 r7 的 `code`。`--cpus` 应使用本板实际可用预算；只启动一个临时原生文本进程和一个结果比较进程。`work-dir` 必须是不存在的新目录，socket 固定在该临时目录中，不探测或替换产品 socket。

启动前核验原始文本 ELF、Medium ONNX、SentencePiece 的已固定 SHA；核验已完成的图像 benchmark、其原始样本和向量归档。原生文本明确传 `--model_format ONNX --edition domestic --sp ... --num_threads_onnx ...`。健康接口严格要求 `data:true`，随后从真实 Popen 对象生成 receipt，以 PID/start_ticks、真实 ELF、启动日志、模型和分词器 SHA、SO_PEERCRED 绑定服务。比较器必须由同一存活控制器启动；不使用被 setproctitle 改写的 cmdline。

输出 `state.json`、`text.log`、`text-receipt.json`、`compare.log`、`text-consistency.json`。正常结束、异常和中断均只停止自己创建的独立进程组。20 条查询逐条记录新旧图像向量的 top1、top5 重合率与分数差；top1 一致率要求至少 95%。没有人工相关性标签，因此该结果不是检索准确率，也不等同于原生相册/Web 产品验收。

不重新执行任何图像推理，不修改默认模型、ai_manager 注册、相册索引、数据卷、系统服务、镜像或板卡电源。原生 txt2vec 的实际运行与日志仍须真板验证；离线测试通过不会被记为中文检索已通过。
