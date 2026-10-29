# This fork

Base: `NadavLor/windbg-ext-mcp` @ `49ec16224fad7428ca2e80608dd656b683c70a0c`
(the `upstream/main` branch is kept as a remote so this can be rebased).

This is a **source fork**, not a patch set. Everything OSEE needs is in the
tree; there are no `.patch` files to apply and no hunk arithmetic to get wrong.

## What is added on top of upstream

* **Streamable-HTTP MCP transport** so a remote client can drive the extension
  over the network instead of stdio.
* **Multi-instance support** — one bridge drives many WinDbgX sessions, each on
  its own named pipe (`WINDBGMCP_PIPE`), with per-call `instance=` pinning.
* **`interrupt` handler** — programmatic Ctrl+Break via `IDebugControl::SetInterrupt`
  (the `".break"` command string does not work on a free-running target).
* **Faithful command echo** — every command shows in the WinDbgX console as
  `0: kd> <command>`, including `!` extension commands. The echo is emitted as a
  single `Output()` with a prompt derived from the engine's run state, because
  `OutputPrompt` silently drops the prompt after an extension command is
  dispatched.
* **Notification spam masked** — `DEBUG_OUTPUT_DEBUGGEE` is cleared so
  `ModLoad:` / `DllLoad:` floods do not bury typed commands. Errors, warnings,
  prompts and requested symbol diagnostics are kept.
* **Quiet flag** — the bridge's own probes (`quiet: true`) never appear in the
  console as if the user had typed them.

Build: open `extension/windbgmcpExt.sln` (VS 2022, Release|x64), or let
`tools/debugger-lab/edge_build.ps1` in OSEE do it.

## Licence / attribution

Upstream is **MIT** (Copyright (c) 2025 Nadav Lorber). MIT requires the
original copyright notice to be preserved, so `LICENSE` keeps that line
verbatim and the fork's own notice is **appended** underneath:

    Copyright (c) 2025 Nadav Lorber
    Copyright (c) 2026 esp0xdeadbeef (fork: OSEE modifications)

That is the conventional shape for a fork: never edit or replace the upstream
holder's line, just add yours. The same terms cover the tree as a whole.
