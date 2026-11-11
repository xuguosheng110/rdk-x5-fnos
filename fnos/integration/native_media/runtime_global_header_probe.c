/* Diagnostic only. Caller holds board lock. No frames, services or user files.
 * Tests whether the documented independent-header mode emits VPS/SPS/PPS at
 * startup, so global-header containers can receive extradata before frame 1. */
#include <stdio.h>
#include <string.h>
#include <hb_media_codec.h>

int main(void)
{
    media_codec_context_t mc = {0};
    mc_av_codec_startup_params_t startup = {0};
    media_codec_buffer_t out = {0};
    media_codec_output_buffer_info_t info = {0};
    int ret, initialized = 0, started = 0, result = 1;
    mc.encoder = 1;
    mc.codec_id = MEDIA_CODEC_ID_H265;
    mc_video_codec_enc_params_t *p = &mc.video_enc_params;
    p->width = 640; p->height = 480; p->pix_fmt = MC_PIXEL_FORMAT_NV12;
    p->frame_buf_count = 3; p->bitstream_buf_count = 3;
    p->bitstream_buf_size = 640 * 480 * 3 / 2;
    p->gop_params.gop_preset_idx = 9;
    p->gop_params.decoding_refresh_type = 2;
    p->enable_user_pts = 1;
    p->rc_params.mode = MC_AV_RC_MODE_H265CBR;
    ret = hb_mm_mc_get_rate_control_config(&mc, &p->rc_params);
    printf("rate_defaults=%d\n", ret); if (ret) goto done;
    p->rc_params.h265_cbr_params.frame_rate = 25;
    p->rc_params.h265_cbr_params.bit_rate = 2000;
    p->rc_params.h265_cbr_params.intra_period = 25;
    ret = hb_mm_mc_initialize(&mc);
    printf("initialize=%d\n", ret); if (ret) goto done;
    initialized = 1;
    ret = hb_mm_mc_set_explicit_header_config(&mc, 0);
    printf("independent_headers=%d\n", ret); if (ret) goto done;
    ret = hb_mm_mc_configure(&mc);
    printf("configure=%d\n", ret); if (ret) goto done;
    ret = hb_mm_mc_start(&mc, &startup);
    printf("start=%d\n", ret); if (ret) goto done;
    started = 1;
    ret = hb_mm_mc_dequeue_output_buffer(&mc, &out, &info, 1500);
    printf("dequeue_without_frames=%d bytes=%u\n", ret, out.vstream_buf.size);
    if (!ret) {
        printf("header_prefix=");
        for (unsigned i = 0; i < out.vstream_buf.size && i < 96; ++i)
            printf("%02x", out.vstream_buf.vir_ptr[i]);
        printf("\n");
        ret = hb_mm_mc_queue_output_buffer(&mc, &out, 1500);
        printf("return_buffer=%d\n", ret);
        if (ret) goto done;
        ret = hb_mm_mc_set_explicit_header_config(&mc, 1);
        printf("restore_inline_headers_while_started=%d\n", ret);
        result = ret != 0;
    }
done:
    if (started) hb_mm_mc_pause(&mc);
    if (initialized) hb_mm_mc_release(&mc);
    return result;
}
