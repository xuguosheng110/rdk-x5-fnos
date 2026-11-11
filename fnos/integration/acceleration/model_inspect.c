/* SPDX-License-Identifier: MIT */
#include <stdio.h>
#include "dnn/hb_dnn.h"

static void shape(const hbDNNTensorShape *s) {
    putchar('[');
    for (int i = 0; i < s->numDimensions; i++) printf("%s%d", i ? "," : "", s->dimensionSize[i]);
    putchar(']');
}
int main(int argc, char **argv) {
    if (argc != 2) return 2;
    hbPackedDNNHandle_t packed = NULL;
    hbDNNHandle_t model = NULL;
    const char **names = NULL, *file = argv[1];
    int n = 0, rc = 1;
    if (hbDNNInitializeFromFiles(&packed, &file, 1)) return 1;
    if (hbDNNGetModelNameList(&names, &n, packed) || n != 1 ||
        hbDNNGetModelHandle(&model, packed, names[0])) goto done;
    printf("model=%s\n", names[0]);
    for (int input = 1; input >= 0; input--) {
        if (input ? hbDNNGetInputCount(&n, model) : hbDNNGetOutputCount(&n, model)) goto done;
        for (int i = 0; i < n; i++) {
            hbDNNTensorProperties p;
            if (input ? hbDNNGetInputTensorProperties(&p, model, i) :
                        hbDNNGetOutputTensorProperties(&p, model, i)) goto done;
            printf("%s[%d] type=%d layout=%d quant=%d bytes=%d valid=",
                input ? "input" : "output", i, p.tensorType, p.tensorLayout, p.quantiType, p.alignedByteSize);
            shape(&p.validShape); printf(" aligned="); shape(&p.alignedShape); putchar('\n');
        }
    }
    rc = 0;
done:
    hbDNNRelease(packed);
    return rc;
}
