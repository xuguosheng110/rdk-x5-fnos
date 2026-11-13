/* Real-board probe of mediasrv's observed context and generated blank segment.
 * Compile on the designated server against pinned native FFmpeg. The board
 * owner runs each mode with the candidate preloaded; outputs are test files. */
#include <stdio.h>
#include <limits.h>
#include <string.h>
#include "libavcodec/avcodec.h"
#include "libavutil/opt.h"

static int write_packets(AVCodecContext *ctx, AVPacket *packet, FILE *output, int *count)
{
    int ret;
    while ((ret = avcodec_receive_packet(ctx, packet)) >= 0) {
        if (fwrite(packet->data, 1, packet->size, output) != (size_t)packet->size)
            return AVERROR(EIO);
        ++*count;
        av_packet_unref(packet);
    }
    return ret == AVERROR(EAGAIN) || ret == AVERROR_EOF ? 0 : ret;
}

int main(int argc, char **argv)
{
    if (argc != 3 || (strcmp(argv[2], "high5") && strcmp(argv[2], "blank") &&
                     strcmp(argv[2], "hevc51") && strcmp(argv[2], "hevcblank") &&
                     strcmp(argv[2], "hevc51-cpu") && strcmp(argv[2], "thumb"))) {
        fprintf(stderr, "usage: %s output high5|blank|hevc51|hevcblank|hevc51-cpu|thumb\n", argv[0]);
        return 2;
    }
    if (avcodec_version() != AV_VERSION_INT(62, 28, 101)) return 2;
    int hevc = !strncmp(argv[2], "hevc", 4);
    int thumbnail = !strcmp(argv[2], "thumb");
    int cpu_control = !strcmp(argv[2], "hevc51-cpu") || thumbnail;
    int explicit_profile = !strcmp(argv[2], "high5") || !strcmp(argv[2], "hevc51") || cpu_control;
    const char *software = hevc ? "libx265" : "libx264";
    const AVCodec *codec = explicit_profile ? avcodec_find_encoder_by_name(software) :
                            avcodec_find_encoder(hevc ? AV_CODEC_ID_HEVC : AV_CODEC_ID_H264);
    if (!codec || strcmp(codec->name, software)) {
        fprintf(stderr, "software negotiation codec not preserved: %s\n", codec ? codec->name : "null");
        return 1;
    }
    AVCodecContext *ctx = avcodec_alloc_context3(codec);
    AVFrame *frame = av_frame_alloc();
    AVPacket *packet = av_packet_alloc();
    FILE *output = fopen(argv[1], "wbx");
    int ret = AVERROR(ENOMEM), packets = 0;
    if (!ctx || !frame || !packet || !output) goto done;
    ctx->width = 848; ctx->height = 476;
    ctx->pix_fmt = AV_PIX_FMT_YUV420P;
    ctx->time_base = (AVRational){1, 12800};
    ctx->framerate = (AVRational){25, 1};
    ctx->flags = hevc ? 0 : AV_CODEC_FLAG_CLOSED_GOP;
    ctx->bit_rate = 2000000;
    if (thumbnail) {
        ctx->width = 1920;
        ctx->height = 1080;
        ctx->bit_rate = 0;
        ctx->gop_size = INT_MAX;
    }
    ctx->max_b_frames = explicit_profile ? 0 : -1;
    if (explicit_profile) {
        ctx->profile = hevc ? AV_PROFILE_HEVC_MAIN : AV_PROFILE_H264_HIGH;
        ctx->level = hevc ? 51 : 50;
        if ((ret = av_opt_set(ctx->priv_data, "preset", "veryfast", 0)) < 0 ||
            (ret = av_opt_set(ctx->priv_data, "crf", hevc ? "28" : "23", 0)) < 0 ||
            (ret = av_opt_set(ctx->priv_data, hevc ? "x265-params" : "x264opts", hevc ? "no-info=1" :
                "subme=0:me_range=4:rc_lookahead=10:me=dia:no_chroma_me:8x8dct=0:partitions=none", 0)) < 0)
            goto done;
    }
    ret = avcodec_open2(ctx, codec, NULL);
    if (ret < 0) goto done;
    if (strcmp(ctx->codec->name, cpu_control ? software : (hevc ? "hevc_hobot" : "h264_hobot"))) {
        fprintf(stderr, "observed native context did not select VPU: %s\n", ctx->codec->name);
        ret = AVERROR(ENOSYS); goto done;
    }
    frame->width = ctx->width; frame->height = ctx->height;
    frame->format = ctx->pix_fmt;
    if ((ret = av_frame_get_buffer(frame, 32)) < 0) goto done;
    for (int i = 0; i < 25; i++) {
        if ((ret = av_frame_make_writable(frame)) < 0) goto done;
        for (int y = 0; y < frame->height; y++)
            memset(frame->data[0] + y * frame->linesize[0], explicit_profile ? 32 + i * 4 : 16, frame->width);
        for (int y = 0; y < frame->height / 2; y++) {
            memset(frame->data[1] + y * frame->linesize[1], 128, frame->width / 2);
            memset(frame->data[2] + y * frame->linesize[2], 128, frame->width / 2);
        }
        frame->pts = i * 512;
        frame->duration = 512;
        frame->pict_type = i ? AV_PICTURE_TYPE_NONE : AV_PICTURE_TYPE_I;
        if ((ret = avcodec_send_frame(ctx, frame)) < 0 ||
            (ret = write_packets(ctx, packet, output, &packets)) < 0) goto done;
    }
    if ((ret = avcodec_send_frame(ctx, NULL)) < 0 ||
        (ret = write_packets(ctx, packet, output, &packets)) < 0) goto done;
    if (packets != 25) { ret = AVERROR_INVALIDDATA; goto done; }
    printf("PASS mode=%s actual=%s packets=%d profile=%d level=%d time_base=1/12800 rate=25/1\n",
           argv[2], ctx->codec->name, packets, ctx->profile, ctx->level);
    ret = 0;
done:
    if (output && fclose(output)) ret = AVERROR(EIO);
    if (ret < 0) fprintf(stderr, "native context probe failed: %d\n", ret);
    av_packet_free(&packet);
    av_frame_free(&frame);
    avcodec_free_context(&ctx);
    return ret < 0;
}
