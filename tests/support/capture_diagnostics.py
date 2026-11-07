from tests.support.onebot_capture import bootstrap_frames


def lifecycle_frames():
    frames = bootstrap_frames()
    for frame in frames[:2]:
        frame["status"] = {"online": True, "good": True}
    return frames
