# Saved shopping lists and a list-first results page: design

Date: 2026-11-27. Status: approved and built (2026-11-27); see section 12. Builds on guided dish building
(`2026-11-27-guided-dish-building-design.md`) and the Postgres accounts work.

## 1. Why

A search's shopping list lives in server memory for 60 minutes and is gone after a
restart, so it can't be the checklist someone carries into the store the next day. The
results page also shows too much at once (four tabs, a meter, a summary, a pile of
warnings) and is organised by store rather than the way people shop. People will use it
on their phones, from any device they log in on.

## 2. Decisions (from the brainstorm)

| # | Question | Decision |
|---|---|---|
| Q1 | What's wrong with the results page | **Too much at once** (A) and **not organised the way you shop** (D); it must work on a phone in the store |
| Q2 | When a list is saved | **Automatically, every finished search**; a "My lists" page; delete any |
| Q3 | Layout | **List first**: the checklist is the page, grouped by store section, a status per item, tap a row for its products and why; Steps, Stores and All products behind buttons |
| Q4 | Products on a saved list | **A "Find products again" button**: no Kroger calls unless asked |
| Approach | | **1**: lists in the account (Postgres, encrypted), one responsive list-first page, ticks that survive a dropped signal, plus **light passive caching** so the app opens and lists show without signal (no sync engine) |

## 3. What can be saved (Kroger's terms)

`docs/data-pipeline-handoff.md` section 10: Kroger data is never stored. A saved list
holds only the shopper's own data:

| Saved | Never saved |
|---|---|
| Dish, cuisine, ZIP, servings, avoid list, diet, date | Product names, brands, sizes, aisles, prices, stock |
| Each item: name, our category, quantity and unit, status (buy / substitute / read the label / leave out), substitute, ticked | Store names and addresses, store ids |
| The recipe steps, after scrubbing (below) | A3's per-item notes (they quote products) |

**Scrubbing**: before saving, any step that contains a product name, a brand or a store
name from that search's result is dropped, by code. Product and store data then exists
only in memory, as today.

## 4. What the shopper sees

**Right after a search** (in the chat) and **on a saved list** (from "My lists") the same
page, list first:

```
Grilled cheese · for 2                          Saved to My lists ✓
11 items · 8 safe · 3 read the label
──────────────────────────────────────────────────────────────
PRODUCE ────────────────────────────── 1/3
[x] Lettuce · 1 head                        ✓
[ ] Tomatoes · 2                            ✓
[ ] Avocado · 1                             ?   (tap: products, why)
DAIRY & EGGS ───────────────────────── 0/2
[ ] Swiss cheese · 4 slices                 ✓
[ ] Butter → vegan butter                   ⇄
BAKERY & GRAINS ────────────────────── 0/1
[ ] Sourdough bread · 1 loaf                ✓
LEAVE OUT ──────────────────────────────────
    Peanuts (you avoid them)
──────────────────────────────────────────────────────────────
[ Steps (7) ]  [ Stores ]  [ All products ]  [ Find products again ]*
```
`*` on a saved list once the search's results have expired.

- **Sections** come from our own ingredient categories: Produce; Dairy & eggs; Meat &
  seafood; Bakery & grains; Pantry (condiments, oils, spices, baking, sweeteners, nuts,
  legumes, other); then "Leave out". Our categories, not Kroger's aisles.
- **Status per item**: ✓ buy (a safe product found), ⇄ use the substitute, ? read the
  label, and leave-out items in their own section. Tapping a row shows, while the
  search's results are in memory, the product to buy, its store and why (verdicts); on
  an old list it shows "Find products again to see products".
- **Ticks**: tapping a row ticks it; ticked rows sink to the bottom of their section; each
  section shows its count. Ticks are saved to the account.
- **One warning line** under the header only when something needs attention ("3 items
  need a label check"); the details sit with each item. The Open Food Facts attribution
  stays in the footer.
- **Phone**: rows at least 48 px tall, the whole row tappable, buttons wrap, the header
  stays visible while scrolling (sticky). "Add to home screen" gives an app icon.
- **My lists**: a sidebar section under the chats: dish, date, "6 of 11 ticked"; open,
  delete.

## 5. Data

New table, migration `0002` (owned by the migration user; `pp_profile` gets select,
insert, update, delete; `pp_auth` nothing):

```
profile.lists
  id              uuid primary key
  user_id         uuid references auth.accounts(id) on delete cascade
  list_encrypted  bytea        the list below, AES-GCM (crypto.py), bound to the user id
  key_version     text
  created_at, updated_at   timestamptz
  index (user_id, created_at desc)
```

The encrypted value: `{"format": 1, "dish", "cuisine", "zip", "servings", "avoid",
"diet", "items": [{"id", "name", "category", "status", "substitute", "quantity", "unit",
"ticked"}], "steps": [...], "source": "guided" | "search"}`. A list is small (a few KB);
a tick re-encrypts the whole value under a row lock (`SELECT ... FOR UPDATE`).

**Where items come from**: A3's answer items (name, status, substitute, quantity, unit),
with the category from the result's ingredient of the same name. Without an A3 answer:
each ingredient is "buy" if it has a safe product, else "read the label".

## 6. Components

| Unit | Where | Does |
|---|---|---|
| `lists.py` | `backend/app/` | `Lists` store: save from a finished search (build the items, scrub), list summaries, get one, tick an item, delete; through `pp_profile` and `crypto.py` |
| chat hook | `chat.py` | When a search job finishes `done`, save its list for the job's owner (in the job's callback, the same place the dish name is recorded today); the search message keeps the new list's id |
| API | `main.py` | `GET /api/lists`, `GET /api/lists/{id}`, `PUT /api/lists/{id}/items/{item_id}` `{ticked}`, `DELETE /api/lists/{id}`, `POST /api/lists/{id}/products` (starts a search at the list's ZIP of its buy and read-the-label items, and of the substitute for a substitute item; leave-out items aren't searched; no A3; the job is read with `GET /api/jobs/{id}` as now) |
| `ListView.tsx` | frontend | The list-first page (section 4), for a fresh search (with live products) and for a saved list (products after "Find products again") |
| `ResultView.tsx` | frontend | Becomes ListView plus the existing Stores, All products and Steps panels behind buttons |
| Sidebar | frontend | "My lists" section |
| `offline.ts` + `public/sw.js` + `public/manifest.webmanifest` | frontend | Light caching (section 7) and "Add to home screen" |

Deleting an account deletes its lists (cascade). Logging out clears the device's copy.

## 7. Light caching (no sync engine)

- **The app's own files**: a small service worker keeps the last-loaded `index.html` and
  its assets, so the app opens without signal. It never caches `/api` responses itself.
- **Lists on the device**: each time the app loads "My lists" or a list, it keeps a copy
  in the browser's storage under the user's id; without signal it shows that copy
  (labelled "Offline: showing your saved copy").
- **Ticks without signal**: shown at once and kept in a small queue on the device, sent
  in order when the browser is back online (`online` event, and on the next load). No
  merging: the last tick sent wins.
- **Never cached on the device**: products, stores, verdicts (Kroger data), chats.
- **Logout and account deletion** remove the device's copies and queue.

## 8. Errors

| Case | What the shopper sees |
|---|---|
| Saving a list fails (database down) | The results still show; a note says "Couldn't save this list" (logged) |
| Tick fails online | The tick is undone with "Couldn't save that tick" |
| A saved list can't be decrypted | "This list can't be read" (500), never an empty list |
| Find products again: no stores, provider down | The same messages as a search |
| Offline and no saved copy | "You're offline and this list isn't on this device yet" |

## 9. Testing

- Migration `0002`: table and grants (pp_profile only), cascade from accounts, downgrade.
- `lists.py`: items from an A3 answer and without one; categories; **no product, brand or
  store name anywhere in the saved bytes or the decrypted value** (checked against the
  result's names); steps scrubbed; encrypted at rest; ticks; another user's list is "not
  found"; delete.
- API (demo engine): a finished search saves a list and the search message links it; My
  lists summaries; tick; delete; Find products again starts a job whose result covers the
  list's items; account deletion removes lists; nothing from Kroger in a database dump.
- Frontend: type check and build; by hand in a phone-sized window and on a real phone on
  the same network: list first, ticks, My lists, offline (devtools "Offline"), add to home
  screen.

## 10. Out of scope

Full offline sync with merging; sharing lists; editing a saved list's items; reordering
by a store's real aisles (Kroger data); pushing new app versions to phones beyond the
service worker's normal update on the next load.

## 11. Build order

Migration `0002` → `lists.py` (TDD) → chat hook and API → ListView and the new results
page → My lists → offline caching, manifest → docs → live check (laptop and phone).

## 12. Changes made while building (2026-11-27)

- **The app also keeps its start-up data on the device** (settings and who's logged in),
  so it can open offline at all; without it the first request fails before any list
  shows. Cleared on logout.
- **A list is saved inside the search job**, before the job is marked done, so the page
  never shows results without knowing its list; the job reports `list_id` / `list_error`.
- **"My lists" is a sidebar section**, and a saved list opens in place of the chat.
- **Ticked items are struck through and sink** within their section; the header shows
  "x/y in the basket" with a progress bar and stays visible while scrolling.
