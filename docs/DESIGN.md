# Design

This document is the contract the code implements. When the code and this document disagree,
one of them has a bug.

## 1. Problem

Claude Code loads the auto-memory index `MEMORY.md` into every session, but only the first
200 lines or 25 KB of it ([docs](https://code.claude.com/docs/en/memory)). Hand-maintained
indexes grow past that limit and the tail is dropped. Trimming by hand tends to delete the
newest line first, because it is the one that exists nowhere else yet.

cc-memory-index makes the index a **build artifact**. Each memory file carries its own index
line in its frontmatter; the index files are rendered from those sources and can be
regenerated at any time.

## 2. Store layout

A *store* is one auto-memory directory, by default
`<config>/projects/<project-key>/memory/`, where `<config>` is `$CLAUDE_CONFIG_DIR` or
`~/.claude`. Claude Code keys auto memory on the git repository, so all worktrees and
subdirectories share one store; outside a repository the project directory is used
([docs](https://code.claude.com/docs/en/memory)). `<project-key>` is that directory with every
non-alphanumeric character replaced by `-`; for a linked worktree it is the main worktree.
When `autoMemoryDirectory` or `CLAUDE_CODE_PROJECT_DIR_NAME` moves the store, pass `--dir` or
set `CC_MEMORY_INDEX_DIR`.

| File | Role |
|---|---|
| `*.md` (other than the two below) | **Sources** — memory files |
| `MEMORY.md` | **L0**, generated. Loaded by Claude Code every session |
| `index-l1.md` | **L1**, generated. Everything that is not resident; read on demand |
| `.memory-index.json` | Optional configuration (§9) |
| `.index-manual.json` | Preamble and index lines that link to no memory file |
| `.index-pending.json` | Rescued lines waiting to be folded into frontmatter (§6) |
| `.index-render.json` | SHA-256 of L0 and L1 as last written by this tool |
| `.index.lock` | Lock target (§7.4) |
| `.index-migrating`, `.index-backup-<ts>/` | Migration only (§8) |
| `.<name>.tmp-<16 hex>` | Temporary file of a write in progress; one left by a killed process is removed by the next writing command once it is 10 minutes old (§7.1) |
| `index-notes.md` | Created by `migrate` for text that was not an index line (§8); an ordinary memory file afterwards |

Source names must match `^[A-Za-z0-9][A-Za-z0-9_.-]*\.md$` and must not contain `..`.
Other names are skipped and reported by `lint`. At most 5,000 sources are read; a source over
1 MiB, or one that is not valid UTF-8, is skipped and reported (patching must round-trip the
text exactly).

## 3. Frontmatter contract

```yaml
---
name: build-commands
description: How to build and test this repository
metadata:
  type: reference
  tier: resident            # resident = L0. Anything else or missing = L1
  idx: "[Build commands](build-commands.md) — make test, never make all"
  idx_section: "Reference"
  idx_sub: ""
  idx_order: 40
---
```

* The schema of memory frontmatter is not documented by Claude Code, so keys are read from
  the `metadata:` block first and from the top level second. Unknown keys are preserved.
* **The default tier is L1.** A new memory never grows L0 unless it is promoted explicitly.
  This is what keeps L0 under budget as the store grows.
* `idx` defaults to `[<name or file stem>](<file>) — <description>`.
* `idx_section` defaults to the capitalised `type` (`Feedback`, `Project`, …) or `Other`.
* `idx_order` defaults to 1,000,000; ties break on file name.

### 3.1 Parser (strict subset, no YAML library)

* The file must start with `---` (an optional UTF-8 BOM is allowed) and the block ends at the
  next line that is exactly `---`. Only the first 64 KiB are read; no closing line within that
  window means *unparseable*.
* Recognised lines: `key: value` at the top level, `metadata:` followed by indented
  `key: value` lines, blank lines and `#` comments. Values are bare scalars, double-quoted
  strings (`\"` and `\\` escapes) or single-quoted strings (`''` escape). Anything else
  (lists, deeper nesting, multi-line scalars) is kept as opaque text and ignored.
* Parsing never evaluates anything. There is no YAML loader, `eval`, or `pickle` anywhere.

### 3.2 Patcher

* Inserts missing keys into the `metadata:` block, creating the block at the end of the
  frontmatter if absent. **Never changes an existing key**, with one exception: `demote`
  replaces the `tier` line.
* Values are written as double-quoted strings with `"` and `\` escaped. A value is first
  sanitised (§5.1), so a patch can never add a line, close the frontmatter, or add a key
  other than the one requested.
* The file's line ending (LF or CRLF) is kept.

## 4. Rendering

Every source becomes an *item* `{file, idx, section, sub, order, tier}`. Items from
`.index-manual.json` and `.index-pending.json` join the same list (§6).

* Sections are ordered by the smallest `order` they contain (then by name), subsections the
  same way, items by `(order, file)`. Within a section, items without a subsection come first,
  so they never appear under another subsection's heading.
* Every item has its own `- ` line in L0 and L1: Claude Code's guidance for `MEMORY.md` is one
  line per entry, and a real session flagged packed lines as wrong. A `## ` or `### ` heading is
  followed directly by its lines; exactly one blank line precedes each heading.
* L0 always starts with the preamble, then a link line to L1, then the budget warning line
  when anything overflowed (§4.1).
* L1 lists one item per line: all non-resident items by section, then an `Overflow` section,
  then an `Unresolved` section (§6.2). L1 is always written, even when empty, because L0
  always links to it.
* Output is deterministic: the same sources produce byte-identical files. Generated files
  use LF line endings.

### 4.1 Budget

The L0 budget defaults to 20,480 bytes and 160 lines, about 80% of Claude Code's 25 KB /
200-line limit, so its own "near the limit" reminders stay away (configurable, never above
25,000 bytes or 200 lines). With one entry per line the line budget usually binds first: about
150 resident entries fit. It is measured on the exact text written, including the preamble, the L1 link line
and a **reserved** warning line, so adding the warning can never push L0 over budget.

1. If the fixed cost (preamble + link line + reserved warning + one maximum-size item) exceeds
   the budget, the configuration is rejected before anything is written (exit 2).
2. Resident items whose `idx` exceeds the per-item cap go to L1 `Overflow`. Nothing is ever
   truncated. The default cap is 1,024 bytes: across 451 real index lines p50 was 142 B, p99
   489 B and the maximum 692 B (non-ASCII text is 3 bytes per character), and 8% exceeded an
   earlier 300-byte cap. The cap exists so that one huge entry cannot push every later entry
   out of L0.
3. The remaining resident items, in render order, are admitted **while they fit**. The first
   item that does not fit, and every item after it, goes to L1 `Overflow` in order.
   (Claude Code measures `MEMORY.md` after Claude writes it and asks Claude to shorten a file
   near or over a limit; keeping L0 under budget keeps those prompts away.)
4. If anything overflowed, L0 gets one warning line naming the count and `suggest`, and
   `build` exits 1. Frontmatter is not changed: overflow is never a silent demotion.

After rendering, every source, manual line and pending entry must be reachable from L0 or L1.
If not, nothing is written (exit 8, internal invariant). A source whose frontmatter cannot be
parsed is still listed in L1 by name and reported (exit 7), so it never silently disappears.

## 5. Untrusted input

Memory files are written by Claude, which reads web pages and documents and can be steered by
them. Their content is treated as **untrusted data**: it is moved, never executed, and never
allowed to change the structure of the files it lands in.

### 5.1 Sanitising

Index text (`idx`, sections, rescued lines) is reduced to one line: CR, LF, TAB, NEL (U+0085)
and the Unicode line and paragraph separators (U+2028, U+2029) become spaces, other C0
controls, DEL and NUL are removed, and the result is stripped.

### 5.2 Paths

* The store root is resolved once with `realpath`. A symlinked root is allowed (stores are
  often kept in a synced folder).
* Entries under the root that are symlinks or Windows name-surrogate reparse points
  (junctions) are **never read or written**. Other reparse points, such as cloud-sync
  placeholders, do not redirect a path and are treated as ordinary files. Entries with more
  than one hard link are never written.
* Link targets taken from index text must be valid source names (§2). Anything that would
  resolve outside the root is ignored and reported.

### 5.3 Limits

64 KiB per frontmatter read, 5,000 sources, 1 MiB of hook input, and for `suggest`: 50
transcripts, 50 MiB per transcript, 200 MiB in total.

## 6. Hand edits

Claude Code's built-in memory instructions tell Claude to add a line to `MEMORY.md` after
writing a memory. A generator that overwrote those lines would lose them silently, so every
write first compares the current L0 and L1 with the hashes in `.index-render.json`. A file that
already equals the fresh render is not an edit (that is how an interrupted build converges). On a
mismatch, each line of the current file is first compared whole (whitespace-normalised) with the
lines of the new render and every default line; a line that matches is generated, whatever ` · `
it contains. Every other line is split, after whitespace normalisation, into *segments*
(` · `-separated pieces, split outside brackets and parentheses), and each segment not in the new
render is classified:

| Segment | Action |
|---|---|
| exactly one link, to an existing source with no `idx` and no pending entry | rescued into `.index-pending.json`, then folded into that file's `idx` (§6.1) |
| no link | appended to `.index-manual.json` in the section where it appeared |
| two or more links, a missing or unsafe target, a target that already has an `idx` (an edited generated line), or an edited preamble | **unrescuable** — nothing is written, exit 3, the message says to edit the frontmatter instead |
| a generated line that was deleted or reordered | no action: its source still exists, so the next render restores it |

Invariant checked at run time: every segment of the current L0 and L1 is present in the new L0
or L1 (whitespace-normalised), or nothing is written.

`build --force` skips this analysis after copying the current L0 and L1 to
`.MEMORY.md.bak-<UTC timestamp>` and `.<l1 name>.bak-<UTC timestamp>` (a `-1`, `-2` … suffix
when the second repeats; the newest five per file are kept, the backup just written is never
pruned, and a backup identical to the newest one is not written again, so rerunning an
interrupted `--force` cannot push an older backup out).

### 6.1 Folding pending lines

1. Write the rescued line to `.index-pending.json`.
2. Patch the target's frontmatter (`idx`, `idx_section`, `idx_sub`, `idx_order`).
3. Remove the entry from `.index-pending.json`.
4. Render L0 and L1.

The renderer uses a pending entry only when its file has no `idx`; when both exist and are
equal the entry is dropped. A crash between any two steps therefore loses nothing, and running
`build` again finishes the job.

### 6.2 Conflicts

A pending entry whose file has since gained a *different* `idx` is kept, rendered under L1
`Unresolved`, and reported by `lint` as an error. So is a pending entry whose fold keeps failing
(for example an existing empty `idx:`, or a `metadata:` that is not a mapping); `lint` says why.
Nothing is discarded automatically.

## 7. Writing safely

### 7.1 One file at a time

Every write replaces exactly one file atomically: a temporary file is created next to the
target with `O_CREAT | O_EXCL` (and `O_NOFOLLOW` where available), written, flushed and
`fsync`ed, then moved over the target with `os.replace`; on POSIX the directory is `fsync`ed
too. Existing permission bits are kept; new files are created `0600`. The temporary file is
removed on any error. A process killed between creating and renaming its temporary file leaves
`.<name>.tmp-<16 hex>`: every writing command, once it holds the lock, removes entries that
match exactly `^\.(.+)\.tmp-[0-9a-f]{16}$`, are regular files with one link and were modified
more than 10 minutes ago (for `migrate --apply` also in its snapshot directory). Links,
directories and hard-linked files are never touched, a failed removal waits for the next run, and
the count is reported as `temp_removed`. The margin protects a write in flight where advisory
locks are not honoured, as on some network and sync file systems.

### 7.2 No rollback

Writes are ordered so that stopping between any two of them leaves a valid store and running
the same command again converges to the same final state (§6.1). There is no journal and no
rollback, so recovery can never write old content over a newer edit.

### 7.3 Concurrent editors

Claude Code's Write and Edit tools do not know about this tool's lock. Every file that is read
is fingerprinted (size and SHA-256). Immediately before each replace, the target is checked
again; if it changed, that file is left alone and the command exits 4 ("edited concurrently —
run again"). A rescued line that could not be folded stays in `.index-pending.json`, so it is
still rendered.

The remaining window is between that last check and the rename itself, measured at about 80 µs
per `MEMORY.md` replace. Within one Claude Code session the hook runs after the tool call has
finished and tool calls are sequential, so the hook does not race its own session; only another
session writing the same store at that instant can. Claude Code 2.1.299 writes files by
replacing them (Edit and Write both produce a new inode, measured), so the window cannot be
closed without an atomic exchange, which the standard library does not offer portably. In a
stress test with a writer appending every 2–8 ms, 3 of 787 lines were lost, every one inside
such a window. The worst case is the loss of an index *line*: Claude writes the memory file
before adding its line, the memory file itself is never written by this race, and it stays
listed in L1.

### 7.4 Lock

Instances of this tool serialise on an OS advisory lock on `.index.lock` (`fcntl.flock` on
POSIX, `msvcrt.locking` on Windows). The kernel releases it when the process exits, so there is
no stale-lock recovery to get wrong. Sources are read only after the lock is held. If the lock
is not acquired within 5 seconds, the command exits 5 without changing anything.

### 7.5 Directory handle

On POSIX the root is opened once and every operation is made relative to that directory
descriptor (`dir_fd`), so replacing the root path during a run cannot redirect writes. Where
`dir_fd` is unavailable (Windows), the root's `(st_dev, st_ino)` is re-checked before each
replace. This is a weaker guarantee and is documented as such in SECURITY.md.

### 7.6 Scope of the threat model

A malicious process running as the same user already has the same rights as this tool and is
out of scope. The controls above defend against content and links that already exist in the
store, against concurrent editors that are not malicious, and against crashes.

## 8. Migration

`migrate` converts a hand-written `MEMORY.md`. It is a dry run unless `--apply` is given.

* Index lines are the top-level `- ` list lines and the `## `/`### ` headings after the
  preamble. Every other line — paragraphs, indented list items, tables, block quotes, HTML, and
  every line of a fenced code block (fences included) or of an HTML comment that starts a line —
  is a *note*.
* A segment of an index line linking to one source gives that source `tier: resident` and its
  `idx`, `idx_section`, `idx_sub` and `idx_order`. Unlinked segments, and second and later
  segments pointing at the same source, become manual lines. Text before the first `## ` heading
  or top-level `- ` line becomes the manual preamble.
* Notes are moved verbatim (each line byte for byte, LF endings) into one new memory file,
  `index-notes.md` (`-2`, `-3`… when the name exists, is reserved or is linked from the index),
  with frontmatter `name: <stem>`, `type: reference`, `tier: resident` and a fixed `idx`
  (`idx_order: 0`), a title, then each original section that had notes under its heading as
  written. No notes, no file; more than 1 MiB of notes is refused (exit 2). Notes are then no
  longer loaded every session; only the notes file's index line is. The dry run prints how many
  lines move and the file name.
* Sources that were not in `MEMORY.md` get no tier, so they are listed in L1.
* `--apply` writes the marker, then the snapshot `.index-backup-<ts>/` and its `manifest.json`
  (every intended after-hash is recorded before the corresponding write), then
  `.index-manual.json`, the notes file and the sources one by one (keys that already exist are
  skipped, so a rerun resumes into the same snapshot). It checks that every original segment is
  in L0 ∪ L1 and every note is in the notes file in order, builds the index under the same lock
  (notes count as moved, not as hand edits; a line added during an interrupted run is still
  rescued), and removes the marker last. Any failure keeps the marker.
* `migrate --restore <ts>` puts back a source only if its current hash equals the recorded
  after-hash, and removes the notes file only if it still holds its after-hash; a file edited
  since is left alone and reported (exit 4).
* A store that already uses this format (a render record, a manual-lines file, or a source
  with a `tier`) is left unchanged; if it was converted but never built, `--apply` builds it.
* Conversely, `build` refuses (exit 2, "run migrate first") a store that has a non-empty
  `MEMORY.md` but no `.index-render.json` and no source with a `tier`: treating every line of
  a hand-written index as a hand edit would move all of it to L1.

## 9. Configuration

`.memory-index.json` (all keys optional; unknown keys or out-of-range values are an error):

| Key | Default | Range |
|---|---|---|
| `budget_bytes` | 20480 | 2048–25000 |
| `budget_lines` | 160 | 20–200 |
| `item_max_bytes` | 1024 | 80–4096 |
| `l1_name` | `index-l1.md` | a valid source name |
| `lock_timeout_s` | 5 | 1–60 |

## 10. Commands and exit codes

`python -m cc_memory_index <command> [--dir PATH] [--json]`. Store discovery: `--dir`, then
`$CC_MEMORY_INDEX_DIR`, then the store of the current working directory. stdout carries a
human summary (`--json`: one JSON object); stderr carries errors and warnings only.

| Command | Writes | Exit codes |
|---|---|---|
| `build [--force]` | yes | 0 · 1 overflow (L0 still within budget) · 3 unrescuable hand edit |
| `check` | no | 0 in sync · 1 out of sync |
| `lint` | no | 0 · 1 errors found |
| `suggest [--transcripts N] [--apply]` | only with `--apply`, which always ends with a build | 0 · 1 |
| `demote FILE` | yes | the following build's code · 2 unknown, invalid or generated name · 7 frontmatter that cannot be patched |
| `migrate [--apply \| --restore TS]` | with `--apply` / `--restore` | 0 · 1 invariant failed (marker kept) · the first build's 3/4/6/8 (marker kept) · 7 unparseable source |
| `hook` | yes, only for an edit inside a store | always 0 |

Common: 2 usage or configuration error · 4 edited concurrently, rerun · 5 lock timeout ·
6 I/O error (state stays valid, rerun) · 7 a source could not be parsed (skipped, reported) ·
8 internal invariant violated (nothing written). While `.index-migrating` exists, every command
reports that `migrate --apply` must be rerun; read-only commands exit 1.

## 11. Hook

Registered as a `PostToolUse` hook for `Write|Edit|MultiEdit`. It reads at most 1 MiB of JSON
from stdin and acts only when `tool_input.file_path` is a `.md` file directly inside a store
**that this tool has already written** — its directory holds a regular `.index-render.json`.
Installing the plugin therefore changes nothing until the user runs `migrate --apply` or
`build` in a store, and it works wherever `autoMemoryDirectory` puts the store. Every other
call exits 0 immediately.

It runs `build` and, when there is something Claude should know (a rescued line, an overflow,
an unrescuable edit, a concurrent edit), prints one JSON object:

```json
{"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "..."}}
```

It always exits 0, including on internal errors (one line on stderr), so a bug in this tool can
never block a session. `CC_MEMORY_INDEX_DISABLE=1` turns it off without uninstalling. Measured on Claude Code 2.1.299: `additionalContext` reaches Claude
verbatim, while exit code 2 is presented to Claude as a "blocking error", which is why it is
not used.

## 12. suggest and transcripts

Without `--transcripts`, `suggest` lists resident items by size, largest first, until the
savings fit the budget. With `--transcripts N` it also counts how many of the N most recent
transcripts of **the same project** (`*.jsonl` files directly in the store's parent directory,
regular files only) mention each memory file, and ranks rarely used items first.

Transcripts are streamed in 1 MiB chunks (with a 512-byte overlap, so a name split across a
chunk boundary is counted once) and only memory file names matching `memory/<source name>`
(either separator, including the escaped `\\` of Windows paths in JSON) are extracted; no
transcript text is kept beyond the current chunk, printed or logged. A transcript that is
skipped by a cap or grows while it is read is not counted at all. Resident items too long to
ever fit L0 are proposed first. Claude
Code documents the transcript format as internal and subject to change, so this is opt-in and
best effort; nothing else depends on it.
