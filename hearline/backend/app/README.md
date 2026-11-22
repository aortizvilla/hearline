# app/

The Hearline backend. Each folder has one job:

| Folder | Job |
|---|---|
| [pipeline/](pipeline/) | **What** gets made: the 6 steps from a user's interest to an MP3 episode, plus the accuracy review before audio |
| [jobs/](jobs/) | **When** it runs: the daily plan, the job queue and the worker |
| [providers/](providers/) | **Who** does the work: swappable LLM, TTS, embeddings, vector search, audio storage, web lookups |
| [database/](database/) | **Where** it's kept: tables and the database connection |
| [tracking/](tracking/) | **How it's doing**: every LLM/TTS call priced, the daily budget, KPIs for the dashboard, alerts |
| [api/](api/) | **How the frontend talks to it**: `/api` endpoints, the podcast feed, the Clerk webhook |
| [dev/](dev/) | Tools for development: the demo listeners and the static demo snapshot |

Files at this level:

- `main.py`: starts the web server (`uvicorn app.main:app`).
- `cli.py`: command line for running pipeline steps by hand (`python -m app.cli --help`).
- `accounts.py`: deleting a listener and everything that is theirs (from the app or the Clerk webhook).
- `plans.py`: subscription plans (`config/plans.yaml`): what each allows, and keeping users within it.
- `config.py`: every setting. Secrets and model names come from `.env`; tuning comes from `config/*.yaml`.

Data lives outside `app/`, in `hearline/backend/storage/`: `hearline.db` for records and `audio/` for audio files.
