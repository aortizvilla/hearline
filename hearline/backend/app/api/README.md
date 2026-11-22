# api/

The HTTP surface, all under `/api`:

| File | Job |
|---|---|
| `routes.py` | The endpoints the app calls: settings, catalogs, plans, episodes, account |
| `admin.py` | The admin dashboard, under `/api/admin` |
| `feed.py` | Each listener's private podcast feed (secret link, RSS + iTunes tags, audio links that redirect to fresh signed URLs) |
| `webhooks.py` | The Clerk webhook (Svix-signed): keeps name and email in step with the login account, erases on `user.deleted` |
| `auth.py` | Login: verifies the Clerk session token behind `current_user()` |

| Endpoint | Does |
|---|---|
| `GET /options`, `GET /interests` | Choices for tone, language, voice and topics (public) |
| `GET /plans` | The plans with limits and prices, and whether users may switch themselves (public) |
| `GET`/`PUT /profile` | Read or update the current user's settings (with Clerk, name and email come from the login) |
| `PUT /plan` | Switch the current user's plan (only while self-serve is on: dev, or `PLAN_SELF_SERVE=true`) |
| `GET /episodes` | The current user's episodes, newest first (`?listened=false`, `?topic=`) |
| `POST /episodes/{id}/progress`, `POST /episodes/{id}/listened` | Playback: furthest second heard; mark as listened |
| `DELETE /account` | Erase the current user and everything that is theirs (`app/accounts.py`) |
| `GET /podcast`, `POST /podcast/reset` | The user's private feed link; a new link (the old one stops working) |
| `GET /feed/{token}.xml`, `GET /feed/{token}/episodes/{id}.mp3` | The feed and its audio, for podcast apps (no login: the token is the key) |
| `POST /dev/login` | Development only (`AUTH_MODE=dev`): sign in as a user by email |
| `GET /admin/access` | Whether the current user is an admin (`ADMIN_USER_IDS`; everyone in dev mode) |
| `PUT /admin/users/{id}/plan` | Admins: move a user to a plan (how paid plans are given until billing exists) |
| `GET /admin/dashboard` | KPIs for admins: users, engagement, cost per episode, generation time, pipeline health (`?days=&language=&voice=&topic=`) |
| `POST /webhooks/clerk` | Clerk's user events (only when `CLERK_WEBHOOK_SECRET` is set) |

Login: with `AUTH_MODE=clerk` every request carries a Clerk session token (`auth.py`); with
`AUTH_MODE=dev` (local only) the `X-User-Id` header picks the user, else the default user.
`APP_ENV=production` refuses to start with dev login. Episode audio is served from `/audio/...`
(local) or through signed links (S3).
