/**
 * A tiny latest-request-wins guard for async UI work.
 *
 * AbortController is still useful for saving network work, but it is not a
 * correctness boundary: a response may already be queued when abort runs.
 * Call begin() before an async operation and check isCurrent() before every
 * state write. invalidate() retires all outstanding tokens when the owning
 * user/resource/route scope changes.
 */
export interface RequestEpoch {
  begin: () => number;
  invalidate: () => void;
  isCurrent: (token: number) => boolean;
}

export function createRequestEpoch(): RequestEpoch {
  let epoch = 0;
  return {
    begin: () => {
      epoch += 1;
      return epoch;
    },
    invalidate: () => {
      epoch += 1;
    },
    isCurrent: (token) => token === epoch,
  };
}
