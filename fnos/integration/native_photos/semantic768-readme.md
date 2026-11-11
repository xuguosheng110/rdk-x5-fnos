# 原生中文 Medium 768 维候选入口

这是与原生中文图文搜索匹配的 768 维图像编码器候选，不使用实验 512 维 CLIP 的权重、预处理或输出。当前未改 ai_manager 注册、默认模型、线上 socket、照片数据库或镜像组装。真板模型推理通过不等于原生搜索或加速验收通过。

## 已确认的模型证据

- 原生图像 ONNX SHA：`73a177536d297a3e3767ee1cd55f4ab9045bd0c2a4ac9a584fb77eaf3b7e1988`。
- 新混合 CPU/BPU 模型：`dd605bf507f259df824d22a199b55ca423f7020c51766edb599e2d709e384939`，221270314 字节，51 个 BPU 子图。
- worker SHA：`2d08cda15f2905251b4006c3608b5fe394a4f946c4f7a7409d9d7653cd49184e`。
- 原生文本模型：`d4712c45f5c5fba9e5603beba03415f06f1d6755d098e781ffcb0a79f4d65a44`；SentencePiece：`b7fe3bcc8d284fcb782691411e8b6fd4f45d7245565b094de6ab795e66bcd32f`。保留原生 `txt2vec.bin`、分词及文本推理流程。
- 主线程真板 28 张留出图：最小余弦 0.9974023265、平均 0.9984195824、参考图像检索 top1 1.0。证据 `fnos/logs/native-product-20261109/semantic-fc2-exit-28-r2.json`。6009 次 BPU ioctl 是参与证据；166.758 秒含启动和 strace，不能作为性能比较。
- 原生预处理r3真板34/34 PASS：28张留出图和RGB、灰度、RGBA、调色板、CMYK、EXIF共6类模式探针全部一致；最大坐标差`8.940696716308594e-08`，最小余弦`0.9999996154471478`。证据`semantic-native-oracle-r3.json`。依赖在运行期间采集了895个Python源/动态库文件SHA，指纹`472cf2afb009d39c39a2228f2557cbffe8e2ca11f42fd8560f29fcfc9e7d3372`；Python3.11.2、NumPy2.0.2、Pillow11.0.0、ORT1.19.0。此结果仍不是速度或原生中文搜索验收。
- 修复是把每层 fc2 输出 Transpose/Reshape 放回 CPU，并保留共享 q/k/v 输入布局修复。原生249个学习权重未变，没有修改 vendor 库、跳过检查或降低精度门槛。

## 文件与原生接口

`semantic768_backend.py` 核验模型和 worker SHA，持久化单个串行 worker，输入为 RGB/NCHW/224/float32，输出严格为768个有限数并按 float32 做 L2 归一化。它复用受限本地读取器；生产模式每次读取当前数据卷挂载，热插拔不要求重启模型。

`semantic768_service.py` 使用现有 `lib.http_service.HttpService`、`trim.img2vec` 名称、`inferImg2Vec` 方法、root peer 检查和0600 Unix socket。请求仍为 `POST /`：

```json
{"method":"inferImg2Vec","body":{"image":"/vol1/example/photo.jpg"}}
```

返回仍为 `{"code":0,"data":{"embedding":[...]}}`。启动先验证模型并进行一次真实推理，再置为 ready；超时或崩溃清理子进程，下次请求可以重新创建。错误不返回伪向量。

健康协议与推理不同：真板`semantic-native-health-protocol-r2.json`确认`healthCheck`返回`{"code":0,"data":true}`。r3严格按布尔值解析健康，推理仍严格要求object/768向量。r1把健康data误当object导致等待超时，未执行任何oracle；r3保留最后响应/错误，未知协议立即失败，只有socket尚未创建/拒绝连接/初始化超时会重试。

`semantic768_entry.py` 是尚未安装的 ai_manager 启动包装器。默认 `enabled:false` 保留原生行为。仅匹配国内版、ONNX、确切 Medium 图像模型 SHA 时才走新后端；Large、international、其它原生参数完整交回已核验的原始可执行文件。启用还要求配置记录预处理、搜索和性能门槛均已通过。候选配置、资产核验或首次推理失败时，在绑定socket之前明确记日志并回原生CPU；恢复原CPU亲和范围，防止CPU预算重复缩减。服务运行中worker超时/退出是服务错误而非坏图片；监督器清理后下一请求可以重新创建worker。它不修改文本入口，不创建假接口，不自动清空或重建索引。

## 必须先通过的预处理 oracle

原生国内版候选流程为 PIL 直接双三次缩放到224，再处理灰度/四通道、转float32除255、转NCHW；不做中心裁剪、EXIF自动旋转或额外均值方差处理。ONNX内部已经有均值方差变换。RGB与原校准张量一致，但灰度、RGBA、调色板、CMYK与EXIF必须由原始 native CPU RPC 对照确认，不能靠量化相似度推断。

`semantic768_oracle.py` 比较“原始 native CPU RPC输出”和“完全相同原始ONNX + 新预处理”的归一化结果。门槛是余弦≥0.999999且逐坐标最大绝对差≤0.00002。任意样本出错或不同均失败。先做这个CPU对照，避免BPU量化误差掩盖预处理偏差。

`semantic768_make_fixtures.py` 已把原100/28划分中的28张留出JPEG和6类模式探针制成验证样本。它们仅用于工程验证，不作为家庭真实搜索准确率或系统发布内容。

## 主线程真板执行入口

`semantic768_run_validation.py` 只启动两个临时实例。父线程负责硬件互锁与部署；本模块不进行切电、烧录、重启、注册或数据库写入。提供已验证的 Python 3.11、NumPy、Pillow和ONNX Runtime依赖；`PYTHONPATH` 顺序为本包`code`、既有依赖目录，`LD_LIBRARY_PATH`含`/usr/hobot/lib`。样本manifest中的目标路径固定为 `/tmp/fnos-semantic768-native-r1/fixtures`。

调用模块时填入真实板卡路径：

```text
/usr/bin/python3.11 -m fnos.integration.native_photos.semantic768_run_validation
  --work-dir /tmp/fnos-semantic768-native-r1/run-r1
  --native-executable <原始未替换的img2vec.bin>
  --native-onnx <原生img2vec-medium-model-1.0.0/img2vec.onnx>
  --native-text-model <原生txt2vec-medium-model-1.0.0/txt2vec.onnx>
  --native-tokenizer <同目录sp.model>
  --native-rpc-dir /usr/trim/lib/ai_manager/com.trim.img2vec/cp311
  --worker <已通过真板的semantic768-worker>
  --model <dd605b候选native_cn_image.bin>
  --manifest /tmp/fnos-semantic768-native-r1/fixtures/manifest.json
  --cpus <一个实际可用逻辑CPU编号> --threads 1 --rounds 3 --warmup 5
```

可以先加 `--oracle-only`，只做CPU预处理验证。每轮使用新的`work-dir`；目录存在即拒绝，避免重复运行。`state.json`记录两个测试PID、状态、CPU范围、冷启动至ready时间；结果写`oracle.json`与`benchmark.json`。中断或正常结束均终止自己创建的进程组，不操作产品进程。

### 复用已完成的r3 oracle，避免重复十分钟推理

r4保持原预处理模块不变。用r4的code和同一依赖目录，先运行以下只读核验与新证据文件生成命令；原r3目录、报告、代码和日志均保留：

```text
/usr/bin/python3.11 -m fnos.integration.native_photos.semantic768_evidence attest
  --run-dir /tmp/fnos-semantic768-native-r3/oracle-r1
  --package-dir /tmp/fnos-semantic768-native-r3
  --manifest /tmp/fnos-semantic768-native-r3/fixtures/manifest.json
  --runtime-identity /tmp/fnos-semantic768-native-r3/oracle-r1/runtime-identity.json
  --report /tmp/fnos-semantic768-native-r4/oracle-reuse.json
```

之后按上方完整验证命令使用新的`work-dir`，去掉`--oracle-only`并增加`--verified-oracle /tmp/fnos-semantic768-native-r4/oracle-reuse.json`，`--threads 1`、`--rounds 3`、`--warmup 5`。本阶段不再创建另一份CPU oracle模型，也不重算34张对照。

复用固定r3包manifest SHA`a665ddb75676470673dfa42aba016be684ef457be1e266d0f1d42c0fb343f85f`，逐项核验包内容、当前预处理、原始CPU可执行文件/ONNX、34张图片、已完成且清理的state、原报告严格数值门槛，以及当前Python/NumPy/PIL/ORT真实路径、源文件和.so与运行期间采集的指纹。任一差异均拒绝复用。没有删减34张、替换模型或降低阈值。

测速每完成一个后端请求都会原子更新`benchmark.json`，包含阶段、已完成/总请求数、当前轮次/图片/后端、最近延迟及CPU秒。总计168个计时请求，另有10次预热；过程异常会保存已有样本与明确错误，不能把部分结果当作通过。

### r5：独立中文查询清单与测后排序对照

r5沿用上述oracle复用流程，目录前缀改为r5即可；若r4已生成`oracle-reuse.json`，可直接引用旧证据文件。r5不改变预处理、模型或计时逻辑，只在计时结束后额外输出`benchmark.vectors.npz`，保存最后一轮真实RPC返回的CPU/BPU各28×768向量，SHA、图片顺序、图片SHA和模型家族同时写入benchmark报告。

`queries-zh-consistency.json`包含20条通用中文查询，绑定原fixture manifest的SHA，完全独立于原manifest。没有`expected_images`或人工标签，只评估结果一致性。由主线程通过原生ai_manager唤起真正的国内Medium `txt2vec`后执行：

```text
/usr/bin/python3.11 -m fnos.integration.native_photos.semantic768_text_compare
  --benchmark-report /tmp/fnos-semantic768-native-r5/benchmark-r1/benchmark.json
  --queries-manifest /tmp/fnos-semantic768-native-r5/queries-zh-consistency.json
  --text-socket <原生txt2vec实际socket路径>
  --text-receipt <同一受控控制器生成的text-receipt.json>
  --report /tmp/fnos-semantic768-native-r5/text-consistency.json
```

该命令不重新做图像推理、不写相册索引。它先验证已完成的benchmark、图像精度/预处理门槛、向量文件SHA、28张图片顺序和模型家族，再绑定真实文本进程与模型/分词器身份。每条查询记录CPU/BPU top1、top5重合率及分数变化，top1一致率门槛95%；没有人工相关性标签，始终注明“不是搜索准确率”。即便排序一致，性能和原生Web/索引验收仍分别判断。

### r6：原生setproctitle与受控启动凭据

真实`img2vec.bin`启动后把`/proc/PID/cmdline`改成`trim.img2vec`。r5在计时前误把丢失argv当作未验证进程，已退出并清理；这不是模型失败，也没有产生速度结论。

r6控制器在实际Popen原始可执行文件并确认健康后，生成`cpu-receipt.json`：原始argv、控制器PID/启动时间、子PID/启动时间、实际`/proc/PID/exe` SHA、模型SHA、以及原生启动日志确认的model path/num_threads/edition前缀SHA。benchmark必须是该仍存活控制器的另一个子进程，socket peer必须是原Popen子PID，当前原生ELF/模型与日志前缀也必须匹配。未知外部CPU进程不会因为名称或维度相同而被接受。r6已自动把receipt传给benchmark，原完整验证命令无需额外参数。

`txt2vec`也可能重写进程标题，因此后续文本核对同样使用受控启动。由独立控制器Popen原始`txt2vec.bin`，显式传国内ONNX、原文本模型、`--sp`原分词器、线程和独立socket；健康通过后调用`semantic768_runtime_identity.capture_receipt(process, list(process.args), socket_path, startup_log, 'text', receipt_path)`，再由该控制器启动`semantic768_text_compare --text-receipt ...`。日志必须确认模型/线程/分词器路径。控制器结束时只清理自己的文本进程；不能拿现有未知768服务或人工拼写argv代替。模型、预处理和已通过的34张oracle保持不变。

## 公平性能与真正产品验收

1. 无strace计时。CPU参考可执行文件和模型哈希必须正确；显式`--num_threads_onnx`与oracle一致。Unix socket的SO_PEERCRED必须对应被计量的PID。每次RPC前后验证所有服务/子进程PID与启动时间未变、进程亲和预算相同、每个线程的亲和集合没有越出预算；跨两次请求的worker重启也拒绝计入热运行。SDK没有已验证的内部CPU线程数设置，因此报告真实线程数，不虚构“内部线程完全相同”；先以单核预算对照，再按生产CPU预算复测。
2. 每方至少3次预热、28张相同文件、3轮固定种子随机顺序，并交替AB/BA顺序。持续使用同一模型进程。报告中位/p95、吞吐、每图CPU秒、采样RSS、线程数、温度和频率。冷启动单独计量。计时中进程重启、被ptrace或CPU亲和不同均失败。
3. 图像向量固定门槛仍为最小余弦≥0.99、均值≥0.995、参考图像检索top1≥0.95。性能至少应表现为端到端中位延迟和吞吐均改善；仅有BPU参与不叫加速。慢于CPU时保持原生CPU默认，依据CPU子图与边界开销继续优化。
4. 配套中文文本使用真实原生`inferTxt2Vec`。manifest可加入`queries`与可选人工标注`expected_images`，通过`--text-socket`读取原生文本RPC。比较新旧图像索引的中文top1、有关图片召回和排序。没有人工标注的查询只能证明结果一致性，不能称准确率。没有查询时状态明确为`isolated_image_rpc_pass_text_pending`；有查询但没有人工标注时为`unlabelled_consistency_only`。有标注时额外检查BPU有关图片命中率不低于原CPU。任何隔离测试都保持`product_acceptance`待原生任务/混合索引/Web/冷启动验收。
5. 即使隔离RPC通过，仍须走真实相册增量索引、旧CPU向量与新BPU向量混合检索、中文Web搜索、任务取消/重试/重启恢复，再进入干净镜像自动安装与冷启动验收。现有数据库必须备份；本代码不自动操作数据库。上述门槛通过之前不修改默认入口或在飞书标为“产品已加速”。

文本查询额外绑定实际服务身份：SO_PEERCRED取得文本PID，核验`/proc/PID/exe`为原生`txt2vec.bin`（SHA `4b45c00d32322a0c7023786055b4ceb47b196d3273f30afe2b3b11862d18c615`），并核验受控启动凭据和启动日志对应的`--onnx_model`、`--sp`文件SHA与国内ONNX模式；查询前后再核对身份。只核验另一份磁盘模型文件、读取被重写的argv、仅检查输出768维，均不能算中文文本配对已通过。

## 最小生产注册变更（由主线程集成）

在镜像组装中额外打包768模型、worker、本目录的新模块、Python依赖和`semantic768.json`；安装时核验并把原始可执行文件固定保留为`/usr/trim/lib/ai_manager/com.trim.img2vec/cp311/img2vec.native.bin`，再原子替换入口。该固定锚点允许JSON缺失/损坏时仍校验SHA并回原CPU；不得只把备份路径存在可能损坏的JSON中。通过既有ai_manager模型安装事件完成自动配置，保留原生Medium选择和缓存路径。`txt2vec`完全不改。未通过或关闭配置时直接启动原生CPU可执行文件。回滚只恢复原始入口和配置，不删除照片、向量数据库或用户设置。

配置需要`enabled`、`native_executable`、`python_root`、`python_deps`、`worker`、`model`、`native_text_model`、`native_tokenizer`、真实`socket_path`、`preprocess_revision`；通过门槛后再记录`activation_acceptance`。原生socket路径以实际ai_manager传参为准，禁止凭名称猜测后替换线上socket。
