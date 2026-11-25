"use client";
/**
 * `useLiveEvents` — one shared EventSource per tab on `/api/v1/notifications/stream` (through the BFF),
 * fanned out to subscribers. Reconnects with backoff. Events are refresh hints only: re-fetch through the
 * API, never trust event payloads for business state.
 */
import { useEffect, useRef, useState } from "react";
import { type LiveEvent, matchesEvent, parseLiveEvent, reconnectDelay } from "@/lib/live-events";

export type LiveStatus = "connecting" | "open" | "closed";

type Subscriber = { events?: readonly string[]; handler: (e: LiveEvent) => void };

const STREAM_URL = "/api/bff/api/v1/notifications/stream";
const subscribers = new Set<Subscriber>();
const statusListeners = new Set<(s: LiveStatus) => void>();
let source: EventSource | null = null;
let status: LiveStatus = "closed";
let attempts = 0;
let retryTimer: ReturnType<typeof setTimeout> | null = null;

function setStatus(next: LiveStatus) {
  status = next;
  for (const l of statusListeners) l(next);
}

function connect() {
  if (source || typeof window === "undefined" || typeof EventSource === "undefined") return;
  setStatus("connecting");
  const es = new EventSource(STREAM_URL, { withCredentials: true });
  source = es;
  es.onopen = () => {
    attempts = 0;
    setStatus("open");
  };
  es.onmessage = (msg: MessageEvent<string>) => {
    const evt = parseLiveEvent(msg.data);
    if (!evt) return;
    for (const s of subscribers) if (matchesEvent(evt, s.events)) s.handler(evt);
  };
  es.onerror = () => {
    if (es.readyState === EventSource.CLOSED) {
      // The server refused or ended the stream (e.g. 401/503): reconnect ourselves with backoff.
      es.close();
      source = null;
      setStatus("closed");
      if (subscribers.size > 0 && retryTimer === null) {
        retryTimer = setTimeout(() => {
          retryTimer = null;
          connect();
        }, reconnectDelay(attempts++));
      }
    } else {
      setStatus("connecting");
    }
  };
}

function disconnectIfIdle() {
  if (subscribers.size > 0) return;
  if (retryTimer) clearTimeout(retryTimer);
  retryTimer = null;
  source?.close();
  source = null;
  setStatus("closed");
}

export function useLiveEvents(
  handler: (e: LiveEvent) => void,
  opts: { events?: readonly string[]; enabled?: boolean } = {},
): LiveStatus {
  const handlerRef = useRef(handler);
  handlerRef.current = handler;
  const [state, setState] = useState<LiveStatus>(status);
  const enabled = opts.enabled ?? true;
  const key = (opts.events ?? []).join("|");

  useEffect(() => {
    if (!enabled) return;
    const events = key ? key.split("|") : undefined;
    const sub: Subscriber = { events, handler: (e) => handlerRef.current(e) };
    subscribers.add(sub);
    statusListeners.add(setState);
    connect();
    setState(status);
    return () => {
      subscribers.delete(sub);
      statusListeners.delete(setState);
      disconnectIfIdle();
    };
  }, [enabled, key]);

  return state;
}
