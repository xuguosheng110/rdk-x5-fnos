/* SPDX-License-Identifier: LGPL-2.1-or-later
 * FFmpeg encoder for the X5 MediaCodec API. Built into pinned FFmpeg source.
 * Buffers remain owned by MediaCodec until copied into an AVPacket.
 */
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <hb_media_codec.h>
#include "libavutil/avassert.h"
#include "libavutil/imgutils.h"
#include "libavutil/opt.h"
#include "avcodec.h"
#include "bsf.h"
#include "codec_internal.h"
#include "encode.h"

typedef struct HobotEncoder {
    const AVClass *class;
    media_codec_context_t mc;
    int initialized, started, h264_profile_verified;
    int hevc_profile_contract, hevc_headers_verified, hevc_vps_level, hevc_sps_level;
    int64_t frames;
    char *preset;
    char *x264opts, *x265params;
    double crf;
    AVBSFContext *metadata;
} HobotEncoder;

static int hobot_encode(AVCodecContext *avctx, AVPacket *pkt, const AVFrame *frame, int *got_packet);

static int mc_error(AVCodecContext *avctx, const char *operation, int code)
{
    av_log(avctx, AV_LOG_ERROR, "X5 %s failed: %d\n", operation, code);
    return AVERROR_EXTERNAL;
}

static av_cold int hobot_close(AVCodecContext *avctx)
{
    HobotEncoder *s = avctx->priv_data;
    if (s->started) {
        hb_mm_mc_pause(&s->mc);
        s->started = 0;
    }
    if (s->initialized) {
        hb_mm_mc_release(&s->mc);
        s->initialized = 0;
    }
    av_bsf_free(&s->metadata);
    return 0;
}

/* Container tags alone do not survive HLS/TS or an elementary-stream remux.
 * Write aspect ratio and colour description into the encoded SPS/VUI itself.
 * This describes the submitted pixels; it does not perform HDR tone mapping. */
static int hobot_set_vui(AVCodecContext *avctx, AVRational rate)
{
    HobotEncoder *s = avctx->priv_data;
    mc_video_vui_params_t vui = {0};
    int ret, sar_num = 0, sar_den = 1, rate_num, rate_den;
    const int colour = avctx->color_primaries != AVCOL_PRI_UNSPECIFIED ||
                       avctx->color_trc != AVCOL_TRC_UNSPECIFIED ||
                       avctx->colorspace != AVCOL_SPC_UNSPECIFIED;
    ret = hb_mm_mc_get_vui_config(&s->mc, &vui);
    if (ret) return mc_error(avctx, "VUI defaults", ret);
    if (avctx->sample_aspect_ratio.num > 0 && avctx->sample_aspect_ratio.den > 0)
        av_reduce(&sar_num, &sar_den, avctx->sample_aspect_ratio.num,
                  avctx->sample_aspect_ratio.den, UINT16_MAX);
    av_reduce(&rate_num, &rate_den, rate.num, rate.den, INT_MAX / 2);
#define SET_COMMON_VUI(v) do { \
    (v).aspect_ratio_info_present_flag = sar_num > 0; \
    (v).aspect_ratio_idc = 255; /* Extended_SAR */ \
    (v).sar_width = sar_num > 0 ? sar_num : 1; \
    (v).sar_height = sar_den; \
    (v).video_signal_type_present_flag = colour || avctx->color_range != AVCOL_RANGE_UNSPECIFIED; \
    (v).video_format = 5; /* unspecified video format */ \
    (v).video_full_range_flag = avctx->color_range == AVCOL_RANGE_JPEG; \
    (v).colour_description_present_flag = colour; \
    (v).colour_primaries = avctx->color_primaries; \
    (v).transfer_characteristics = avctx->color_trc; \
    (v).matrix_coefficients = avctx->colorspace; \
    (v).vui_timing_info_present_flag = 1; \
    (v).vui_num_units_in_tick = rate_den; \
} while (0)
    if (avctx->codec_id == AV_CODEC_ID_HEVC) {
        SET_COMMON_VUI(vui.h265_vui);
        vui.h265_vui.vui_time_scale = rate_num;
        vui.h265_vui.vui_poc_proportional_to_timing_flag = 1;
        vui.h265_vui.vui_num_ticks_poc_diff_one_minus1 = 0;
    } else {
        SET_COMMON_VUI(vui.h264_vui);
        vui.h264_vui.vui_time_scale = 2 * rate_num;
        vui.h264_vui.vui_fixed_frame_rate_flag = avctx->framerate.num > 0;
    }
#undef SET_COMMON_VUI
    ret = hb_mm_mc_set_vui_config(&s->mc, &vui);
    return ret ? mc_error(avctx, "set VUI", ret) : 0;
}

/* SDK H265 VUI timing accepts fractions, but its VPS stays at the integer CBR
 * frame rate (confirmed from a true-board r18 stream). Normalise both parameter
 * sets with the pinned native metadata BSF; encoded picture data is unchanged. */
static int hobot_init_metadata(AVCodecContext *avctx, AVRational rate)
{
    HobotEncoder *s = avctx->priv_data;
    const AVBitStreamFilter *filter;
    int ret;
    if (avctx->codec_id != AV_CODEC_ID_HEVC) return 0;
    filter = av_bsf_get_by_name("hevc_metadata");
    if (!filter) return AVERROR_BSF_NOT_FOUND;
    ret = av_bsf_alloc(filter, &s->metadata);
    if (ret < 0) return ret;
    ret = avcodec_parameters_from_context(s->metadata->par_in, avctx);
    if (ret < 0) return ret;
    s->metadata->time_base_in = avctx->time_base;
    ret = av_opt_set_q(s->metadata->priv_data, "tick_rate", rate, 0);
    if (ret < 0) return ret;
    ret = av_opt_set_int(s->metadata->priv_data, "num_ticks_poc_diff_one", 1, 0);
    if (ret < 0) return ret;
    return av_bsf_init(s->metadata);
}

/* MediaCodec does not expose parameter sets until a frame has been encoded,
 * even with independent-header mode. Global-header muxers need them at open.
 * Encode one owned black frame, retain only extracted parameter sets, then force
 * a fresh IDR. No caller frame is consumed and no bootstrap packet is returned.
 * The native TS path (no GLOBAL_HEADER flag) does not enter this function. */
static int hobot_global_header(AVCodecContext *avctx)
{
    HobotEncoder *s = avctx->priv_data;
    const AVBitStreamFilter *filter = av_bsf_get_by_name("extract_extradata");
    AVBSFContext *extract = NULL;
    AVFrame *frame = NULL;
    AVPacket *packet = NULL;
    size_t size = 0;
    uint8_t *header;
    int ret, got = 0;
    int64_t public_frames = s->frames;
    if (!filter) return AVERROR_BSF_NOT_FOUND;
    ret = av_bsf_alloc(filter, &extract);
    if (ret < 0) goto done;
    ret = avcodec_parameters_from_context(extract->par_in, avctx);
    if (ret < 0) goto done;
    extract->time_base_in = avctx->time_base;
    ret = av_bsf_init(extract);
    if (ret < 0) goto done;
    frame = av_frame_alloc();
    packet = av_packet_alloc();
    if (!frame || !packet) { ret = AVERROR(ENOMEM); goto done; }
    frame->format = avctx->pix_fmt;
    frame->width = avctx->width;
    frame->height = avctx->height;
    frame->pts = 0;
    frame->pict_type = AV_PICTURE_TYPE_I;
    ret = av_frame_get_buffer(frame, 32);
    if (ret < 0) goto done;
    for (int y = 0; y < frame->height; ++y)
        memset(frame->data[0] + y * frame->linesize[0],
               avctx->color_range == AVCOL_RANGE_JPEG ? 0 : 16, frame->width);
    for (int y = 0; y < frame->height / 2; ++y) {
        if (frame->format == AV_PIX_FMT_NV12) {
            memset(frame->data[1] + y * frame->linesize[1], 128, frame->width);
        } else {
            memset(frame->data[1] + y * frame->linesize[1], 128, frame->width / 2);
            memset(frame->data[2] + y * frame->linesize[2], 128, frame->width / 2);
        }
    }
    ret = hobot_encode(avctx, packet, frame, &got);
    if (ret < 0) goto done;
    if (!got) { ret = AVERROR_INVALIDDATA; goto done; }
    ret = av_bsf_send_packet(extract, packet);
    if (ret < 0) goto done;
    ret = av_bsf_receive_packet(extract, packet);
    if (ret < 0) goto done;
    header = av_packet_get_side_data(packet, AV_PKT_DATA_NEW_EXTRADATA, &size);
    if (!header || !size || size > INT_MAX - AV_INPUT_BUFFER_PADDING_SIZE) {
        ret = AVERROR_INVALIDDATA;
        goto done;
    }
    av_freep(&avctx->extradata);
    avctx->extradata_size = 0;
    avctx->extradata = av_mallocz(size + AV_INPUT_BUFFER_PADDING_SIZE);
    if (!avctx->extradata) { ret = AVERROR(ENOMEM); goto done; }
    memcpy(avctx->extradata, header, size);
    avctx->extradata_size = size;
    /* The next actual user frame must not reference the discarded warm-up. */
    ret = hb_mm_mc_request_idr_frame(&s->mc);
    if (ret) ret = mc_error(avctx, "IDR after global header extraction", ret);
    else av_log(avctx, AV_LOG_VERBOSE, "X5 global headers prepared: %zu bytes; bootstrap image discarded\n", size);
done:
    s->frames = public_frames;
    if (s->metadata) av_bsf_flush(s->metadata);
    av_frame_free(&frame);
    av_packet_free(&packet);
    av_bsf_free(&extract);
    return ret;
}

static av_cold int hobot_init(AVCodecContext *avctx)
{
    HobotEncoder *s = avctx->priv_data;
    mc_video_codec_enc_params_t *p = &s->mc.video_enc_params;
    mc_av_codec_startup_params_t startup = {0};
    AVRational rate = avctx->framerate.num > 0 && avctx->framerate.den > 0 ?
                      avctx->framerate : av_inv_q(avctx->time_base);
    int ret, fps, gop;
    int64_t bitrate = avctx->bit_rate > 0 ? avctx->bit_rate : 4000000;
    s->hevc_profile_contract = avctx->codec_id == AV_CODEC_ID_HEVC &&
                              avctx->profile == AV_PROFILE_HEVC_MAIN && avctx->level == 51;
    /* These pinned native-app options only tune CPU search speed / SEI metadata.
     * Do not silently accept arbitrary x264/x265 bitstream requests. */
    if ((s->x264opts && strcmp(s->x264opts,
          "subme=0:me_range=4:rc_lookahead=10:me=dia:no_chroma_me:8x8dct=0:partitions=none")) ||
        (s->x265params && strcmp(s->x265params, "no-info=1"))) {
        av_log(avctx, AV_LOG_ERROR, "Unsupported software-specific codec option\n");
        return AVERROR(EINVAL);
    }
    if (avctx->width < 64 || avctx->height < 64 ||
        avctx->width > 4096 || avctx->height > 4096 ||
        (avctx->width & 1) || (avctx->height & 1)) {
        av_log(avctx, AV_LOG_ERROR, "X5 encoder requires even dimensions in 64..4096\n");
        return AVERROR(EINVAL);
    }
    if (bitrate > 700000000) return AVERROR(EINVAL);
    if (rate.num <= 0 || rate.den <= 0 || av_q2d(rate) < 1 || av_q2d(rate) > 240)
        return AVERROR(EINVAL);
    fps = av_q2d(rate) + 0.5;
    if (fps < 1 || fps > 240) return AVERROR(EINVAL);
    ret = hobot_init_metadata(avctx, rate);
    if (ret < 0) return ret;
    gop = avctx->gop_size > 0 ? avctx->gop_size : fps * 2;
    if (gop > 2047) return AVERROR(EINVAL);
    s->mc.encoder = 1;
    s->mc.codec_id = avctx->codec_id == AV_CODEC_ID_HEVC ? MEDIA_CODEC_ID_H265 : MEDIA_CODEC_ID_H264;
    /* SPS cropping is relative to coded macroblocks, not 8-pixel API alignment. */
    p->width = FFALIGN(avctx->width, 16); p->height = FFALIGN(avctx->height, 16);
    if (p->width != avctx->width || p->height != avctx->height) {
        p->frame_cropping_flag = 1;
        p->crop_rect.width = avctx->width;
        p->crop_rect.height = avctx->height;
    }
    p->pix_fmt = MC_PIXEL_FORMAT_NV12;
    if (avctx->codec_id == AV_CODEC_ID_H264 &&
        avctx->profile == AV_PROFILE_H264_HIGH && avctx->level == 50) {
        /* SDK h264_profile is explicitly marked unsupported. Its reviewed
         * default is High for 8-bit; set only the supported level register and
         * verify both real SPS fields before returning encoded output. */
        p->h264_enc_config.h264_level = MC_H264_LEVEL5;
    }
    if (s->hevc_profile_contract) {
        /* The native CPU encoder ignores ctx.level=51 and chose Level 3 on
         * the observed 848x476 input. Preserve automatic level selection and
         * verify Main/Main-tier plus a legal level <= 5.1 in both VPS/SPS. */
        p->h265_enc_config.main_still_picture_profile_enable = 0;
        p->h265_enc_config.h265_level = MC_H265_LEVEL_UNSPECIFIED;
        p->h265_enc_config.h265_tier = 0;
    }
    p->bitstream_buf_size = FFALIGN(p->width * p->height * 3 / 2, 1024);
    p->frame_buf_count = 3; p->bitstream_buf_count = 3;
    /* X5 wave521cl supports presets 1 and 9 only: no multi-reference or B frames.
     * See pinned x5-multimedia-samples/sample_codec/sample_codec.c. */
    p->gop_params.gop_preset_idx = 9; /* IP with one reference, no reordering. */
    p->gop_params.decoding_refresh_type = 2;
    p->enable_user_pts = 1;
    p->rc_params.mode = avctx->codec_id == AV_CODEC_ID_HEVC ? MC_AV_RC_MODE_H265CBR : MC_AV_RC_MODE_H264CBR;
    ret = hb_mm_mc_get_rate_control_config(&s->mc, &p->rc_params);
    if (ret) return mc_error(avctx, "rate-control defaults", ret);
    if (avctx->codec_id == AV_CODEC_ID_HEVC) {
        p->rc_params.h265_cbr_params.intra_period = gop;
        p->rc_params.h265_cbr_params.bit_rate = FFMAX(1, bitrate / 1000);
        p->rc_params.h265_cbr_params.frame_rate = fps;
        if (s->crf >= 0) p->rc_params.h265_cbr_params.intra_qp = s->crf + 0.5;
    } else {
        p->rc_params.h264_cbr_params.intra_period = gop;
        p->rc_params.h264_cbr_params.bit_rate = FFMAX(1, bitrate / 1000);
        p->rc_params.h264_cbr_params.frame_rate = fps;
        if (s->crf >= 0) p->rc_params.h264_cbr_params.intra_qp = s->crf + 0.5;
    }
    ret = hb_mm_mc_initialize(&s->mc);
    if (ret) return mc_error(avctx, "initialize", ret);
    s->initialized = 1;
    ret = hobot_set_vui(avctx, rate);
    if (ret) return ret;
    ret = hb_mm_mc_configure(&s->mc);
    if (ret) return mc_error(avctx, "configure", ret);
    ret = hb_mm_mc_start(&s->mc, &startup);
    if (ret) return mc_error(avctx, "start", ret);
    s->started = 1;
    if (avctx->flags & AV_CODEC_FLAG_GLOBAL_HEADER) {
        ret = hobot_global_header(avctx);
        if (ret < 0) return ret;
    }
    if (s->preset)
        av_log(avctx, AV_LOG_VERBOSE, "Software preset %s has no effect on fixed-function X5 VPU\n", s->preset);
    avctx->has_b_frames = 0;
    avctx->max_b_frames = 0;
    av_log(avctx, AV_LOG_INFO, "X5 VPU encoder active: %dx%d %d fps, %" PRId64 " bit/s\n", p->width, p->height, fps, bitrate);
    av_log(avctx, AV_LOG_VERBOSE, "X5 VUI input: SAR %d/%d range %d primaries %d transfer %d matrix %d rate %d/%d\n",
           avctx->sample_aspect_ratio.num, avctx->sample_aspect_ratio.den, avctx->color_range,
           avctx->color_primaries, avctx->color_trc, avctx->colorspace, rate.num, rate.den);
    return 0;
}

static int is_key(const uint8_t *data, int size, enum AVCodecID codec)
{
    for (int i = 0; i + 4 < size; i++) {
        if (!data[i] && !data[i+1] && data[i+2] == 1) {
            int type = codec == AV_CODEC_ID_HEVC ? (data[i+3] >> 1) & 63 : data[i+3] & 31;
            if (codec == AV_CODEC_ID_HEVC ? type >= 16 && type <= 21 : type == 5) return 1;
        }
    }
    return 0;
}

static int verify_native_h264_profile(AVCodecContext *avctx, const AVPacket *pkt)
{
    HobotEncoder *s = avctx->priv_data;
    if (avctx->codec_id != AV_CODEC_ID_H264 ||
        avctx->profile != AV_PROFILE_H264_HIGH || avctx->level != 50)
        return 0;
    for (int i = 0; i + 6 < pkt->size; i++) {
        const uint8_t *p = pkt->data + i;
        if (!p[0] && !p[1] && p[2] == 1 && (p[3] & 31) == 7) {
            if (p[4] != 100 || p[6] != 50) {
                av_log(avctx, AV_LOG_ERROR, "X5 SPS profile/level mismatch: %u/%u, required 100/50\n", p[4], p[6]);
                return AVERROR_INVALIDDATA;
            }
            s->h264_profile_verified = 1;
        }
    }
    if (!s->h264_profile_verified) {
        av_log(avctx, AV_LOG_ERROR, "X5 first H264 output did not prove the requested SPS profile/level\n");
        return AVERROR_INVALIDDATA;
    }
    return 0;
}

static int hevc_level_within_51(int level)
{
    switch (level) {
    case 30: case 60: case 63: case 90: case 93:
    case 120: case 123: case 150: case 153:
        return 1;
    default:
        return 0;
    }
}

static int verify_native_hevc_profile(AVCodecContext *avctx, const AVPacket *pkt)
{
    HobotEncoder *s = avctx->priv_data;
    int prior = s->hevc_headers_verified;
    if (!s->hevc_profile_contract) return 0;
    for (int i = 0; i + 4 < pkt->size; i++) {
        const uint8_t *p = pkt->data + i;
        if (p[0] || p[1] || p[2] != 1) continue;
        int type = (p[3] >> 1) & 63;
        if (type != 32 && type != 33) continue;
        int end = i + 5;
        while (end + 2 < pkt->size &&
               (pkt->data[end] || pkt->data[end+1] || pkt->data[end+2] != 1)) end++;
        if (end + 2 >= pkt->size) end = pkt->size;
        uint8_t rbsp[16];
        unsigned used = 0, zeros = 0;
        for (int j = i + 5; j < end && used < sizeof(rbsp); j++) {
            uint8_t value = pkt->data[j];
            if (zeros >= 2 && value == 3) { zeros = 0; continue; }
            rbsp[used++] = value;
            zeros = value ? 0 : zeros + 1;
        }
        /* VPS has 32 bits before profile_tier_level; SPS has eight. General
         * profile/tier is its first byte, general_level_idc its twelfth. */
        unsigned offset = type == 32 ? 4 : 1;
        if (used < offset + 12 || (rbsp[offset] & 31) != 1 ||
            (rbsp[offset] & 32) || !hevc_level_within_51(rbsp[offset + 11])) {
            av_log(avctx, AV_LOG_ERROR, "X5 HEVC parameter set %d violates Main/Main-tier/Level<=5.1\n", type);
            return AVERROR_INVALIDDATA;
        }
        if (type == 32) {
            s->hevc_vps_level = rbsp[offset + 11];
            s->hevc_headers_verified |= 1;
        } else {
            s->hevc_sps_level = rbsp[offset + 11];
            s->hevc_headers_verified |= 2;
        }
    }
    if (s->hevc_headers_verified != 3 || s->hevc_vps_level != s->hevc_sps_level) {
        av_log(avctx, AV_LOG_ERROR, "X5 HEVC output did not prove matching reviewed VPS/SPS levels\n");
        return AVERROR_INVALIDDATA;
    }
    if (prior != 3) {
        const char *debug = getenv("FNOS_X5_VPU_DIAGNOSTICS");
        if (debug && !strcmp(debug, "1"))
            fprintf(stderr, "X5 HEVC bitstream verified: Main, Main tier, VPS/SPS general_level_idc=%d; native ctx.level=51 is not a level_idc\n",
                    s->hevc_sps_level);
    }
    return 0;
}

static int hobot_encode(AVCodecContext *avctx, AVPacket *pkt, const AVFrame *frame, int *got_packet)
{
    HobotEncoder *s = avctx->priv_data;
    media_codec_buffer_t in = {0}, out = {0};
    media_codec_output_buffer_info_t info = {0};
    mc_video_frame_buffer_info_t *v = &in.vframe_buf;
    int ret, width = avctx->width, height = avctx->height;
    *got_packet = 0;
    if (!frame) return 0; /* synchronous IP encoder has no delayed frames */
    if (frame->pict_type == AV_PICTURE_TYPE_I) {
        ret = hb_mm_mc_request_idr_frame(&s->mc);
        if (ret) return mc_error(avctx, "force IDR", ret);
    }
    ret = hb_mm_mc_dequeue_input_buffer(&s->mc, &in, 3000);
    if (ret) return mc_error(avctx, "dequeue input", ret);
    in.type = MC_VIDEO_FRAME_BUFFER;
    int stride = s->mc.video_enc_params.width;
    int padded_height = s->mc.video_enc_params.height;
    v->width = stride; v->height = padded_height; v->pix_fmt = MC_PIXEL_FORMAT_NV12;
    v->size = stride * padded_height * 3 / 2;
    v->pts = frame->pts;
    if (!v->vir_ptr[0] || !v->vir_ptr[1]) return AVERROR_EXTERNAL;
    /* Pad to the hardware geometry; signal the visible rectangle in SPS. */
    memset(v->vir_ptr[0], 16, stride * padded_height);
    memset(v->vir_ptr[1], 128, stride * padded_height / 2);
    for (int y = 0; y < height; y++)
        memcpy(v->vir_ptr[0] + y * stride, frame->data[0] + y * frame->linesize[0], width);
    for (int y = 0; y < height / 2; y++) {
        uint8_t *uv = v->vir_ptr[1] + y * stride;
        if (frame->format == AV_PIX_FMT_NV12)
            memcpy(uv, frame->data[1] + y * frame->linesize[1], width);
        else
            for (int x = 0; x < width / 2; x++) {
                uv[2*x] = frame->data[1][y * frame->linesize[1] + x];
                uv[2*x+1] = frame->data[2][y * frame->linesize[2] + x];
            }
    }
    ret = hb_mm_mc_queue_input_buffer(&s->mc, &in, 3000);
    if (ret) return mc_error(avctx, "queue input", ret);
    ret = hb_mm_mc_dequeue_output_buffer(&s->mc, &out, &info, 3000);
    if (ret) return mc_error(avctx, "dequeue output", ret);
    if (!out.vstream_buf.vir_ptr || !out.vstream_buf.size || out.vstream_buf.size > INT_MAX)
        ret = AVERROR_INVALIDDATA;
    else {
        ret = av_new_packet(pkt, out.vstream_buf.size);
        if (ret >= 0) {
            memcpy(pkt->data, out.vstream_buf.vir_ptr, pkt->size);
            pkt->pts = pkt->dts = frame->pts;
            pkt->duration = frame->duration;
            if (is_key(pkt->data, pkt->size, avctx->codec_id)) pkt->flags |= AV_PKT_FLAG_KEY;
            *got_packet = 1;
            s->frames++;
        }
    }
    int queued = hb_mm_mc_queue_output_buffer(&s->mc, &out, 3000);
    if (queued) return mc_error(avctx, "return output", queued);
    if (ret >= 0 && *got_packet) {
        ret = verify_native_h264_profile(avctx, pkt);
        if (ret < 0) {
            av_packet_unref(pkt);
            *got_packet = 0;
            return ret;
        }
    }
    if (ret >= 0 && *got_packet && s->metadata) {
        ret = av_bsf_send_packet(s->metadata, pkt);
        if (ret >= 0) ret = av_bsf_receive_packet(s->metadata, pkt);
        if (ret < 0) {
            *got_packet = 0;
            av_log(avctx, AV_LOG_ERROR, "X5 encoded HEVC metadata normalisation failed: %d\n", ret);
        }
    }
    if (ret >= 0 && *got_packet) {
        ret = verify_native_hevc_profile(avctx, pkt);
        if (ret < 0) {
            av_packet_unref(pkt);
            *got_packet = 0;
        }
    }
    return ret;
}

static const char *hobot_item_name(void *unused)
{
    (void)unused;
    return "X5 VPU";
}

static const AVOption hobot_options[] = {
    {"preset", "Accept the application's software speed preset (VPU uses fixed-function encoding)",
     offsetof(HobotEncoder, preset), AV_OPT_TYPE_STRING, {.str = NULL}, 0, 0,
     AV_OPT_FLAG_VIDEO_PARAM | AV_OPT_FLAG_ENCODING_PARAM},
    {"crf", "Initial intra QP hint; X5 continues to enforce the application's target bitrate, not x264 CRF",
     offsetof(HobotEncoder, crf), AV_OPT_TYPE_DOUBLE, {.dbl = -1}, -1, 51,
     AV_OPT_FLAG_VIDEO_PARAM | AV_OPT_FLAG_ENCODING_PARAM},
    {"x264opts", "Compatibility for the pinned native application's CPU search-speed settings",
     offsetof(HobotEncoder, x264opts), AV_OPT_TYPE_STRING, {.str = NULL}, 0, 0,
     AV_OPT_FLAG_VIDEO_PARAM | AV_OPT_FLAG_ENCODING_PARAM},
    {"x265-params", "Compatibility for the pinned native application's no-info setting",
     offsetof(HobotEncoder, x265params), AV_OPT_TYPE_STRING, {.str = NULL}, 0, 0,
     AV_OPT_FLAG_VIDEO_PARAM | AV_OPT_FLAG_ENCODING_PARAM},
    {NULL}
};

static const AVClass hobot_class = {
    .class_name = "hobot encoder", .item_name = hobot_item_name,
    .version = LIBAVUTIL_VERSION_INT, .option = hobot_options,
};
static const FFCodecDefault hobot_defaults[] = {{"b", "4000000"}, {"g", "50"}, {"bf", "0"}, {NULL}};
#define HOBOT_ENCODER(NAME, ID) \
const FFCodec ff_##NAME##_hobot_encoder = { \
    .p.name = #NAME "_hobot", CODEC_LONG_NAME("D-Robotics X5 VPU " #NAME " encoder"), \
    .p.type = AVMEDIA_TYPE_VIDEO, .p.id = ID, \
    /* The no-delay encode callback path copies frame opaque data in encode.c. */ \
    .p.capabilities = AV_CODEC_CAP_ENCODER_REORDERED_OPAQUE, \
    .priv_data_size = sizeof(HobotEncoder), .init = hobot_init, \
    FF_CODEC_ENCODE_CB(hobot_encode), .close = hobot_close, \
    .caps_internal = FF_CODEC_CAP_INIT_CLEANUP, \
    CODEC_PIXFMTS(AV_PIX_FMT_NV12, AV_PIX_FMT_YUV420P), \
    .p.priv_class = &hobot_class, .defaults = hobot_defaults, .p.wrapper_name = "hobot", \
};
HOBOT_ENCODER(h264, AV_CODEC_ID_H264)
HOBOT_ENCODER(hevc, AV_CODEC_ID_HEVC)
