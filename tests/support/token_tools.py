


TOOLS = [{"type": "function", "function": {"name": "current_time",
    "description": "读取当前时间。", "parameters": {"type": "object", "properties": {},
                                                "additionalProperties": False}}}]


MESSAGES = [{"role": "system", "content": "Use available tools when useful."},
            {"role": "user", "content": "现在几点？"}]


CALL = {"id": "clock-call", "type": "function",
        "function": {"name": "current_time", "arguments": "{}"}}


CONTINUATION = [*MESSAGES, {"role": "assistant", "content": None, "tool_calls": [CALL]},
                {"role": "tool", "tool_call_id": "clock-call", "content": "2026-10-25T03:00Z"}]
