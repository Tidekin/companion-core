import httpx


def model_response(text):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}],
                                    "usage": {"prompt_tokens": 100, "completion_tokens": 20}})
