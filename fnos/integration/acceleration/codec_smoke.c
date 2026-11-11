/* Hardware encoder probe, using the official X5 MediaCodec buffer API.
 * This validates the driver/runtime, not fnOS media application integration.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "hb_media_codec.h"

#define CHECK(call) do { int e = (call); if (e) { \
    fprintf(stderr, "%s failed: %d\n", #call, e); goto cleanup; } } while (0)

int main(int argc, char **argv) {
    if (argc != 3 || (strcmp(argv[1], "h264") && strcmp(argv[1], "h265"))) {
        fprintf(stderr, "usage: codec_smoke h264|h265 output.es\n"); return 2;
    }
    media_codec_context_t ctx = {0};
    mc_av_codec_startup_params_t startup = {0};
    int initialized = 0, started = 0, result = 1, frames = 0;
    FILE *out = NULL;
    const int width = 640, height = 480;
    ctx.encoder = 1;
    ctx.codec_id = strcmp(argv[1], "h264") ? MEDIA_CODEC_ID_H265 : MEDIA_CODEC_ID_H264;
    mc_video_codec_enc_params_t *p = &ctx.video_enc_params;
    p->width = width; p->height = height; p->pix_fmt = MC_PIXEL_FORMAT_NV12;
    p->bitstream_buf_size = width * height * 3 / 2;
    p->frame_buf_count = 3; p->bitstream_buf_count = 3;
    p->gop_params.gop_preset_idx = 1;
    p->gop_params.decoding_refresh_type = 2;
    p->rc_params.mode = ctx.codec_id == MEDIA_CODEC_ID_H264 ? MC_AV_RC_MODE_H264FIXQP : MC_AV_RC_MODE_H265FIXQP;
    CHECK(hb_mm_mc_get_rate_control_config(&ctx, &p->rc_params));
    if (ctx.codec_id == MEDIA_CODEC_ID_H264) {
        p->rc_params.h264_fixqp_params.force_qp_I = 23;
        p->rc_params.h264_fixqp_params.force_qp_P = 23;
        p->rc_params.h264_fixqp_params.force_qp_B = 23;
        p->rc_params.h264_fixqp_params.intra_period = 30;
    } else {
        p->rc_params.h265_fixqp_params.force_qp_I = 23;
        p->rc_params.h265_fixqp_params.force_qp_P = 23;
        p->rc_params.h265_fixqp_params.force_qp_B = 23;
        p->rc_params.h265_fixqp_params.intra_period = 30;
    }
    CHECK(hb_mm_mc_initialize(&ctx)); initialized = 1;
    CHECK(hb_mm_mc_configure(&ctx));
    CHECK(hb_mm_mc_start(&ctx, &startup)); started = 1;
    out = fopen(argv[2], "wb");
    if (!out) { perror("output"); goto cleanup; }
    for (int frame = 0; frame < 30; ++frame) {
        media_codec_buffer_t in = {0}, encoded = {0};
        media_codec_output_buffer_info_t info = {0};
        CHECK(hb_mm_mc_dequeue_input_buffer(&ctx, &in, 3000));
        in.type = MC_VIDEO_FRAME_BUFFER;
        in.vframe_buf.width = width; in.vframe_buf.height = height;
        in.vframe_buf.pix_fmt = MC_PIXEL_FORMAT_NV12;
        in.vframe_buf.size = width * height * 3 / 2;
        if (!in.vframe_buf.vir_ptr[0] || !in.vframe_buf.vir_ptr[1]) goto cleanup;
        /* Visible moving gradient; decoded frames can be independently checked. */
        unsigned char *y = in.vframe_buf.vir_ptr[0];
        for (int row = 0; row < height; ++row)
            for (int col = 0; col < width; ++col)
                y[row * width + col] = 16 + ((col + row + frame * 7) % 220);
        memset(in.vframe_buf.vir_ptr[1], 128, width * height / 2);
        CHECK(hb_mm_mc_queue_input_buffer(&ctx, &in, 3000));
        CHECK(hb_mm_mc_dequeue_output_buffer(&ctx, &encoded, &info, 3000));
        size_t n = encoded.vstream_buf.size;
        int written = n && fwrite(encoded.vstream_buf.vir_ptr, 1, n, out) == n;
        CHECK(hb_mm_mc_queue_output_buffer(&ctx, &encoded, 3000));
        if (!written) { fprintf(stderr, "empty output or write failure\n"); goto cleanup; }
        ++frames;
    }
    result = 0;
cleanup:
    if (out && fclose(out)) result = 1;
    if (started && hb_mm_mc_pause(&ctx)) result = 1;
    if (initialized && hb_mm_mc_release(&ctx)) result = 1;
    if (!result) printf("{\"backend\":\"x5-vpu\",\"codec\":\"%s\",\"frames\":%d}\n", argv[1], frames);
    return result;
}
