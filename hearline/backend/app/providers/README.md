# providers/

The outside services the pipeline uses, each behind a small interface so it can be swapped through `.env` without touching pipeline code.

| File | Service | Options |
|---|---|---|
| `llm.py` | Text generation and web search | Anthropic (Claude), OpenAI |
| `mock_llm.py` | Fake LLM for tests and `--mock` runs | No API key, no cost |
| `tts.py` | Text to speech | mock (beep audio), macOS `say`, ElevenLabs, Amazon Polly |
| `sounds.py` | Episode intro/transition/outro sounds, generated once (`python -m app.cli sounds`) | ElevenLabs Sound Effects |
| `embeddings.py` | Text to vectors, to compare meaning | local MiniLM model, hashing fallback |
| `vectors.py` | Find the closest vectors | in-Python search (swap for pgvector) |
| `voices.py` | The voice catalog (`config/voices.yaml` → `dim_voice`) and each voice's preview sample per language | recorded once with the current TTS (`python -m app.cli voice-previews`) |
| `storage.py` | Where audio files live | local disk (dev) or S3 with expiring signed links |
| `web.py` | Free web lookups for research: is an article link alive, Wikipedia background | no key |
| `check.py` | `python -m app.cli check`: keys, models, prices and voices work | free, no generation |

`__init__.py` builds the set the app uses (`build_providers`) from the settings in `.env`.
