// This browser-targeted tsconfig carries no @types/node (its `lib` is
// DOM-only). layout.test.tsx needs one real Node builtin -- a disk read of
// styles.css, so the test exercises the actual shipped file rather than a
// hand-transcribed copy that could silently drift. This file declares just
// that one function's shape rather than pulling all of @types/node's
// ambient globals (process, Buffer, timer-return-type overrides, ...) into
// the whole app's typecheck.
//
// A script-mode file (no top-level import/export of its own) is required
// here: inside a module file, `declare module "x" { ... }` augments an
// existing module rather than introducing a new ambient one.
declare module "node:fs" {
  export function readFileSync(path: string, encoding: string): string;
}
