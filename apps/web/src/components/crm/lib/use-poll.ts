"use client";
/** Re-runs `tick` with a growing delay while `active` is true (async jobs: imports, exports, ingestion). */
import { useEffect, useRef } from "react";
import { nextPollDelay } from "./http";

export function usePoll(tick: () => void, active: boolean): void {
  const tickRef = useRef(tick);
  tickRef.current = tick;
  useEffect(() => {
    if (!active) return;
    let attempt = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const schedule = () => {
      const delay = nextPollDelay(attempt++);
      if (delay === null) return;
      timer = setTimeout(() => {
        if (typeof document === "undefined" || document.visibilityState !== "hidden") tickRef.current();
        schedule();
      }, delay);
    };
    schedule();
    return () => {
      if (timer) clearTimeout(timer);
    };
  }, [active]);
}
