# Hearline

Personalized podcast generator: turns a user's interests into a scheduled, news-based audio briefing.

The backend contains a **scalable content pipeline** whose cost grows with the number of distinct
topics and stories, not with the number of users. Login is Clerk (`AUTH_MODE=clerk`; local development runs without login). Selecting topics in the Settings UI runs step 1 (classification). Production runs on AWS: see [hearlineCDK/README.md](hearlineCDK/README.md).

## How the pipeline works

```mermaid
---
config:
  layout: elk
---
flowchart TB
    USER(["User settings<br/>topics · preferences<br/>PUT /api/profile"]):::trigger
    CRON(["Daily scheduler"]):::trigger

    subgraph SHARED["SHARED · runs per topic and story, not per user"]
        S1["<b>1 · Topics</b><br/>alias → embedding → LLM<br/><i>when a user picks a topic</i>"]:::llm
        WEB{{"<b>Internet</b><br/>web search"}}:::search
        S2["<b>2 · Research</b><br/>5-12 stories · plural sources · relevance + link checks<br/>Wikipedia background · local-language outlets<br/><i>once per topic per day, per language in use</i>"]:::llm
        S3["<b>3 · Scripts</b><br/>write · adapt · fact-check<br/><i>text per story × tone × language</i><br/><i>audio per voice</i>"]:::llm
    end

    subgraph PERUSER["PER USER · free once phrases are cached"]
        S4["<b>4 · Assemble</b><br/>due topics · unheard stories<br/>rank · fit target minutes<br/><i>once per user per day</i>"]:::local
        S5["<b>5 · Phrases</b><br/>greeting by first name · rundown teasers · transitions · outro<br/><i>only if not cached: once per phrase × voice</i>"]:::llm
        S6["<b>6 · Audio</b><br/>stitch · level loudness · MP3<br/><i>once per episode</i>"]:::local
    end

    TTS{{"<b>ElevenLabs</b><br/>text-to-speech · Flash v2.5<br/><i>paid per character</i>"}}:::tts
    T[("dim_topic<br/>bridge_user_topic")]:::dim
    PR[("dim_profile<br/>tone · language · voice<br/>minutes · delivery time")]:::dim
    ST[("dim_story")]:::dim
    CL[("dim_story_clip")]:::dim
    PH[("dim_phrase_clip")]:::dim
    EP[("fact_episode<br/>fact_episode_story")]:::fact

    USER -->|topics| S1 -->|topic + keyword filter| T
    USER -->|preferences| PR
    PR -->|tone · language · voice in use| S3
    PR -->|minutes · tone · language · voice| S4
    T -->|subscribed topics| S2
    WEB <-->|queries · articles + URLs| S2
    S3 <-->|script → speech| TTS
    S5 <-->|phrase → speech| TTS
    CRON ==> S2
    S2 ==>|new stories| ST
    ST ==>|one job per story × setting| S3
    S3 ==>|verified scripts + audio| CL
    CL ==>|step 3 done → assemble| S4
    EP -.->|14-day heard ledger| S4
    S4 ==>|phrases needed in this tone · language · voice| S5
    S5 ==>|recorded phrases| PH
    PH ==>|phrase audio| S6
    S4 -->|running order + story clips| S6
    S6 ==> EP

    subgraph LEGEND["Legend"]
        direction LR
        L1(["trigger"]):::trigger
        L2["LLM"]:::llm
        L3{{"internet"}}:::search
        L4{{"ElevenLabs (TTS)"}}:::tts
        L5["local, free"]:::local
        L6[("dim_ table")]:::dim
        L7[("fact_ table")]:::fact
        A1[" "]:::blank ==>|main flow| A2[" "]:::blank -->|side input| A3[" "]:::blank -.->|feedback| A4[" "]:::blank
    end

    classDef trigger fill:#fef3c7,stroke:#b45309,stroke-width:2px,color:#1f2937
    classDef llm fill:#dbeafe,stroke:#1d4ed8,stroke-width:2px,color:#1f2937
    classDef search fill:#ede9fe,stroke:#6d28d9,stroke-width:2px,color:#1f2937
    classDef tts fill:#ffedd5,stroke:#ea580c,stroke-width:2px,color:#1f2937
    classDef local fill:#dcfce7,stroke:#15803d,stroke-width:2px,color:#1f2937
    classDef dim fill:#ccfbf1,stroke:#0f766e,stroke-width:2px,color:#1f2937
    classDef fact fill:#ffe4e6,stroke:#be123c,stroke-width:2px,color:#1f2937
    classDef blank fill:none,stroke:none
    style SHARED fill:#f8fafc,stroke:#64748b,stroke-dasharray:4 3
    style PERUSER fill:#f0fdf4,stroke:#15803d,stroke-dasharray:4 3
    style LEGEND fill:#ffffff,stroke:#cbd5e1
```

**Legend:** yellow = trigger, blue = LLM, purple hexagon = internet (web search), orange hexagon = ElevenLabs voice (TTS; Polly also supported), green = local and free, teal cylinder = `dim_` table (things described), rose cylinder = `fact_` table (events that pile up). Italics = how often the step runs. Thick arrows = the main flow, run in order (2 → 3 → 4 → 5 → 6); thin arrows = side inputs; dotted = feedback. Every paid call (steps 1, 2, 3, 5) is logged to `fact_cost_event`.

| Step | Module | Runs | LLM (prompt in `hearline/backend/prompts/`) | What it does |
|---|---|---|---|---|
| 1 Topics | `app/pipeline/step1_topics.py` | when a user selects a topic (Settings, `PUT /api/profile`) | `SMALL_MODEL`, only if no alias/embedding match (`topic_resolve.txt`) | Classifies every selected topic (catalog slug or free text) into a shared topic: exact alias, then embedding similarity, then **one** cheap LLM call (`same_as` / `child_of` / `new`). Every decision is stored as an alias, so a phrase never costs twice. Niche interests become keyword filters on a broad topic. Catalog topics are seeded aliases, so selecting them is free. Subscriber counts are refreshed right away, so step 2 picks the topic up on its next run. Settings offers 30 topics (`config/interests.yaml`), and each suggests its own subcategories as one-tap chips (`config/subtopics.yaml`: 5 per topic, e.g. History > World War II, Ancient Rome; loaded into `dim_subtopic` marked `suggested`). Listeners can pick up to 5 **subcategories** per topic or type their own (Film > "Cannes Festival"): each is classified into a shared `Subtopic` of that topic (known phrase → embedding → one small call, `subtopic_resolve.txt`, which merges synonyms and translations and rejects phrases that don't fit). Stories are tagged with the subtopics they mention, research is told which ones listeners follow, and matching stories rank 2× higher (a focus, not a filter). |
| 2 Research | `app/pipeline/step2_research.py` | per active topic per day, in English + each local language its listeners use | `RESEARCHER_MODEL` + web search (`research_system.txt`) | Finds 5-12 recent stories, **plural in sources** (at most 2 per outlet; other outlets reporting a story are kept and named in its script). Also researched **per language** in that language's own outlets (`research.languages`: e.g. El País, Clarín for Spanish; told today's English headlines so it adds local news); listeners get English stories plus their own language's, which rank 1.5x. Checks: one small-model **relevance** call drops off-topic stories; a story whose other sources include a known story is a duplicate; links must be a single article (not a section/tag page) and alive. Headlines and summaries are written in neutral language (loaded source wording is not copied). When an episode is put together, outlets are weighted by `research.source_weights` (news agencies and public broadcasters 1.2×; demotions are an editorial choice left to config), and the same event reported under two topics by different outlets is heard once (headline-embedding match across topics). For lesser-known names it adds **Wikipedia background** (free, in the story's language) that scripts may use to explain. Inline citations stripped, deduped by URL and meaning. Saved as `Story` (+ `language`, `sources`, `background`). |
| 3 Scripts | `app/pipeline/step3_scripts.py` | per story × tone × language (text), per voice (audio) | `WRITER_MODEL` writes the base (`script_system.txt`), `SMALL_MODEL` adapts (`adapt_system.txt`) and checks (`verify_system.txt`) | One base script per story is written from the stored story fields only; every other tone and language is adapted from a fact-checked script of the same story by the small model (`clips.strategy: base_adapt`, about 58% cheaper than writing each one, see [pipeline/README.md](hearline/backend/app/pipeline/README.md)). Voices share the words. Local checks run (length, no URLs/markdown, source attribution), then the checker verifies every claim **one by one with evidence**: code re-checks that each quoted piece of evidence really is in the story record, and that anything the script attributes to an outlet comes from what the outlet reported (headline/summary), never from the podcast's own `why_it_matters` framing or Wikipedia background. A rejection gets a **second opinion** from `WRITER_MODEL` that rules on each objection; it is overturned only if every objection is ruled invalid (`clips.second_opinion`). A failing script is rewritten once; if it still fails it is stored as `verified=false` and never used. Only verified scripts are recorded (TTS). Stored in `StoryClip`. |
| 4 Assemble | `app/pipeline/step4_assemble.py` | per user per day | none | Picks unseen stories (14-day ledger), skips filler (`min_importance`), ranks them by importance × weight × recency and fills each topic up to the minutes the user chose for it (at least one story per topic). On a slow news day it fills, in order: unheard stories up to 4 days old (`backfill_hours`; recency keeps today's news first), then up to 2 "Some context:" **explainers** made from the Wikipedia background research stored (status `explainer`, source Wikipedia, fact-checked, shared, never repeated, never teased in the rundown); otherwise the episode is shorter, never padded with invented content, then runs steps 5 and 6 and saves the episode, ledger, transcript and sources. `--dry-run` stops before audio. |
| 5 Phrases | `app/pipeline/step5_phrases.py` | per phrase × voice, once | `SMALL_MODEL` (`phrase_system.txt`) | Greeting, a "coming up" rundown, transitions and outro. One call writes every missing phrase of an episode; each is recorded once and shared by all listeners. Only the rundown's teasers mention news: one short line per story (`teaser:story:<id>`), shared by everyone who gets that story, so the rundown costs nothing per user. A placeholder name ("Listener") is greeted without a name. Stored in `PhraseClip`. |
| 6 Audio | `app/pipeline/step6_audio.py` | per episode | none | Joins the already-recorded phrases and story audio, normalizes loudness and exports MP3 with ID3 tags. |
| Jobs | `app/jobs/` | continuously | DB-backed queue (`worker.py`) with retries, backoff, a dead-letter state, idempotency keys, per-type concurrency limits and a daily USD budget cap. `scheduler.py` holds the daily plan and job handlers. |

Every billable call is written to `CostEvent` (tokens, searches, TTS characters, USD from `config/pricing.yaml`).

## Plans

Three subscription plans, defined in [`hearline/backend/config/plans.yaml`](hearline/backend/config/plans.yaml) (limits and prices are edited there, no code change). Each limit is a cost lever:

| | Free | Plus | Pro |
|---|---|---|---|
| Price (shown only, no billing yet) | $0 | $4.99/mo | $9.99/mo |
| Topics | 2 | 8 | all |
| Minutes per topic / per episode | 5 / 15 | 10 / 30 | 20 / 60 |
| Voices | 2 shared (aria, leo) | all | all |
| Tones | 1 shared (conversational) | all | all |
| Focus subcategories | no | yes | yes |

Free listeners share two voices and one tone, so their stories are recorded at most twice per language and reused by every Free listener (the biggest saving). New users start on Free; a downgrade trims their Settings to fit (first topics kept, minutes shortened, voice/tone switched). Step 4 also caps each episode by plan.

Data model: `dim_user.plan` is the current plan; `fact_subscription` keeps one row per period on a plan (status, dates, price, source, and a billing reference for when billing exists). There is no billing yet: an admin changes plans (`PUT /api/admin/users/{id}/plan` or `python -m app.cli set-plan <user_id> <plan>`), or users switch themselves on the Subscription page in dev / with `PLAN_SELF_SERVE=true`.

## Prerequisites

- Python 3.11+
- Node.js 20+ and npm
- ffmpeg (MP3 export): `brew install ffmpeg` / `apt install ffmpeg`. Without it, episodes are exported as WAV.

## Setup

```bash
cp .env.example hearline/backend/.env        # then fill in keys and model names
cp hearline/frontend/.env.example hearline/frontend/.env   # optional: Clerk publishable key (login)

cd hearline/backend
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pip install -e ".[embeddings]"       # local sentence-transformers (needs PyTorch)
#   or: pip install -e ".[embeddings-onnx]"  # same MiniLM model on ONNX (fastembed), e.g. Intel Macs
pip install -e ".[openai]"           # only if LLM_PROVIDER=openai
python -m app.cli migrate            # create / upgrade the database (also runs on startup)
python -m app.cli voice-previews     # record each voice's Settings sample per language (once; ~$0.16 on ElevenLabs)

cd ../frontend
npm install
```

## Run

```bash
# Everything at once, shaped like production (Postgres + migrate + API + worker; needs Docker)
docker compose up --build

# ...or each piece by hand (SQLite):
# Backend API (http://localhost:8000, docs at /docs)
cd hearline/backend && uvicorn app.main:app --reload

# Frontend: the product at http://localhost:5173, the admin dashboard at http://admin.localhost:5173
cd hearline/frontend && npm run dev

# Content pipeline worker: runs the daily plan every hour (scheduler.plan_every_minutes) and
# processes the queued research / clip / assemble jobs. The only process the pipeline needs.
cd hearline/backend && python -m app.jobs.worker
```

### Pipeline CLI (`cd hearline/backend`)

```bash
python -m app.cli check                              # free: API keys, models, prices, voices, ffmpeg
python -m app.cli research --topic ai [--force]      # step 2 for one topic (--force: no subscriber count needed)
python -m app.cli clips --story 12                   # step 3 for one story (scripts + audio, subscriber combos)
python -m app.cli assemble --user 3 [--dry-run]      # steps 4-6 (dry run: transcript only, no audio, no episode)
python -m app.cli run-daily [--immediate]            # plan the day + process due jobs
python -m app.cli costs --days 7                     # cost by stage / per episode / per active user
python -m app.cli check-episodes --days 1            # audit stored episodes: format + accuracy rules (free)
python -m app.cli demo                               # dev: 4 demo listeners (en/es) through the real pipeline
python -m app.cli demo --email ana@example.com --language es --topics technology,world   # dev: one listener
```

Global flags: `--mock` (offline mock LLM + TTS), `--db URL`, `-v`. Any `.env` value can be overridden per command, e.g. `TTS_PROVIDER=say python -m app.cli ...` for free local voices while testing.

The SQLite file runs in WAL mode: recent writes sit in `hearline.db-wal` until checkpointed. Viewers that open only `hearline.db` can look empty; use the `sqlite3` CLI, or run `sqlite3 storage/hearline.db "pragma wal_checkpoint(TRUNCATE);"` first. Where the text lives: stories in `dim_story`, story scripts in `dim_story_clip.script`, greeting/transitions/outro in `dim_phrase_clip.text`, full episode transcript in `fact_episode`.

If you change `models.py`, add a migration: `cd hearline/backend && alembic revision --autogenerate -m "what changed"`, read the generated file in `app/database/migrations/versions/`, then `python -m app.cli migrate`. A test fails if the models and migrations disagree.

**Postgres** (needed for more than one worker machine): `pip install -e '.[postgres]'` and set `DATABASE_URL=postgresql+psycopg://user:password@host:5432/hearline`. Job claiming then uses `FOR UPDATE SKIP LOCKED`, and the daily plan uses `INSERT ... ON CONFLICT DO NOTHING`.

## Static demo (Cloudflare, free)

A server-free demo of the frontend: the demo listeners' profiles, episodes and audio are saved
as files and the site reads them instead of the API. No database, no worker, no app keys. It
deploys from a private repo, and anyone with the link can open it.

1. Refresh the snapshot after changing the demo episodes (it reads the local store):
   `cd hearline/backend && python -m app.cli export-demo` writes `hearline/frontend/demo-data/`
   (about 17 MB: only the `example.com` demo listeners from `app/dev/demo.py`). Commit it.
   Only demo builds include it; the production build leaves it out.
2. Free Cloudflare account > **Workers & Pages** > **Create** > **Import a repository**: pick
   this GitHub repo (Cloudflare asks GitHub for access to it; private repos work).
3. Build settings:

   | Setting | Value |
   |---|---|
   | Project name | `hearline-demo-7f3k` (must match `name` in `hearline/frontend/wrangler.jsonc`) |
   | Build command | `npm run build` |
   | Deploy command | `npx wrangler deploy` |
   | Root directory | `hearline/frontend` |
   | Build variables | `VITE_STATIC_DEMO` = `1`, `NODE_VERSION` = `22` |

   `wrangler.jsonc` publishes `dist/` as static files (no Worker code) and serves `index.html`
   for app paths like `/settings`.
4. **Deploy**. Every push to `main` then rebuilds the site, at
   `https://hearline-demo-7f3k.<your-subdomain>.workers.dev` (shown in the Cloudflare dashboard).
   Share that link.
5. Optional, to limit it to people you name: Cloudflare Zero Trust > Access > add an application
   for the site's address with the emails allowed (free up to 50 users; they get a
   one-time code by email).

Sign in with a demo email (`sofia@example.com`, `lucia@example.com`, `mateo@example.com`,
`emma@example.com`, `james@example.com`). What a visitor changes stays in their browser. The
private podcast feed needs the full app, and the admin dashboard is a snapshot.
Try it locally: `cd hearline/frontend && VITE_STATIC_DEMO=1 npm run dev`.

**Keys never reach git**: app keys live only in `hearline/backend/.env` (git-ignored; the repo
has `.env.example` with empty values). The demo needs no keys, and Cloudflare reads the repo
through its own GitHub connection: no token is stored anywhere.

## Test

```bash
cd hearline/backend && pytest && ruff check .
cd hearline/frontend && npm run build && npm test
hearlineCDK/cdk/.venv/bin/python -m pytest -q hearlineCDK/cdk
```

Pipeline tests use a mock LLM, mock TTS and hashing embeddings, so they need no network or API keys.
Frontend tests (Vitest + Testing Library) mock the API client. CI (`.github/workflows/ci.yml`)
runs all three on every push once the repo is on GitHub.

## Folder map

| Path | Purpose |
|---|---|
| `hearline/backend/app/main.py` | FastAPI entry point (`uvicorn app.main:app`) |
| `hearline/backend/app/cli.py` | Pipeline command line (`python -m app.cli`) |
| `hearline/backend/app/config.py` | Every setting: `.env` (keys, models, providers) and typed `config/*.yaml` loaders |
| `hearline/backend/app/api/` | `routes.py`: `/api` endpoints for the frontend (profile, interests, episodes); `feed.py`: each listener's private podcast feed (secret link, RSS + iTunes tags, audio links that redirect to fresh signed URLs); `auth.py`: Clerk token check behind `current_user()`; `admin.py`: the admin dashboard; `webhooks.py`: the Clerk webhook (identity sync, erasure) |
| `hearline/backend/app/accounts.py`, `plans.py` | Deleting a listener and everything that is theirs; subscription plans and their limits |
| `hearline/backend/app/database/` | `db.py` (engine, sessions) and `models.py` (UI tables + `Topic`, `TopicAlias`, `UserTopic`, `Story`, `StoryClip`, `EpisodeStory`, `CostEvent`, `Job`) |
| `hearline/backend/app/pipeline/` | **What** gets made, numbered in run order: `step1_topics.py` → `step2_research.py` → `step3_scripts.py` → `step4_assemble.py` → `step5_phrases.py` → `review.py` (accuracy review of the whole episode) → `step6_audio.py`. Quality checks at every level: `app/pipeline/README.md`. Each folder in `app/` has a short README. |
| `hearline/backend/app/jobs/` | **When** it runs: `worker.py` (queue + worker) and `scheduler.py` (daily plan + job handlers) |
| `hearline/backend/app/providers/` | **Who** does the work, swappable: `llm.py` (Anthropic, OpenAI), `mock_llm.py`, `embeddings.py`, `vectors.py` (in-Python cosine; swap for pgvector), `tts.py` (mock, macOS `say`, Amazon Polly, ElevenLabs), `storage.py` (audio files: local disk or S3 with signed links) |
| `hearline/backend/app/tracking/` | **How it's doing**: `costs.py` (CostEvent pricing, daily budget, cost report), `kpis.py` (daily facts), `dashboard.py` (admin KPIs: cost per episode, generation time, engagement), `alerts.py` (failed jobs, budget cap), `metrics.py` (cache hit counters) |
| `hearline/backend/storage/` | Local data, not committed. `hearline.db` holds the **records** (users, profiles, topics, stories, episode details, costs, jobs). `audio/` holds the **audio files**: `clips/` (shared story clips) and `users/<user_id>/episodes/<episode_id>.mp3`. `archive/` holds old fact rows as dated files for analytics. |
| `hearline/backend/app/dev/` | Dev tooling: `demo.py` (demo listeners through the real pipeline, in the one store), `static_demo.py` (their app as static files for the demo site) |
| `hearline/backend/config/` | `pipeline.yaml` (tuning, including the web search settings), `pricing.yaml`, `seed_topics.yaml` (52 topics), `subtopics.yaml` (suggested subcategories per topic), `voices.yaml` (the voice catalog: Settings labels + provider voices), plus the UI's `interests.yaml`, `options.yaml` (languages), `default_profile.yaml` |
| `hearline/backend/prompts/` | System prompts: `topic_resolve` + `subtopic_resolve` (step 1), `research_system` + `relevance_system` (step 2), `script_system` + `adapt_system` + `verify_system` (step 3), `phrase_system` (step 5), `review_system` (episode review); `tones.yaml` (4 tone presets) |
| `hearline/backend/app/database/migrations/` | Alembic database migrations (`0001` baseline, then one per model change); `app/database/migrate.py` applies them |
| `hearline/backend/Dockerfile`, `docker-compose.yml` (repo root) | One backend image for API, worker and migrations; a local Postgres stack shaped like production |
| `hearlineCDK/` | AWS as code (CDK): one EC2 server (API, worker, Postgres) behind CloudFront, S3, secrets in SSM; `deploy.sh` is the one command; `cdk/test_stack.py` checks the stack offline |
| `hearline/frontend/src/` | React app: `pages/` (landing, home, topic, settings, subscription), `admin/` (the dashboard, its own page), `auth.tsx` (Clerk sign-in when `VITE_CLERK_PUBLISHABLE_KEY` is set; email sign-in in dev), `staticDemo.ts` (the server-free demo) |
| `hearline/frontend/demo-data/` | The static demo snapshot (`app.cli export-demo`), only in demo builds |
| `.github/workflows/` | `ci.yml`: backend tests + lint, frontend build + tests, stack test (the demo site is built by Cloudflare Pages from the repo) |

## Proof of scale (mocked LLM/TTS, 200 users)

Measured on 2026-11-22 with a since-removed simulation of 200 mock users (mock LLM/TTS, real local embeddings, real model prices):

| Metric | Value |
|---|---|
| Users / episodes generated | 200 / 194 (6 inactive users skipped) |
| Subscriptions (user × topic) → distinct topics | 613 → 29 |
| Research calls (shared) vs. one per subscription | 29 vs. 613 |
| Clips generated / used in episodes / prefetched but unused | 938 / 881 / 57 |
| Clip slots in episodes / served by reuse | 2,298 / 1,417 (62% reuse) |
| Total estimated cost / per episode | $13.41 / **$0.069** |
| Per-user marginal cost (intro/outro + its TTS) | $0.0067 per episode |

Cost is dominated by clip TTS and writing, and those scale with stories × voice combinations, not with users.

## Roadmap

Done: the shared, cached content pipeline; Settings wired to `bridge_user_topic` with free-text
interests, subcategories and scheduling; web player and a private podcast feed per listener;
the admin dashboard (separate app at `admin.<domain>`); plans (Free / Plus / Pro, without
billing); the landing page with the product film; AWS deployment as code (one server, CDK,
[hearlineCDK/](hearlineCDK/), not deployed yet).

Next:
1. **Deploy** (hearlineCDK/README.md, *Status*): domain, Clerk production instance, AWS credentials,
   `./deploy.sh`.
2. **Billing** for Plus and Pro (Stripe or Clerk Billing; `fact_subscription` already has
   `provider` / `provider_ref`).
3. **Legal**: privacy policy and terms (EU listeners: GDPR; account deletion is done).
4. A "your briefing is ready" notification; a first git commit (CI is ready in `.github/`).
5. **Mobile app** (iOS / Android on the same API: background playback, offline downloads, a
   push when the briefing is ready).
6. **Memory across episodes**: story arcs followed over days (a weekly catch-up of the arcs you
   missed), callbacks to what you already heard, and no repeated content: a story you heard
   comes back only as a one-line update. Built on the listening ledger and `update_of_id`.
7. **Language-learning mode**: a story in your language, then in the one you're learning,
   slower, with three key words at the end (reuses the shared per-language clips).
8. **Local layer**: your city's weather, transit and local headlines open the briefing (shared
   per city).
9. **Ratings** per episode and story, next to the listening data already kept.
10. **Learning from what works**: ratings and listening signals as the reward, first to rank
   stories per listener, then reinforcement learning on generation (scripts, length, tone,
   order). Details in `solution.md`, *Next steps*.

## Assumptions

Existing app:

- Login: `current_user()` in `app/api/routes.py`. With `AUTH_MODE=clerk` it verifies the Clerk session token (RS256, public keys from the Clerk JWKS, `azp` checked against `CLERK_AUTHORIZED_PARTIES`) and maps its `sub` to `User.external_id`, creating the user with the default profile on first sign-in. With `AUTH_MODE=dev` it uses the optional `X-User-Id` header or the single default user. Every request also refreshes `last_active_at` (at most hourly); the daily plan skips users inactive for 14 days. `APP_ENV=production` refuses to start with dev login, SQLite or local audio.
- Local development uses one store: the SQLite file `hearline/backend/storage/hearline.db` and the audio in `hearline/backend/storage/audio/` (`DATABASE_URL` / `AUDIO_DIR` in `.env`). `python -m app.cli demo` adds the demo listeners to it. Its schema is managed by Alembic migrations (`hearline/backend/app/database/migrations/`); a database created before migrations existed is detected and upgraded on startup.
- `.env` is read from `hearline/backend/.env` or the repo root.
- Tailwind v4, React 19, React Router 7. Interest images are Wikipedia thumbnails.
- Tones come from `hearline/backend/prompts/tones.yaml` (presets plus aliases) and languages from `hearline/backend/config/options.yaml`. Voices are defined once in `config/voices.yaml` (6 voices: Aria, Leo, Nova, Kai, Oliver, Iris, each with a style, accent and gender, mapped to an ElevenLabs voice, Polly voices per language and a macOS `say` voice). On startup they're loaded into the `dim_voice` table, which Settings reads through `GET /api/options`; a voice removed from the file is deactivated, not deleted. Each voice has a short preview sample per language in audio storage (`voice-previews`); Settings plays the one for the language selected. Each voice also has a native speaker per language (ElevenLabs Voice Library voices added to the account, listed under `languages` in `voices.yaml`) with its accent, so Settings shows e.g. Spain or Latin American for Spanish, France or Quebec for French, Brazil or Portugal for Portuguese; `check` verifies every one. Recordings are tagged with the provider *and* its voice id, so remapping a voice re-records clips the next time they're needed.
- Episode status values: `pending | generating | ready | failed`.

Content pipeline:

- **Models and providers**: model ids come only from `RESEARCHER_MODEL`, `WRITER_MODEL` and `SMALL_MODEL` (old name `VERIFIER_MODEL` still works), and prices only from `config/pricing.yaml`. `.env.example` uses OpenAI: `gpt-5.4-mini` for research and writing, `gpt-5.4-nano` for checking, topic resolution and phrases. `LLM_PROVIDER=anthropic|openai|mock` switches every LLM stage. The OpenAI provider uses the Responses API with its `web_search` tool; batches go through the OpenAI Batch API (50% off; requests with web search always run directly, so research is never batched) and it has no blocked-domain filter. `pricing.yaml` has prices for the OpenAI models in use; any other model falls back to the conservative `default` entry with a warning (`python -m app.cli check` flags it).
- **Web search tool**: `web_search_20250305` as specified (`research.search` in `config/pipeline.yaml`). `web_search_20260209` (dynamic filtering) is a one-line change for newer models. Anthropic rejects allowed and blocked domain lists together, so `allowed_domains` wins when both are set.
- **Anthropic request features**: static instructions go in the system prompt with `cache_control`. Prompts shorter than the model's minimum cacheable length (about 1-4K tokens) are silently not cached, so current savings are small until prompts grow. Research and writing requests use server-side refusal fallbacks (`refusal_fallbacks: default`); batch requests cannot carry them. `pause_turn` responses from the web search loop are resumed up to 3 times. JSON from structured-output calls (verifier, frame) is still parsed defensively.
- **Batches**: `scheduler.use_batches: true` sends queued research and clip jobs through the Message Batches API (50% price; results within 24h). Clip phases (write, verify, regenerate, verify) become separate batches. A `pause_turn` inside a batch is resumed with a direct call.
- **Embeddings**: the default provider is `sentence-transformers/all-MiniLM-L6-v2`. PyTorch has no wheels for x86_64 macOS (this dev venv), so the same model runs through fastembed/ONNX, and the `hashing` provider is the last-resort fallback (also used in tests). Topic match threshold 0.75 and story dedupe threshold 0.85 were chosen from measured similarities ("AI" vs "artificial intelligence" = 0.79; unrelated topics 0.2-0.35).
- **Topic resolution**: the topic-resolution LLM call uses `SMALL_MODEL` (the cheap one). Seeding stores each topic's key, display name and aliases as zero-cost aliases. With `niche_as_keyword: true`, a `child_of` decision maps the phrase onto the parent topic plus a keyword filter, so no extra research unit is created. The most common subscriber keywords are passed to the shared research call as focus hints.
- **Keyword filters**: empty `keyword_filters` means "all stories of the topic". Subscribing to the broad topic clears filters; a niche subscription only adds a filter when the user is not already broad. A story matches if any keyword appears in its normalized headline or summary.
- **Dedupe and updates**: canonical URLs are globally unique, so a story found by two topics belongs to the first. Headline-embedding dedupe runs within the same topic over 3 days. A near-duplicate reported at least 6h later is kept as a new `Story` with `status="update"` and `update_of_id`. That is the "flagged update": users who heard the original still get the update, and a user gets the update instead of the original when both are unseen.
- **Lookback**: research keeps stories published in the last 24h (date-only dates count as noon UTC of that day). Assembly considers stories published in the last 48h.
- **Tones**: 4 presets in `prompts/tones.yaml` (conversational, formal, energetic, calm), and Settings offers exactly these 4. Each tone is another script and recording of every story, so the list stays short; a humorous tone was dropped because it played as conversational (migration 0015 moved any profile that had it). Clips are cached per voice as specified, so TTS cost scales with the voices in use.
- **Clip length**: 20-60s, translated to words by per-language words-per-minute (150 for English), with 15% tolerance in local checks.
- **Voice cache per provider**: recordings are stored under a folder named after the TTS provider and engine (`clips/elevenlabs-eleven_flash_v2_5/...`, `phrases/say/...`). Switching `TTS_PROVIDER` or the Polly engine re-records a clip or phrase the next time it's needed instead of reusing audio from the old voice; old files stay on disk.
- **Episode length**: each topic is filled up to the minutes the user chose for it; `max_per_topic` (12) is only a safety cap. Stories scored below `assemble.min_importance` (4 of 10) are never used. Research asks for 5-12 stories: asking for a higher minimum on a slow news day made the model pad with off-topic stories, so episodes are shorter on quiet days rather than padded.
- **Clip prefetch**: research jobs enqueue clip jobs only for the top 4 stories per topic (`clip_prefetch_top_k`), for each (tone preset, language, voice) used by an active subscriber. Other clips are generated on demand during assembly and cached. Prefetching every story wasted 32% of clips in the 200-user simulation above (417 unused; $16.67 vs $13.41 in total).
- **Profile additions**: `Profile` gained `target_minutes` (length of the daily multi-topic briefing), `delivery_hour` and `timezone`; `User` gained `last_active_at`. The spec's `Episode.duration_sec` reuses the existing `Episode.duration_seconds`. New `Episode` fields: `sources` (JSON). Pipeline episodes use `topic="briefing"`.
- **Ledger**: `EpisodeStory` also stores `user_id` and `created_at` (denormalized) so the 14-day exclusion is a single indexed query.
- **Scheduling**: assemble jobs run at the user's local `delivery_hour`. A job waits (re-queued without using an attempt) while any of today's research is still running. Users inactive for 14 days are skipped. A user with no unseen stories gets no episode; this is logged.
- **Queue**: SQLite claims use an atomic conditional `UPDATE`; Postgres additionally uses `FOR UPDATE SKIP LOCKED`. Permanent errors (refusals, 4xx) are dead-lettered immediately; other errors retry with exponential backoff (30s, 60s, 120s) up to 4 attempts. The worker runs one thread per process; scale out with more processes.
- **Budget cap**: when today's `CostEvent` total (UTC) reaches `daily_budget_usd` ($25 by default), workers stop claiming jobs and log `DAILY BUDGET CAP REACHED`. A job already running finishes.
- **TTS**: `mock` writes a tone WAV sized to speech pace and is priced like a cloud neural voice ($16 per 1M characters) so offline runs show realistic costs. `say` is macOS local speech (free). `elevenlabs` needs `ELEVENLABS_API_KEY` and `tts.elevenlabs_model`.
- **Audio**: segments are level-matched, joined with 600ms gaps and 150ms fades, optional intro / transition / outro sounds from `hearline/backend/sounds/` are added 8 dB under the voice, then the whole episode is levelled to the podcast standard (-16 LUFS integrated, -1 dBTP true peak) with ffmpeg's two-pass `loudnorm`. Output is a 64 kbps MP3 with ID3 title, description, artist and album; without ffmpeg it falls back to a WAV levelled by average level (-1 dBFS peak cap).
- **Mock LLM**: used by tests and `--mock`. It returns the same text formats as a real model, so parsing, validation and costing run for real. Token usage is synthetic (characters / 4, plus 18K input tokens and 3 searches per research call).
- **Removed stubs**: the old `pipeline.generate_episode()` and `audio/script.py` stubs (and `prompts/script.txt`) were removed in favor of this pipeline.
- **Episodes per user**: every `Episode` row has a `user_id`, and the API only returns or updates the current user's episodes (anyone else's look like a 404). Each episode stores the tone, language and voice it was made with, so history stays correct after a profile change, plus `listened_at`. Episode audio is at `storage/audio/users/<user_id>/episodes/<episode_id>.mp3`; story clips stay shared at `storage/audio/clips/` because reusing them across users is what keeps costs flat.
- **Storage, not a data lake**: episodes are small relational rows read by user and date, so they belong in the database (SQLite now, Postgres in production) with audio in object storage (S3/R2/GCS) under the same `users/<id>/...` keys. A data lake (Parquet or Iceberg on S3) fits analytics on listening and cost events, exported from the database, not serving the app.
- **Scale**: the daily plan reads every active user's delivery settings in one streamed query and inserts jobs in bulk, skipping existing keys in the database. Episodes are built in the 3 hours before delivery (`build_lead_hours`), at a fixed spot per user, and published at the delivery time. An episode waits only for its own topics' research, until 15 minutes before delivery (`research_wait_margin_minutes`), then goes ahead with what it has.
- **Retention**: maintenance keeps raw cost events 90 days (daily totals in `fact_cost_daily` forever), heard-story rows 30 days (then archived to `storage/archive/`), finished jobs 7 days and dead jobs 30. Episodes store only their greeting/transition/outro text; the transcript is rebuilt on demand.
- **TTS limits**: Polly uses the AWS SDK's adaptive retry mode, which slows down when throttled. ElevenLabs retries 429 ("too many concurrent requests") and 5xx answers with backoff. Shared phrases keep per-user TTS calls near zero.
- **Settings drive the pipeline**: the Settings page and the pipeline share one table, `bridge_user_topic`. Each catalog interest maps to a pipeline topic by alias, with no model call (`environment` → Climate & Environment, `film` → Film & TV, `art` → Art & Design); a test checks every interest maps. Frequency: `daily` every day, `weekdays` Monday-Friday, `weekly` on Mondays (`assemble.weekly_weekday`) with a 7-day story window. An episode lasts the sum of the day's topic minutes, up to `assemble.max_episode_minutes` (30), and a topic's minutes also set its ranking weight (minutes / 10). The API seeds the topic catalog at startup.
- **Episode image**: the Settings-page picture of the episode's first topic (or of its parent topic, e.g. `ai` → Technology).
- **Audio size**: MP3 at 64 kbps mono (podcast quality for speech), about 4.8 MB per 10 minutes instead of 7.2 MB at 96 kbps.

