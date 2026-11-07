from types import SimpleNamespace


def astrbot_event(text="你好", message_id="42", segment="text"):
    return SimpleNamespace(
        get_platform_name=lambda: "aiocqhttp",
        get_sender_id=lambda: "10001",
        get_group_id=lambda: "",
        unified_msg_origin="qq:FriendMessage:10001",
        message_str=text,
        message_obj=SimpleNamespace(
            self_id="10002",
            message_id=message_id,
            timestamp=1792382400,
            raw_message={
                "post_type": "message",
                "message_type": "private",
                "self_id": 10002,
                "user_id": 10001,
                "message_id": message_id,
                "message": [{"type": segment, "data": {"text": text}}],
            },
        ),
    )
