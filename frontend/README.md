# Pantry Path frontend

The chat app: React 19, TypeScript and Vite. It has no UI library and no state library.

## Run it

Start the backend first (`backend/README.md`), then:

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173; /api goes to the backend on :8000
```

`PANTRYPATH_API_URL=http://localhost:8765 npm run dev` points the dev server at a backend on another port. `npm run build` type-checks and writes `dist/`. When `dist/` exists, the backend serves it at http://localhost:8000, so a demo needs only the backend.

## What it does

- **Accounts.** Log in, or create an account with a name, username and password. Right after sign-up the app asks for your food preferences (you can skip). The sidebar shows who is logged in, with **Preferences** and **Log out**. Log out, and anyone else can log in on the same browser. The login is an HttpOnly cookie, so the app's code never handles it. If it expires or ends elsewhere (a password change on another browser), the app returns to the login screen and says why.
- **Preferences.** Name, ZIP, allergies, diet, household size, and needs we can't check yet, saved with the account. The same dialog changes the password or deletes the account; both ask for the current password.
- **Chat.** Type a dish ("Pad thai for 2, no peanuts"). The assistant asks for anything missing, then shows a **confirm card**: the dish, ZIP, allergens, diet and servings, read from the chat and your preferences. Each one says where it came from. Saved allergens are locked, and the diet can't go below the saved one. Doubts appear as "Please check" notes.
- **Search.** Shows the stages while the backend searches, then the results:
  - **Shopping list**, grouped by store, with check-off boxes; then substitutes, label checks and what to leave out.
  - **Stores**, with how many items to buy at each (and a route link in live mode).
  - **Every product checked**: each product's verdict (Safe, Not safe, Couldn't verify), reason and evidence source.
  - **Steps**, when the model wrote them, plus the warnings and the Open Food Facts attribution.
- **Old chats** reopen from the sidebar, each user seeing only their own. Their store results expire, because store data is never saved, and one click searches again.
- Light and dark themes, and a phone layout with a drawer menu.

## Code

| File | |
|---|---|
| `src/App.tsx` | State: config, the logged-in user, chats, the open chat; every API call; back to the login screen on logout or an expired login |
| `src/api.ts` | The backend's API, one function per endpoint; a 401 on a logged-in call returns to the login screen |
| `src/types.ts` | The backend's shapes. `Result` follows the `result.json` contract (`docs/result-v1.schema.json`), `Answer` is A3's answer |
| `src/components/AuthScreen.tsx` | Log in and create an account |
| `src/components/PreferencesDialog.tsx` | Food preferences (also the onboarding after sign-up), password change, account deletion |
| `src/components/Sidebar.tsx` | The logged-in user, new chat, the chat list |
| `src/components/ChatView.tsx` | Header, messages, composer, welcome screen |
| `src/components/ConfirmCard.tsx` | The request to check and edit before searching |
| `src/components/SearchCard.tsx` | Polls a search; progress, expired and failed states |
| `src/components/ResultView.tsx` | Shopping list, stores, products, steps, warnings |
| `src/components/controls.tsx` | Chips and segmented controls |
| `src/styles.css` | All styles; colors are CSS variables with a dark set |
