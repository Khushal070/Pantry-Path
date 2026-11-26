import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import type { Job, Message, SearchData, StrongerModel } from "../types";
import { cap } from "../format";
import { AlertIcon, CheckIcon, InfoIcon, RefreshIcon } from "../icons";
import { ResultView } from "./ResultView";

const POLL_MS = 1000;

/** A confirmed search: progress while it runs, then the results. Results live in the
 * backend's memory only, so an old chat may find them expired and offer to search again. */
export function SearchCard({
  message,
  mode,
  stronger,
  onRerun,
  onAskStronger,
}: {
  message: Message;
  mode: "demo" | "live";
  stronger?: StrongerModel;
  onRerun: () => Promise<void>;
  onAskStronger?: () => Promise<void>;
}) {
  const data = message.data as unknown as SearchData;
  const [job, setJob] = useState<Job | null>(null);
  const [expired, setExpired] = useState(false);
  const [offline, setOffline] = useState(false);
  const [busy, setBusy] = useState(false);
  const [rerunError, setRerunError] = useState<string | null>(null);

  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    setJob(null);
    setExpired(false);
    const poll = async () => {
      try {
        const next = await api.job(data.job_id);
        if (stopped) return;
        setJob(next);
        setOffline(false);
        if (next.status === "running") timer = window.setTimeout(poll, POLL_MS);
      } catch (e) {
        if (stopped) return;
        if (e instanceof ApiError && e.status === 404) {
          setExpired(true);
        } else {
          setOffline(true);
          timer = window.setTimeout(poll, POLL_MS * 3);
        }
      }
    };
    poll();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [data.job_id]);

  async function rerun() {
    setBusy(true);
    setRerunError(null);
    try {
      await onRerun();
    } catch (e) {
      setRerunError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const dish = cap(data.dish ?? data.request.query);
  const retry = (label: string) => (
    <>
      <button className="btn ghost" onClick={rerun} disabled={busy}>
        <RefreshIcon size={16} /> {busy ? "Starting…" : label}
      </button>
      {rerunError && <p className="form-error">{rerunError}</p>}
    </>
  );

  // No store near the ZIP: nothing to retry, so say why and what to do instead
  const noStores =
    (job?.status === "failed" && job.error_kind === "no_stores") ||
    (expired && data.status === "failed" && data.error_kind === "no_stores");
  if (noStores) {
    return (
      <div className="card" role="status">
        <p className="callout info">
          <InfoIcon size={16} /> {job?.error ?? data.error}
        </p>
      </div>
    );
  }
  if (expired) {
    return (
      <div className="card muted-card">
        <p>
          <strong>{dish}</strong>: these results have expired. Store data is never saved, so I need to
          search again.
        </p>
        {retry("Search again")}
      </div>
    );
  }
  if (job?.status === "failed") {
    return (
      <div className="card error-card" role="alert">
        <p className="row gap-sm">
          <AlertIcon size={18} /> {job.error ?? "The search failed."}
        </p>
        {retry("Try again")}
      </div>
    );
  }
  if (job?.status === "done" && job.result) {
    // offered on the first search only: a redone one shows which model it used instead
    const offer =
      stronger?.available && onAskStronger && !data.stronger
        ? { label: stronger.label ?? "a stronger model", onAsk: onAskStronger }
        : null;
    return (
      <ResultView job={job} mode={mode} listKey={`pantrypath.ticked.${message.id}`} stronger={offer} />
    );
  }
  return <Progress job={job} query={dish} mode={mode} offline={offline} />;
}

function Progress({
  job,
  query,
  mode,
  offline,
}: {
  job: Job | null;
  query: string;
  mode: "demo" | "live";
  offline: boolean;
}) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const stage = job?.stage ?? "queued";
  const searching = stage === "queued" || stage === "searching";
  const seconds = job ? Math.max(0, Math.round((now - new Date(job.created_at).getTime()) / 1000)) : 0;
  const steps = [
    { label: "Read your request", state: "done" },
    { label: "Find nearby stores and check every product's label", state: searching ? "active" : "done" },
    { label: "Write your shopping list", state: stage === "answering" ? "active" : searching ? "todo" : "done" },
  ];
  return (
    <div className="card progress" aria-live="polite">
      <h3 className="card-title">Searching for {query}</h3>
      <ol className="steps">
        {steps.map((s) => (
          <li key={s.label} className={s.state}>
            <span className="step-dot">{s.state === "done" ? <CheckIcon size={12} /> : null}</span>
            {s.label}
          </li>
        ))}
      </ol>
      <small className="muted">
        {offline
          ? "Lost touch with the server; still trying…"
          : `${seconds}s${mode === "live" ? " · a live search can take a minute or two" : ""}`}
      </small>
    </div>
  );
}
