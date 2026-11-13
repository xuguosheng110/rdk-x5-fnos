/* SPDX-License-Identifier: LGPL-2.1-or-later
 * In-process adapter for pinned fnOS mediasrv libavcodec 62.28.101.
 * Explicit h264_hobot/hevc_hobot names can be tested without changing defaults.
 * The installer must check the native library hash before enabling substitution.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "libavcodec/avcodec.h"
#include "libavcodec/codec_internal.h"
#include "libavutil/opt.h"
extern const FFCodec ff_h264_hobot_encoder;
extern const FFCodec ff_hevc_hobot_encoder;
extern const FFCodec ff_h264_hobot_decoder;
extern const FFCodec ff_hevc_hobot_decoder;

static int enabled(void)
{
    const char *value = getenv("FNOS_X5_VPU_ENABLE");
    return value && !strcmp(value, "1") && avcodec_version() == AV_VERSION_INT(62, 28, 101);
}

static int compatible_encoder_option(const char *key, const char *value)
{
    if (!strcmp(key, "preset") || !strcmp(key, "crf")) return 1;
    if (!strcmp(key, "x264opts"))
        return !strcmp(value, "subme=0:me_range=4:rc_lookahead=10:me=dia:no_chroma_me:8x8dct=0:partitions=none");
    if (!strcmp(key, "x265-params")) return !strcmp(value, "no-info=1");
    return 0;
}

/* mediasrv may replace FFmpeg's log callback and hide informational selection
 * messages. Opt-in stderr diagnostics identify the exact native open context;
 * they do not alter routing or require guessing a profile/B-frame contract. */
static void encoder_diagnostics(AVCodecContext *ctx, const AVCodec *codec,
                                AVDictionary **options)
{
    const char *debug = getenv("FNOS_X5_VPU_DIAGNOSTICS");
    const AVDictionaryEntry *entry = NULL;
    char *private = NULL;
    if (!debug || strcmp(debug, "1")) return;
    fprintf(stderr, "X5 encoder context: name=%s fmt=%d bits=%d size=%dx%d "
            "profile=%d level=%d max_b_frames=%d flags=%x field_order=%d "
            "rate=%d/%d time_base=%d/%d trc=%d primaries=%d colorspace=%d "
            "hw_device=%d hw_frames=%d\n", codec->name, ctx->pix_fmt,
            ctx->bits_per_raw_sample, ctx->width, ctx->height, ctx->profile,
            ctx->level, ctx->max_b_frames, ctx->flags, ctx->field_order,
            ctx->framerate.num, ctx->framerate.den, ctx->time_base.num,
            ctx->time_base.den, ctx->color_trc, ctx->color_primaries,
            ctx->colorspace, !!ctx->hw_device_ctx, !!ctx->hw_frames_ctx);
    if (ctx->priv_data && codec->priv_class &&
        av_opt_serialize(ctx->priv_data, AV_OPT_FLAG_ENCODING_PARAM,
                         AV_OPT_SERIALIZE_SKIP_DEFAULTS, &private, '=', ':') >= 0)
        fprintf(stderr, "X5 encoder private: %s\n", private ? private : "");
    av_free(private);
    while (options && (entry = av_dict_get(*options, "", entry, AV_DICT_IGNORE_SUFFIX)))
        fprintf(stderr, "X5 encoder option: %s=%s\n", entry->key, entry->value);
}

/* Keep the real software encoder's advertised formats during negotiation. In
 * particular, an early libx265 -> VPU alias silently coerced Main10 to 8-bit.
 * Switch only once the actual context and private options are available. */
static int open_encoder(AVCodecContext *ctx, const AVCodec *original,
                        AVDictionary **options,
                        int (*next)(AVCodecContext *, const AVCodec *, AVDictionary **))
{
    const AVCodec *replacement = !strcmp(original->name, "libx264") ?
        &ff_h264_hobot_encoder.p : &ff_hevc_hobot_encoder.p;
    AVRational rate = ctx->framerate.num > 0 && ctx->framerate.den > 0 ?
                      ctx->framerate : av_inv_q(ctx->time_base);
    AVDictionary *private = NULL, *merged = NULL;
    const AVDictionaryEntry *entry = NULL;
    const AVClass *private_class = original->priv_class;
    char *serialized = NULL;
    int ret;
    encoder_diagnostics(ctx, original, options);
    /* Exact native mediasrv request observed on X5. The SDK's default 8-bit
     * H264 stream is High, and its level register is supported. hobotenc checks
     * the actual SPS before exposing a packet; other explicit profiles stay on
     * software until their bitstream contract has been implemented and tested. */
    const int native_high5 = !strcmp(original->name, "libx264") &&
                             ctx->profile == AV_PROFILE_H264_HIGH && ctx->level == 50;
    /* Native libx265 ignores this AVCodecContext level field: the exact
     * observed Main/51 context emitted level_idc 90 in the CPU control. Keep
     * automatic SDK level selection, bounded by Main tier Level 5.1, rather
     * than falsely translating the unused 51 into a forced 153 bitstream. */
    const int native_main51 = !strcmp(original->name, "libx265") &&
                              ctx->profile == AV_PROFILE_HEVC_MAIN && ctx->level == 51;
    const char *debug = getenv("FNOS_X5_VPU_DIAGNOSTICS");
    const char *reason = "unsupported format, profile, timing, HDR, or interlacing";
    if ((ctx->pix_fmt != AV_PIX_FMT_YUV420P && ctx->pix_fmt != AV_PIX_FMT_NV12) ||
        ctx->bits_per_raw_sample > 8 || ctx->width < 256 || ctx->height < 128 ||
        ctx->width > 3840 || ctx->height > 2160 || (ctx->width & 1) || (ctx->height & 1) ||
        (!native_high5 && !native_main51 &&
         (ctx->profile != AV_PROFILE_UNKNOWN || ctx->level != AV_LEVEL_UNKNOWN)) ||
        ctx->max_b_frames > 0 || ctx->hw_device_ctx || ctx->hw_frames_ctx ||
        (ctx->flags & (AV_CODEC_FLAG_INTERLACED_DCT | AV_CODEC_FLAG_INTERLACED_ME | AV_CODEC_FLAG_QSCALE)) ||
        (ctx->field_order != AV_FIELD_UNKNOWN && ctx->field_order != AV_FIELD_PROGRESSIVE) ||
        ctx->color_trc == AVCOL_TRC_SMPTE2084 || ctx->color_trc == AVCOL_TRC_ARIB_STD_B67 ||
        rate.num <= 0 || rate.den <= 0 || av_q2d(rate) < 1 || av_q2d(rate) > 60)
        goto native;
    /* Pinned FFmpeg h264_levels.c, table A-1/A-2: Level 5 MaxFS=22080,
     * MaxMBPS=589824. Use the conservative High-profile VCL bitrate bound. */
    if (native_high5) {
        int64_t macroblocks = ((ctx->width + 15) / 16) * ((ctx->height + 15) / 16);
        reason = "H264 High Level 5 workload exceeds the level contract";
        if (macroblocks > 22080 || macroblocks * rate.num > 589824LL * rate.den ||
            ctx->bit_rate > 168750000 || ctx->rc_max_rate > 168750000)
            goto native;
    }
    if (native_main51) {
        int64_t pixels = (int64_t)ctx->width * ctx->height;
        reason = "HEVC Main tier workload exceeds Level 5.1";
        if (pixels > 8912896 || pixels * rate.num > 534773760LL * rate.den ||
            ctx->bit_rate > 40000000 || ctx->rc_max_rate > 40000000)
            goto native;
    }
    /* mediasrv thumbnail extraction requests INT_MAX (no periodic keyframes).
     * The hardware encoder accepts at most 2047. Preserve native semantics
     * and avoid a failed hardware open for any larger explicit GOP. */
    reason = "GOP size exceeds the hardware limit";
    if (ctx->gop_size > 2047)
        goto native;
    reason = "software-specific encoder options";
    if (ctx->priv_data && original->priv_class) {
        ret = av_opt_serialize(ctx->priv_data, AV_OPT_FLAG_ENCODING_PARAM,
                              AV_OPT_SERIALIZE_SKIP_DEFAULTS, &serialized, '=', ':');
        if (ret < 0 || (serialized && av_dict_parse_string(&private, serialized, "=", ":", 0) < 0))
            goto native;
        while ((entry = av_dict_get(private, "", entry, AV_DICT_IGNORE_SUFFIX)))
            if (!compatible_encoder_option(entry->key, entry->value)) goto native;
    }
    entry = NULL;
    while (options && (entry = av_dict_get(*options, "", entry, AV_DICT_IGNORE_SUFFIX))) {
        if (!strcmp(entry->key, "profile") || !strcmp(entry->key, "level") ||
            !strcmp(entry->key, "pixel_format") || !strcmp(entry->key, "pix_fmt") ||
            !strcmp(entry->key, "flags") || !strcmp(entry->key, "field_order") ||
            (!strcmp(entry->key, "bf") && strcmp(entry->value, "0")) || !strcmp(entry->key, "color_trc") ||
            !strcmp(entry->key, "video_size") || !strcmp(entry->key, "width") ||
            !strcmp(entry->key, "height") || !strcmp(entry->key, "framerate") ||
            !strcmp(entry->key, "time_base"))
            goto native;
        if (private_class && av_opt_find(&private_class, entry->key, NULL, 0, AV_OPT_SEARCH_FAKE_OBJ) &&
            !compatible_encoder_option(entry->key, entry->value)) goto native;
    }
    if (av_dict_copy(&merged, private, 0) < 0 ||
        (options && av_dict_copy(&merged, *options, 0) < 0)) goto native;
    av_free(serialized);
    av_dict_free(&private);
    void *old_private = ctx->priv_data;
    ctx->priv_data = NULL;
    ctx->codec = replacement;
    ret = next(ctx, replacement, &merged);
    if (debug && !strcmp(debug, "1"))
        fprintf(stderr, "X5 encoder route: %s -> %s open_return=%d profile=%d level=%d\n",
                original->name, replacement->name, ret, ctx->profile, ctx->level);
    if (old_private && original->priv_class) av_opt_free(old_private);
    av_free(old_private);
    if (options) {
        av_dict_free(options);
        *options = merged;
    } else {
        av_dict_free(&merged);
    }
    return ret;
native:
    av_free(serialized);
    av_dict_free(&private);
    av_dict_free(&merged);
    av_log(ctx, AV_LOG_INFO, "X5 VPU encoder not selected: %s; retaining native %s\n", reason, original->name);
    if (debug && !strcmp(debug, "1"))
        fprintf(stderr, "X5 encoder route: native %s reason=%s\n", original->name, reason);
    return next(ctx, original, options);
}

/* Select at open time, when stream parameters are known. Keep native decode for
 * unprobed streams, 10-bit/chroma variants, interlacing, custom decoder options,
 * and explicit hardware backends. This never substitutes unsupported content. */
int avcodec_open2(AVCodecContext *ctx, const AVCodec *codec, AVDictionary **options)
{
    int (*next)(AVCodecContext *, const AVCodec *, AVDictionary **) = dlsym(RTLD_NEXT, "avcodec_open2");
    const char *enable = getenv("FNOS_X5_VPU_DECODE");
    const AVCodec *original = codec ? codec : (ctx ? ctx->codec : NULL);
    const AVCodec *replacement = NULL;
    if (!next) return AVERROR(ENOSYS);
    if (ctx && original && enabled() && !avcodec_is_open(ctx) &&
        (!ctx->codec || ctx->codec == original) && av_codec_is_encoder(original) &&
        (!strcmp(original->name, "libx264") || !strcmp(original->name, "libx265")))
        return open_encoder(ctx, original, options, next);
    if (!ctx || !original || !enable || strcmp(enable, "1") ||
        avcodec_version() != AV_VERSION_INT(62, 28, 101) || avcodec_is_open(ctx) ||
        (ctx->codec && ctx->codec != original) || !av_codec_is_decoder(original) ||
        ctx->pix_fmt != AV_PIX_FMT_YUV420P || ctx->bits_per_raw_sample > 8 ||
        ctx->width < 64 || ctx->height < 64 || ctx->width > 3840 || ctx->height > 2160 ||
        (ctx->width & 1) || (ctx->height & 1) || ctx->hw_device_ctx || ctx->hw_frames_ctx ||
        (ctx->field_order != AV_FIELD_UNKNOWN && ctx->field_order != AV_FIELD_PROGRESSIVE))
        return next(ctx, codec, options);
    if (!strcmp(original->name, "h264") &&
        (ctx->profile == AV_PROFILE_H264_BASELINE || ctx->profile == AV_PROFILE_H264_CONSTRAINED_BASELINE ||
         ctx->profile == AV_PROFILE_H264_MAIN || ctx->profile == AV_PROFILE_H264_HIGH))
        replacement = &ff_h264_hobot_decoder.p;
    if (!strcmp(original->name, "hevc") && ctx->profile == AV_PROFILE_HEVC_MAIN)
        replacement = &ff_hevc_hobot_decoder.p;
    if (!replacement) return next(ctx, codec, options);
    if (ctx->priv_data && original->priv_class) {
        char *custom = NULL;
        int ret = av_opt_serialize(ctx->priv_data, AV_OPT_FLAG_DECODING_PARAM,
                                  AV_OPT_SERIALIZE_SKIP_DEFAULTS, &custom, '=', ':');
        int keep_native = ret < 0 || (custom && *custom);
        av_free(custom);
        if (keep_native) return next(ctx, codec, options);
    }
    void *old_private = ctx->priv_data;
    ctx->priv_data = NULL;
    ctx->codec = replacement;
    int ret = next(ctx, replacement, options);
    if (old_private && original->priv_class) av_opt_free(old_private);
    av_free(old_private);
    return ret;
}

const AVCodec *avcodec_find_decoder_by_name(const char *name)
{
    const AVCodec *(*next)(const char *) = dlsym(RTLD_NEXT, "avcodec_find_decoder_by_name");
    if (name && avcodec_version() == AV_VERSION_INT(62, 28, 101)) {
        if (!strcmp(name, "h264_hobot_dec")) return &ff_h264_hobot_decoder.p;
        if (!strcmp(name, "hevc_hobot_dec")) return &ff_hevc_hobot_decoder.p;
    }
    return next ? next(name) : NULL;
}

const AVCodec *avcodec_find_encoder_by_name(const char *name)
{
    const AVCodec *(*next)(const char *) = dlsym(RTLD_NEXT, "avcodec_find_encoder_by_name");
    if (name && avcodec_version() == AV_VERSION_INT(62, 28, 101)) {
        if (!strcmp(name, "h264_hobot"))
            return &ff_h264_hobot_encoder.p;
        if (!strcmp(name, "hevc_hobot"))
            return &ff_hevc_hobot_encoder.p;
    }
    return next ? next(name) : NULL;
}

const AVCodec *avcodec_find_encoder(enum AVCodecID id)
{
    const AVCodec *(*next)(enum AVCodecID) = dlsym(RTLD_NEXT, "avcodec_find_encoder");
    /* This native build lists rkmpp before the software encoders. mediasrv's
     * blank/tail segment uses the ID lookup rather than a codec name. Returning
     * rkmpp makes that segment fail on X5; returning VPU here would instead
     * narrow pixel-format negotiation before Main10/HDR is known. Use the real
     * software codec for negotiation and the same conservative open-time gate
     * used by explicit libx264/libx265 callers. Explicit hardware names remain
     * untouched. The original default is retained when this adapter is off. */
    if (enabled() && (id == AV_CODEC_ID_H264 || id == AV_CODEC_ID_HEVC)) {
        const AVCodec *(*find_name)(const char *) = dlsym(RTLD_NEXT, "avcodec_find_encoder_by_name");
        const char *name = id == AV_CODEC_ID_H264 ? "libx264" : "libx265";
        const AVCodec *software = find_name ? find_name(name) : NULL;
        if (software) return software;
    }
    return next ? next(id) : NULL;
}

