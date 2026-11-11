/* Real BPU inference check; not a fnOS Photos integration claim. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "dnn/hb_dnn.h"

#define CHECK(call) do { int e = (call); if (e) { \
    fprintf(stderr, "%s failed: %d\n", #call, e); goto cleanup; } } while (0)

int main(int argc, char **argv) {
    if (argc != 3) { fprintf(stderr, "usage: bpu_smoke model.bin image-224x224.nv12\n"); return 2; }
    int result = 1, in_alloc = 0, out_alloc = 0;
    hbPackedDNNHandle_t packed = NULL;
    hbDNNHandle_t model = NULL;
    hbDNNTaskHandle_t task = NULL;
    hbDNNTensor input = {0}, output = {0}, *outputs = &output;
    const char **names = NULL, *file = argv[1];
    int count = 0;
    FILE *image = NULL;
    struct timespec begin, end;
    CHECK(hbDNNInitializeFromFiles(&packed, &file, 1));
    CHECK(hbDNNGetModelNameList(&names, &count, packed));
    if (count != 1) { fprintf(stderr, "expected one model\n"); goto cleanup; }
    CHECK(hbDNNGetModelHandle(&model, packed, names[0]));
    CHECK(hbDNNGetInputCount(&count, model));
    if (count != 1) goto cleanup;
    CHECK(hbDNNGetOutputCount(&count, model));
    if (count != 1) goto cleanup;
    CHECK(hbDNNGetInputTensorProperties(&input.properties, model, 0));
    CHECK(hbDNNGetOutputTensorProperties(&output.properties, model, 0));
    fprintf(stderr, "input_type=%d input_bytes=%d output_type=%d output_bytes=%d\n",
        input.properties.tensorType, input.properties.alignedByteSize,
        output.properties.tensorType, output.properties.alignedByteSize);
    if (input.properties.tensorType != HB_DNN_IMG_TYPE_NV12 ||
        input.properties.alignedByteSize != 224 * 224 * 3 / 2 ||
        output.properties.tensorType != HB_DNN_TENSOR_TYPE_F32 ||
        output.properties.alignedByteSize < 1000 * (int)sizeof(float)) {
        fprintf(stderr, "unexpected model tensor contract\n"); goto cleanup;
    }
    CHECK(hbSysAllocCachedMem(&input.sysMem[0], input.properties.alignedByteSize)); in_alloc = 1;
    CHECK(hbSysAllocCachedMem(&output.sysMem[0], output.properties.alignedByteSize)); out_alloc = 1;
    image = fopen(argv[2], "rb");
    if (!image) { perror("input image"); goto cleanup; }
    if (fread(input.sysMem[0].virAddr, 1, input.properties.alignedByteSize, image) !=
        (size_t)input.properties.alignedByteSize || fgetc(image) != EOF) {
        fprintf(stderr, "incorrect NV12 byte length\n"); goto cleanup;
    }
    CHECK(hbSysFlushMem(&input.sysMem[0], HB_SYS_MEM_CACHE_CLEAN));
    hbDNNInferCtrlParam ctrl;
    HB_DNN_INITIALIZE_INFER_CTRL_PARAM(&ctrl);
    clock_gettime(CLOCK_MONOTONIC, &begin);
    CHECK(hbDNNInfer(&task, &outputs, &input, model, &ctrl));
    CHECK(hbDNNWaitTaskDone(task, 5000));
    clock_gettime(CLOCK_MONOTONIC, &end);
    CHECK(hbSysFlushMem(&output.sysMem[0], HB_SYS_MEM_CACHE_INVALIDATE));
    float *values = (float *)output.sysMem[0].virAddr;
    int best = 0;
    for (int i = 1; i < 1000; ++i) if (values[i] > values[best]) best = i;
    printf("{\"backend\":\"x5-bpu\",\"class_index\":%d,\"score\":%.8f,\"latency_ms\":%.3f}\n",
        best, values[best], (end.tv_sec - begin.tv_sec) * 1000.0 + (end.tv_nsec - begin.tv_nsec) / 1e6);
    result = 0;
cleanup:
    if (task) hbDNNReleaseTask(task);
    if (image) fclose(image);
    if (out_alloc) hbSysFreeMem(&output.sysMem[0]);
    if (in_alloc) hbSysFreeMem(&input.sysMem[0]);
    if (packed) hbDNNRelease(packed);
    return result;
}
