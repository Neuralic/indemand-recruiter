import type Vapi from '@vapi-ai/web'

/**
 * @vapi-ai/web 3.1.0 is CommonJS (`exports.default = Vapi`, `__esModule: true`). Depending on the bundler's interop, a dynamic
 * import() hands back either the class as `.default`, or the whole `exports` object as `.default` with the class one level deeper.
 * Vite 8 / rolldown does the latter, so `new mod.default(...)` threw "t is not a constructor". Accept both shapes.
 */
export async function loadVapi(): Promise<typeof Vapi> {
  const mod = (await import('@vapi-ai/web')) as unknown as { default?: unknown }
  let c: unknown = mod.default
  for (let i = 0; i < 3 && typeof c !== 'function' && c && typeof c === 'object'; i++) c = (c as { default?: unknown }).default
  if (typeof c !== 'function') throw new Error('The voice SDK did not load (no constructor found in @vapi-ai/web)')
  return c as typeof Vapi
}

/** A readable one-line description of anything thrown or emitted by the SDK, for the console and the "Technical details" box. */
export function describeError(e: unknown): string {
  if (e instanceof Error) return `${e.name}: ${e.message}`
  try {
    const s = JSON.stringify(e)
    return s && s !== '{}' ? s.slice(0, 600) : String(e)
  } catch {
    return String(e)
  }
}
