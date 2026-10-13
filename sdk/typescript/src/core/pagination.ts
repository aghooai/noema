/** A list page as the API returns it (`SessionList`, `UsageReport`, `CalibratorList`, ...). */
export interface PageBody<T> {
  data: T[];
  has_more: boolean;
  next_cursor: string | null;
}

/**
 * Lazily auto-paging iterable: each iteration starts at `after` (undefined = the first page) and follows
 * `next_cursor` while `has_more`. A page is fetched only when the previous one is exhausted.
 */
export function paginate<T>(
  fetchPage: (after: string | undefined) => Promise<PageBody<T>>,
  after?: string,
): AsyncIterable<T> {
  return {
    async *[Symbol.asyncIterator]() {
      let cursor = after;
      for (;;) {
        const page = await fetchPage(cursor);
        yield* page.data;
        if (!page.has_more || page.next_cursor === null) return;
        cursor = page.next_cursor;
      }
    },
  };
}
