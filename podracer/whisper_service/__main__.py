import argparse

import uvicorn

from podracer.config import load_config
from podracer.logging_config import configure_logging
from podracer.whisper_service.app import create_app


def main():
    configure_logging()  # env/auto for early logs; config.toml applied below

    parser = argparse.ArgumentParser(prog="podracer-whisper",
                                     description="Run the whisperx transcription service")
    parser.add_argument("--host", default=None, help="Bind host (default: from config)")
    parser.add_argument("--port", type=int, default=None, help="Port (default: from config)")
    args = parser.parse_args()

    cfg = load_config()
    configure_logging(cfg.log_format)  # apply config.toml format (env still wins)
    host = args.host or cfg.whisper_service_host
    port = args.port or cfg.whisper_service_port
    app = create_app(cfg)
    # log_config=None so uvicorn's access/error loggers use our root handler.
    uvicorn.run(app, host=host, port=port, log_config=None)


if __name__ == "__main__":
    main()
