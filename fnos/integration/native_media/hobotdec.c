/* SPDX-License-Identifier: LGPL-2.1-or-later
 * X5 decoder for the pinned fnOS FFCodec ABI. No private FFmpeg symbols are
 * imported. Hardware buffers are copied and returned before the callback exits.
 */
#include <string.h>
#include <hb_media_codec.h>
#include <hb_media_error.h>
#include "libavutil/imgutils.h"
#include "libavutil/time.h"
#include "avcodec.h"
#include "codec_internal.h"

#define READY_MAX 32
#define TIMING_MAX 128
typedef struct PacketTiming {
    int64_t pts, duration;
    int key;
} PacketTiming;
typedef struct HobotDecoder {
    media_codec_context_t mc;
    int initialized, started, draining, eof, failed, first_picture;
    AVFrame *ready[READY_MAX];
    unsigned head, count;
    PacketTiming timing[TIMING_MAX];
    unsigned timing_count;
} HobotDecoder;

static int packet_has_picture(const AVPacket *packet, enum AVCodecID codec)
{
    for (int i = 0; i + 3 < packet->size; i++) {
        if (!packet->data[i] && !packet->data[i+1] && packet->data[i+2] == 1) {
            int type = codec == AV_CODEC_ID_HEVC ? (packet->data[i+3] >> 1) & 63 : packet->data[i+3] & 31;
            if (codec == AV_CODEC_ID_HEVC ? type <= 31 : type >= 1 && type <= 5) return 1;
        }
    }
    return 0;
}

/* X5 returns pictures in display order but its vframe pts remains in submission
 * order (verified with independent B-frame pixel hashes). Pick the earliest
 * pending presentation timestamp; each displayed picture has already been
 * submitted, including every reference needed to reorder it. */
static int set_frame_timing(HobotDecoder *s, AVFrame *frame)
{
    if (!s->timing_count) return AVERROR_INVALIDDATA;
    unsigned first = 0;
    for (unsigned i = 1; i < s->timing_count; i++) {
        if (s->timing[i].pts == AV_NOPTS_VALUE || s->timing[first].pts == AV_NOPTS_VALUE) break;
        if (s->timing[i].pts < s->timing[first].pts) first = i;
    }
    frame->pts = s->timing[first].pts;
    frame->duration = s->timing[first].duration;
    if (s->timing[first].key) {
        frame->flags |= AV_FRAME_FLAG_KEY;
        /* Native HlsTranscoder waits on pict_type == I before accepting any
         * decoded video. Setting only AV_FRAME_FLAG_KEY leaves it waiting. */
        frame->pict_type = AV_PICTURE_TYPE_I;
    }
    memmove(s->timing + first, s->timing + first + 1,
            (--s->timing_count - first) * sizeof(*s->timing));
    return 0;
}

static int decoder_error(AVCodecContext *avctx, const char *op, int code)
{
    av_log(avctx, AV_LOG_ERROR, "X5 decoder %s failed: %d\n", op, code);
    return AVERROR_EXTERNAL;
}

static av_cold int hobot_dec_close(AVCodecContext *avctx)
{
    HobotDecoder *s = avctx->priv_data;
    if (s->started) hb_mm_mc_pause(&s->mc);
    if (s->initialized) hb_mm_mc_release(&s->mc);
    for (unsigned i = 0; i < READY_MAX; i++) av_frame_free(&s->ready[i]);
    memset(s, 0, sizeof(*s));
    return 0;
}

static av_cold int hobot_dec_init(AVCodecContext *avctx)
{
    HobotDecoder *s = avctx->priv_data;
    mc_video_codec_dec_params_t *p = &s->mc.video_dec_params;
    mc_av_codec_startup_params_t startup = {0};
    int ret;
    /* Only the tested 8-bit 4:2:0 path. Unsupported profiles must keep their
     * native software decoder, not silently lose precision. */
    if (avctx->bits_per_raw_sample > 8 ||
        (avctx->pix_fmt != AV_PIX_FMT_NONE && avctx->pix_fmt != AV_PIX_FMT_YUV420P &&
         avctx->pix_fmt != AV_PIX_FMT_YUVJ420P && avctx->pix_fmt != AV_PIX_FMT_NV12))
        return AVERROR(ENOSYS);
    s->mc.encoder = 0;
    s->mc.codec_id = avctx->codec_id == AV_CODEC_ID_HEVC ? MEDIA_CODEC_ID_H265 : MEDIA_CODEC_ID_H264;
    p->feed_mode = MC_FEEDING_MODE_FRAME_SIZE;
    p->pix_fmt = MC_PIXEL_FORMAT_NV12;
    p->bitstream_buf_size = 4 * 1024 * 1024;
    p->bitstream_buf_count = 4;
    p->frame_buf_count = 8;
    if (avctx->codec_id == AV_CODEC_ID_HEVC) {
        p->h265_dec_config.bandwidth_Opt = 1;
        p->h265_dec_config.reorder_enable = 1;
    } else {
        p->h264_dec_config.bandwidth_Opt = 1;
        p->h264_dec_config.reorder_enable = 1;
    }
    ret = hb_mm_mc_initialize(&s->mc);
    if (ret) return decoder_error(avctx, "initialize", ret);
    s->initialized = 1;
    ret = hb_mm_mc_configure(&s->mc);
    if (ret) return decoder_error(avctx, "configure", ret);
    ret = hb_mm_mc_start(&s->mc, &startup);
    if (ret) return decoder_error(avctx, "start", ret);
    s->started = 1;
    s->first_picture = 1;
    avctx->pix_fmt = AV_PIX_FMT_NV12;
    av_log(avctx, AV_LOG_INFO, "X5 VPU decoder active (%s, 8-bit NV12)\n", avctx->codec->name);
    return 0;
}

/* One output operation. A timeout is normal while more compressed input is
 * required. EOS is explicit; timeout while draining is never reported as EOF. */
static int receive_output(AVCodecContext *avctx, int timeout)
{
    HobotDecoder *s = avctx->priv_data;
    media_codec_buffer_t out = {0};
    media_codec_output_buffer_info_t info = {0};
    mc_video_frame_buffer_info_t *v = &out.vframe_buf;
    mc_h264_h265_output_frame_info_t *detail = &info.video_frame_info;
    AVFrame *frame = NULL;
    int ret, release;
    if (s->count == READY_MAX) return AVERROR(ENOBUFS);
    /* The SDK creates its render component only after parsing the first SPS.
     * Do not dequeue from it before status reports an available output. */
    int64_t deadline = av_gettime_relative() + timeout * 1000LL;
    for (;;) {
        mc_inter_status_t status = {0};
        ret = hb_mm_mc_get_status(&s->mc, &status);
        if (ret) return decoder_error(avctx, "status", ret);
        if (status.cur_output_buf_cnt) break;
        if (av_gettime_relative() >= deadline) return 0;
        av_usleep(1000);
    }
    ret = hb_mm_mc_dequeue_output_buffer(&s->mc, &out, &info, FFMAX(timeout, 1));
    if (ret == HB_MEDIA_ERR_WAIT_TIMEOUT) return 0;
    if (ret) return decoder_error(avctx, "dequeue output", ret);
    ret = 0;
    if (v->frame_end || detail->frame_display_index == -1) {
        s->eof = 1;
    } else if (v->size && detail->frame_display_index >= 0 && detail->decode_result) {
        int width = detail->display_width > 0 ? detail->display_width : v->width;
        int height = detail->display_height > 0 ? detail->display_height : v->height;
        unsigned x = detail->display_rect.x_pos, y = detail->display_rect.y_pos;
        if (width <= 0 || height <= 0 || width > 4096 || height > 4096 ||
            (width & 1) || (height & 1) || (x & 1) || (y & 1) ||
            x + width > (unsigned)v->width || y + height > (unsigned)v->height ||
            v->stride < v->width || !v->vir_ptr[0] || !v->vir_ptr[1] ||
            v->pix_fmt != MC_PIXEL_FORMAT_NV12) {
            av_log(avctx, AV_LOG_ERROR, "Invalid X5 output geometry %dx%d in %dx%d stride %d\n",
                   width, height, v->width, v->height, v->stride);
            ret = AVERROR_INVALIDDATA;
            goto done;
        }
        frame = av_frame_alloc();
        if (!frame) { ret = AVERROR(ENOMEM); goto done; }
        frame->format = AV_PIX_FMT_NV12;
        frame->width = width; frame->height = height;
        ret = av_frame_get_buffer(frame, 32);
        if (ret < 0) goto done;
        for (int row = 0; row < height; row++)
            memcpy(frame->data[0] + row * frame->linesize[0],
                   v->vir_ptr[0] + (y + row) * v->stride + x, width);
        for (int row = 0; row < height / 2; row++)
            memcpy(frame->data[1] + row * frame->linesize[1],
                   v->vir_ptr[1] + (y / 2 + row) * v->stride + x, width);
        ret = set_frame_timing(s, frame);
        if (ret < 0) goto done;
        frame->pkt_dts = AV_NOPTS_VALUE;
        frame->time_base = avctx->pkt_timebase;
        frame->sample_aspect_ratio = avctx->sample_aspect_ratio;
        frame->color_range = avctx->color_range;
        frame->color_primaries = avctx->color_primaries;
        frame->color_trc = avctx->color_trc;
        frame->colorspace = avctx->colorspace;
        frame->chroma_location = avctx->chroma_sample_location;
        avctx->width = width; avctx->height = height;
        s->ready[(s->head + s->count++) % READY_MAX] = frame;
        frame = NULL;
        ret = 1;
    } else if (!detail->decode_result && v->size) {
        ret = AVERROR_INVALIDDATA;
    }
done:
    av_frame_free(&frame);
    release = hb_mm_mc_queue_output_buffer(&s->mc, &out, 0);
    if (release) return decoder_error(avctx, "release output", release);
    return ret;
}

static int hobot_decode(AVCodecContext *avctx, AVFrame *frame, int *got, AVPacket *packet)
{
    HobotDecoder *s = avctx->priv_data;
    media_codec_buffer_t in = {0};
    int ret = 0;
    *got = 0;
    if (s->failed) return s->failed;
    if (!s->eof && (packet->size || !s->draining)) {
        int picture = packet_has_picture(packet, avctx->codec_id);
        /* Transport-stream seeks may begin before the next random-access
         * picture. Submitting those slices makes this SDK terminate its task. */
        if (s->first_picture && picture && !(packet->flags & AV_PKT_FLAG_KEY))
            return packet->size;
        int header_size = s->first_picture && picture && avctx->extradata_size >= 4 &&
                          !avctx->extradata[0] && !avctx->extradata[1] ? avctx->extradata_size : 0;
        if (picture && s->timing_count == TIMING_MAX) return AVERROR(ENOBUFS);
        if (header_size > (int)s->mc.video_dec_params.bitstream_buf_size ||
            packet->size > (int)s->mc.video_dec_params.bitstream_buf_size - header_size)
            return AVERROR(ENOBUFS);
        /* Drain available frames before asking the VPU for another input slot.
         * The legacy FFCodec callback must consume the whole video packet. */
        ret = receive_output(avctx, 0);
        if (ret < 0) goto fail;
        for (unsigned retry = 0; retry < 30; retry++) {
            ret = hb_mm_mc_dequeue_input_buffer(&s->mc, &in, 100);
            if (ret != HB_MEDIA_ERR_WAIT_TIMEOUT) break;
            ret = receive_output(avctx, 0);
            if (ret < 0) goto fail;
            ret = HB_MEDIA_ERR_WAIT_TIMEOUT;
        }
        if (ret) { ret = decoder_error(avctx, "dequeue input", ret); goto fail; }
        if (!in.vstream_buf.vir_ptr || in.vstream_buf.size < (unsigned)(packet->size + header_size)) {
            ret = AVERROR_INVALIDDATA; goto fail;
        }
        in.type = MC_VIDEO_STREAM_BUFFER;
        if (header_size) memcpy(in.vstream_buf.vir_ptr, avctx->extradata, header_size);
        if (packet->size) memcpy(in.vstream_buf.vir_ptr + header_size, packet->data, packet->size);
        in.vstream_buf.size = packet->size + header_size;
        in.vstream_buf.pts = packet->pts;
        in.vstream_buf.stream_end = !packet->size;
        ret = hb_mm_mc_queue_input_buffer(&s->mc, &in, 1000);
        if (ret) { ret = decoder_error(avctx, "queue input", ret); goto fail; }
        if (picture) s->timing[s->timing_count++] = (PacketTiming){packet->pts, packet->duration,
                                                                               !!(packet->flags & AV_PKT_FLAG_KEY)};
        if (picture) s->first_picture = 0;
        s->draining = !packet->size;
    }
    if (!s->count && !s->eof) {
        ret = receive_output(avctx, s->draining ? 3000 : 0);
        if (ret < 0) goto fail;
        if (s->draining && !s->count && !s->eof) {
            ret = AVERROR(ETIMEDOUT); goto fail;
        }
    }
    if (s->count) {
        AVFrame **first = &s->ready[s->head];
        av_frame_move_ref(frame, *first);
        av_frame_free(first);
        s->head = (s->head + 1) % READY_MAX; s->count--;
        *got = 1;
    }
    return packet->size;
fail:
    s->failed = ret;
    return ret;
}

static void hobot_dec_flush(AVCodecContext *avctx)
{
    HobotDecoder *s = avctx->priv_data;
    hobot_dec_close(avctx);
    s->failed = hobot_dec_init(avctx);
}

#define HOBOT_DECODER(NAME, ID, BSF) \
const FFCodec ff_##NAME##_hobot_decoder = { \
    .p.name = #NAME "_hobot_dec", CODEC_LONG_NAME("D-Robotics X5 VPU " #NAME " decoder"), \
    .p.type = AVMEDIA_TYPE_VIDEO, .p.id = ID, .p.capabilities = AV_CODEC_CAP_DELAY, \
    .priv_data_size = sizeof(HobotDecoder), .init = hobot_dec_init, \
    FF_CODEC_DECODE_CB(hobot_decode), .close = hobot_dec_close, .flush = hobot_dec_flush, \
    .caps_internal = FF_CODEC_CAP_INIT_CLEANUP | FF_CODEC_CAP_SETS_PKT_DTS | FF_CODEC_CAP_SETS_FRAME_PROPS, \
    .bsfs = BSF, .p.wrapper_name = "hobot", \
};
HOBOT_DECODER(h264, AV_CODEC_ID_H264, "h264_mp4toannexb")
HOBOT_DECODER(hevc, AV_CODEC_ID_HEVC, "hevc_mp4toannexb")
