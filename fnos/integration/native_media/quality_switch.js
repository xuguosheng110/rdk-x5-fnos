/* SPDX-License-Identifier: MIT */
/* The native HLS rendition reset can reuse segments from the previous stream. */
if (o.current?.isHlsM3u8Mode && r?.currentTime > 0 &&
    Number.isFinite(r.config?.mediaConfig?.duration) &&
    r.config.mediaConfig.duration - r.currentTime <= 3) {
    r.pause();
    await Lh?.(d)?.catch(console.error);
    oi({resolution: i.resolution, bitrate: i.bitrate});
    return;
}
