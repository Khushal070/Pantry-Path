# Pantry Path backend

The chat API the React app (`frontend/`) talks to. FastAPI, with accounts and food preferences in Postgres (preferences encrypted), and chats and store results held in memory only: nothing is kept on the server's disk.

## Run it

Python 3.12, from the repo root:

```bash
cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
docker compose up -d                 # Postgres 18 on 127.0.0.1:5433 (docker-compose.yml)
alembic upgrade head                 # create the tables and the two database users
uvicorn app.main:app --reload        # http://localhost:8000, API docs at /docs
```

Before the first run, copy the database settings from `.env.example` into the repo-root `.env` and fill them in (Storage, below): two passwords you pick for the database users, the same two in the app's URLs, and an encryption key from `python -c "from app.crypto import new_key; print(new_key())"`, set as `PANTRYPATH_PROFILE_KEYS=v1:<key>`. The backend won't start without them, and says which one is missing.

Then start the frontend (`frontend/README.md`), open it and create an account. This is **demo mode**: it needs no model server, store keys or DVC data.

If `frontend/dist` exists (`npm run build`), the backend also serves the app at http://localhost:8000, so a demo runs as one process.

## Demo mode and live mode

| | `PANTRYPATH_MODE=demo` (default) | `PANTRYPATH_MODE=live` |
|---|---|---|
| Reads the chat | A keyword reader, a stand-in for A1 | A1, the champion (`pantrypath_ml.runtime`) with its cross-check |
| Dishes | The 70 dev-split catalog dishes (`app/engines/demo_catalog.json`) | The 100-dish catalog, and A2 for anything else |
| Stores and products | 3 synthetic stores, synthetic products | Kroger (Target as fallback) through `build_result` |
| Verdicts | The real diet rules (`pantrypath_core.diet_rules.assess`) | The same |
| Shopping list | Decided by code the way A3's hybrid decides it; templated summary; no steps | A3 |

Demo results are validated against the `result.json` contract, so the frontend sees the same shape in both modes. Nothing in demo mode is shopping advice, and the app says so.

**Live mode** needs:

```bash
pip install -r requirements-live.txt     # the model and pipeline packages
(cd .. && dvc pull)                      # dish catalog + allergen snapshot
ollama pull qwen3:4b-q4_K_M && ollama serve
PANTRYPATH_MODE=live uvicorn app.main:app
```

plus `KROGER_CLIENT_ID` and `KROGER_CLIENT_SECRET` in the repo-root `.env` (copy `.env.example`; `REDCIRCLE_API_KEY` adds the Target fallback). For the deployed model server, set `MODEL_SERVER_URL` to the Cloud Run URL and `MODEL_SERVER_AUTH=google` (`ml/README.md`). Live mode fails at startup, with the reason, if the packages, the data or the champion are missing. It never falls back to demo data on its own.

Good to know when running it:

- **Without store keys the chat still works.** A1 reads the chat and shows confirm cards; a search then says which keys are missing. The store providers are set up once at startup (so Kroger's token and 15-minute cache carry over between searches): restart the backend after editing `.env`.
- **Kroger covers Kroger-family stores only** (Kroger, Ralphs, Fred Meyer, King Soopers, Harris Teeter and others), and some regions have none: New York City's 10010 has no store within 10 miles, for example. Such a search ends with a message that says so, without results and without running A3. Test with 45202, or add `REDCIRCLE_API_KEY` for Target.
- **Ollama unloads the model after 5 idle minutes**, and the next message takes 15 to 45 s while it loads again. For a demo, keep it loaded: `OLLAMA_KEEP_ALIVE=1h ollama serve`. Warm, A1 takes about 3 s, A2 (a dish outside the catalog) about 20 s and A3 about 25 s on an Apple Silicon laptop.
- **"Not right? Ask a stronger model".** A2 runs on the local champion, which knows popular dishes but can miss regional ones (for dal dhokli it used the wrong dal and left out the peanuts). With `LLM_API_KEY` set in `.env` (plus `LLM_PROVIDER` and `LLM_MODEL`, e.g. `gemini` and `gemini-3.6-flash`), a result whose ingredients a model worked out offers this button. It searches again with that hosted model working out the ingredients, with the champion's A2 prompt, and adds the new search under the first one, which stays as it was. Only the dish's name reaches the provider; verdicts still come from `diet_rules`. Without a key, or in demo mode, the button doesn't appear. Free tiers allow only a few requests a day, and a busy or used-up model ends that search with a message that says so (`app/stronger_model.py`).
- **Checked on 2026-11-17** with Ollama 0.40.16 and `qwen3:4b-q4_K_M`: a full search with Kroger near 45202 (quiche, no milk or gluten, vegetarian): 3 Kroger stores and 180 products checked (42 safe, 114 not safe, 24 couldn't verify), then A3's list with steps, about 30 s after the confirm. Also A1 through the app (one message and two-message chats), A2 on a dish outside the catalog and A3 on a synthetic `result.json`. That run found A1 failing on chats without a ZIP: the model wrote `"zip": "null"` as a string. `normalize_zip` in `ml/src/pantrypath_ml/parse_request.py` now reads that as "no ZIP" (with a test). The A1 evaluation sets have no chat without a ZIP, though the app now relies on that case, since saved preferences hold the ZIP.

## Accounts

A shopper signs up with a name, a username and a password, sets their food preferences (allergies, diet, ZIP, household size, needs we can't check yet), logs out, and logs in again from any browser. Several people can use one browser by logging out and in. Everything about accounts is in `app/auth.py`:

- **Passwords** are hashed with scrypt from Python's standard library (no extra package), with a random salt per password. They are checked in constant time and never logged or sent back. A login with an unknown username costs the same time as a wrong password, and both get the same message.
- **A login** is a random token in a cookie named `pantrypath_login`. It is `HttpOnly`, so page scripts can't read it, and `SameSite=Lax`, so other sites can't send it with their requests. The database stores only the token's SHA-256, so its rows can't be used to log in. A login lasts 30 days (`PANTRYPATH_LOGIN_DAYS`). Logging out deletes it on the server and ends the user's open chats (in every browser: chats belong to the user).
- **Five wrong passwords** for one username within ten minutes block that username's logins until the window passes. The count is kept in memory.
- **Changing the password** logs out every other browser on that account. **Deleting the account** asks for the password again, then deletes the account, its logins, its preferences and its chats.
- **Every chat and search belongs to one user.** Another user's chat or search answers "not found", exactly like one that doesn't exist. `tests/test_auth.py` checks each endpoint.

Behind HTTPS, set `PANTRYPATH_SECURE_COOKIES=true` so the cookie is only ever sent over HTTPS. Leave it off for `http://localhost`.

## How a chat works

1. **The shopper types.** `POST /api/sessions/{id}/messages`. The engine reads every message since the last search (A1 in live mode).
2. **The reading meets the user's saved preferences** (`app/drafts.py`). All of it is deterministic:
   - allergens: the chat's plus the saved ones. The chat can't drop a saved allergen ("milk is fine today"); the card says so and points to Preferences.
   - diet: the stricter of the two.
   - ZIP: the chat's, else the saved one, else the assistant asks.
   - servings: the chat's, else the saved household size.
   - needs we can't check yet (halal, low sodium, ...): from both; the answer warns about each.
   - A1's doubts (an allergen it missed, "nuts": peanuts too?, an allergen the chat never named) become "Please check" notes. A doubt the saved preferences already cover is dropped.
3. **The assistant asks or confirms.** It asks for a missing dish or ZIP, or shows a **confirm card**. The shopper can edit the card; saved allergens stay locked, and the server applies them again whatever the card sends. Only the newest card can be searched.
4. **The search runs in the background.** `POST /api/sessions/{id}/search` starts `build_result` and then A3 in a thread. The app polls `GET /api/jobs/{id}` and shows the stages.
5. **A new message after a search starts a new request**, so the old dish isn't read again.
6. **The chat ends** after `PANTRYPATH_CHAT_IDLE_MIN` minutes without a message (default 20), when the shopper logs out or deletes it, or when the server restarts. Chats are never saved. Only what's saved in Preferences carries over to the next chat.

## Storage: Postgres, and memory

| What | Where | Kept |
|---|---|---|
| Accounts: username, password hash | `auth.accounts` | until the account is deleted |
| Logins: the token's SHA-256, expiry | `auth.logins` | until logout, expiry, a password change elsewhere, or account deletion |
| Food preferences, **encrypted** | `profile.profiles` | until the account is deleted |
| Chats | this process's memory (`app/chat_memory.py`) | until 20 idle minutes, logout, deletion, or a restart |
| Search results | this process's memory (`app/jobs.py`) | 60 minutes |
| Failed-login counts | this process's memory | 10 minutes |

**Two schemas, two database users.** The app connects twice (`app/db.py`): as `pp_auth`, which can use only the `auth` schema, and as `pp_profile`, which can use only `profile`. Neither can read the other's tables or change any table's shape. Deleting an account deletes its logins and profile with it (`ON DELETE CASCADE`). The tables and both users come from the migrations in `migrations/` (`alembic upgrade head`), run as the database owner (`PANTRYPATH_DB_OWNER_URL`), never by the app. The first migration creates the two users with SQL, with the passwords in `PANTRYPATH_DB_AUTH_PASSWORD` and `PANTRYPATH_DB_PROFILE_PASSWORD`: on Cloud SQL, users made with gcloud or the console join `cloudsqlsuperuser`, which can read every table.

**Preferences are encrypted by the app** (`app/crypto.py`) before they reach the database: AES-256-GCM, with the user's id bound in, so a profile copied onto another account doesn't decrypt, and a changed byte is caught. The database holds only ciphertext and the version of the key that made it. Keys come from `PANTRYPATH_PROFILE_KEYS`, newest first (`v2:<key>,v1:<key>`): the first encrypts, all of them decrypt. To change the key, put a new one first and keep the old one until every profile has been saved again. A profile that can't be decrypted (its key is gone) answers 500, and is never shown as blank preferences. **Losing every key loses every profile**: keep them in Secret Manager in production, and back them up.

The profile is stored as one value, `{"format": 1, "prefs": {...}}`, so new fields (for example, where a saved allergy came from) need no table change.

**Store data is never saved.** Kroger's terms forbid storing its data (`docs/data-pipeline-handoff.md`, section 10). A search's result and answer live in this process's memory (`app/jobs.py`) for `PANTRYPATH_RESULT_TTL_MIN` minutes (default 60, never past `result.json`'s `expires_at`). They are never logged. A chat keeps only the confirmed request, the dish's name and whether the search worked. When the results are gone (expired, or the server restarted), the app offers to search again with one click. `tests/test_api.py` checks that no product or store name reaches the database, and `tests/test_storage.py` that no chat does.

## API

Everything except `config`, `health` and `/api/auth/*` needs a login, and answers 401 without one.

| Method and path | What it does |
|---|---|
| `GET /api/config` | Mode, engine info, allergen, diet and need labels for the forms |
| `POST /api/auth/register` | `{name, username, password}`: create an account and log it in |
| `POST /api/auth/login` | `{username, password}`: log in (sets the cookie) |
| `POST /api/auth/logout` | Log out (ends the login on the server and the user's open chats, clears the cookie) |
| `GET /api/me` | The logged-in user and their preferences |
| `PUT /api/me/preferences` | `{name, zip, avoid, diet, servings, needs}`: save preferences |
| `POST /api/me/password` | `{current_password, new_password}`: change the password |
| `POST /api/me/delete` | `{password}`: delete the account, its logins, preferences and chats |
| `GET /api/sessions`, `POST /api/sessions` | List your chats that are still open (in memory), start one |
| `GET`, `DELETE /api/sessions/{id}` | Read a chat with its messages, delete it; 404 once it has ended |
| `POST /api/sessions/{id}/messages` | `{text}`: the shopper's message and the assistant's reply |
| `POST /api/sessions/{id}/search` | `{message_id, query, zip, avoid, diet, servings}`: search a confirm card |
| `POST /api/sessions/{id}/messages/{message_id}/rerun` | Search a confirmed request again |
| `POST /api/sessions/{id}/messages/{message_id}/rerun-stronger` | Search it again with the stronger model working out the ingredients: a new search message. 409 for a catalog dish or one already redone, 503 without a stronger model |
| `GET /api/jobs/{id}` | A search's stage, then its `result` (contract v1.0), `answer` and `warnings`; 404 once expired |

The interactive docs are at http://localhost:8000/docs.

## Settings

Environment variables, or the repo-root `.env`:

| Variable | Default | |
|---|---|---|
| `PANTRYPATH_MODE` | `demo` | `demo` or `live` |
| `PANTRYPATH_DB_AUTH_URL` | required | Postgres as `pp_auth`, e.g. `postgresql+psycopg://pp_auth:<password>@127.0.0.1:5433/pantrypath` |
| `PANTRYPATH_DB_PROFILE_URL` | required | Postgres as `pp_profile`, the same way |
| `PANTRYPATH_PROFILE_KEYS` | required | The keys that encrypt preferences, newest first: `v1:<base64 of 32 bytes>` |
| `PANTRYPATH_CHAT_IDLE_MIN` | `20` | Minutes a chat stays in memory after its last message |
| `PANTRYPATH_DB_OWNER_URL` | for migrations | Postgres as the owner; only `alembic` uses it |
| `PANTRYPATH_DB_AUTH_PASSWORD`, `PANTRYPATH_DB_PROFILE_PASSWORD` | for the first migration | The passwords it gives `pp_auth` and `pp_profile` |
| `PANTRYPATH_LOGIN_DAYS` | `30` | Days a login lasts |
| `PANTRYPATH_SECURE_COOKIES` | `false` | `true` when served over HTTPS |
| `PANTRYPATH_RESULT_TTL_MIN` | `60` | Minutes a search's results stay in memory |
| `PANTRYPATH_SEARCH_WORKERS` | `2` | Searches that run at once |
| `PANTRYPATH_DEMO_DELAY_S` | `1.2` | Demo mode: seconds each search stage pretends to take |
| `PANTRYPATH_CORS_ORIGINS` | none | Extra allowed origins, comma-separated (the Vite dev server proxies `/api`, so it needs none) |

## Tests

```bash
pip install -r requirements-dev.txt
docker compose up -d      # the tests need the Docker Postgres
pytest                    # 114 tests (14 more need the live packages), no network or model
ruff check .
```

Tests that need Postgres fail, saying how to start it, when none answers at `PANTRYPATH_TEST_DB_URL` (default the Docker one). They never touch the development database: `tests/pg.py` makes a throwaway database with its own two users, migrated with the real migrations, and drops it at the end. `tests/test_migrations.py` checks the schemas, the users' rights and the cascading deletes; `tests/test_crypto.py` the encryption; `tests/test_storage.py` what is saved where, restarts, key changes and startup errors.

`tests/test_stronger_model.py` covers "Ask a stronger model": the route and the messages it adds (with the demo engine and a stand-in), the live engine's client choice and error messages, and how `.env` turns it on; its live and `.env` tests skip without the live packages.

`tests/test_live_outcome.py` covers how live mode reads A1's `ParseResult`, with stand-ins, so it runs without the model packages. Live mode has run end to end with the local model and Kroger (see Demo mode and live mode).

## Limits

- **One process.** Chats, search results and failed-login counts live in one uvicorn worker's memory. Several workers, or several Cloud Run instances, would each have their own: run one worker, and deploy with at most one instance. Accounts and preferences are in Postgres, so they survive restarts and new instances.
- **No "forgot password".** There is no email, so a lost password can't be reset from the app. Whoever runs the database can delete that user's row in `auth.accounts`.
- **The search starts when the shopper confirms**, not while they chat as `docs/project-plan.md` plans.
- **The route is a Google Maps link** through the stores, nearest first, not an optimized route.
- `scripts/build_demo_catalog.py` rebuilds the demo dishes from `ml/evalsets/a3_dev.jsonl` (the dev split only).
