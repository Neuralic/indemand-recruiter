/** Vapi sometimes drops a character and leaves U+FFFD (usually an em dash). Between two letters it was most likely an apostrophe; elsewhere a
 *  spaced dash reads naturally. Same rule as the server's clean_text, so live captions and the stored transcript agree. */
export function cleanText(s: string): string {
  if (!s.includes('�')) return s
  return s.replace(/([A-Za-z])�(?=[A-Za-z])/g, "$1'").replace(/\s*�\s*/g, ' — ').replace(/\s+/g, ' ').trim()
}
