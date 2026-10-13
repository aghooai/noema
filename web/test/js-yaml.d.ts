// js-yaml ships without types and @types/js-yaml is not in the pinned toolchain; the tests use only `load` and `dump`.
declare module "js-yaml" {
  export function load(source: string): unknown;
  export function dump(value: unknown): string;
}
