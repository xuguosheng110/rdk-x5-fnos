/* SPDX-License-Identifier: MIT
 * Persistent X5 CLIP worker; -DFNOS_FACEID builds the experimental InsightFace
 * worker, -DFNOS_FACEID128 builds the upstream 128D FaceID NV12 worker,
 * -DFNOS_SEMANTIC768 builds the paired native Chinese semantic image worker.
 * Protocol: X5CL / X5IF / X5F1 / X5CN, then fixed tensors. FaceID128 uses
 * 128 learned features followed by 384 zeroes in the native 512-float envelope;
 * this preserves cosine distance exactly, without inventing learned features.
 * Compiled on the build server; no dependency on a Python-version-specific SDK.
 * stdout is reserved for wire data; vendor stdout is redirected to stderr.
 */
#define _POSIX_C_SOURCE 200809L
#if defined(FNOS_FACEID128) && !defined(FNOS_FACEID)
#define FNOS_FACEID
#endif
#if defined(FNOS_SEMANTIC768) && defined(FNOS_FACEID)
#error "Semantic768 and FaceID are different model contracts"
#endif
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "dnn/hb_dnn.h"

#define INPUT_FLOATS (3 * 224 * 224)
#ifdef FNOS_SEMANTIC768
#define OUTPUT_FLOATS 768
#else
#define OUTPUT_FLOATS 512
#endif
#define CHECK(call) do { int rc = (call); if (rc) { \
    fprintf(stderr, "%s: %d\n", #call, rc); goto cleanup; } } while (0)

static int contract(const hbDNNTensorProperties *p, int input) {
#ifdef FNOS_FACEID128
    const int valid_in[] = {1, 3, 112, 112}, aligned_in[] = {1, 3, 112, 128};
    const int valid_out[] = {1, 128, 1, 1}, aligned_out[] = {1, 128, 1, 4};
    if (p->tensorLayout != HB_DNN_LAYOUT_NCHW || p->quantiType != SHIFT ||
        p->validShape.numDimensions != 4 || p->alignedShape.numDimensions != 4 ||
        p->tensorType != (input ? HB_DNN_IMG_TYPE_NV12_SEPARATE : HB_DNN_TENSOR_TYPE_S32) ||
        p->alignedByteSize != (input ? 112 * 128 * 3 / 2 : 2048)) return 0;
    for (int i = 0; i < 4; i++)
        if (p->validShape.dimensionSize[i] != (input ? valid_in[i] : valid_out[i]) ||
            p->alignedShape.dimensionSize[i] != (input ? aligned_in[i] : aligned_out[i])) return 0;
    return 1;
#endif
#ifdef FNOS_FACEID
    if (input) {
        const int valid[] = {1, 3, 112, 112}, aligned[] = {1, 3, 112, 128};
        if (p->tensorType != HB_DNN_IMG_TYPE_NV12 || p->tensorLayout != HB_DNN_LAYOUT_NCHW ||
            p->quantiType != NONE || p->alignedByteSize != 112 * 128 * 3 / 2 ||
            p->validShape.numDimensions != 4 || p->alignedShape.numDimensions != 4) return 0;
        for (int i = 0; i < 4; i++)
            if (p->validShape.dimensionSize[i] != valid[i] || p->alignedShape.dimensionSize[i] != aligned[i]) return 0;
        return 1;
    }
#endif
    const int n = input ? INPUT_FLOATS : OUTPUT_FLOATS;
    int64_t count = 1;
    if (p->tensorType != HB_DNN_TENSOR_TYPE_F32 || p->quantiType != NONE ||
        p->validShape.numDimensions < 1 || p->validShape.numDimensions > 4 ||
        p->alignedByteSize != n * (int)sizeof(float)) return 0;
    for (int i = 0; i < p->validShape.numDimensions; i++) {
        int d = p->validShape.dimensionSize[i];
        if (d < 1 || d > n || p->alignedShape.dimensionSize[i] != d) return 0;
        count *= d;
    }
    if (count != n) return 0;
    if (input && (p->validShape.numDimensions != 4 ||
        p->validShape.dimensionSize[0] != 1 || p->validShape.dimensionSize[1] != 3 ||
        p->validShape.dimensionSize[2] != 224 || p->validShape.dimensionSize[3] != 224)) return 0;
    return 1;
}

int main(int argc, char **argv) {
    if (argc != 2) { fprintf(stderr, "usage: clip_worker model.bin\n"); return 2; }
    uint16_t endian = 1;
    if (*(uint8_t *)&endian != 1) return 2;
    int wire_fd = dup(STDOUT_FILENO);
    if (wire_fd < 0 || dup2(STDERR_FILENO, STDOUT_FILENO) < 0) return 2;
    FILE *wire = fdopen(wire_fd, "wb");
    if (!wire) { close(wire_fd); return 2; }
    setvbuf(wire, NULL, _IONBF, 0);
    int result = 1, in_alloc = 0, out_alloc = 0, uv_alloc = 0;
    hbPackedDNNHandle_t packed = NULL;
    hbDNNHandle_t model = NULL;
    hbDNNTaskHandle_t task = NULL;
    hbDNNTensor input = {0}, output = {0}, *outputs = &output;
    const char **names = NULL, *file = argv[1];
    int count = 0;
    CHECK(hbDNNInitializeFromFiles(&packed, &file, 1));
    CHECK(hbDNNGetModelNameList(&names, &count, packed));
    if (count != 1) goto cleanup;
    CHECK(hbDNNGetModelHandle(&model, packed, names[0]));
    CHECK(hbDNNGetInputCount(&count, model)); if (count != 1) goto cleanup;
    CHECK(hbDNNGetOutputCount(&count, model)); if (count != 1) goto cleanup;
    CHECK(hbDNNGetInputTensorProperties(&input.properties, model, 0));
    CHECK(hbDNNGetOutputTensorProperties(&output.properties, model, 0));
    fprintf(stderr, "model=%s in_type=%d in_bytes=%d out_type=%d out_bytes=%d\n",
        names[0], input.properties.tensorType, input.properties.alignedByteSize,
        output.properties.tensorType, output.properties.alignedByteSize);
    if (!contract(&input.properties, 1) || !contract(&output.properties, 0)) {
        fprintf(stderr, "model tensor contract rejected\n"); goto cleanup;
    }
    CHECK(hbSysAllocCachedMem(&input.sysMem[0], input.properties.alignedByteSize)); in_alloc = 1;
#ifdef FNOS_FACEID128
    CHECK(hbSysAllocCachedMem(&input.sysMem[1], 56 * 128)); uv_alloc = 1;
#endif
    CHECK(hbSysAllocCachedMem(&output.sysMem[0], output.properties.alignedByteSize)); out_alloc = 1;
#ifdef FNOS_FACEID128
    const char *magic = "X5F1";
#elif defined(FNOS_FACEID)
    const char *magic = "X5IF";
#elif defined(FNOS_SEMANTIC768)
    const char *magic = "X5CN";
#else
    const char *magic = "X5CL";
#endif
    if (fwrite(magic, 1, 4, wire) != 4) goto cleanup;
    for (;;) {
#ifdef FNOS_FACEID
        /* Unpadded NV12 on the wire; explicitly populate the 128-byte stride. */
        uint8_t frame[112 * 112 * 3 / 2];
        size_t bytes = fread(frame, 1, sizeof(frame), stdin);
        if (!bytes && feof(stdin)) { result = 0; break; }
        if (bytes != sizeof(frame)) { fprintf(stderr, "truncated tensor\n"); break; }
        uint8_t *in = input.sysMem[0].virAddr;
        memset(in, 0, 112 * 128); memset(in + 112 * 128, 128, 56 * 128);
#ifdef FNOS_FACEID128
        for (int row = 0; row < 112; row++) memcpy(in + row * 128, frame + row * 112, 112);
        uint8_t *uv = input.sysMem[1].virAddr;
        memset(uv, 128, 56 * 128);
        for (int row = 0; row < 56; row++) memcpy(uv + row * 128, frame + 112 * 112 + row * 112, 112);
        CHECK(hbSysFlushMem(&input.sysMem[1], HB_SYS_MEM_CACHE_CLEAN));
#else
        for (int row = 0; row < 168; row++) memcpy(in + row * 128, frame + row * 112, 112);
#endif
        int valid = 1;
#else
        float *in = input.sysMem[0].virAddr;
        size_t bytes = fread(in, 1, INPUT_FLOATS * sizeof(float), stdin);
        if (!bytes && feof(stdin)) { result = 0; break; }
        if (bytes != INPUT_FLOATS * sizeof(float)) { fprintf(stderr, "truncated tensor\n"); break; }
        int valid = 1;
        for (int i = 0; i < INPUT_FLOATS; i++)
            if (!isfinite(in[i]) || in[i] < 0 || in[i] > 1) { valid = 0; break; }
        if (!valid) { fprintf(stderr, "invalid input tensor\n"); break; }
#endif
        CHECK(hbSysFlushMem(&input.sysMem[0], HB_SYS_MEM_CACHE_CLEAN));
        hbDNNInferCtrlParam ctrl;
        HB_DNN_INITIALIZE_INFER_CTRL_PARAM(&ctrl);
#ifdef FNOS_FACEID
        /* The pinned faceid model uses the resizer/ROI input source. */
        hbDNNRoi roi = {0, 0, 111, 111};
        CHECK(hbDNNRoiInfer(&task, &outputs, &input, &roi, 1, model, &ctrl));
#else
        CHECK(hbDNNInfer(&task, &outputs, &input, model, &ctrl));
#endif
        int rc = hbDNNWaitTaskDone(task, 10000);
        if (rc) {
            /* Do not free DMA buffers while an asynchronous task may own them. */
            fprintf(stderr, "BPU task failed/timed out: %d\n", rc);
            _Exit(3);
        }
        CHECK(hbSysFlushMem(&output.sysMem[0], HB_SYS_MEM_CACHE_INVALIDATE));
#ifdef FNOS_FACEID128
        /* Upstream FaceIDExtractor uses every fourth S32 element. The common
         * quantization factor cancels during cosine normalization, matching its
         * ToFloat implementation. Padding cannot affect norms or distances. */
        float out[OUTPUT_FLOATS] = {0};
        const int32_t *raw = output.sysMem[0].virAddr;
        for (int i = 0; i < 128; i++) out[i] = (float)raw[i * 4];
#else
        float *out = output.sysMem[0].virAddr;
#endif
        for (int i = 0; i < OUTPUT_FLOATS; i++) if (!isfinite(out[i])) valid = 0;
        CHECK(hbDNNReleaseTask(task)); task = NULL;
        if (!valid || fwrite(out, sizeof(float), OUTPUT_FLOATS, wire) != OUTPUT_FLOATS) break;
    }
cleanup:
    if (task) hbDNNReleaseTask(task);
    if (out_alloc) hbSysFreeMem(&output.sysMem[0]);
    if (uv_alloc) hbSysFreeMem(&input.sysMem[1]);
    if (in_alloc) hbSysFreeMem(&input.sysMem[0]);
    if (packed) hbDNNRelease(packed);
    fclose(wire);
    return result;
}
