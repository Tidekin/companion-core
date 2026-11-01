"""Independent native HTTP service with only model inference replaced for tests."""

import argparse

import uvicorn

from companion_voice_node.providers import validate_wav
from companion_voice_node.speech_service import create_app, load_service_config
from tests.test_local_voice_providers import wav_bytes


class Engine:
    def load(self):
        pass

    async def synthesize(self, text):
        return validate_wav(wav_bytes())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args()
    uvicorn.run(create_app(load_service_config(args.config), engine=Engine()),
                host="127.0.0.1", port=args.port, access_log=False, log_level="error")
