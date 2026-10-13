import { useSyncExternalStore } from "react";

/** A minimal observable state holder: views subscribe with `useStore`, logic stays testable without a DOM. */
export class Store<S> {
  #state: S;
  readonly #listeners = new Set<() => void>();

  constructor(initial: S) {
    this.#state = initial;
  }

  get state(): S {
    return this.#state;
  }

  set(update: Partial<S> | ((s: S) => S)): void {
    this.#state = typeof update === "function" ? update(this.#state) : { ...this.#state, ...update };
    for (const l of [...this.#listeners]) l();
  }

  readonly subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  };
}

export function useStore<S>(store: Store<S>): S {
  const get = (): S => store.state;
  return useSyncExternalStore(store.subscribe, get, get);
}

/** Drains an async iterable into an array (list endpoints page lazily; the views show at most `max` rows). */
export async function take<T>(it: AsyncIterable<T>, max: number): Promise<T[]> {
  const out: T[] = [];
  if (max <= 0) return out;
  for await (const x of it) {
    out.push(x);
    if (out.length >= max) break;
  }
  return out;
}
