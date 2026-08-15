/**
 * Last-request-wins guard for overlapping async work.
 *
 * The content script's reload() runs from a timer, a URL watcher and a side
 * toggle watcher, so two reloads can overlap. Without a guard the slowest
 * response paints last, and a stale contract sits beside a fresh order ticket.
 * Every reload takes a token at start and may only mutate the panel while its
 * token is still the newest.
 */
export class Latest {
  private generation = 0;

  /** Start one unit of work; returns the token it must present to write. */
  begin(): number {
    this.generation += 1;
    return this.generation;
  }

  /** True while 'token' is still the newest unit of work. */
  isCurrent(token: number): boolean {
    return token === this.generation;
  }
}
