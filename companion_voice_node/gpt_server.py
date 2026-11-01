"""Launch pinned GPT-SoVITS without persisting its transcript/phoneme console output."""

import argparse
import os
import runpy
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--script", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    script = args.script.resolve(strict=True)
    config = args.config.resolve(strict=True)
    if not 1024 <= args.port <= 65535 or script.name != "api_v2.py":
        parser.error("Expected the pinned api_v2.py and a non-privileged port")
    sys.argv = [str(script), "-a", "127.0.0.1", "-p", str(args.port), "-c", str(config)]
    null = os.open(os.devnull, os.O_WRONLY)
    try:
        for descriptor in (1, 2):
            os.dup2(null, descriptor)
    finally:
        os.close(null)
    runpy.run_path(str(script), run_name="__main__")


if __name__ == "__main__":
    main()
