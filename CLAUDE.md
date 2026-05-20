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

# Run the whisper transcription service (requires torch + GPU)
.venv/bin/python3 -m podracer.whisper_service --host 0.0.0.0 --port 9000
```

## Rules

- Always run `ruff check` and `ty check` before committing. Fix all errors.
- Use `ruff check --fix` to auto-fix import sorting and simple lint issues.
- **No nested imports.** All `import` and `from ... import ...` statements go at
  the top of the module, never inside functions. If a heavy dep (e.g. torch,
  whisperx) shouldn't be loaded by every caller, put that code in its own module
  so callers can opt in by importing that module. Enforced by ruff `PLC0415`.
