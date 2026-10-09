"""The eval dataset: a committed manifest of prod episode ids plus per-episode
case files that are pulled from prod and kept out of git (transcripts and
speaker names are not public).

Layout (``--dataset`` root, default ``eval/``)::

    eval/manifest.json            committed: [{"id": 123, "tags": ["short", "solo"]}, ...]
    eval/data/<id>/case.json      gitignored: transcript, show notes, description, prod summary
    eval/data/<id>/labels.json    gitignored: hand-corrected speaker names (seeded from prod)
    eval/runs/...                 gitignored: run outputs
    eval/reports/<date>-*.html    committed: redacted reports (scores only, no transcript text)

``pack`` zips the manifest and case files so the dataset can be kept outside
the repo and handed to ``matrix --cases``; ``unpack`` is the inverse.
"""
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from podracer.models import PodcastSummary

DEFAULT_DATASET = Path("eval")


@dataclass
class EvalCase:
    episode_id: int
    transcript: str
    tags: list[str] = field(default_factory=list)
    show_notes: str | None = None
    podcast_description: str | None = None
    episode_title: str | None = None
    podcast_title: str | None = None
    # The summary prod stored for this episode: frozen upstream inputs (speaker
    # key, chapter list) for the downstream evals, and the labels seed.
    prod_summary: PodcastSummary | None = None
    # Hand-corrected speaker names: {"speakers": [{"name": ..., "labels": ["SPEAKER_00", ...]}]}
    labels: dict | None = None

    @property
    def case_dir_name(self) -> str:
        return str(self.episode_id)


def manifest_path(root: Path) -> Path:
    return root / "manifest.json"


def load_manifest(root: Path) -> list[dict]:
    path = manifest_path(root)
    if not path.exists():
        raise FileNotFoundError(f"no manifest at {path}")
    data = json.loads(path.read_text())
    episodes = data["episodes"] if isinstance(data, dict) else data
    for e in episodes:
        e.setdefault("tags", [])
    return episodes


def case_dir(root: Path, episode_id: int) -> Path:
    return root / "data" / str(episode_id)


def write_case(root: Path, episode_id: int, payload: dict) -> Path:
    d = case_dir(root, episode_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "case.json"
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False))
    labels = d / "labels.json"
    if not labels.exists() and payload.get("summary"):
        # Seed from prod's speaker list; the human corrects it. Seeding means
        # day one measures agreement with prod, not truth — see eval/README.md.
        summary = PodcastSummary.model_validate(payload["summary"])
        labels.write_text(json.dumps({
            "seeded_from": "prod_summary",
            "verified": False,
            "speakers": [
                {"name": s.name, "role": s.role,
                 "labels": [part.strip() for part in s.label.split(",") if part.strip()]}
                for s in summary.speakers
            ],
        }, indent=1, ensure_ascii=False))
    return path


def load_case(root: Path, entry: dict) -> EvalCase:
    episode_id = int(entry["id"])
    d = case_dir(root, episode_id)
    path = d / "case.json"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} missing — run `python -m podracer.evals fetch` to pull it from prod")
    payload = json.loads(path.read_text())
    prod_summary = PodcastSummary.model_validate(payload["summary"]) if payload.get("summary") else None
    labels_path = d / "labels.json"
    labels = json.loads(labels_path.read_text()) if labels_path.exists() else None
    return EvalCase(
        episode_id=episode_id,
        transcript=payload["transcript"],
        tags=list(entry.get("tags", [])),
        show_notes=payload.get("show_notes"),
        podcast_description=payload.get("podcast_description"),
        episode_title=payload.get("episode_title"),
        podcast_title=payload.get("podcast_title"),
        prod_summary=prod_summary,
        labels=labels,
    )


def load_dataset(root: Path = DEFAULT_DATASET, ids: list[int] | None = None) -> list[EvalCase]:
    entries = load_manifest(root)
    if ids:
        wanted = set(ids)
        entries = [e for e in entries if int(e["id"]) in wanted]
        missing = wanted - {int(e["id"]) for e in entries}
        if missing:
            raise ValueError(f"episode ids not in manifest: {sorted(missing)}")
    return [load_case(root, e) for e in entries]


CASE_FILES = ("case.json", "labels.json")


def pack_cases(root: Path, zip_path: Path) -> list[int]:
    """Zip manifest.json plus every manifest episode's case files into zip_path."""
    entries = load_manifest(root)
    packed: list[int] = []
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(manifest_path(root), "manifest.json")
        for e in entries:
            episode_id = int(e["id"])
            d = case_dir(root, episode_id)
            if not (d / "case.json").exists():
                raise FileNotFoundError(f"{d / 'case.json'} missing — fetch it before packing")
            for name in CASE_FILES:
                if (d / name).exists():
                    zf.write(d / name, f"data/{episode_id}/{name}")
            packed.append(episode_id)
    return packed


def unpack_cases(zip_path: Path, root: Path, *, manifest: bool = True) -> list[int]:
    """Extract a pack into root (data/<id>/...; the manifest too unless told not to).
    Only the expected member names are extracted, so a foreign zip cannot write
    outside root."""
    ids: set[int] = set()
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            parts = member.split("/")
            if member == "manifest.json":
                if manifest:
                    manifest_path(root).parent.mkdir(parents=True, exist_ok=True)
                    manifest_path(root).write_bytes(zf.read(member))
                continue
            if len(parts) == 3 and parts[0] == "data" and parts[1].isdigit() and parts[2] in CASE_FILES:
                d = case_dir(root, int(parts[1]))
                d.mkdir(parents=True, exist_ok=True)
                (d / parts[2]).write_bytes(zf.read(member))
                ids.add(int(parts[1]))
    return sorted(ids)
