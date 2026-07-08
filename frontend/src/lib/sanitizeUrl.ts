/**
 * URL scheme allowlist for externally-sourced links.
 *
 * RAG source URLs originate from third-party RSS `<link>` values. The ingest
 * pipeline validates schemes server-side (WS2), but rows written before that
 * fix — or any future ingest path — must still never reach an `href`
 * unchecked: a `javascript:`/`data:` URI rendered as a clickable citation is
 * a stored-XSS surface. React escapes text, NOT URL attribute values.
 *
 * Allowlist, never blocklist: only http(s) may be rendered as a link.
 */
const ALLOWED_PROTOCOLS = new Set(['http:', 'https:']);

/**
 * Returns the URL when its scheme is http/https, else null (render as plain
 * text). Relative and malformed URLs return null — source citations are
 * always absolute, so anything else is suspect by construction.
 */
export function sanitizeUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    return ALLOWED_PROTOCOLS.has(new URL(url).protocol) ? url : null;
  } catch {
    return null;
  }
}
