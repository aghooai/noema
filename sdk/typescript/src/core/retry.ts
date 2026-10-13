/** Retry policy (IMPL-035): full jitter, base 0.5 s, cap 8 s, `maxRetries` 2 by default, honoring `retry-after`. */
export const DEFAULT_MAX_RETRIES = 2;
export const BACKOFF_BASE_MS = 500;
export const BACKOFF_CAP_MS = 8_000;
/** A `retry-after` above this is not waited for inside one call: the error is thrown instead. */
export const MAX_RETRY_AFTER_S = 60;

/** Time and randomness used by the retry loop; tests replace them for deterministic runs. */
export const retryClock = {
  random: (): number => Math.random(),
  sleep: (ms: number, signal?: AbortSignal): Promise<void> =>
    new Promise<void>((resolve, reject) => {
      if (signal?.aborted) {
        reject(signal.reason);
        return;
      }
      const onAbort = (): void => {
        clearTimeout(timer);
        reject(signal?.reason);
      };
      const timer = setTimeout(() => {
        signal?.removeEventListener("abort", onAbort);
        resolve();
      }, ms);
      signal?.addEventListener("abort", onAbort, { once: true });
    }),
};

/**
 * Delay before retry number `attempt + 1` (attempt 0 = after the first failure): the server's `retry-after` when
 * given, else a uniformly random delay in [0, min(cap, base * 2^attempt)). Null means do not retry.
 */
export function retryDelayMs(attempt: number, retryAfterS: number | null): number | null {
  if (retryAfterS !== null) return retryAfterS > MAX_RETRY_AFTER_S ? null : retryAfterS * 1000;
  return Math.floor(retryClock.random() * Math.min(BACKOFF_CAP_MS, BACKOFF_BASE_MS * 2 ** attempt));
}
