"""
Time-Travel Debugging (TTD) tools for the WinDbg MCP server.

Adds MCP tools to RECORD and OPEN TTD traces from the bridge host, plus helpers
to drive the `!tt` (time-travel) command set once a trace is loaded.

Why on the bridge host: TTD recording is a host-side action (launching TTD.exe
against a PID), not a debugger command. The bridge already runs on the WinDbgX
host, so it is the right place to expose it over MCP.

NOTE: TTD cannot record AppContainer targets (e.g. MicrosoftEdgeCP.exe) - the
recorder's loader is blocked by AppContainer ("Loader initialization status
AccessDenied"). It works on normal processes (ch.exe, RuntimeBroker, etc.).
"""
import logging
import os
import subprocess
from typing import Dict, Any, List

from fastmcp import FastMCP, Context

from core.communication import send_command

logger = logging.getLogger(__name__)

# TTD binaries (copied out of the WindowsApps package by edge_provision).
TTD_EXE = os.environ.get("TTD_EXE", r"C:\Tools\ttd\TTD.exe")
TTD_OUT = os.environ.get("TTD_OUT", r"C:\Tools\traces")


def _run(cmd: List[str], timeout: int = 30) -> Dict[str, Any]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {"rc": p.returncode, "stdout": p.stdout.strip(), "stderr": p.stderr.strip()}
    except subprocess.TimeoutExpired:
        return {"rc": None, "stdout": "", "stderr": "timeout"}
    except Exception as e:
        return {"rc": None, "stdout": "", "stderr": str(e)}


def register_ttd_tools(mcp: FastMCP) -> None:
    """Register TTD (time-travel) tools."""

    @mcp.tool()
    async def ttd_record(ctx: Context, pid: int, module: str = "", out: str = "", maxfile_mb: int = 2048) -> Dict[str, Any]:
        """
        Start recording a TTD trace of a running process.

        Args:
            pid: target process id (decimal)
            module: optional module name to scope recording (e.g. "chakra.dll") - big perf/size win
            out: output directory (default C:\\Tools\\traces)
            maxfile_mb: ring-buffer size cap in MB

        Returns:
            status + the .run path (poll ttd_status to wait for it to appear)

        NOTE: AppContainer targets (MicrosoftEdgeCP.exe) cannot be recorded
        (AccessDenied). Use a normal process, or WinDbgX live attach for Edge.
        """
        if not os.path.exists(TTD_EXE):
            return {"ok": False, "error": f"TTD.exe not found at {TTD_EXE} (run edge_provision.ps1)"}
        odir = out or TTD_OUT
        os.makedirs(odir, exist_ok=True)
        cmd = [TTD_EXE, "-accepteula", "-attach", str(pid), "-out", odir,
               "-timestampFileName", "-maxfile", str(maxfile_mb)]
        if module:
            cmd += ["-module", module]
        # launch detached (recording continues until stopped)
        try:
            subprocess.Popen(cmd, cwd=odir)
            logger.info(f"ttd_record: {' '.join(cmd)}")
            return {"ok": True, "launched": " ".join(cmd), "out": odir,
                    "note": "recording started; call ttd_status to see the .run; ttd_stop to end"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    async def ttd_status(ctx: Context, out: str = "") -> Dict[str, Any]:
        """List running TTD.exe processes and produced .run traces."""
        odir = out or TTD_OUT
        procs = _run(["tasklist", "/fi", "imagename eq TTD.exe", "/fo", "csv", "/nh"])
        recording = "TTD.exe" in procs.get("stdout", "")
        runs = []
        if os.path.isdir(odir):
            runs = [os.path.join(odir, f) for f in os.listdir(odir) if f.endswith(".run")]
        return {"recording": recording, "ttd_procs": procs.get("stdout", ""), "runs": runs, "out": odir}

    @mcp.tool()
    async def ttd_stop(ctx: Context) -> Dict[str, Any]:
        """Stop TTD recording (ends the trace cleanly)."""
        r = _run(["taskkill", "/f", "/im", "TTD.exe"])
        return {"stopped": True, "detail": r}

    @mcp.tool()
    @mcp.tool()
    async def ttd_launch(ctx: Context, exe: str, args: str = "", out: str = "",
                         maxfile_mb: int = 2048) -> Dict[str, Any]:
        """
        RECORD a brand-new process under TTD instead of attaching to a live pid.

        ttd_record uses `TTD.exe -attach <pid>`, which fails whenever another
        debugger already holds the process -- and WinDbgX always does. Launching
        makes TTD the first debugger the target sees, so a crash during startup or
        under our breakpoints lands in the trace.
        """
        if not os.path.exists(TTD_EXE):
            return {"ok": False, "error": f"TTD.exe not found at {TTD_EXE}"}
        if not os.path.exists(exe):
            return {"ok": False, "error": f"exe not found: {exe}"}
        odir = out or TTD_OUT
        os.makedirs(odir, exist_ok=True)
        cmd = [TTD_EXE, "-accepteula", "-out", odir, "-timestampFileName",
               "-maxfile", str(maxfile_mb), "-launch", exe]
        if args:
            cmd += args.split()
        try:
            p = subprocess.Popen(cmd, cwd=odir)
            logger.info("ttd_launch: %s", " ".join(cmd))
            return {"ok": True, "launched": " ".join(cmd), "ttd_pid": p.pid,
                    "out": odir,
                    "note": "poll ttd_status until a .run with non-zero size appears, then ttd_stop"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    async def ttd_open(ctx: Context, run_path: str,
                       ext_dll: str = r"C:\Tools\windbg-ext-mcp\extension\build\x64\Release\windbgmcpExt.dll") -> Dict[str, Any]:
        """Open a .run trace in WinDbgX with the MCP extension loaded, then use `!tt`."""
        if not os.path.exists(run_path):
            return {"ok": False, "error": f"trace not found: {run_path}"}
        if os.path.getsize(run_path) == 0:
            return {"ok": False, "error": f"trace is 0 bytes - recording never started: {run_path}"}
        alias = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\WinDbgX.exe")
        cargs = [alias, "-z", run_path]
        if ext_dll and os.path.exists(ext_dll):
            cargs += ["-c", ".load " + ext_dll]
        try:
            subprocess.Popen(cargs)
            return {"ok": True, "opened": run_path,
                    "hint": "poll list_instances for the new pipe, then ttd_seek '0' / '100'"}
        except Exception as e:
            return {"ok": False, "error": str(e),
                    "note": "from a Session-0 bridge the UWP alias may fail WinError 1312; "
                            "launch via schtasks /it instead"}

    @mcp.tool()
    async def ttd_seek(ctx: Context, position: str = "0", instance: str = "") -> Dict[str, Any]:
        """
        Time-travel: seek within the loaded trace via the `!tt` command.
        position: "0" (start), "100" (end), or "0:100" (percent range), or a literal time.
        Runs against the currently selected WinDbg instance (pipe).
        """
        cmd = f"!tt {position}"
        try:
            out = send_command(cmd)
            return {"command": cmd, "output": out}
        except Exception as e:
            return {"command": cmd, "error": str(e)}
