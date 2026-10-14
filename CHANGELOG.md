# Changelog

All notable changes are documented here. Format: [Keep a Changelog](https://keepachangelog.com/), versioning: [SemVer](https://semver.org/).

## [Unreleased]
### Added
- `build` / `check`: generate `MEMORY.md` (L0, resident memories within a byte and line budget) and `index-l1.md` (L1, everything else plus an Overflow section) from each memory file's frontmatter. Nothing is truncated or dropped.
- Hand-edit rescue: a line Claude adds to `MEMORY.md` is moved into that memory file's `metadata.idx`; unlinked lines become manual lines; edits that cannot be attributed are refused with a reason and nothing is written.
- `migrate`: converts a hand-written index after a snapshot; dry run by default, resumable, undoable with `--restore`; non-index text is moved verbatim into a notes memory file.
- `lint`, `suggest` (optional transcript ranking that keeps no transcript text) and `demote`.
- Claude Code plugin with a `PostToolUse` hook that acts only on opted-in stores and always exits 0.
- Safe store I/O: no link or junction is followed, atomic single-file replaces re-checked against what was read, an OS advisory lock, and write ordering that converges after a crash at any point.
