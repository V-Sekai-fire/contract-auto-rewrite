# contract-auto-rewrite

**A standalone C++ `auto` gate.**

Your C++ carries standalone `auto` the "avoid autos" policy wants gone.

**check** — dependency-free, pure stdlib; no `compile_commands.json`, no `libclang`. Fails on *all* standalone `auto` in changed C++ files (`.cpp`/`.h`/`.hpp`/`.cc`/`.cxx`), ignoring comments and string/char/raw-string bodies; range-for `for (auto &x : v)` and `if (auto w = ...) {}` count.
**rewrite** (local) swaps `auto` for the concrete type Clang deduces, via `libclang` (from pixi) + `compile_commands.json`; conservative, leaves ambiguous types alone.

## Gate behavior

**Fail-only.** CI runs `check` only on changed C++ files in a pull request (and merge group) against the base branch; it never rewrites or commits. `self-test` is the negative control (a clean file passes, an `auto` decl fails) and runs before `check` so a drifted gate cannot read as a pass.

Open a pull request to see the gate fire.

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

`--ccfile` is a `compile_commands.json`; it strips the compiler binary/`-o`/`-c` and adds the Xcode SDK include paths so `libclang` resolves the C++ stdlib.

`check` needs no `compile_commands.json`/`libclang`; `rewrite` needs `libclang` (from pixi), deliberately not used in CI. A standalone `auto` in a changed C++ file fails the check.

Try `rewrite` locally to swap `auto` for the concrete type.
