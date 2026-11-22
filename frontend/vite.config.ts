import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Node's process, without pulling in @types/node for one line
declare const process: { env: Record<string, string | undefined> };

// The backend runs on :8000 (cd backend && uvicorn app.main:app --reload). The dev server
// forwards /api there, so the app and the API share one origin: the login cookie just
// works and there's no CORS. PANTRYPATH_API_URL points it at another backend.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": process.env.PANTRYPATH_API_URL ?? "http://localhost:8000" },
  },
});
