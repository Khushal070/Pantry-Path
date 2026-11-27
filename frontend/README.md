# Pantry Path frontend

The chat app: React 19, TypeScript and Vite. It has no UI library and no state library. It works on laptops and phones, can be added to a phone's home screen, and shows saved lists without signal.

## Run it

Start the backend first (`backend/README.md`: Postgres in Docker, `alembic upgrade head`, then uvicorn), then:

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173; /api goes to the backend on :8000
```

`PANTRYPATH_API_URL=http://localhost:8765 npm run dev` points the dev server at a backend on another port. `npm run build` type-checks and writes `dist/`. When `dist/` exists, the backend serves it at http://localhost:8000, so a demo needs only the backend.

The app needs a backend with guided builds and saved lists (the API in `backend/README.md`). With `PANTRYPATH_GUIDED=false` on the backend, the chat goes straight to the confirm card, as before.

**Offline and "Add to home screen"** work only in the built app (`npm run build`, served by the backend), and only over https or on `localhost`: browsers allow service workers nowhere else. To try offline on a laptop: open a list in the built app, turn on "Offline" in the browser's developer tools (Network tab), and reload.

## What it does

- **Accounts.** Log in, or create an account with a name, username and password. Right after sign-up the app asks for your food preferences (you can skip). The sidebar shows who is logged in, with **Preferences** and **Log out**. The login is an HttpOnly cookie, so the app's code never handles it. If it expires or ends elsewhere, the app returns to the login screen and says why. Logging out ends your open chats and clears this device's saved copies.
- **Preferences.** Name, ZIP, allergies, diet, household size, needs we can't check yet, and **your favourite brands** (learned from your picks; each can be removed). The same dialog changes the password or deletes the account.
- **Building a dish** (`BuildCard`). Name a dish and choose what goes in it, one step at a time (bread, protein, cheese, ...). The usual options start ticked; options that clash with your allergies or diet are left out, with the reason and a safe swap. Products are fetched in the background ("Finding products…"): the card names the **main store** (the one with safe products for most of your items), and every ticked option shows its product with a Safe / Read the label tag and its aisle. **Change** offers up to 3 alternatives; your favourite brand is filled in (★ Your usual), and a favourite brand of the same family is offered first. Add your own items in the box or by typing in the chat ("add pineapple and Dave's Killer Bread", "no onions"); one that clashes asks you to use the swap, search it anyway, or remove it. **Use the usual** searches right away; **Start over** ends the build.
- **The summary** (`ConfirmCard` at the end of a build) lists your products with store and aisle, the new favourites it will remember, and asks before replacing a favourite ("Make X your usual Y instead of Z?"). It asks **"Anything else to add?"**: adding opens the build again with the new items. The dish and ZIP come from the build; changing allergies or diet there searches your items afresh.
- **Search.** Shows the stages while the backend searches, then the result **list first** (`ListView`): sections by store area (Produce, Dairy & eggs, Meat & seafood, Bakery & grains, Pantry, Leave out), a status per item (✓ buy, ⇄ swap, ? read the label), **Your pick** with its store and aisle, tap a row to tick it, ⌄ for the products and why. **Steps**, **Stores** and **All products** sit behind buttons. Without a build (a confirm card), the same list shows the items the shopping-list writer chose.
- **My lists** (sidebar). Every finished search is saved to your account as a checklist: items, quantities, status, your ticks, the steps and the brand you picked. Never store data: a saved list shows products again with **Find products again**. Ticks sync across devices.
- **Offline** (`offline.ts`, `public/sw.js`). The app keeps its own files and a copy of your lists on the device, so it opens and shows your lists without signal ("Offline: your saved copy"). Ticks made offline wait on the device and are sent when you're back online (the last tick wins). Store data and chats are never kept on the device.
- **"Not right? Ask a stronger model"**, under the title of a dish whose ingredients a model worked out, when the backend has a stronger model (`stronger_model` in `/api/config`). Not offered for a build: you chose the ingredients.
- **Chats** live on the server for 20 idle minutes and are never saved; the sidebar lists the open ones. Light and dark themes, and a phone layout with a drawer menu.

## Code

| File | |
|---|---|
| `src/App.tsx` | State: config, the logged-in user, chats, the open chat or saved list; every API call; refreshes a build while its products load; opens offline with the device's copy |
| `src/api.ts` | The backend's API, one function per endpoint; a 401 on a logged-in call returns to the login screen |
| `src/offline.ts` | The device's copies of lists and start-up data, the offline tick queue, clearing them on logout |
| `src/types.ts` | The backend's shapes. `Result` follows the `result.json` contract (`docs/result-v1.schema.json`), `Answer` is A3's answer; `BuildData`, `ProductChoice`, `SavedList` are the build and list shapes |
| `src/format.ts` | Labels, numbers, links, aisle text |
| `src/components/AuthScreen.tsx` | Log in and create an account |
| `src/components/PreferencesDialog.tsx` | Food preferences (also the onboarding after sign-up), favourite brands, password change, account deletion |
| `src/components/Sidebar.tsx` | The logged-in user, new chat, the chat list, My lists |
| `src/components/ChatView.tsx` | Header, messages, composer (it adds to an open build or summary), welcome screen |
| `src/components/BuildCard.tsx` | A guided build: steps, product picks with Change, additions and clashes |
| `src/components/ConfirmCard.tsx` | The request to check and edit before searching; at the end of a build, the products, favourite questions and "Anything else to add?" |
| `src/components/SearchCard.tsx` | Polls a search; progress, expired and failed states |
| `src/components/ResultView.tsx` | A finished search: its saved list with the live products (ListView) |
| `src/components/ListView.tsx` | The list-first checklist, shared by results and saved lists |
| `src/components/SavedListPage.tsx` | A saved list from My lists: ticks, Find products again, delete, offline copy |
| `src/components/Panels.tsx` | Stores and All products, behind the list's buttons |
| `src/components/controls.tsx` | Chips and segmented controls |
| `src/styles.css` | All styles; colors are CSS variables with a dark set |
| `public/sw.js`, `public/manifest.webmanifest`, `public/icon-*.png` | The service worker (the app's own files only, never `/api`), and the home-screen app |
