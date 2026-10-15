# Security

## What this tool does

cc-memory-index reads and writes files in one directory: a Claude Code auto-memory store. It
makes no network requests, starts no processes, and never evaluates anything it reads. A CI gate
(`scripts/check_forbidden.py`) reads the syntax tree of the package and of the plugin hook. It
fails the build on any import outside an allowlist of standard-library modules, on
process-creation or interpreter-introspection attributes (`os.system`, `os.posix_spawn`,
`sys.modules`, …), on `eval`, `exec`, `compile`, `__import__` or `__builtins__`, and on
`getattr`-style access with a computed name.

The Claude Code hook acts only on a store that already contains `.index-render.json`, which
this tool writes the first time you run `build` or `migrate --apply` there. Installing the
plugin changes nothing until you opt a store in. `CC_MEMORY_INDEX_DISABLE=1` turns the hook off.

## Trust model

Memory files are written by Claude, and Claude reads web pages and documents that can try to
steer it. Everything in a memory file is therefore treated as **untrusted data**:

* Frontmatter is read by a strict subset parser. There is no YAML loader, `eval` or `pickle`.
* Text that becomes an index line is reduced to a single line (line breaks, NEL, U+2028/U+2029,
  control characters, bidirectional controls and U+FEFF removed) before it is written anywhere,
  so it cannot add headings, close a frontmatter block or add keys. The frontmatter patcher
  refuses any frontmatter whose structure it does not fully understand, and re-parses every patch
  before writing it.
* A crafted `.index-render.json` cannot hide a memory file or make `migrate --restore` remove or
  revert one: generated files are recognised by their header, and restore takes no names from the
  record. A crafted `.memory-index.json` cannot point `l1_name` at a memory file either.
* Link extraction runs in linear time, so no index line can stall the hook.
* Link targets taken from index text must be plain file names in the store. A path such as
  `../../.ssh/id_rsa.md` is never opened: `build` refuses a hand-added line that points there
  (exit 3, nothing written) and `migrate` keeps such a line as plain text.
* Nothing is truncated: text that is too long for `MEMORY.md` is listed in the on-demand index
  instead. The tool moves content; it never amplifies it.
* The hook tells Claude what happened through `additionalContext` in fixed sentences with counts
  and validated memory file names only. Text read from the store is never relayed, so a crafted
  file cannot use the hook to put instructions in front of Claude.
* `migrate` moves the text of a hand-written `MEMORY.md` that is not an index line verbatim into
  the body of a new memory file, below a frontmatter block the tool writes itself, so that text
  cannot add frontmatter keys or index headings. It is then no longer loaded every session; only
  the notes file's index line is.

**Not covered:** this tool does not judge whether a memory's *meaning* is malicious (for
example, an instruction planted through prompt injection). It keeps such text where it was put;
reviewing what Claude remembers is still up to you (`/memory` in Claude Code).

## Links, races and crashes

* Symlinks and Windows name-surrogate reparse points (junctions) inside the store are never read
  or written; files with more than one hard link are never written. The store directory itself
  may be a symlink, for example into a synced folder.
* On Linux and macOS every operation goes through one directory descriptor, so renaming or
  replacing the store path during a run cannot redirect a write. Windows has no equivalent API;
  there the directory's identity is checked again before every write, which narrows the window
  but does not close it.
* Every write replaces one file atomically (temporary file, `fsync`, rename). Writes are ordered
  so that stopping between any two leaves a valid store, and running the command again finishes
  the job. There is no rollback that could put old content over a newer edit. A temporary file
  left by a process killed mid-write is removed by the next writing command once it is 10
  minutes old; only regular, singly linked files matching this tool's exact temp-name pattern are
  removed, never links or directories.
* Claude Code's own Write and Edit tools do not take this tool's lock. Each file is fingerprinted
  when read and checked again immediately before it is replaced; if it changed, it is left alone
  and the command asks to be rerun. A concurrent edit that lands in the instant between that
  final check and the rename (about 80 µs per index write) can still be overwritten. Within one
  session the hook runs after the tool call has finished, so only another session writing the
  same store at that instant can hit it, and what it can lose is an index line: the memory file
  it points to is written first, is not touched by this race, and stays listed in the on-demand
  index. Closing the window would need an atomic directory-entry exchange, which the Python
  standard library does not provide portably.
* A malicious process running as your own user already has the same access as this tool and is
  out of scope.

## Transcripts

Only `suggest --transcripts N` reads session transcripts, and only when you pass that flag. It
reads the N most recent `*.jsonl` files of the same project (symlinks ignored, size-capped),
extracts memory file names and keeps a count per file. No transcript text is stored, printed or
logged. Claude Code documents the transcript format as internal, so this ranking is best effort.

## Supply chain

The package has no runtime dependencies. CI uses GitHub Actions pinned to full commit SHAs with
read-only permissions, and Dependabot proposes updates to them.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's
[private vulnerability reporting](https://github.com/oh-namgyu/cc-memory-index/security/advisories/new)
rather than in a public issue. Include the version, your OS and Python version, and the smallest
store or hook payload that reproduces the problem. You should get a reply within a week.
