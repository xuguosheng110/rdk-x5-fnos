# Third-party components

This repository publishes the X5 integration layer. Existing SPDX headers and upstream copyright notices retain their original meaning. Kernel patches and derivative kernel components retain their applicable upstream licenses; no blanket relicensing of Linux, fnOS, D-Robotics libraries, models, or other bundled image components is intended.

The system image includes fnOS ARM user space, Linux and D-Robotics runtime/model components. Their respective licenses and notices apply. This community release does not claim ownership of proprietary components or imply vendor endorsement. Third-party full source and model payloads are not included in this Git source export. Build inputs must be obtained from their upstream distributions under their applicable terms.

Model source revisions and hashes: `fnos/integration/native_photos/model-sources.json`. Entries marked experimental or failed are not the default product route. v0.1.0 uses official face detection, landmark106 and FaceID128; it does not ship the experimental semantic BPU route as the default.
