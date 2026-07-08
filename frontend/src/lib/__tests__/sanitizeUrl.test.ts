/**
 * sanitizeUrl tests (WS8)
 * =======================
 * Scheme allowlist for externally-sourced hrefs — anything but http(s)
 * must never render as a link.
 */
import { describe, it, expect } from 'vitest';
import { sanitizeUrl } from '@/lib/sanitizeUrl';

describe('sanitizeUrl', () => {
  it.each([
    'https://example.com/article',
    'http://example.com/article?x=1#frag',
    'https://sub.domain.example.co.in/path',
  ])('allows %s', (url) => {
    expect(sanitizeUrl(url)).toBe(url);
  });

  it.each([
    'javascript:alert(1)',
    'JAVASCRIPT:alert(1)',
    'JaVaScRiPt:alert(1)',
    ' javascript:alert(1)',                       // leading whitespace trick
    'data:text/html,<script>alert(1)</script>',
    'vbscript:msgbox(1)',
    'file:///etc/passwd',
    'ftp://example.com/x',
    'blob:https://example.com/uuid',
  ])('rejects %s', (url) => {
    expect(sanitizeUrl(url)).toBeNull();
  });

  it('rejects relative and malformed URLs (citations are always absolute)', () => {
    expect(sanitizeUrl('/relative/path')).toBeNull();
    expect(sanitizeUrl('not a url')).toBeNull();
    expect(sanitizeUrl('//scheme-relative.example.com')).toBeNull();
  });

  it('handles null/undefined/empty', () => {
    expect(sanitizeUrl(null)).toBeNull();
    expect(sanitizeUrl(undefined)).toBeNull();
    expect(sanitizeUrl('')).toBeNull();
  });
});
