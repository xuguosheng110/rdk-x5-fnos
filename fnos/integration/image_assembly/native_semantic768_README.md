# Native 768D semantic staging

This is an offline image integration interface. It does not access a board,
change the Photos index, or start a complete image assembly. The native face
detector/FaceID BPU integration remains independent and unchanged.

## Current default: preserve the vendor CPU path

```python
from fnos.integration.image_assembly.native_semantic768 import (
    stage_native_semantic768, verify_native_semantic768,
)
report = stage_native_semantic768(offline_root)
verify_native_semantic768(offline_root, report)
```

Without a complete independently approved review, this writes only
`usr/share/fnos-x5/native-semantic768-manifest.json`. It verifies and leaves the
original `img2vec.bin` unchanged. It does not install the 220 MB experimental
model, worker, wrapper, configuration, or new Python dependencies. A missing
review is not a partial installation and does not reduce native functionality.

The server offline check used the real 917,392-byte vendor ELF, SHA-256
`3399764ba791ec678f10b96d66558146001f3dbcafe8e6acf1e281fe2461a534`, and verified
that no candidate payload or launcher replacement appeared.

## Optional activation after separate acceptance

The enabled API is implemented and fixture-tested; it has not received product
activation acceptance. It requires both a review file and its independently
supplied SHA-256. Do not construct a PASS review from unit-test output.

```python
report = stage_native_semantic768(
    offline_root, pinned_assets_directory,
    activation_review=independent_review_path,
    activation_review_sha256=independently_verified_review_sha256,
)
```

The review schema is `fnos-semantic768-activation-review/v1`, with
`reviewer: Codex-independent`, `status: PASS`, and `artifacts` exactly equal to
`semantic768_ready.artifact_identity()`. Each of these `gates` must independently
contain `status: PASS` and the actual report's `evidence_sha256`:

- `native_preprocess_oracle`
- `equal_budget_performance`
- `production_default_performance`
- `native_chinese_search`
- `mixed_cpu_bpu_index`

The production budget gate must compare the observed native production launch
and thread/affinity budget. A single-core improvement is not sufficient and no
production budget numbers are hardcoded by this interface. If native CPU is
faster in the actual product budget, retain the default CPU implementation.

The enabled `assets.json` schema is `fnos-semantic768-assets/v1`; `files` must
contain exactly `native_cn_image.bin` and `semantic768-worker`, with their
reviewed SHA-256 values (`dd605b…4939`, `2d08cd…184e`). The stager reuses and fully
verifies the existing native Photos manifest and its NumPy/Pillow dependencies.
It installs only the three pinned semantic runtime modules, the model/worker,
the thin ready dispatcher, policy, and launcher. It saves the vendor ELF at
the fixed sibling `img2vec.native.bin` before an atomic launcher replacement.

## Model-ready behavior

The normal ai_manager process launch is the readiness event. The dispatcher
uses its actual `--onnx_model` and `--socket_path`; it does not invent a socket
path or poll the whole filesystem. The domestic Medium layout is:

```text
/volN/@sysappmeta/ai-manager/models/
  img2vec-medium-model-1.0.0/img2vec.onnx
  txt2vec-medium-model-1.0.0/txt2vec.onnx
  txt2vec-medium-model-1.0.0/sp.model
```

All three content hashes must match the native 768D coordinate space. The
existing product bootstrap already seeds these models before restarting
ai_manager. The dispatcher atomically writes a private per-socket RAM config,
then invokes the frozen `semantic768_entry.py`. A missing/wrong model, unknown
native arguments, another edition, bad policy, or config-write error delegates
the original arguments to the pinned vendor CPU program. A running service
failure is not caught to start a second service on its socket.

Existing CPU vectors, Photos databases, tokenizer, text executable, application
defaults, and user data are not mutated. No index reset or alternate 512D
embedding namespace is introduced.

## Verification and limits

Run `python3 -m unittest fnos.tests.test_native_semantic768_staging -v`.
24 tests passed on macOS and build-server. They include disabled/native preservation,
active staging with synthetic review/fixture assets, corruption and symlink
rejection, frozen-runtime drift, failed atomic entry replacement, required
production-budget evidence, model pairing, exact CPU argument fallback, private
runtime files, refusal to start CPU after a running service fails, and rejection
of dormant candidate files already present in a disabled root filesystem.

Server evidence is in
`/srv/fnos-build/native-semantic768-staging-r1-20261110/`:
`offline-acceptance.json`, `unittest.log`, and `real-cpu-root/`.
The original evidence copy is `semantic768_staging_offline_acceptance.json`;
`semantic768_staging_offline_acceptance_r2.json` supersedes its source hashes
and adds the dormant-file regression.

The explicit assembler option `--native-semantic-cpu` records the unchanged CPU
path in build state and runs `verify_native_semantic768` against the extracted
final root with `require_root_owner=True`. It adds one critical check (49 with
the existing full product options). It does not expose BPU activation flags.
Complete-image boot, native Chinese UI,
and production-performance acceptance remain separate from these offline
checks.
