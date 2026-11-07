import json

from tests.support.onebot_events import raw_event


def recall_frame(message_id=1, **changes):
    return json.dumps({"time": raw_event()["time"], "self_id": 10002, "post_type": "notice",
                       "notice_type": "friend_recall", "user_id": 10001,
                       "message_id": message_id, **changes})
