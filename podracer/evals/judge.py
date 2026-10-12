"""LLM judge: pointwise rubric grading of one eval item against its source.

The judge is any :class:`Backend` (so it can be a Claude model via the
anthropic backend, or a non-Anthropic model via OpenRouter to check for
same-family bias). It gets the source material the generating model saw, the
instructions that model was given, and the candidate output, and returns one
1-5 score per rubric dimension plus a list of concrete issues.

The source block carries ``cache_control`` so repeated runs on the same
episode (and the full-transcript evals of one episode, which share a source)
reuse the cached transcript on the anthropic backend; other backends flatten
the blocks. A cache hit needs an identical prefix, so the system prompt is the
same for every eval and the per-eval rubric goes in the user block *after* the
source.
"""
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from podracer.evals import logger
from podracer.summarize import Backend, ChatResult, _chat

_JUDGE_ATTEMPTS = 3

JUDGE_SYSTEM = """\
You are a strict, calibrated evaluator of podcast-processing output.

You will be given SOURCE MATERIAL (a transcript, or a transcript segment, \
plus any show notes), the INSTRUCTIONS a model was given, and the CANDIDATE \
OUTPUT that model produced from the source. Grade the candidate strictly \
against the source material: a claim, number, name, date or quote that the \
source does not support counts against faithfulness even if it is true in \
the world. Judge what the instructions asked for, not what you would have \
asked for.

Score each listed dimension from 1 to 5:
- 5: you could not improve it; no flaws you would mention.
- 4: good; minor flaws a careful reader would notice.
- 3: usable, with clear flaws.
- 2: poor; a reader would be misled or badly underserved.
- 1: unusable.
Most competent outputs land at 3 or 4. Reserve 5 for genuinely flawless work \
and give it readily when earned; do not drift toward the middle.

For "issues", list concrete problems you found, each as one short line that \
quotes or pinpoints the offending text (an unsupported claim, a wrong name, \
a missed topic, a boundary in the wrong place). An empty list means you \
looked and found none. "overall" is your 1-5 holistic grade of the output \
for its intended use, not an average of the dimensions.

Return exactly one score per requested dimension, using the dimension names \
verbatim."""

# One rubric per prompt. Dimension names are what the judge must echo back.
RUBRICS: dict[str, dict[str, str]] = {
    "speakers": {
        "correctness": "Each identified name is the right person for its SPEAKER label(s), spelled as "
                       "in the show notes when they are provided (the transcript often misspells names).",
        "completeness": "Every real speaker with lines in the transcript is identified; no invented people; "
                        "roles/titles are right when the source states them.",
        "merging": "Labels that belong to one person are merged into one entry; distinct people are not "
                   "merged together; advertisement/sponsor voices are marked as advertiser.",
        "evidence": "The cited timestamp and quote actually support the identification.",
    },
    "summary": {
        "faithfulness": "Every statement is supported by the transcript; nothing invented, no numbers or "
                        "claims the speakers did not make; ads ignored.",
        "coverage": "The major topics of the whole episode are represented, not just the first half or "
                    "the loudest thread.",
        "insight": "Conveys the arguments and why they matter (the reasoning, the stakes), not a "
                   "'they talked about X' recap.",
        "attribution": "Views and arguments are credited to the right named speaker.",
        "writing": "Concise, well organized, within the requested length; synthesized in its own "
                   "words rather than echoing the transcript.",
    },
    "chapters": {
        "boundaries": "Each chapter starts where its topic actually begins in the transcript; "
                      "timestamps are accurate.",
        "coverage": "Chapters span the whole episode from start to finish with no major section skipped.",
        "granularity": "Segmentation follows natural topic transitions: not splitting one thread into "
                       "many slivers, not lumping distinct topics together.",
        "titles": "Titles are specific and descriptive of the segment; the 1-2 sentence summaries are "
                  "accurate.",
        "ads_and_teaser": "No chapters for ad/sponsor reads; if the episode opens with a teaser montage "
                          "it is one chapter titled 'Teaser'. Score 5 when the episode has neither "
                          "and the output correctly creates nothing for them.",
    },
    "chapter_detail": {
        "faithfulness": "Everything attributed to the speakers is in the segment; no invented numbers, "
                        "dates, prices, named studies or quotes.",
        "substance": "A reader learns what was actually argued and why, in enough depth to not need "
                     "to listen; not a vague 'they discussed X'.",
        "asides": "Any definitional asides (background the speakers did not explain) are correct and "
                  "phrased so the reader can tell they are the writer's explanation, not the "
                  "speakers' words. Score 5 if there are none and none were needed.",
        "concision": "Length matches the substance of the segment: 150-300 words for a real chapter, "
                     "shorter for a thin one; no padding, no repetition.",
    },
    "highlights": {
        "faithfulness": "Each highlight is something a speaker actually said at roughly that timestamp; "
                        "no invented figures or claims.",
        "specificity": "Highlights carry the concrete particulars (numbers, thresholds, names, "
                       "conditions) rather than generalizing them away.",
        "coverage": "Highlights are spread evenly across the whole episode in proportion to its "
                    "substance; dense technical stretches are not under-covered.",
        "attribution": "Each highlight is credited to the speaker who actually said it.",
        "kinds_and_dedup": "'takeaway' vs 'opinion' labels fit; no point is listed twice; all major "
                           "speakers are represented.",
    },
}


class DimensionScore(BaseModel):
    dimension: str
    score: int
    rationale: str


class Verdict(BaseModel):
    scores: list[DimensionScore]
    issues: list[str]
    overall: int


@dataclass
class Judgement:
    scores: dict[str, int]
    overall: int
    issues: list[str]
    rationales: dict[str, str]
    input_tokens: int | None
    output_tokens: int | None


class JudgeError(RuntimeError):
    """The judge never returned a usable verdict."""


def _clamp(v: int) -> int:
    return max(1, min(5, int(v)))


def judge_blocks(source: str, instructions: str, candidate: str, rubric: dict[str, str]) -> list[dict]:
    """The judge's user content: the (cacheable) source first, then the task
    and the rubric for this eval."""
    dims = "\n".join(f"- {name}: {desc}" for name, desc in rubric.items())
    return [
        {"type": "text", "text": f"SOURCE MATERIAL:\n\n{source}", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": (
            f"INSTRUCTIONS THE MODEL WAS GIVEN:\n\n{instructions}\n\n"
            f"CANDIDATE OUTPUT:\n\n{candidate}\n\n"
            f"Grade the candidate on exactly these dimensions:\n{dims}"
        )},
    ]


def judge(backend: Backend, eval_name: str, source: str, instructions: str, candidate: str) -> Judgement:
    rubric = RUBRICS[eval_name]
    dims = list(rubric)
    blocks = judge_blocks(source, instructions, candidate or "(empty output)", rubric)
    schema = Verdict.model_json_schema()
    last: str | None = None
    for attempt in range(_JUDGE_ATTEMPTS):
        result: ChatResult = _chat(backend, JUDGE_SYSTEM, blocks, schema, repair=attempt == _JUDGE_ATTEMPTS - 1)
        try:
            verdict = Verdict.model_validate_json(result.content)
        except ValidationError as e:
            last = f"invalid json: {e.errors()[0]['msg'] if e.errors() else e}"
            logger.warning("judge_invalid_output", attempt=attempt + 1, reason=last)
            continue
        scores = {s.dimension.strip().lower(): _clamp(s.score) for s in verdict.scores}
        missing = [d for d in dims if d not in scores]
        if missing:
            last = f"missing dimensions {missing}"
            logger.warning("judge_missing_dimensions", attempt=attempt + 1, missing=missing)
            continue
        return Judgement(
            scores={d: scores[d] for d in dims},
            overall=_clamp(verdict.overall),
            issues=[i.strip() for i in verdict.issues if i.strip()],
            rationales={s.dimension.strip().lower(): s.rationale for s in verdict.scores},
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )
    raise JudgeError(f"judge failed after {_JUDGE_ATTEMPTS} attempts: {last}")
