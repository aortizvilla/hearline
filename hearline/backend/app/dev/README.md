# dev/

Tools for development only; the app doesn't use them.

- `demo.py`: demo listeners (English and Spanish, free and Plus plans, overlapping topics) added to the one store through the real API and run through the real pipeline with the providers from `.env`. `python -m app.cli demo` adds them all; `python -m app.cli demo --email ana@example.com --language es --topics technology,world` adds one and prints its transcript, sources and private feed. Nothing is deleted; afterwards sign in at http://localhost:5173 with a listener's email.
- `static_demo.py`: the demo listeners as static files for a server-free demo site. `python -m app.cli export-demo` calls the real API for each demo listener and writes every answer the frontend needs, plus the audio they link to, to `hearline/frontend/demo-data/`. A frontend built with `VITE_STATIC_DEMO=1` reads those files (Cloudflare Pages: root README, *Static demo*).
