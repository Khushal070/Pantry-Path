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

## Guided builds: choosing the dish step by step

With `PANTRYPATH_GUIDED=true` (the default), a dish the shopper names becomes a **build card** before anything is searched (spec: `docs/superpowers/specs/2026-11-27-guided-dish-building-design.md`):

1. **A plan** (`app/plans.py`), from the fastest source that has one: a hand-written template for a kind of food (`config/dish_templates.yaml`: sandwich, salad, pasta, wrap, bowl; instant), the dish catalog (its ingredients grouped into steps by food category, with "% of recipes" from the catalog; `config/variants.yaml` turns a generic one into kinds, such as sandwich bread into white, whole wheat, sourdough or rye), or **one model call** with `ml/prompts/plan_dish.txt` for any other dish (about 15–25 s on a laptop).
2. **Options are screened** against the shopper's allergens and diet before they're shown (`app/screen.py`: the real diet rules on the name plus what that food usually contains). A clashing option is left out with the reason, and its swap from A3's table (`ml/configs/substitutes.yaml`) is offered in its place.
3. **One step at a time** (`app/guided.py`): the usual options start ticked; Next, Back, "Use the usual" (search now) and Start over. Anything typed while a build is open adds or drops items ("add pineapple and Dave's Killer Bread", "no onions", "hold the mayo"); a product name is searched by its name. An addition that clashes waits for the shopper: use the swap, search it anyway (its products are judged as usual and show as not safe), or remove it.
4. **In the background**, the stores near the ZIP are looked up as soon as the dish is read, then products for the usual options, then each pick and addition as it's made (`LiveEngine.prefetch`, Kroger only: Target costs credits per call). They land in the Kroger provider's own 15-minute in-memory cache, so the final search reads them instead of asking again.
5. **A summary card** (the confirm card, with the chosen ingredients) starts **one search for the merged list** (`result_for_ingredients` in the data pipeline). The result and A3's answer are the same as any search's.

"Use the usual" on an untouched catalog dish is the search from before guided builds (its whole catalog profile; "Ask a stronger model" can redo it). Searches of chosen ingredients don't offer the stronger model: the shopper chose them. With `PANTRYPATH_GUIDED=false`, or for a dish no source can plan, the chat goes straight to the confirm card as before.

## Choosing products and favourite brands

A build also chooses **products** (spec: `docs/superpowers/specs/2026-11-27-product-choice-design.md`). When the dish is planned, the products of every option, of the shopper's favourite brands ("Dave's Killer Bread sandwich bread") and of family hints are fetched in the background at Kroger only, judged by the diet rules (`engine.options`), and the build card fills in (`app/choices.py`):

- **Main store**: the store with a safe product for the most usual options, nearest on a tie; recomputed every search, never saved.
- **Each option's pick**: the favourite brand if found and not unsafe; else the first safe product at the main store; else the nearest other store's ("only at …"); else one to read the label of. **Change** offers up to 3 alternatives and the family brand first ("your usual cheese brand", not picked). Unsafe products are never offered.
- **Favourites** (`favourites: {item: brand}` in the encrypted profile): a brand picked for an item without one is kept when the search starts; a different brand for an item that has one is only asked about on the summary card ("Make X your usual Y instead of Z?"). Loose produce isn't remembered. Preferences lists them, each removable (`PUT /api/me/favourites`).
- **The summary asks "Anything else to add?"** (a box on the card, or anything typed in the chat at that point): the build opens again at its last step with the items added (searched, checked, with products; an offered option is just ticked), and Review makes a new summary (`POST /api/sessions/{id}/messages/{summary_id}/add`).
- **The final search** gives A3 only each item's chosen product, so the list buys exactly the picks; the job carries the picks (`choices`) for "Your pick" with store and aisle. Saved lists keep each item's brand (the shopper's), never the product; "Find products again" searches "brand item".
- **Steps that match the list** (`app/stepcheck.py`): after A3, foods named in the steps or summary that aren't on the list (the vocabulary: catalog ingredients, the build's options, templates, variants and A3's substitutes table) make A3 try once more with "use only these ingredients"; what is still stray is replaced by the shopper's pick of the same build step, or its step is dropped.

**Stored**: item → brand only (the shopper's preference). Kroger's product records, stores and aisles stay in memory (the chat and the search job). The team should confirm this split against Kroger's developer agreement.

## Saved lists ("My lists")

Every finished search saves its shopping list to the account (`app/lists.py`, table `profile.lists`, migration `0002`; spec: `docs/superpowers/specs/2026-11-27-saved-lists-design.md`). A list is the shopper's own data only: the dish, ZIP, servings, avoid list and diet, each item's name, our category, quantity, status (buy, substitute, read the label, leave out), substitute and tick, and the recipe steps. **Never Kroger data**: no product, brand, size, aisle, price, stock or store; A3's per-item notes aren't kept, and a step that names a product, brand or store from the search's result is dropped before saving. Lists are encrypted like the profile, readable only as `pp_profile`, and deleted with the account.

The app shows every result list first (`frontend/src/components/ListView.tsx`): sections by store area from our own categories, a status per item, tap to tick (ticks are saved to the account), and Steps, Stores and All products behind buttons. A saved list opened later has no products until **Find products again**, which searches its items fresh at its ZIP (the substitute for a substitute item; leave-out items not at all), in memory only, without A3.

The frontend keeps a passive copy of the lists and its own files on the device (`frontend/src/offline.ts`, `frontend/public/sw.js`), so it opens and shows lists without signal; ticks made offline are sent when the browser is back online (the last tick wins). Store data and chats are never kept on the device, and logging out clears it. Service workers need https or `localhost`.

## Storage: Postgres, and memory

| What | Where | Kept |
|---|---|---|
| Accounts: username, password hash | `auth.accounts` | until the account is deleted |
| Logins: the token's SHA-256, expiry | `auth.logins` | until logout, expiry, a password change elsewhere, or account deletion |
| Food preferences, **encrypted** | `profile.profiles` | until the account is deleted |
| Saved shopping lists, **encrypted** (no Kroger data) | `profile.lists` | until deleted, or the account is |
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
| `PUT /api/me/favourites` | `{item, brand}`: set a favourite brand; `brand: null` removes it |
| `PUT /api/me/preferences` | `{name, zip, avoid, diet, servings, needs}`: save preferences |
| `POST /api/me/password` | `{current_password, new_password}`: change the password |
| `POST /api/me/delete` | `{password}`: delete the account, its logins, preferences and chats |
| `GET /api/sessions`, `POST /api/sessions` | List your chats that are still open (in memory), start one |
| `GET`, `DELETE /api/sessions/{id}` | Read a chat with its messages, delete it; 404 once it has ended |
| `POST /api/sessions/{id}/messages` | `{text}`: the shopper's message and the assistant's reply |
| `POST /api/sessions/{id}/builds/{message_id}` | A move on the newest build card: `{action: next \| back \| finish, picks}`, `{action: usual, picks}` (searches now: the reply has a `job`), `{action: add, text}`, `{action: resolve, name, choice: swap \| anyway \| remove}`, `{action: choose, name, key}` (another product for an item), `{action: cancel}`. 409 for an old card or an open clash |
| `GET /api/lists`, `GET /api/lists/{id}` | My lists (newest first), one list |
| `PUT /api/lists/{id}/items/{item_id}` | `{ticked}`: tick or untick an item |
| `DELETE /api/lists/{id}` | Delete a saved list |
| `POST /api/lists/{id}/products` | Find products again: a job (no A3) with fresh store results for the list's items; never saved |
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
| `PANTRYPATH_GUIDED` | `true` | Guided builds before searching; `false` goes straight to the confirm card |
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
pytest                    # 212 tests (some need the live packages), no network or model
ruff check .
```

Tests that need Postgres fail, saying how to start it, when none answers at `PANTRYPATH_TEST_DB_URL` (default the Docker one). They never touch the development database: `tests/pg.py` makes a throwaway database with its own two users, migrated with the real migrations, and drops it at the end. `tests/test_migrations.py` checks the schemas, the users' rights and the cascading deletes; `tests/test_crypto.py` the encryption; `tests/test_storage.py` what is saved where, restarts, key changes and startup errors. Guided builds: `tests/test_screen.py` (name checks), `test_plans.py` (plans from the catalog, templates and the model), `test_guided.py` (a build's moves), `test_guided_api.py` (the whole flow in demo mode) and `test_live_guided.py` (the background search with a counting fake Kroger, and the model's plan). Product choice: `tests/test_choices.py` (main store, picks, favourites, hints), `test_product_choice.py` (through the API), `test_stepcheck.py`. Saved lists: `tests/test_lists.py` (no Kroger data in a list or the database, ticks, find products again, privacy, deletion). The API tests use the confirm-card flow unless they ask for guided builds.

`tests/test_stronger_model.py` covers "Ask a stronger model": the route and the messages it adds (with the demo engine and a stand-in), the live engine's client choice and error messages, and how `.env` turns it on; its live and `.env` tests skip without the live packages.

`tests/test_live_outcome.py` covers how live mode reads A1's `ParseResult`, with stand-ins, so it runs without the model packages. Live mode has run end to end with the local model and Kroger (see Demo mode and live mode).

## Limits

- **One process.** Chats, search results and failed-login counts live in one uvicorn worker's memory. Several workers, or several Cloud Run instances, would each have their own: run one worker, and deploy with at most one instance. Accounts and preferences are in Postgres, so they survive restarts and new instances.
- **No "forgot password".** There is no email, so a lost password can't be reset from the app. Whoever runs the database can delete that user's row in `auth.accounts`.
- **The search starts when the shopper confirms**, not while they chat as `docs/project-plan.md` plans.
- **The route is a Google Maps link** through the stores, nearest first, not an optimized route.
- `scripts/build_demo_catalog.py` rebuilds the demo dishes from `ml/evalsets/a3_dev.jsonl` (the dev split only).
