#!/usr/bin/env python
"""
WinDbg MCP Server

Main entry point for the MCP server that brokers between MCP clients and the
WinDbg extension over a named pipe.
"""
import argparse
import asyncio as _aio
import logging
import os
import sys
from pathlib import Path
from typing import Dict

from fastmcp import FastMCP

# Make intra-package absolute imports like `from config import ...` resolve
sys.path.insert(0, str(Path(__file__).parent))

from config import LOG_FORMAT, load_environment_config, LOG_LEVEL, DEBUG_ENABLED
from tools import register_all_tools, get_tool_info
from core.server_initialization import ServerInitializer, InitializationConfig


# ---------------------------------------------------------------------------
# Transport hook (Streamable HTTP).
#
# Upstream hardwires stdio, which is unreachable from another machine. This
# lets the same process serve MCP over Streamable HTTP so a remote client
# (e.g. a Linux agent) can drive the WinDbgX session behind \\.\pipe\windbgmcp.
#
# Precedence: CLI flag > environment variable > default.
# ---------------------------------------------------------------------------
ENV_TRANSPORT = "WINDBG_MCP_TRANSPORT"
ENV_HOST = "WINDBG_MCP_HOST"
ENV_PORT = "WINDBG_MCP_PORT"
ENV_PATH = "WINDBG_MCP_PATH"

DEFAULT_TRANSPORT = "stdio"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_PATH = "/mcp"


def _configure_logging() -> logging.Logger:
    load_environment_config()
    logging.basicConfig(level=getattr(logging, LOG_LEVEL), format=LOG_FORMAT)
    logger = logging.getLogger(__name__)
    if DEBUG_ENABLED:
        logger.setLevel(logging.DEBUG)
        logging.getLogger("fastmcp").setLevel(logging.DEBUG)
    return logger


class WinDbgMCPServer:
    """Main WinDbg MCP Server class."""

    def __init__(self, transport: str = DEFAULT_TRANSPORT, host: str = DEFAULT_HOST,
                 port: int = DEFAULT_PORT, path: str = DEFAULT_PATH) -> None:
        # stateless_http=True: fastmcp 2.5.1's stateful StreamableHTTPSessionManager
        # binds its task group to the FIRST request's lifespan context, so after the
        # initial MCP handshake finishes the session manager shuts down and every
        # later request 500s ("task group was not initialized"). Stateless mode
        # provisions a fresh transport per request and stays up.
        self.mcp = FastMCP(stateless_http=True)
        self.initializer = ServerInitializer(InitializationConfig())
        self._initialized = False
        self.transport = transport
        self.host = host
        self.port = port
        self.path = path
        self.logger = logging.getLogger(__name__)

    def start(self) -> None:
        """Start the WinDbg MCP Server."""
        try:
            self._log_startup_banner()
            self.initializer.initialize()
            self._initialized = True
            self._register_tools()
            if self.transport == "stdio":
                self.logger.info("MCP server ready. Listening on stdio.")
            else:
                self.logger.info(
                    "MCP server ready. Listening on http://%s:%d%s (transport=%s)",
                    self.host, self.port, self.path, self.transport,
                )
            self._run_server()
        except Exception as e:  # pragma: no cover - startup path
            self.logger.error(f"Failed to start server: {e}")
            raise

    def _log_startup_banner(self) -> None:
        tool_info: Dict = get_tool_info()
        self.logger.info("WinDbg MCP Server")
        self.logger.info("=" * 40)
        self.logger.info(f"Total tools: {tool_info['total_tools']}")
        self.logger.info("Tool categories:")
        for category, details in tool_info["categories"].items():
            self.logger.info(f"  {category}: {len(details['tools'])} tools")

    def _register_tools(self) -> None:
        self.logger.debug("Registering tools…")
        register_all_tools(self.mcp)
        self._register_http_routes()

    def _register_http_routes(self) -> None:
        """Plain-HTTP helpers alongside the MCP endpoint.

        GET /list -> JSON of the LIVE WinDbgX sessions this bridge can drive.
        Cheap to poll from a script/agent, no MCP handshake required:

            curl http://<host>:8001/list

        Sessions are discovered on every request, so a WinDbgX started later
        appears automatically and one that exited disappears.
        """
        try:
            from starlette.responses import JSONResponse as _JSONResponse
        except Exception:                                       # pragma: no cover
            self.logger.warning("/list route skipped (starlette missing)")
            return

        @self.mcp.custom_route("/list", methods=["GET"])
        async def _list_sessions(request):
            try:
                from core.communication import list_windbg_instances as _lw
                from core.communication import instance_status as _is
                live = _lw()
                # No "current" field. There is no implicit default session: a
                # client must name the instance on every call (instance=...).
                #
                # A server-wide "current" was shared by every client, so one
                # client calling select_instance() silently redirected everyone
                # else's commands -- which is how our breakpoints ended up in an
                # agent's kernel debugger. Listing only what exists is honest;
                # reporting what someone happened to select last is not.
                # ONE shape: a list of objects.
                #
                #   [{"id": "\\\\.\\pipe\\windbgmcp.NNNN", "running": true|false}, ...]
                #
                # `running` is the thing that actually gets misdiagnosed: a
                # session can exist and be free-running, in which case no
                # breakpoint will ever fire no matter how correct it is.
                # Reporting it at the source removes that guesswork.
                #
                # ONE list of {id, running}. No wrapper object, no second array.
                # One cheap `.lastevent` per live pipe, then the response is
                # built straight from it.
                # BOUND, and run OFF the event loop.
                #
                # instance_status() sends a real `.lastevent` down the pipe. If a
                # session is halted at a breakpoint -- or its pipe is held by a
                # long command -- that call blocks for the full command timeout,
                # so /list itself hangs. Then the supervisor's 4s probe fails,
                # the bridge is judged dead and restarted, and the new bridge
                # hangs in the same place. Net effect: :8001 LISTENs forever and
                # answers nothing, while the debugger sessions are fine.
                #
                # /list is a DISCOVERY endpoint: it must always answer, quickly,
                # even when every session is wedged. So each probe is bounded and
                # a silent pipe is reported as running=false + an error, which is
                # honest and actionable, instead of a hung HTTP request.
                import concurrent.futures as _cf

                # Probe every pipe CONCURRENTLY with a HARD 2.5s ceiling for the
                # whole batch. Per-pipe threads with individual timeouts were not
                # enough: send_command's own retry layer waits 15s, and inside a
                # ThreadPoolExecutor we simply cannot cancel it -- the /list
                # request still hung. Submitting all pipes at once and taking
                # whatever finished in the window keeps /list O(2.5s) no matter
                # how many sessions are stuck.
                def _probe(pipe):
                    try:
                        return _is(pipe)
                    except Exception as e:
                        return {"id": pipe, "running": False, "responds": False,
                                "error": "%s" % (type(e).__name__,)}

                def _collect():
                    if not live:
                        return []
                    ex = _cf.ThreadPoolExecutor(max_workers=max(1, len(live)))
                    futs = {ex.submit(_probe, p): p for p in live}
                    done, notdone = _cf.wait(futs, timeout=2.5)
                    out_ = []
                    for f in done:
                        try: out_.append(f.result())
                        except Exception as e:
                            out_.append({"id": futs[f], "running": False,
                                         "responds": False, "error": str(e)})
                    # A pipe that did not answer in time is NOT dead -- it is busy.
                    # Say exactly that rather than claiming running:false silently.
                    for f in notdone:
                        out_.append({"id": futs[f], "running": False, "responds": False,
                                     "error": "no answer within 2.5s (busy or wedged)"})
                    ex.shutdown(wait=False)
                    return out_

                stats = await _aio.get_event_loop().run_in_executor(None, _collect)
                out = []
                for s in stats:
                    if not isinstance(s, dict):
                        out.append({"id": s, "running": False}); continue
                    row = {"id": s["id"], "running": bool(s.get("running"))}
                    # A BUGCHECK IS NOT "HALTED".
                    #
                    # running=false means "stopped"; it does not say WHY,
                    # and the two reasons need opposite responses: a target
                    # halted at a breakpoint resumes with `g`, while one
                    # stopped in a bugcheck has a dead kernel and only a VM
                    # power cycle helps. Reporting it here means an agent
                    # reads the truth off /list instead of sending `g` at a
                    # corpse and filling the console with bugcheck banners.
                    if s.get("bugcheck"):
                        row["bugcheck"] = True
                        if s.get("bugcheck_frame"):
                            row["bugcheck_frame"] = s.get("bugcheck_frame")
                    # Surface WHY when the state is unknown, instead of a
                    # misleading false / null. A session that does not answer
                    # must not look like one that is merely running.
                    if not s.get("responds", True):
                        row["error"] = s.get("error") or "session did not answer"
                    out.append(row)

                # SORT BY id.
                #
                # Two reasons this matters beyond cosmetics:
                #   1. list_windbg_instances() returns pipes in OS-enumeration
                #      order, which changes as sessions come and go. A consumer
                #      that diffs /list (or a human glancing at it) saw the same
                #      set of sessions appear to "move" on every call.
                #   2. One-port-per-VM means the ids are already unique and
                #      meaningful (windbgmcp.kdbg.<vm>), so sorting
                #      groups them stably per machine.
                # Failures carry id="" and sort first; keep them visible rather
                # than dropping them, sorted by their error text.
                out.sort(key=lambda r: (str(r.get("id") or ""), str(r.get("error") or "")))
                return _JSONResponse(out)
            except Exception as e:                              # pragma: no cover
                # Answer in the SAME shape, but say what broke. Returning a bare
                # 500 with an empty list hid a NameError for several rounds --
                # an error you cannot see is worse than a crash.
                self.logger.error("/list failed: %s", e)
                return _JSONResponse(
                    [{"id": "", "running": False, "error": "%s: %s" % (type(e).__name__, e)}],
                    status_code=500)

    def _run_server(self) -> None:
        try:
            if self.transport == "stdio":
                self.mcp.run()
            else:
                # Streamable HTTP: "http", "streamable-http", "sse"
                self.mcp.run(
                    transport=self.transport,
                    host=self.host,
                    port=self.port,
                    path=self.path,
                )
        except KeyboardInterrupt:
            pass


def _resolve_transport(args: argparse.Namespace) -> tuple[str, str, int, str]:
    """CLI wins over env; env wins over defaults."""
    transport = args.transport or os.environ.get(ENV_TRANSPORT) or DEFAULT_TRANSPORT
    host = args.host or os.environ.get(ENV_HOST) or DEFAULT_HOST
    port = args.port or int(os.environ.get(ENV_PORT) or DEFAULT_PORT)
    path = args.path or os.environ.get(ENV_PATH) or DEFAULT_PATH
    return transport, host, int(port), path


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    parser = argparse.ArgumentParser(prog="windbg-mcp", description="WinDbg MCP server")
    parser.add_argument("--list-tools", action="store_true", help="Print available tools and exit")
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    # Reuse the bridge's OWN pipe discovery from the shell instead of
    # re-implementing it (or shelling out to an ad-hoc Python snippet). The
    # supervisor reaper needs exactly this list, and it must agree with what
    # /list reports -- one implementation, one truth.
    parser.add_argument("--list-pipes", action="store_true",
                        help="Print live WinDbgX pipes (one per line) and exit")
    parser.add_argument(
        "--transport", default=None,
        choices=["stdio", "streamable-http", "http", "sse"],
        help=f"MCP transport (default: ${ENV_TRANSPORT} or '{DEFAULT_TRANSPORT}')",
    )
    parser.add_argument("--host", default=None,
                        help=f"Bind host for HTTP transports (default: ${ENV_HOST} or '{DEFAULT_HOST}')")
    parser.add_argument("--port", type=int, default=None,
                        help=f"Bind port for HTTP transports (default: ${ENV_PORT} or {DEFAULT_PORT})")
    parser.add_argument("--path", default=None,
                        help=f"HTTP path (default: ${ENV_PATH} or '{DEFAULT_PATH}')")
    args = parser.parse_args(argv)

    if args.version:
        from mcp_server import __version__
        print(__version__)
        return 0

    if args.list_pipes:
        from core.communication import list_windbg_instances
        for pipe in list_windbg_instances():
            print(pipe)
        return 0

    if args.list_tools:
        info = get_tool_info()
        print(f"Total tools: {info['total_tools']}")
        for cat, details in info["categories"].items():
            print(f"- {cat}: {', '.join(details['tools'])}")
        return 0

    transport, host, port, path = _resolve_transport(args)
    server = WinDbgMCPServer(transport=transport, host=host, port=port, path=path)
    server.start()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
