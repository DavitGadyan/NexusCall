"use client";
/**
 * Renders untrusted HTML (email bodies and similar) sanitised with DOMPurify inside a fully sandboxed
 * iframe: no scripts, no same-origin access, no forms, no remote images (tracking pixels) by default.
 */
import * as React from "react";

const FRAME_CSP = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:";

export function SafeHtmlFrame({ html, title, minHeight = 240 }: { html: string; title: string; minHeight?: number }) {
  const [doc, setDoc] = React.useState<string | null>(null);
  React.useEffect(() => {
    let cancelled = false;
    // Loaded in the browser only, so the server bundle never pulls in jsdom.
    void import("isomorphic-dompurify").then(({ default: DOMPurify }) => {
      if (cancelled) return;
      const clean = DOMPurify.sanitize(html, {
        USE_PROFILES: { html: true },
        FORBID_TAGS: ["style", "form", "input", "button", "iframe", "object", "embed", "link", "meta", "base"],
        FORBID_ATTR: ["srcset", "action", "formaction"],
        ALLOW_UNKNOWN_PROTOCOLS: false,
      });
      setDoc(
        `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${FRAME_CSP}">` +
          `<base target="_blank"><style>body{font:14px/1.5 system-ui,sans-serif;margin:12px;color:#111;word-wrap:break-word}img{max-width:100%}</style></head><body>${clean}</body></html>`,
      );
    });
    return () => {
      cancelled = true;
    };
  }, [html]);
  if (doc === null) return <div className="animate-pulse rounded-md bg-surface-2" style={{ minHeight }} aria-busy="true" />;
  return (
    <iframe
      title={title}
      sandbox=""
      referrerPolicy="no-referrer"
      srcDoc={doc}
      className="w-full rounded-md border border-border bg-white"
      style={{ minHeight }}
    />
  );
}
