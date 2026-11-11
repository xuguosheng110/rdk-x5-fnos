/* Decode the bounded 640x480, 30-frame codec_smoke fixture through X5 VPU. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "hb_media_codec.h"
#define CHECK(call) do { int e = (call); if (e) { \
    fprintf(stderr, "%s failed: %d\n", #call, e); goto cleanup; } } while (0)

int main(int argc, char **argv) {
    if (argc != 4 || (strcmp(argv[1], "h264") && strcmp(argv[1], "h265"))) {
        fprintf(stderr, "usage: decode_smoke h264|h265 input.es output.nv12\n"); return 2;
    }
    media_codec_context_t ctx = {0};
    mc_av_codec_startup_params_t startup = {0};
    int initialized = 0, started = 0, result = 1, frames = 0;
    FILE *in = NULL, *out = NULL;
    ctx.codec_id = strcmp(argv[1], "h264") ? MEDIA_CODEC_ID_H265 : MEDIA_CODEC_ID_H264;
    mc_video_codec_dec_params_t *p = &ctx.video_dec_params;
    p->feed_mode = MC_FEEDING_MODE_STREAM_SIZE;
    p->pix_fmt = MC_PIXEL_FORMAT_NV12;
    p->bitstream_buf_size = 1024 * 1024;
    p->bitstream_buf_count = 3; p->frame_buf_count = 5;
    if (ctx.codec_id == MEDIA_CODEC_ID_H264) {
        p->h264_dec_config.bandwidth_Opt = 1;
        p->h264_dec_config.reorder_enable = 1;
    } else {
        p->h265_dec_config.bandwidth_Opt = 1;
        p->h265_dec_config.reorder_enable = 1;
    }
    in = fopen(argv[2], "rb"); out = fopen(argv[3], "wb");
    if (!in || !out) { perror("fixture file"); goto cleanup; }
    CHECK(hb_mm_mc_initialize(&ctx)); initialized = 1;
    CHECK(hb_mm_mc_configure(&ctx));
    CHECK(hb_mm_mc_start(&ctx, &startup)); started = 1;
    media_codec_buffer_t input = {0};
    CHECK(hb_mm_mc_dequeue_input_buffer(&ctx, &input, 3000));
    input.type = MC_VIDEO_STREAM_BUFFER;
    size_t n = fread(input.vstream_buf.vir_ptr, 1, input.vstream_buf.size, in);
    if (!n || ferror(in) || fgetc(in) != EOF) {
        fprintf(stderr, "fixture empty or exceeds one input buffer\n"); goto cleanup;
    }
    input.vstream_buf.size = n;
    CHECK(hb_mm_mc_queue_input_buffer(&ctx, &input, 3000));
    memset(&input, 0, sizeof(input));
    CHECK(hb_mm_mc_dequeue_input_buffer(&ctx, &input, 3000));
    input.type = MC_VIDEO_STREAM_BUFFER;
    input.vstream_buf.size = 0; input.vstream_buf.stream_end = 1;
    CHECK(hb_mm_mc_queue_input_buffer(&ctx, &input, 3000));
    while (frames < 30) {
        media_codec_buffer_t output = {0};
        media_codec_output_buffer_info_t info = {0};
        CHECK(hb_mm_mc_dequeue_output_buffer(&ctx, &output, &info, 5000));
        mc_video_frame_buffer_info_t *v = &output.vframe_buf;
        if (v->width != 640 || v->height != 480 || v->pix_fmt != MC_PIXEL_FORMAT_NV12 ||
            !v->vir_ptr[0] || !v->vir_ptr[1] || v->stride < 640 || v->vstride < 640) {
            fprintf(stderr, "unexpected decoded frame: %dx%d fmt=%d stride=%d,%d\n",
                    v->width, v->height, v->pix_fmt, v->stride, v->vstride);
            hb_mm_mc_queue_output_buffer(&ctx, &output, 3000); goto cleanup;
        }
        int written = 1;
        for (int row = 0; row < 480; ++row)
            if (fwrite(v->vir_ptr[0] + row * v->stride, 1, 640, out) != 640) written = 0;
        for (int row = 0; row < 240; ++row)
            if (fwrite(v->vir_ptr[1] + row * v->vstride, 1, 640, out) != 640) written = 0;
        CHECK(hb_mm_mc_queue_output_buffer(&ctx, &output, 3000));
        if (!written) goto cleanup;
        ++frames;
    }
    result = 0;
cleanup:
    if (in) fclose(in);
    if (out && fclose(out)) result = 1;
    if (started && hb_mm_mc_pause(&ctx)) result = 1;
    if (initialized && hb_mm_mc_release(&ctx)) result = 1;
    if (!result) printf("{\"backend\":\"x5-vpu\",\"decode\":\"%s\",\"frames\":%d}\n", argv[1], frames);
    return result;
}
