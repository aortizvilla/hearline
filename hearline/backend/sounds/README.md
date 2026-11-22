# Episode sounds

Optional music and sound effects that frame every episode. Step 6 picks them up by name;
any that is missing is skipped, so episodes stay voice-only until you add files.

| File | Plays | Suggested length |
|---|---|---|
| `intro.mp3` | before the greeting | 2–4 s sting |
| `transition.mp3` | before each topic's transition line | 0.5–1.5 s whoosh / chime |
| `outro.mp3` | after the sign-off | 3–6 s tail with a fade-out |

`.wav`, `.ogg`, `.flac` and `.m4a` work too. Loudness doesn't need to be prepared: each clip is
set `sfx_offset_db` (default -8 dB, `config/pipeline.yaml`) under the voice, then the whole
episode is levelled to -16 LUFS / -1 dBTP.

## Licences

Only use files you may redistribute commercially without attribution in the audio
(e.g. CC0 from Freesound, the Pixabay Content License). Record each file here:

| File | Source URL | Author | Licence |
|---|---|---|---|
| `intro.mp3` | ElevenLabs Sound Effects, generated 2026-11-22 (`app/providers/sounds.py`) | Hearline | ElevenLabs terms: commercial use on paid plans |
| `transition.mp3` | same | Hearline | same |
| `outro.mp3` | same | Hearline | same |

Regenerate with `python -m app.cli sounds --force` (prompts in `app/providers/sounds.py`).
