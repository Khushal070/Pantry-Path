import { useEffect, useState } from "react";
import { api } from "../api";
import type { Job, SavedItem, SavedList } from "../types";
import { cap, clockTime } from "../format";
import { AlertIcon, ChevronIcon, MenuIcon, RefreshIcon, TrashIcon } from "../icons";
import {
  announceListsChanged,
  deviceUser,
  flushQueue,
  forgetList,
  loadList,
  pendingTicks,
  tick,
} from "../offline";
import { ListView } from "./ListView";

const POLL_MS = 1000;

/** A saved list from My lists: opens from the account (or this device's copy offline),
 * ticks sync, and "Find products again" fetches fresh store products (never saved). */
export function SavedListPage({
  listId,
  mode,
  onBack,
  onDeleted,
  onOpenMenu,
}: {
  listId: string;
  mode: "demo" | "live";
  onBack: () => void;
  onDeleted: () => void;
  onOpenMenu: () => void;
}) {
  const [list, setList] = useState<SavedList | null>(null);
  const [offline, setOffline] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [finding, setFinding] = useState(false);
  const [pending, setPending] = useState(0);

  useEffect(() => {
    let stopped = false;
    setList(null);
    setJob(null);
    setError(null);
    flushQueue(deviceUser())
      .catch(() => 0)
      .then(() => loadList(deviceUser(), listId))
      .then((r) => {
        if (stopped) return;
        setList(r.list);
        setOffline(r.offline);
        setPending(pendingTicks(deviceUser()));
      })
      .catch((e) => !stopped && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      stopped = true;
    };
  }, [listId]);

  // back online: send the queued ticks and reload
  useEffect(() => {
    const online = async () => {
      await flushQueue(deviceUser());
      const r = await loadList(deviceUser(), listId).catch(() => null);
      if (r) {
        setList(r.list);
        setOffline(r.offline);
      }
      setPending(pendingTicks(deviceUser()));
      announceListsChanged();
    };
    window.addEventListener("online", online);
    return () => window.removeEventListener("online", online);
  }, [listId]);

  async function onTick(item: SavedItem) {
    if (!list) return;
    try {
      setList(await tick(deviceUser(), list, item.id, !item.ticked));
      setPending(pendingTicks(deviceUser()));
      announceListsChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't save that tick.");
    }
  }

  async function findProducts() {
    if (!list) return;
    setFinding(true);
    setError(null);
    try {
      let next = await api.findProducts(list.id);
      while (next.status === "running") {
        await new Promise((r) => window.setTimeout(r, POLL_MS));
        next = await api.job(next.id);
      }
      if (next.status === "failed") setError(next.error ?? "The store search didn't work.");
      setJob(next);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setFinding(false);
    }
  }

  async function remove() {
    if (!list || !window.confirm(`Delete the list for "${cap(list.dish)}"?`)) return;
    try {
      await api.deleteList(list.id);
      forgetList(deviceUser(), list.id);
      announceListsChanged();
      onDeleted();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  const result = job?.status === "done" ? job.result : null;
  return (
    <section className="chat saved-page" aria-label="Saved list">
      <header className="chat-head">
        <button className="icon-btn only-mobile" onClick={onOpenMenu} aria-label="Open menu">
          <MenuIcon />
        </button>
        <button className="btn ghost small back" onClick={onBack}>
          <ChevronIcon size={14} className="flip" /> Chat
        </button>
        <h1 className="chat-title-main">{list ? cap(list.dish) : "Saved list"}</h1>
        {list && (
          <button className="icon-btn" onClick={remove} aria-label="Delete this list" title="Delete this list">
            <TrashIcon size={16} />
          </button>
        )}
      </header>
      <div className="messages">
        <div className="messages-inner">
          {error && (
            <p className="callout warn" role="alert">
              <AlertIcon size={16} /> {error}
            </p>
          )}
          {!list && !error && <p className="muted">Loading your list…</p>}
          {list && (
            <>
              <ListView
                list={list}
                result={result}
                mode={mode}
                onTick={onTick}
                notice={
                  offline ? (
                    <span className="pill warn">Offline: your saved copy</span>
                  ) : (
                    <span className="muted small">Saved {new Date(list.created_at).toLocaleDateString()}</span>
                  )
                }
                actions={
                  <button className="btn primary" onClick={findProducts} disabled={finding || offline}>
                    <RefreshIcon size={16} />{" "}
                    {finding ? "Finding products…" : result ? "Find products again" : "Find products near " + list.zip}
                  </button>
                }
              />
              {pending > 0 && (
                <p className="muted small">
                  {pending} tick{pending === 1 ? "" : "s"} waiting for signal: they'll be saved when you're back online.
                </p>
              )}
              {job?.status === "done" && (
                <p className="footnote">
                  {[
                    ...job.warnings,
                    job.expires_at && `Store details expire at ${clockTime(job.expires_at)} and are never saved.`,
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                </p>
              )}
            </>
          )}
        </div>
      </div>
    </section>
  );
}
