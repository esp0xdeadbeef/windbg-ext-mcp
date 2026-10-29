# This fork

Base: `NadavLor/windbg-ext-mcp` @ `49ec16224fad7428ca2e80608dd656b683c70a0c`
(`upstream/main` is kept as a remote so this can be rebased onto it).

This is a **source fork**, not a patch set. All changes live in the tree; there
are no `.patch` files to apply and no hunk arithmetic to get wrong.

## What this fork adds on top of upstream

* **Streamable-HTTP MCP transport.** Upstream hardwires stdio, which is
  unreachable from another machine. This adds an HTTP transport (enabled by
  `WINDBG_MCP_TRANSPORT` or `--transport`) so a remote MCP client can drive
  WinDbgX over the network.

* **Multi-instance support.** One bridge drives many WinDbgX sessions, each on
  its own named pipe (`WINDBGMCP_PIPE`, default
  `\\.\pipe\windbgmcp.<pid>`), with per-call `instance=` pinning. `/list` reports
  the live sessions and their run state. Unpinned calls are refused when
  several sessions are live, rather than silently landing in whichever session
  happens to be "current".

* **`interrupt` handler.** Programmatic Ctrl+Break via
  `IDebugControl::SetInterrupt`. The `".break"` command string does not work on
  a free-running kernel target (no current scope), so an API call is used
  instead.

* **Faithful command echo.** Every command issued through the bridge appears in
  the WinDbgX console exactly as if typed, including `!` extension commands:

  ```
  0: kd> !process 0 0
  ...
  ```

  The echo is emitted as a single `Output()` call with a prompt derived from the
  engine's run state (`0: kd>` broken in, `?: kd>` free-running). Using
  `IDebugControl::OutputPrompt` for the command text loses the prompt — and
  sometimes the whole line — because dbgeng resets its per-context prompt format
  while dispatching an extension command.

* **Notification output masked.** `DEBUG_OUTPUT_DEBUGGEE` is cleared on the
  bridge's output client so `ModLoad:` / `DllLoad:` / thread-creation floods do
  not bury typed commands and their results. `NORMAL`, `ERROR`, `WARNING`,
  `PROMPT`, `EXTENSION_WARNING`, `SYMBOLS` and `STATUS` are all still shown, so
  errors and requested symbol diagnostics remain visible.

* **Quiet flag.** Commands the bridge issues itself (health checks, mode
  detection) are marked `quiet: true` and are never mirrored to the console —
  the console must not claim the user typed something they did not.

* **Process-wide debug client.** One `IDebugClient` and output callback for the
  process lifetime. Creating a client per command made dbgeng replay its startup
  banner before every reply.

## Building

Open `extension/windbgmcpExt.sln` with Visual Studio 2022 (Release | x64), or run
MSBuild directly:

```
msbuild extension\windbgmcpExt.sln /p:Configuration=Release /p:Platform=x64 /t:Rebuild
```

## Licence / attribution

Upstream is **MIT** (Copyright (c) 2025 Nadav Lorber). MIT requires the original
copyright notice to be preserved, so `LICENSE` keeps that line verbatim and this
fork's own notice is **appended** underneath:

```
Copyright (c) 2025 Nadav Lorber
Copyright (c) 2026 esp0xdeadbeef (fork modifications)
```

That is the conventional shape for a fork: never edit or replace the upstream
holder's line, just add yours. The same terms cover the tree as a whole.
