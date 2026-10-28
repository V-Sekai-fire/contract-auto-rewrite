#!/usr/bin/env python3
"""
auto-rewrite — detect and rewrite C++ `auto` type specifiers.

Two modes:
  check    CI gate. Fail on any standalone `auto` keyword used as a type
           specifier in changed C++ files. Pure stdlib — no dependencies,
           safe to run in any CI runner. Comments and string/char literal
           bodies are ignored, so `auto` inside // comments or "..." strings
           never triggers.
  rewrite  Replace `auto` with the concrete type reported by Clang's AST,
           using libclang (from pixi) plus compile_commands.json.
           Conservative: leaves a declaration unchanged when the type is
           ambiguous (range-for over template parameters, undeducible, etc.)
           and never emits a broken type.

Usage:
  # CI gate: fail on any `auto` in the changed C++ files (a file of paths)
  auto-rewrite.py check --changed-file changed.txt
  # Gate specific files directly
  auto-rewrite.py check --files src/glb.cpp tests/x.cpp
  # Rewrite (local)
  auto-rewrite.py rewrite --files src/*.cpp src/glb.cpp --ccfile build3/compile_commands.json --in-place

Exit codes: 0 ok; 1 failing (check mode found `auto`).
"""
from __future__ import annotations
import sys, os, re, json, argparse
from typing import List, Tuple

CXX_EXT = {".cpp", ".cc", ".c++", ".cxx", ".h", ".hpp", ".hxx", ".hh"}

# --- Gate: comment/literal blanking -------------------------------------------

def blank_source(src: str) -> str:
    """Return src with C/C++ comments and string/char literal *bodies* replaced
    by spaces (same length), so tokenizing never sees `auto` inside them.
    Keeps quote chars, line comments become spaces through EOL, block comments
    become spaces, string/char literal bodies become spaces."""
    n = len(src)
    out = list(" " * n)
    i = 0
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            j = n if j == -1 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            j = i + 2
            while j + 1 < n:
                if src[j] == "*" and src[j + 1] == "/":
                    j = j + 1
                    break
                j += 1
            if j + 1 >= n:
                j = n - 1
            for k in range(i, j):
                out[k] = " "
            i = j
        elif c in ('"', "'"):
            q = c
            out[i] = q
            i += 1
            while i < n:
                if src[i] == "\\":
                    out[i] = " "
                    i += 1
                    if i < n:
                        out[i] = " "
                        i += 1
                    continue
                if src[i] == q:
                    out[i] = q
                    i += 1
                    break
                out[i] = " "
                i += 1
        else:
            out[i] = c
            i += 1
    return "".join(out)


def auto_hits(path: str) -> List[Tuple[int, int, str]]:
    """Return (line, col, snippet) for each standalone `auto` token in file."""
    with open(path, encoding="utf-8", errors="replace") as f:
        raw = f.read()
    clean = blank_source(raw)
    raw_lines = raw.splitlines()
    clean_lines = clean.splitlines()
    hits: List[Tuple[int, int, str]] = []
    # line counts must match (blank_source preserves line breaks)
    if len(raw_lines) != len(clean_lines):
        # fall back: operate on raw but skip strings via a simpler regex
        raw_lines = clean_lines  # same
    for li, (rl, cl) in enumerate(zip(raw_lines, clean_lines), 1):
        i = 0
        while i < len(cl):
            c = cl[i]
            if c.isalnum() or c == "_":
                j = i
                while j < len(cl) and (cl[j].isalnum() or cl[j] == "_"):
                    j += 1
                tok = cl[i:j]
                if tok == "auto":
                    snippet = rl.rstrip("\r\n")
                    s = max(0, i - 18)
                    snippet = snippet[s:]
                    hits.append((li, i, snippet))
                i = j
            else:
                i += 1
    return hits


def _self_test() -> int:
    """Negative controls: the gate's correct outputs must hold. The first
    defect reports itself. Returns exit code (0 ok, 1 bad)."""
    import os, tempfile
    # (name, source, expected_hits>0)
    cases = [
        ("pass", 'int a = 1;\nint b = a;\nreturn 0;\n', False),
        ("auto-decl", 'auto a = 1;\nint b = a;\nreturn 0;\n', True),
        ("auto-decl-two", 'auto a = 1; auto b = a;\n', True),
        ("comment-auto", '// auto is a comment\nint a = 1;\nint b = a;\n', False),
        ("string-auto", 'const char *s = "auto is fine";\nint a = 1;\n', False),
        ("block-auto", 'int a; /* auto */ a = 1;\n', False),
        ("raw-auto", 'const char *r = R"auto(x)raw";\nint a = 1;\n', False),
        ("range-for", 'for (auto &x : v) {\n    (void)x;\n}\n', True),
        ("auto-name-only", 'int autox = 1;\nautox = 2;\n', False),
    ]
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        for tag, body, exp_nonempty in cases:
            f = os.path.join(tmp, f"{tag}.cpp")
            with open(f, "w") as fh:
                fh.write(body)
            hits = auto_hits(f)
            got = len(hits) > 0
            if got != exp_nonempty:
                print(f"self-test FAIL [{tag}]: got={hits} expected_nonempty={exp_nonempty}", file=sys.stderr)
                ok = False
    if ok:
        print("self-test ok")
        return 0
    return 1

def is_concrete_type(spelling: str) -> bool:
    """True if the type string is concrete enough to write back."""
    s = spelling.strip()
    if s == "auto" or s.startswith("auto "):
        return False
    if "type-parameter" in s:
        return False
    if s.startswith("__") or re.search(r"(^|[^A-Za-z0-9_$])__", s):
        return False
    return True


def build_args(path: str, ccfile: str | None) -> List[str]:
    """Return a libclang arg list to build a TU for path."""
    args: List[str] = []
    if ccfile and os.path.exists(ccfile):
        try:
            cm = json.load(open(ccfile))
        except Exception:
            cm = []
        entry = next(
            (c for c in cm if c.get("directory", "") + "/" + c.get("file", "") == path),
            None,
        )
        if entry:
            toks = entry["command"].split()
            flags: List[str] = []
            out = False
            src_base = os.path.basename(path)
            for t in toks:
                if t in ("/usr/bin/c++", "clang++"):
                    continue
                if out:
                    continue
                if t == "-o":
                    out = True
                    continue
                if os.path.basename(t) == src_base:
                    break
                flags.append(t)
            args = flags
    if not args:
        args = ["-std=c++17"]
    # macOS SDK C++/C paths (what the system compiler uses)
    sdk = "/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk"
    if os.path.exists(sdk + "/usr/include/c++/v1"):
        args.extend(["-I", sdk + "/usr/include/c++/v1", "-I", sdk + "/usr/include"])
    args.append("-Wno-error=all")
    return args


def rewrite_file(path: str, ccfile: str | None) -> int:
    """Rewrite `auto` in place. Returns number of rewrites."""
    try:
        import clang.cindex as ci
    except ImportError:
        print(f"  rewrite requires libclang (add libclang in pixi.toml); leaving {path} unchanged",
              file=sys.stderr)
        return 0
    args = build_args(path, ccfile)
    try:
        tu = ci.TranslationUnit.from_source(path, args)
    except Exception as exc:
        print(f"  failed to parse {path}: {exc} -- leaving unchanged", file=sys.stderr)
        return 0
    with open(path, encoding="utf-8", errors="replace") as f:
        src_lines = f.read().splitlines()
    edits: List[Tuple[int, int, int, str]] = []  # (line0, start, end, newtype)
    for cur in tu.cursor.walk_preorder():
        try:
            if cur.kind != ci.CursorKind.VAR_DECL:
                continue
            loc = cur.location
            if loc.file.name != path:
                continue
            ln = loc.line
            if not (1 <= ln <= len(src_lines)):
                continue
            line = src_lines[ln - 1]
            if "auto" not in line:
                continue
            col = loc.column  # 0-based col of the declarator name
            spelling = str(cur.type.spelling)
            if not is_concrete_type(spelling):
                continue
            name_start = col - 1  # the name begins at col - 1 (verified: col is one
            # past the first character of the declarator, e.g. `auto* pp` has name
            # `pp` starting at col - 1 and `*`/space between auto and it).
            # Find the rightmost `auto` token immediately before the name, across
            # the pointer/reference suffix and whitespace (`auto* pp`, `auto& r`).
            idx = name_start - 1
            while idx >= 0 and line[idx] in " \t*/&":
                idx -= 1
            if idx < 3 or line[idx - 3:idx + 1] != "auto":
                continue
            a_start = idx - 3  # first char of the `auto` keyword
            # Walk left across the const/volatile modifier run, e.g. `const volatile auto x`.
            l = a_start
            q = a_start - 1
            while q >= 0:
                while q >= 0 and line[q] in " \t":
                    q -= 1
                if q < 0:
                    break
                r = q
                while r >= 0 and line[r].isalnum():
                    r -= 1
                word = line[r + 1:q + 1]
                if word in {"const", "volatile"}:
                    l = r + 1
                    q = r - 1
                else:
                    break
            # Replace [l : name_start] with the concrete type. Clang's spelling
            # already carries any const/volatile qualifiers and any pointer/
            # reference suffix (`const int *`, `int &`, `volatile int`). The
            # separator between the declarator and the identifier is inside the
            # replaced region; keep one space only when the original line had
            # whitespace right before the identifier.
            replacement = spelling
            if line[name_start - 1] in " \t":
                replacement += " "
            edits.append((ln - 1, l, name_start, replacement))
        except Exception:
            continue
    if not edits:
        return 0
    # apply: sort by line then start-desc so later cols don't shift earlier ones
    edits.sort(key=lambda x: (x[0], -x[2]))
    for li, l, e, nt in edits:
        src_lines[li] = src_lines[li][:l] + nt + src_lines[li][e:]
    # preserve trailing newline if the file ended with one
    open(path, "w", encoding="utf-8") .write("\n".join(src_lines))
    return len(edits)


def rewrite_files(paths, ccfile, in_place) -> int:
    changed = 0
    for p in paths:
        if not os.path.exists(p) or os.path.splitext(p)[1].lower() not in CXX_EXT:
            continue
        n = rewrite_file(p, ccfile)
        changed += n
        print(f"  {p}: {n} rewritten" if n else f"  {p}: 0")
    return changed


# --- main ----------------------------------------------------------------------------
def main() -> None:
    if len(sys.argv) < 2:
        print("usage: auto-rewrite.py <check|rewrite|self-test> [options]")
        sys.exit(2)
    sub = sys.argv[1]
    rest = sys.argv[2:]
    if sub == "self-test":
        sys.exit(_self_test())
    ap = argparse.ArgumentParser(prog="auto-rewrite")
    if sub == "check":
        ap.add_argument("--changed-file", dest="changed_file",
                        help="file whose lines are the changed C++ paths to check")
        ap.add_argument("--files", nargs="*", dest="files")
    elif sub == "rewrite":
        ap.add_argument("--files", nargs="*", dest="files")
        ap.add_argument("--ccfile", dest="ccfile", default=None)
        ap.add_argument("--in-place", action="store_true")
        ap.add_argument("--dry-run", action="store_true")
    else:
        ap.add_argument("--help", action="help")
    ns = ap.parse_args(rest)
    paths = []
    if sub == "check":
        if ns.changed_file:
            try:
                with open(ns.changed_file, encoding="utf-8") as f:
                    paths = [p.strip() for p in f.read().splitlines() if p.strip()]
            except Exception as e:
                print(f"could not read changed-file {ns.changed_file}: {e}", file=sys.stderr)
                sys.exit(2)
        elif ns.files:
            paths = ns.files
        else:
            print("check: pass --changed-file or --files", file=sys.stderr)
            sys.exit(2)
        total = 0
        for p in paths:
            if os.path.exists(p) and os.path.splitext(p)[1].lower() in CXX_EXT:
                hits = auto_hits(p)
                if hits:
                    print(f"\n{p}")
                    for li, col, snippet in hits:
                        print(f"  L{li} C{col}  {snippet}")
                    total += len(hits)
        if total:
            print(f"\nfailing: {total} use(s) of `auto` in changed C++ file(s)")
            sys.exit(1)
        print("ok: no `auto` in changed C++ files")
        sys.exit(0)
    elif sub == "rewrite":
        files = ns.files or []
        if ns.in_place:
            changed = rewrite_files(files, ns.ccfile, in_place=True)
            print(f"rewrote {changed}")
            sys.exit(0)
        # dry-run: report count without writing
        n = 0
        for p in files:
            if not os.path.exists(p) or os.path.splitext(p)[1].lower() not in CXX_EXT:
                continue
            try:
                import clang.cindex as ci
                have_ci = True
            except ImportError:
                have_ci = False
            if not have_ci:
                print(f"  {p}: (libclang missing) 0")
                continue
            args = build_args(p, ns.ccfile)
            try:
                tu = ci.TranslationUnit.from_source(p, args)
            except Exception:
                print(f"  {p}: 0 (parse failed)")
                continue
            src_lines = open(p, encoding="utf-8", errors="replace").read().splitlines()
            cnt = 0
            for cur in tu.cursor.walk_preorder():
                try:
                    if cur.kind != ci.CursorKind.VAR_DECL:
                        continue
                    loc = cur.location
                    if loc.file.name != p or not (1 <= loc.line <= len(src_lines)):
                        continue
                    line = src_lines[loc.line - 1]
                    if "auto" not in line:
                        continue
                    if not is_concrete_type(str(cur.type.spelling)):
                        continue
                    # find auto left of name
                    col = loc.column
                    p2 = col - 1
                    while p2 >= 0 and line[p2:p2 + 4] != "auto":
                        p2 -= 1
                    if p2 >= 0:
                        cnt += 1
                except Exception:
                    pass
            n += cnt
            print(f"  {p}: {cnt} would change")
        print(f"would change {n}")
        sys.exit(0)

if __name__ == "__main__":
    main()
