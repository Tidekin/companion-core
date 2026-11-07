from companion_onebot_bridge.spool import encode


def frame(config, now):
    return encode({"post_type": "notice", "notice_type": "friend_recall",
        "time": int(now.timestamp()), "self_id": int(config.bot_qq_id),
        "user_id": int(config.owner_qq_id), "message_id": 1})
