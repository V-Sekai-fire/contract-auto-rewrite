# contract-auto-rewrite

A small, standalone C++ `auto` gate for this org. It enforces the "avoid
autos" policy at two levels:

- **check** — dependency-free, pure-stdlib gate. Fails on *all* standalone
  `auto` type keywords in changed C++ files (`.cpp` / `.h` / `.hpp` / `.cc` /
  `.cxx`). Comments, string/char literals and raw-string literals are ignored.
  Range-for (`for (auto &x : v)`) and ternary `if (auto w = ...) {}` count.
- **rewrite** (local, optional) — replaces `auto` with the concrete type
  Clang deduces, using `libclang` (pip `clang`) + a `compile_devices.json`.
  Conservative: ambiguous types (dependent types, template params, `__`
  internal identifiers) are left alone.

## Gate behavior

- **Fail-only.** CI runs `check` only. It never rewrites or commits anything.
- CI checks *changed* C++ files in a pull request (and merge group) against the
  base branch.
- **Self-test** is the negative control: it proves the gate's own outputs are
  correct (a pass file is clean; an `auto` decl is flagged; comment/string/raw
  auto is ignored). CI runs the self-test before the check, so a check whose
  logic has drifted cannot read as a pass.

## Usage

```
# Gate (CI) — fail on any auto in the listed files
python auto-rewrite.py check --changed-file changed-cpp.txt
python auto-rewrite.py check --files src/foo.cpp include/bar.hpp

# Local rewrite (dry-run, reports count)
python auto-rewrite.py rewrite --files src/foo.cpp --ccfile build3/compile_commands.json

# Local rewrite (in-place)
python auto-rewrite.py rewrite --files src/foo.cpp --ccfile build3/compile_commands.json --in-place

# Self-test (negative control)
python auto-rewrite.py self-test
```

`--ccfile` is a `compile_commands.json`. The tool extracts the compiler command
for the source, strips the compiler binary / `-o` / `-c`, and adds the Xcode
SDK include paths so `libclang` resolves the C++ standard library.

## Notes

- `check` needs no `compile_commands.json` and no `libclang` — it is a token
  scan, so it runs on any CI runner.
- `rewrite` needs `pip install clang`. It is deliberately NOT used in CI.
- The gate is opinionated: a standalone `auto` anywhere in a changed C++ file
  fails the check. Use the `rewrite` helper locally when you want the concrete
  type instead of the policy flag.
