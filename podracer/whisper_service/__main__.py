import argparse
import logging
import sys

import uvicorn

from podracer.config import load_config
from podracer.whisper_service.app import create_app


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)

    parser = argparse.ArgumentParser(prog="podracer-whisper",
                                     description="Run the whisperx transcription service")
    parser.add_argument("--host", default=None, help="Bind host (default: from config)")
    parser.add_argument("--port", type=int, default=None, help="Port (default: from config)")
    args = parser.parse_args()

    cfg = load_config()
    host = args.host or cfg.whisper_service_host
    port = args.port or cfg.whisper_service_port
    app = create_app(cfg)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
