// Light, passive offline support for saved lists (no sync engine).
//
// - Every list or list of lists the app loads is copied to this browser's storage, under
//   the user's id, so it still shows without signal.
// - A tick that can't reach the server is applied locally and queued; the queue is sent,
//   in order, when the browser is back online or on the next load. The last tick wins.
// - Only the shopper's own data is kept: never products, stores or chats (Kroger data).
// - Logging out or deleting the account removes this user's copies and queue.
import { api, ApiError } from "./api";
import type { SavedList, SavedListSummary } from "./types";

const PREFIX = "pantrypath.offline.";
const summariesKey = (user: string) => `${PREFIX}${user}.lists`;
const listKey = (user: string, id: string) => `${PREFIX}${user}.list.${id}`;
const queueKey = (user: string) => `${PREFIX}${user}.queue`;

interface QueuedTick {
  listId: string;
  itemId: string;
  ticked: boolean;
}

function read<T>(key: string): T | null {
  try {
    const raw = window.localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : null;
  } catch {
    return null;
  }
}

function write(key: string, value: unknown) {
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // storage full or off (a private window): the app works online only
  }
}

/** A network failure (no signal), not an answer from the server. */
export const isOffline = (e: unknown) => e instanceof ApiError && e.status === 0;

export async function loadSummaries(user: string): Promise<{ lists: SavedListSummary[]; offline: boolean }> {
  try {
    const lists = await api.lists();
    write(summariesKey(user), lists);
    return { lists, offline: false };
  } catch (e) {
    const copy = read<SavedListSummary[]>(summariesKey(user));
    if (isOffline(e) && copy) return { lists: copy, offline: true };
    throw e;
  }
}

export async function loadList(user: string, id: string): Promise<{ list: SavedList; offline: boolean }> {
  try {
    const list = await api.list(id);
    write(listKey(user, id), list);
    return { list, offline: false };
  } catch (e) {
    const copy = read<SavedList>(listKey(user, id));
    if (isOffline(e) && copy) return { list: applyQueue(user, copy), offline: true };
    if (isOffline(e)) throw new ApiError(0, "You're offline and this list isn't on this device yet.");
    throw e;
  }
}

function applyQueue(user: string, list: SavedList): SavedList {
  const queue = read<QueuedTick[]>(queueKey(user)) ?? [];
  const ticks = new Map(queue.filter((q) => q.listId === list.id).map((q) => [q.itemId, q.ticked]));
  return { ...list, items: list.items.map((i) => (ticks.has(i.id) ? { ...i, ticked: ticks.get(i.id)! } : i)) };
}

/** Tick an item: saved to the account, or queued on this device when there's no signal.
 * Returns the list as it should now show. Throws only on a real server refusal. */
export async function tick(user: string, list: SavedList, itemId: string, ticked: boolean): Promise<SavedList> {
  const local = { ...list, items: list.items.map((i) => (i.id === itemId ? { ...i, ticked } : i)) };
  write(listKey(user, list.id), local);
  try {
    const saved = await api.tick(list.id, itemId, ticked);
    write(listKey(user, list.id), saved);
    return saved;
  } catch (e) {
    if (!isOffline(e)) throw e;
    const queue = (read<QueuedTick[]>(queueKey(user)) ?? []).filter(
      (q) => !(q.listId === list.id && q.itemId === itemId),
    );
    write(queueKey(user), [...queue, { listId: list.id, itemId, ticked }]);
    return local;
  }
}

/** Send queued ticks, in order; stops at the first one that still can't get through. */
export async function flushQueue(user: string): Promise<number> {
  const queue = read<QueuedTick[]>(queueKey(user)) ?? [];
  let sent = 0;
  for (const q of queue) {
    try {
      await api.tick(q.listId, q.itemId, q.ticked);
    } catch (e) {
      if (isOffline(e)) break; // still no signal: keep the rest
      // the list or item is gone (deleted elsewhere): drop the tick
    }
    sent += 1;
  }
  write(queueKey(user), queue.slice(sent));
  return sent;
}

export const pendingTicks = (user: string) => (read<QueuedTick[]>(queueKey(user)) ?? []).length;

/** Forget everything this device keeps for the user (logout, account deletion). */
export function clearDevice(user: string) {
  try {
    const prefix = `${PREFIX}${user}.`;
    for (let i = window.localStorage.length - 1; i >= 0; i -= 1) {
      const key = window.localStorage.key(i);
      if (key?.startsWith(prefix)) window.localStorage.removeItem(key);
    }
  } catch {
    // storage off: nothing to clear
  }
}

export function forgetList(user: string, id: string) {
  try {
    window.localStorage.removeItem(listKey(user, id));
  } catch {
    // storage off
  }
}

// The logged-in user whose copies are used (App sets it on login and clears it on logout)
let current: string | null = null;
export const setDeviceUser = (id: string | null) => {
  current = id;
};
export const deviceUser = () => current ?? "anonymous";

/** Tell whoever lists the saved lists (the sidebar) that they changed. */
export const LISTS_CHANGED = "pantrypath:lists-changed";
export const announceListsChanged = () => window.dispatchEvent(new Event(LISTS_CHANGED));

// The app's own start-up data (settings and who's logged in), so it can open offline
const bootKey = "pantrypath.offline.boot";
export function saveBoot(config: unknown, user: unknown) {
  write(bootKey, { config, user });
}
export function readBoot<C, U>(): { config: C; user: U | null } | null {
  return read<{ config: C; user: U | null }>(bootKey);
}
export function clearBoot() {
  try {
    window.localStorage.removeItem(bootKey);
  } catch {
    // storage off
  }
}
