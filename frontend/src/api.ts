// The backend's API (backend/app/main.py). In development Vite forwards /api to :8000.
// The login is an HttpOnly cookie the browser sends by itself; this code never sees it.
import type {
  AppConfig,
  ChatReply,
  Job,
  Preferences,
  SearchRequest,
  Session,
  SessionSummary,
  User,
} from "./types";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

// Called when a request that needs a login gets 401 (the login expired or was ended
// elsewhere): the app goes back to the login screen.
let onLoggedOut: (() => void) | null = null;
export function whenLoggedOut(handler: (() => void) | null) {
  onLoggedOut = handler;
}

async function call<T>(method: string, path: string, body?: unknown, needsLogin = true): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      method,
      credentials: "same-origin",
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "Can't reach the Pantry Path server. Is the backend running?");
  }
  if (response.status === 401 && needsLogin) onLoggedOut?.();
  if (response.status === 204) return undefined as T;
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ApiError(response.status, detailText(payload) ?? `Request failed (${response.status})`);
  }
  return payload as T;
}

/** FastAPI errors: {detail: "text"} or {detail: [{msg, loc}, ...]} for validation. */
function detailText(payload: unknown): string | null {
  const detail = (payload as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail.length) {
    const first = detail[0] as { msg?: string; loc?: unknown[] };
    const field = first.loc?.[first.loc.length - 1];
    return field ? `${String(field)}: ${first.msg}` : (first.msg ?? null);
  }
  return null;
}

export const api = {
  config: () => call<AppConfig>("GET", "/config", undefined, false),

  // accounts
  me: () => call<User>("GET", "/me", undefined, false), // 401 here just means "not logged in"
  register: (name: string, username: string, password: string) =>
    call<User>("POST", "/auth/register", { name, username, password }, false),
  login: (username: string, password: string) =>
    call<User>("POST", "/auth/login", { username, password }, false),
  logout: () => call<void>("POST", "/auth/logout", undefined, false),
  savePreferences: (prefs: Preferences) => call<User>("PUT", "/me/preferences", prefs),
  changePassword: (currentPassword: string, newPassword: string) =>
    call<void>("POST", "/me/password", { current_password: currentPassword, new_password: newPassword }),
  deleteAccount: (password: string) => call<void>("POST", "/me/delete", { password }),

  // chats
  sessions: () => call<SessionSummary[]>("GET", "/sessions"),
  createSession: () => call<Session>("POST", "/sessions"),
  session: (id: string) => call<Session>("GET", `/sessions/${id}`),
  deleteSession: (id: string) => call<void>("DELETE", `/sessions/${id}`),
  say: (sessionId: string, text: string) =>
    call<ChatReply>("POST", `/sessions/${sessionId}/messages`, { text }),
  search: (sessionId: string, messageId: string, request: SearchRequest, servings: number | null) =>
    call<ChatReply>("POST", `/sessions/${sessionId}/search`, {
      message_id: messageId,
      ...request,
      servings,
    }),
  rerun: (sessionId: string, messageId: string) =>
    call<ChatReply>("POST", `/sessions/${sessionId}/messages/${messageId}/rerun`),
  /** Search again with the stronger model working out the ingredients: a new search message. */
  rerunStronger: (sessionId: string, messageId: string) =>
    call<ChatReply>("POST", `/sessions/${sessionId}/messages/${messageId}/rerun-stronger`),
  job: (id: string) => call<Job>("GET", `/jobs/${id}`),
};
