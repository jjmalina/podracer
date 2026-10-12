"""Pull eval cases from a deployed podracer instance over SSH.

There is no read API for transcripts yet, so this runs a small read-only
sqlite query on the host via ``ssh`` and writes one ``case.json`` per manifest
episode. Host and DB path come from flags or ``PODRACER_EVAL_SSH`` /
``PODRACER_EVAL_DB`` so no deployment details live in this (public) repo.
``--ssh`` is passed to ``ssh`` after shell-style splitting, so it may carry
options: ``"-i ~/.ssh/key user@host"``.
"""
import json
import os
import shlex
import subprocess
from pathlib import Path

from podracer.evals import logger
from podracer.evals.dataset import case_dir, load_manifest, write_case

DEFAULT_DB = "/var/lib/podracer/podracer.db"

# Runs on the remote host with the system python3 (no sqlite3 CLI there).
# Read-only URI; prints one JSON object per requested episode id.
_REMOTE_SCRIPT = r'''
import json, sqlite3, sys
ids = [int(x) for x in sys.argv[2].split(",")]
c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
q = """select e.id, e.title, p.title, p.description, e.show_notes, t.text, s.data
       from episodes e join podcasts p on p.id = e.podcast_id
       join transcripts t on t.episode_id = e.id
       left join summaries s on s.episode_id = e.id
       where e.id = ?"""
for i in ids:
    row = c.execute(q, (i,)).fetchone()
    if row is None:
        print(json.dumps({"id": i, "error": "no transcript"}))
        continue
    print(json.dumps({
        "id": row[0], "episode_title": row[1], "podcast_title": row[2],
        "podcast_description": row[3], "show_notes": row[4], "transcript": row[5],
        "summary": json.loads(row[6]) if row[6] else None,
    }, ensure_ascii=False))
'''


def fetch(root: Path, ssh: str | None = None, db: str | None = None,
          ids: list[int] | None = None, force: bool = False) -> list[int]:
    ssh = ssh or os.environ.get("PODRACER_EVAL_SSH")
    db = db or os.environ.get("PODRACER_EVAL_DB") or DEFAULT_DB
    if not ssh:
        raise RuntimeError("need --ssh user@host (or PODRACER_EVAL_SSH)")
    entries = load_manifest(root)
    wanted = [int(e["id"]) for e in entries if not ids or int(e["id"]) in ids]
    todo = [i for i in wanted if force or not (case_dir(root, i) / "case.json").exists()]
    if not todo:
        logger.info("eval_fetch_nothing_to_do", have=len(wanted))
        return []
    cmd = ["ssh", *(os.path.expanduser(tok) for tok in shlex.split(ssh)),
           "python3", "-", db, ",".join(map(str, todo))]
    logger.info("eval_fetch", host=ssh, episodes=todo)
    proc = subprocess.run(cmd, input=_REMOTE_SCRIPT, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"ssh failed ({proc.returncode}): {proc.stderr.strip()}")
    fetched: list[int] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if payload.get("error"):
            logger.warning("eval_fetch_missing", episode_id=payload["id"], error=payload["error"])
            continue
        write_case(root, int(payload["id"]), payload)
        fetched.append(int(payload["id"]))
    logger.info("eval_fetch_done", fetched=len(fetched), requested=len(todo))
    return fetched
