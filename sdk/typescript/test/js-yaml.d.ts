// js-yaml ships without types and @types/js-yaml is not in the pinned toolchain; the tests need only `load`.
declare module "js-yaml" {
  export function load(source: string): unknown;
}
