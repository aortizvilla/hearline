---
name: check-accuracy
description: Audit Hearline episodes for accuracy and format - unsupported claims, repeated stories or sentences, missing data points, stock lines, background that names Wikipedia or is off-topic, wrong transitions. Use when asked to check, verify or review the quality or accuracy of generated episodes or scripts.
---

# Check accuracy

Hearline checks quality in four places (see `hearline/backend/app/pipeline/README.md`):

| Where | What | Code |
|---|---|---|
| Each script (step 3) | Format: length, URLs, markdown, source named, data points as digits, no stock lines ("That matters because"), background never names Wikipedia, no English left in other languages | `local_issues()` in `app/pipeline/step3_scripts.py` |
| Each script (step 3) | Accuracy: claim-by-claim fact-check against the story record, evidence re-checked in code, second opinion on rejection | `verify_request()`, `judge()`; `prompts/verify_system.txt` |
| Each phrase (step 5) | Greeting uses the name, teaser length, no URLs; rundown and transitions are fixed wording | `phrase_issues()` in `app/pipeline/step5_phrases.py` |
| Each episode (before recording) | The whole episode: same news told twice, claims not in the record (teasers too), background not explaining its story, contradictions. Flagged segments are removed and the episode re-planned | `app/pipeline/review.py`; `prompts/review_system.txt` |

## Steps

1. From `hearline/backend/`, audit recent episodes (no model calls, free):
   `.venv/bin/python -m app.cli check-episodes --days 1` (or `--episode ID`).
   It re-runs every format rule on each segment, checks one subject per episode and
   background placement, and prints what the accuracy review found and removed
   (`fact_episode.quality`).
2. For any FAIL, or when the user doubts a specific episode, read its transcript:
   `.venv/bin/python -c "from sqlmodel import Session; from app.database.db import make_engine; from app.config import get_settings; from app.database.models import Episode; from app.pipeline.step4_assemble import episode_transcript; s=Session(make_engine(get_settings().database_url)); print(episode_transcript(s, s.get(Episode, ID)))"`
   and compare each segment with its story (`dim_story.summary`, `headline`) yourself:
   every number, name, date and cause in the script must be in the story's record.
3. Report per episode: pass or fail, each problem with the segment and a quote, and
   whether it is a rule gap (the checks missed it) or a content problem.
4. For a rule gap, propose the smallest check that would catch it, in the file listed
   above, with a test. Never weaken a check to make an episode pass.
