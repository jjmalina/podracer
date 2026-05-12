# Podracer

Local-first podcast knowledge platform: ingest audio, transcribe, summarize, search, and chat with your archive.

## Commands

```bash
# Lint
.venv/bin/ruff check podracer/

# Auto-fix lint
.venv/bin/ruff check --fix podracer/

# Type check
.venv/bin/ty check podracer/

# Run the CLI
.venv/bin/python3 -m podracer.cli <command>

# Start the web UI (with auto-reload for development)
.venv/bin/python3 -m podracer.cli serve --host 0.0.0.0 --port 8080 --reload
```

## Rules

- Always run `ruff check` and `ty check` before committing. Fix all errors.
- Use `ruff check --fix` to auto-fix import sorting and simple lint issues.
