# Third-party components

This repository publishes the X5 integration layer. Existing SPDX headers and upstream copyright notices retain their original meaning. Kernel patches and derivative kernel components retain their applicable upstream licenses; no blanket relicensing of Linux, fnOS, D-Robotics libraries, models, or other bundled image components is intended.

The system image includes fnOS ARM user space, Linux and D-Robotics runtime/model components. Their respective licenses and notices apply. This community release does not claim ownership of proprietary components or imply vendor endorsement. Third-party full source and model payloads are not included in this Git source export. Build inputs must be obtained from their upstream distributions under their applicable terms.

Model source revisions and hashes: `fnos/integration/native_photos/model-sources.json`. Entries marked experimental or failed are not the default product route. v0.1.0 uses official face detection, landmark106 and FaceID128; it does not ship the experimental semantic BPU route as the default.

## 许可核对（2026-11-11）

| 组件 | 许可边界 / 上游依据 |
| --- | --- |
| 本项目原创独立适配代码与文档 | 根目录 LICENSE 的 MIT 授权；已有 SPDX 和第三方衍生文件除外 |
| Linux 内核及衍生补丁 | 按对应源码的 GPL-2.0-only 等标识及 COPYING；[Linux 许可规则](https://docs.kernel.org/process/license-rules.html)，分发二进制时应提供对应源码及必要构建材料 |
| RDK 构建工具 | [D-Robotics/x5-rdk-gen LICENSE](https://github.com/D-Robotics/x5-rdk-gen/blob/main/LICENSE) 为 Apache-2.0；不能据此推断所有 RDK 二进制、固件和模型使用相同协议 |
| FaceID 参考代码 | [D-Robotics/faceid LICENSE](https://github.com/D-Robotics/faceid/blob/develop/LICENSE)，仓库声明 Apache-2.0；其他模型及运行库仍分别按其分发条款 |
| 飞牛系统及专有应用 | [飞牛最终用户许可协议 V2](https://fnnas.com/terms)（2026-09-03）；开源组件按各自许可证，专有部分不由本项目重新授权 |

飞牛协议第 1 节授予有限个人非商用许可；第 3 节对分发、修改等行为规定限制，第 6 节保留开源组件自身许可的优先性。本仓库没有保存证明完整修改镜像获得再分发许可的材料，因此**不能将已提供下载等同于已取得第三方分发授权**。应核实适用版本的明确许可或取得书面授权；“免费”“家庭使用”“注明来源”都不能自动替代该授权。本说明不是飞牛的授权书。

v0.2.0 Release 的内核归档包含 r16 对应的 X5 Linux 6.18 源码快照、构建配置和编译器版本。该归档与适配仓库分别保留各自的上游许可。BSP 的精确提交对应关系仍列在 TODO 中。第三方商标、名称及版权归各自权利人。
