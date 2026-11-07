from companion.video_media_contract import ClipRange, MediaSelection, VideoTarget


def target(duration=3):
    return VideoTarget("content_" + "1" * 32, "observation_source", "a" * 64,
                       "https://www.bilibili.com/video/BV1234567890", "BV1234567890",
                       123, 1, duration)


def preview(value, times=(0.15, 2.1)):
    return MediaSelection(value.sha256, "b" * 64, "preview", frame_times=times)


def clips(value, spans=((0.25, 1.25),)):
    return MediaSelection(value.sha256, "b" * 64, "clips",
                          clips=tuple(ClipRange(*span) for span in spans))
