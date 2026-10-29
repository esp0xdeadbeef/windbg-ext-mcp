"""
Command execution tools for WinDbg MCP server.

This module contains tools for executing WinDbg commands and command sequences.
"""
import logging
import time
from typing import Dict, Any, List, Optional, Union
from fastmcp import FastMCP, Context

from core.validation import validate_command, is_safe_for_automation
from core.context import get_context_manager, save_context, restore_context
from core.error_handler import enhance_error, error_enhancer, DebugContext, ErrorCategory
from core.hints import get_parameter_help, validate_tool_parameters
from core.communication import send_command, send_command_async, CommunicationError, TimeoutError, ConnectionError, list_windbg_instances, set_windbg_instance, get_windbg_instance, select_thread_instance, instance_status
from core.execution import get_executor, execute_command as execute_unified

from .tool_utilities import (
    detect_kernel_mode, get_command_suggestions
)

logger = logging.getLogger(__name__)

def register_execution_tools(mcp: FastMCP):
    """Register all command execution tools."""
    
    @mcp.tool()
    async def continue_session(ctx: Context, instance: str = "") -> Dict[str, Any]:
        """Resume the CALLER'S session WITHOUT blocking.

        Scope is the caller's selected session, NOT every live instance: other
        agents may be driving other WinDbgX sessions through the same bridge.

        A blocking `g` monopolises that instance's pipe, so you can't inspect
        or break in afterwards. This fires `g` asynchronously and returns at
        once; poll with run_on_all '.lastevent' to see state.
        """
        import threading as _th

        _exp = (instance or "").strip()
        if _exp:
            targets = [_exp]
        else:
            _cur = get_windbg_instance()
            _live = list_windbg_instances()
            if _cur and (not _live or _cur in _live):
                targets = [_cur]
            elif len(_live) == 1:
                targets = _live
            else:
                return {"continued": [],
                        "error": "ambiguous session: %d live, none selected" % len(_live),
                        "hint": "select_instance(pipe_name=...) or pass instance="}
        started = []
        for name in targets:
            def _go(n=name):
                try:
                    send_command("g", timeout_ms=600000, instance=n)
                except Exception:
                    pass
            _th.Thread(target=_go, daemon=True).start()
            started.append(name)
        return {"continued": started}

    @mcp.tool()
    async def break_session(ctx: Context, instance: str = "") -> Dict[str, Any]:
        """Interrupt the CALLER'S session so commands can be issued.

        Scope is deliberately NOT "every live session": other agents may be
        driving other WinDbgX instances through the same bridge, and breaking
        those would yank the target out from under them.

        Resolution order for the target:
          1. explicit ``instance`` argument (pipe name), else
          2. the session selected for this request/thread
             (``select_instance`` / per-call ``instance=``), else
          3. the only live session, if exactly one exists.

        If several sessions are live and none is selected, this REFUSES rather
        than guessing -- call ``select_instance`` first.

        Returns {"broken": [...], "skipped": [...], "results": {...}}.
        """
        explicit = (instance or "").strip()
        if explicit:
            targets = [explicit]
        else:
            current = get_windbg_instance()
            live = list_windbg_instances()
            if current and (not live or current in live):
                targets = [current]
            elif len(live) == 1:
                targets = live
            else:
                return {
                    "broken": [],
                    "skipped": live,
                    "error": "ambiguous session: %d live, none selected"
                             % len(live),
                    "hint": "call select_instance(pipe_name=...) first, or pass instance=",
                }

        def one(name):
            # 1) Preferred: the extension's `interrupt` handler ->
            #    IDebugControl::SetInterrupt(DEBUG_INTERRUPT_ACTIVE). This is the
            #    API behind Ctrl+Break and is reentrant, so it works on a
            #    FREE-RUNNING target (kernel included) where a command string
            #    cannot be used.
            try:
                from core.communication import interrupt_instance as _ii
                r = _ii(name)
                if r.get("broke") or ("error" not in r and r.get("output")):
                    return name, r.get("output") or "target interrupted"
                last = "ERROR: %s" % r.get("error", "interrupt did not land")
            except Exception as e:
                last = "ERROR: %s" % e

            # 2) Fallback: command strings (need a current scope; harmless to try).
            #    NOTE: `one()` is a plain sync helper run in a worker thread, so it
            #    must use the BLOCKING send_command, not await send_command_async.
            for cmd in (".breakin", ".break"):
                try:
                    out = send_command(cmd, timeout_ms=5000, instance=name)
                    if "outside of scope" not in (out or ""):
                        return name, out
                    last = (out or "").strip()
                except Exception as e:
                    last = "ERROR: %s" % e
            return name, last

        # run the (blocking) per-target work in threads so the event loop stays
        # free for other agents; gather so we still answer promptly.
        import asyncio as _aio
        loop = _aio.get_event_loop()
        outs = await _aio.gather(*[loop.run_in_executor(None, one, n) for n in targets])
        out = {}
        for name, res in outs:
            out[name] = res
        return {"broken": targets, "skipped": [], "results": out}

    @mcp.tool()
    async def break_in(ctx: Context = None, instance: str = "", timeout_ms: int = 15000) -> Dict[str, Any]:
        r"""Interrupt a RUNNING target in ONE session (programmatic Ctrl+Break).

        Why this exists: `.break` and `.breakin` are command STRINGS, and dbgeng
        refuses them while the target is free-running ("outside of scope"), so a
        kernel target mid-boot cannot be halted that way. This calls the
        extension's `interrupt` handler ->
        IDebugControl::SetInterrupt(DEBUG_INTERRUPT_ACTIVE), which is reentrant
        and is the same API Ctrl+Break drives.

        Unlike run_on_all, this touches exactly the session you name -- use it
        when other agents share the bridge.

        Returns {"instance","broke","output"} or {"instance","error"}.
        """
        import asyncio as _aio
        # _selected_instance was used BARE here but never imported, so every
        # break_in without an explicit instance died with
        #   NameError: name '_selected_instance' is not defined
        # and reported it as "no instance selected". Import it from the same
        # module as interrupt_instance.
        from core.communication import interrupt_instance as _ii, _selected_instance

        name = (instance or "").strip()
        if not name:
            try:
                name = _selected_instance()
            except Exception as e:
                return {"error": "no instance selected: %s" % e,
                        "hint": "pass instance=\\.\\pipe\\windbgmcp.NNNN"}
        loop = _aio.get_event_loop()
        res = await loop.run_in_executor(None, lambda: _ii(name, timeout_ms))
        return res

    @mcp.tool()
    async def run_on_all(ctx: Context, command: str = "", timeout_ms: int = 20000) -> Dict[str, Any]:
        r"""Run the SAME command on EVERY WinDbgX instance in parallel.

        Returns a per-instance result map, so you can see which renderer is in
        which state (e.g. command='.lastevent' -> which one hit a break).
        """
        import concurrent.futures as _cf

        def one(name):
            try:
                return name, send_command(command, timeout_ms=timeout_ms, instance=name)
            except Exception as e:
                return name, f"ERROR: {e}"

        names = list_windbg_instances()
        results: Dict[str, Any] = {}
        # run_on_all fans out across instances: do it in threads so a slow/absent
        # target cannot block the event loop (and therefore other agents).
        # asyncio.gather over run_in_executor keeps the loop free while waiting.
        import asyncio as _aio
        loop = _aio.get_event_loop()
        outs = await _aio.gather(*[
            loop.run_in_executor(None, one, n) for n in names
        ], return_exceptions=True) if names else []
        for item in outs:
            if isinstance(item, Exception):
                results["?"] = f"ERROR: {item}"
            else:
                results[item[0]] = item[1]
        return {"command": command, "results": results}

    @mcp.tool()
    async def list_instances(ctx: Context) -> Dict[str, Any]:
        r"""List the WinDbgX instances (named pipes) this bridge can drive.

        Set the pipe list on the WinDbgX host via the WINDBGMCP_PIPES env var
        (comma-separated), one per WinDbgX process, e.g.
            WINDBGMCP_PIPES=\\.\pipe\windbgmcp.<pidA>,\\.\pipe\windbgmcp.<pidB>

        Pipes are discovered LIVE on every call: a WinDbgX started after this
        bridge came up shows up automatically, and one that exited simply stops
        appearing (no stale entries).

        Returns the live pipes and the currently selected one.

        Ownership is a CONVENTION, not enforced state (see CLAUDE.md): read this
        list, pick a pipe, and say which one you took. Use sessinfo.bat on the KD
        host to tell a kernel (-k) session from a userland (-p) one.
        """
        live = list_windbg_instances()
        return {
            "instances": live,
            "current": get_windbg_instance(),
            "count": len(live),
        }

    @mcp.tool()
    async def select_instance(ctx: Context, pipe_name: str = "") -> Dict[str, Any]:
        """Pin THIS MCP client session to a specific WinDbgX instance (pipe name).

        Per-client (not per-process): two agents share one bridge and each must be
        able to drive its own session without redirecting the other's commands.
        """
        if not pipe_name.strip():
            return {"error": "pipe_name is required", "instances": list_windbg_instances()}
        cid = None
        try:
            cid = ctx.client_id
        except Exception:
            pass
        current = set_windbg_instance(pipe_name, client_id=cid)
        return {"current": current, "client_id": cid}

    @mcp.tool()
    async def break_all(ctx: Context, instance: str = "") -> Dict[str, Any]:
        """DEPRECATED alias for break_session().

        Kept for compatibility only. It does NOT break every session: other
        agents share this bridge, so it interrupts only the caller's context
        (see break_session). Pass instance= to be explicit.
        """
        return await break_session(ctx, instance)

    @mcp.tool()
    async def continue_all(ctx: Context, instance: str = "") -> Dict[str, Any]:
        """DEPRECATED alias for continue_session() (caller's session only)."""
        return await continue_session(ctx, instance)

    @mcp.tool()
    async def run_command(ctx: Context, action: str = "", command: str = "", validate: bool = True, resilient: bool = True, optimize: bool = True, instance: str = "") -> Union[str, Dict[str, Any]]:
        """
        Execute a WinDbg command with validation, resilience, and performance optimization.
        
        Args:
            ctx: The MCP context
            action: Action type (empty string for execute - maintained for framework compatibility)
            command: The WinDbg command to execute
            validate: Whether to validate the command for safety (default: True)
            resilient: Whether to use resilient execution with retries (default: True)  
            optimize: Whether to use performance optimization (default: True)
            
        Returns:
            Command result or error information
        """
        logger.debug(f"Executing command: {command}, validate: {validate}, resilient: {resilient}, optimize: {optimize}, instance: {instance}")

        # OSEE multi-instance patch: per-call instance wins; otherwise use the
        # pipe this MCP CLIENT session pinned via select_instance. Both are put in
        # the thread-local, which is what send_command() consults. Without the
        # client pin, two agents' selections collided and both drove the same
        # session.
        _explicit = instance.strip() if instance and instance.strip() else None
        if not _explicit:
            try:
                from core.communication import client_instance as _ci
                _explicit = _ci(getattr(ctx, "client_id", None))
            except Exception:
                _explicit = None
        select_thread_instance(_explicit)
        
        # Validate parameters
        is_valid, validation_errors = validate_tool_parameters("run_command", action, {"command": command})
        if not is_valid:
            enhanced_error = enhance_error("parameter", 
                                         tool_name="run_command", 
                                         command=command, 
                                         missing_param="command")
            return enhanced_error.to_dict()
        
        if not command.strip():
            enhanced_error = enhance_error("parameter", 
                                         tool_name="run_command", 
                                         command="", 
                                         missing_param="command")
            error_dict = enhanced_error.to_dict()
            error_dict["help"] = get_parameter_help("run_command")
            return error_dict
        
        # Update context for better error suggestions  
        error_enhancer.update_context(DebugContext.KERNEL_MODE if detect_kernel_mode() else DebugContext.USER_MODE)
        
        try:
            # Command validation if requested
            if validate:
                is_valid, validation_error = validate_command(command)
                if not is_valid:
                    enhanced_error = enhance_error("validation", 
                                                 command=command, 
                                                 validation_errors=[validation_error] if validation_error else ["Validation failed"])
                    return enhanced_error.to_dict()
                
                # Check if command is safe for automation
                if not is_safe_for_automation(command):
                    enhanced_error = enhance_error("safety", command=command)
                    return enhanced_error.to_dict()
            
            # Use unified execution system -- OFF THE EVENT LOOP.
            #
            # execute_unified does BLOCKING named-pipe I/O. Calling it directly
            # from this `async def` handler parks the whole asyncio event loop:
            # every other MCP request AND the plain GET /list endpoint stop
            # answering until the command returns. That is why /list went dead
            # whenever a command was in flight, and it is the reason sessions
            # looked "serialised" from the outside.
            #
            # Run it in a worker thread instead. Command execution on one session
            # is still serialised by that session's pipe -- which is correct and
            # unavoidable -- but the HTTP layer, /list and other sessions stay
            # responsive.
            import asyncio as _aio
            _loop = _aio.get_event_loop()

            # CARRY THE INSTANCE PIN INTO THE WORKER THREAD.
            #
            # select_thread_instance() above stores the pin in a THREAD-LOCAL.
            # run_in_executor() then runs execute_unified on a DIFFERENT thread,
            # where _thread_local.instance is None, so send_command fell back to
            # the process-wide default -- whichever instance was selected last.
            #
            # Measured symptom: /list correctly showed three sessions with
            # distinct running states, but every run_command with instance=<X>
            # landed in ONE session (AWE_04). The pin was silently discarded, so
            # kernel commands drove the wrong VM -- the worst possible failure for
            # a debugger bridge, because the output looks plausible.
            _pinned = _explicit

            def _run_pinned_cmd(cmd):
                from core.communication import select_thread_instance as _sti
                _sti(_pinned)
                return execute_unified(
                    command=cmd,
                    resilient=resilient,
                    optimize=optimize,
                    async_mode=False,
                )

            # "quit-session" is the EXPLICIT quit form (see validation.py): the
            # bare "q" stays blocked as dangerous, but a deliberate session quit
            # must be expressible. Translate it to the engine's real command here,
            # after validation, so the blocklist keeps its teeth.
            _qcmd = "q" if command.strip().lower() in ("quit-session", "quitsession") else command

            execution_result = await _loop.run_in_executor(
                None,
                lambda: _run_pinned_cmd(_qcmd))
            
            if execution_result.success:
                # Convert to legacy format for backward compatibility
                return execution_result.to_legacy_format()
            else:
                # ON FAILURE, TELL THE CALLER WHETHER THE TARGET IS RUNNING.
                #
                # Almost every "mystery" failure here had the same cause: the
                # target was FREE-RUNNING, so a command that needs a current
                # process (`x`, `dt`, `lm`, `!process`, `bl`) fails with
                #   0x80040205 Illegal process error
                # and one that needs a halted target (`g`) fails with
                #   Debuggee already running error
                # Neither error says that, so the text gets blamed on the
                # command, the bridge, or the extension -- and the real fix
                # (pause the debugger) is never obvious.
                #
                # So probe the SESSION STATE and attach it as a hint. This does
                # not change the error, it explains it.
                try:
                    from core.communication import instance_status as _ist
                    _st = _ist(_explicit) if _explicit else {}
                    if isinstance(_st, dict):
                        if _st.get("running") is True:
                            execution_result.error = (
                                "%s\n\nHINT: the target is RUNNING (free-running). "
                                "This command needs a halted target -- pause it first "
                                "with break_in, or send 'g' only when it is stopped. "
                                "A failure here is usually the debugger's state, not "
                                "the command." % (execution_result.error,))
                        elif _st.get("halted") is True:
                            execution_result.error = (
                                "%s\n\nHINT: the target is HALTED at a break. If you "
                                "expected it to be running, resume it with 'g'."
                                % (execution_result.error,))
                        elif _st.get("responds") is False:
                            execution_result.error = (
                                "%s\n\nHINT: the session did not answer (%s). The "
                                "debugger may be paused or disconnected."
                                % (execution_result.error, _st.get("error") or "no response"))
                except Exception:
                    pass
                # Convert execution error to enhanced error format
                enhanced_error = enhance_error("execution", 
                                             command=command, 
                                             timeout_category=execution_result.timeout_category,
                                             original_error=execution_result.error)
                error_dict = enhanced_error.to_dict()
                
                # Add execution metadata
                error_dict.update({
                    "execution_method": execution_result.execution_mode.value,
                    "execution_time": execution_result.execution_time,
                    "retries_attempted": execution_result.retries_attempted,
                    "timeout_ms": execution_result.timeout_ms,
                    "timed_out": execution_result.timed_out
                })
                
                return error_dict
                    
        except Exception as e:
            enhanced_error = enhance_error("unexpected", 
                                         tool_name="run_command", 
                                         command=command, 
                                         original_error=str(e))
            return enhanced_error.to_dict()

    @mcp.tool()
    async def run_sequence(ctx: Context, commands: List[str], stop_on_error: bool = False) -> Dict[str, Any]:
        """
        Execute a sequence of WinDbg commands with error handling and performance optimization.
        
        Args:
            ctx: The MCP context
            commands: List of commands to execute in sequence
            stop_on_error: Whether to stop execution if a command fails (default: False)
            
        Returns:
            Results of all commands with execution summary and performance metrics
        """
        logger.debug(f"Executing command sequence: {len(commands)} commands, stop_on_error: {stop_on_error}")
        
        # Validate parameters - Fixed parameter validation
        if not commands:
            enhanced_error = enhance_error("parameter", 
                                         tool_name="run_sequence", 
                                         missing_param="commands")
            error_dict = enhanced_error.to_dict()
            error_dict["help"] = get_parameter_help("run_sequence")
            error_dict["error_details"] = "Parameter 'commands' is required for this operation"
            return error_dict
        
        if not isinstance(commands, list):
            enhanced_error = enhance_error("parameter", 
                                         tool_name="run_sequence", 
                                         missing_param="commands")
            error_dict = enhanced_error.to_dict()
            error_dict["help"] = get_parameter_help("run_sequence")
            error_dict["error_details"] = "Parameter 'commands' must be a list of strings"
            return error_dict
        
        # Update context for better error suggestions
        error_enhancer.update_context(DebugContext.KERNEL_MODE if detect_kernel_mode() else DebugContext.USER_MODE)
        
        # Save context before sequence execution for potential rollback
        context_manager = get_context_manager()
        context_saved = save_context(send_command)
        
        results = []
        successful_commands = 0
        failed_commands = 0
        total_execution_time = 0.0
        execution_stopped = False
        
        try:
            for i, command in enumerate(commands):
                if not command.strip():
                    # Skip empty commands
                    results.append({
                        "command": command,
                        "index": i,
                        "success": False,
                        "error": "Empty command",
                        "skipped": True
                    })
                    continue
                
                logger.debug(f"Executing command {i+1}/{len(commands)}: {command}")
                
                # Validate each command
                is_valid, validation_error = validate_command(command)
                if not is_valid:
                    result = {
                        "command": command,
                        "index": i,
                        "success": False,
                        "error": f"Validation failed: {validation_error}",
                        "validation_errors": [validation_error] if validation_error else ["Validation failed"]
                    }
                    results.append(result)
                    failed_commands += 1
                    
                    if stop_on_error:
                        execution_stopped = True
                        logger.warning(f"Stopping sequence execution at command {i+1} due to validation error")
                        break
                    continue
                
                # Check if command is safe for automation
                if not is_safe_for_automation(command):
                    result = {
                        "command": command,
                        "index": i,
                        "success": False,
                        "error": "Command not safe for automation",
                        "safety_concern": True
                    }
                    results.append(result)
                    failed_commands += 1
                    
                    if stop_on_error:
                        execution_stopped = True
                        logger.warning(f"Stopping sequence execution at command {i+1} due to safety concern")
                        break
                    continue
                
                # Execute command with unified execution system
                try:
                    execution_result = execute_unified(
                        command=command,
                        resilient=True,
                        optimize=True,
                        async_mode=False
                    )
                    
                    execution_time = execution_result.execution_time
                    total_execution_time += execution_time
                    
                    if execution_result.success:
                        result = {
                            "command": command,
                            "index": i,
                            "success": True,
                            "result": execution_result.result,
                            "execution_time": execution_time,
                            "cached": execution_result.cached,
                            "retries_used": execution_result.retries_attempted,
                            "timeout_category": execution_result.timeout_category,
                            "execution_mode": execution_result.execution_mode.value,
                            "suggestions": get_command_suggestions(command, execution_result.result)
                        }
                        successful_commands += 1
                    else:
                        result = {
                            "command": command,
                            "index": i,
                            "success": False,
                            "error": execution_result.error or "Command execution failed",
                            "execution_time": execution_time,
                            "retries_used": execution_result.retries_attempted,
                            "timeout_category": execution_result.timeout_category,
                            "execution_mode": execution_result.execution_mode.value,
                            "timed_out": execution_result.timed_out
                        }
                        failed_commands += 1
                        
                        if stop_on_error:
                            execution_stopped = True
                            logger.warning(f"Stopping sequence execution at command {i+1} due to execution failure")
                            break
                    
                    results.append(result)
                    
                except Exception as e:
                    result = {
                        "command": command,
                        "index": i,
                        "success": False,
                        "error": f"Unexpected error: {str(e)}",
                        "exception": True
                    }
                    results.append(result)
                    failed_commands += 1
                    
                    if stop_on_error:
                        execution_stopped = True
                        logger.error(f"Stopping sequence execution at command {i+1} due to exception: {e}")
                        break
            
            # Prepare summary
            summary = {
                "total_commands": len(commands),
                "successful_commands": successful_commands,
                "failed_commands": failed_commands,
                "execution_stopped": execution_stopped,
                "total_execution_time": total_execution_time,
                "average_execution_time": total_execution_time / len(commands) if commands else 0,
                "context_saved": context_saved,
                "sequence_performance": "excellent" if failed_commands == 0 else "good" if failed_commands < len(commands) * 0.2 else "poor"
            }
            
            # Add recommendations
            recommendations = []
            if failed_commands > 0:
                recommendations.append(f"⚠️ {failed_commands} commands failed - review individual results")
                if not stop_on_error:
                    recommendations.append("💡 Consider using stop_on_error=true for critical sequences")
            
            if total_execution_time > 30.0:
                recommendations.append("⏱️ Long execution time - consider breaking into smaller sequences")
                recommendations.append("🚀 Use async_manager for parallel execution of independent commands")
            
            cached_commands = sum(1 for r in results if r.get("cached", False))
            if cached_commands > 0:
                recommendations.append(f"🎯 {cached_commands} commands served from cache - optimization working")
            
            if successful_commands == len(commands):
                recommendations.append("✅ All commands executed successfully")
            
            return {
                "sequence_results": results,
                "summary": summary,
                "recommendations": recommendations,
                "context_recovery": {
                    "context_saved": context_saved,
                    "recovery_hint": "Use restore_context if sequence caused issues" if context_saved else None
                }
            }
            
        except Exception as e:
            enhanced_error = enhance_error("unexpected", 
                                         tool_name="run_sequence", 
                                         original_error=str(e))
            error_dict = enhanced_error.to_dict()
            error_dict["partial_results"] = results
            error_dict["commands_processed"] = len(results)
            return error_dict 

    @mcp.tool()
    async def breakpoint_and_continue(ctx: Context, breakpoint: str, continue_execution: bool = True, clear_existing: bool = False) -> Dict[str, Any]:
        """
        Set a breakpoint and optionally continue execution.
        
        Args:
            ctx: The MCP context
            breakpoint: Breakpoint specification (e.g., "nt!NtCreateFile", "0x12345678", "kernel32!CreateFileW")
            continue_execution: Whether to continue execution after setting breakpoint (default: True)
            clear_existing: Whether to clear existing breakpoints first (default: False)
            
        Returns:
            Results of breakpoint setting and execution control with debugging guidance
        """
        logger.debug(f"Setting breakpoint: {breakpoint}, continue: {continue_execution}, clear_existing: {clear_existing}")
        
        # Validate parameters
        if not breakpoint or not breakpoint.strip():
            enhanced_error = enhance_error("parameter", 
                                         tool_name="breakpoint_and_continue", 
                                         missing_param="breakpoint")
            return enhanced_error.to_dict()
        
        # Update context for better error suggestions
        error_enhancer.update_context(DebugContext.KERNEL_MODE if detect_kernel_mode() else DebugContext.USER_MODE)
        
        # Save context before breakpoint operations
        context_manager = get_context_manager()
        context_saved = save_context(send_command)
        
        results = []
        
        try:
            # Step 1: Clear existing breakpoints if requested
            if clear_existing:
                logger.debug("Clearing existing breakpoints")
                try:
                    clear_result = execute_unified("bc *", resilient=True, optimize=True)
                    results.append({
                        "step": "clear_existing_breakpoints",
                        "command": "bc *",
                        "success": clear_result.success,
                        "result": clear_result.result if clear_result.success else clear_result.error,
                        "execution_time": clear_result.execution_time,
                        "execution_mode": clear_result.execution_mode.value
                    })
                    if not clear_result.success:
                        logger.warning(f"Failed to clear existing breakpoints: {clear_result.error}")
                except Exception as e:
                    results.append({
                        "step": "clear_existing_breakpoints",
                        "command": "bc *",
                        "success": False,
                        "error": str(e)
                    })
            
            # Step 2: Set the new breakpoint
            bp_command = f"bp {breakpoint}"
            logger.debug(f"Setting breakpoint with command: {bp_command}")
            
            bp_result = execute_unified(bp_command, resilient=True, optimize=True)
            results.append({
                "step": "set_breakpoint",
                "command": bp_command,
                "success": bp_result.success,
                "result": bp_result.result if bp_result.success else bp_result.error,
                "execution_time": bp_result.execution_time,
                "cached": bp_result.cached,
                "execution_mode": bp_result.execution_mode.value
            })
            
            if not bp_result.success:
                return {
                    "success": False,
                    "error": f"Failed to set breakpoint: {bp_result.error}",
                    "breakpoint": breakpoint,
                    "steps_completed": results,
                    "suggestions": [
                        "Check that the symbol/address is valid",
                        "Ensure symbols are loaded (.reload)",
                        "Verify the module is loaded (lm)",
                        "Try a different breakpoint format"
                    ]
                }
            
            # Step 3: List breakpoints to confirm
            try:
                list_result = execute_unified("bl", resilient=True, optimize=True)
                results.append({
                    "step": "list_breakpoints",
                    "command": "bl",
                    "success": list_result.success,
                    "result": list_result.result if list_result.success else list_result.error,
                    "execution_time": list_result.execution_time,
                    "execution_mode": list_result.execution_mode.value
                })
            except Exception as e:
                results.append({
                    "step": "list_breakpoints",
                    "command": "bl",
                    "success": False,
                    "error": str(e)
                })
            
            # Step 4: Continue execution if requested
            execution_result = None
            if continue_execution:
                logger.debug("Continuing execution")
                try:
                    exec_result = execute_unified("g", resilient=True, optimize=True)
                    execution_result = {
                        "step": "continue_execution",
                        "command": "g",
                        "success": exec_result.success,
                        "result": exec_result.result if exec_result.success else exec_result.error,
                        "execution_time": exec_result.execution_time,
                        "execution_mode": exec_result.execution_mode.value
                    }
                    results.append(execution_result)
                    
                    if not exec_result.success:
                        logger.warning(f"Failed to continue execution: {exec_result.error}")
                        
                except Exception as e:
                    execution_result = {
                        "step": "continue_execution",
                        "command": "g",
                        "success": False,
                        "error": str(e)
                    }
                    results.append(execution_result)
            
            # Prepare comprehensive response
            total_time = sum(r.get("execution_time", 0) for r in results)
            successful_steps = sum(1 for r in results if r.get("success", False))
            
            response = {
                "success": True,
                "breakpoint": breakpoint,
                "breakpoint_set": results[1 if clear_existing else 0].get("success", False),
                "execution_continued": execution_result.get("success", False) if execution_result else False,
                "steps_completed": results,
                "summary": {
                    "total_steps": len(results),
                    "successful_steps": successful_steps,
                    "total_execution_time": total_time,
                    "context_saved": context_saved
                }
            }
            
            # Add debugging guidance
            guidance = []
            if execution_result and execution_result.get("success"):
                guidance.extend([
                    "✅ Breakpoint set and execution continued",
                    "🎯 Target will break when the specified location is hit",
                    "📊 Use 'k' to examine call stack when breakpoint hits",
                    "🔍 Use 'r' to examine registers when breakpoint hits",
                    "➡️ Use 'p' to step over or 'g' to continue after breakpoint"
                ])
            elif results[1 if clear_existing else 0].get("success", False):
                guidance.extend([
                    "✅ Breakpoint set successfully",
                    "⏸️ Use 'g' to continue execution and hit the breakpoint",
                    "🎯 Target will break when the specified location is hit"
                ])
            
            if clear_existing and results[0].get("success", False):
                guidance.append("🧹 Previous breakpoints cleared successfully")
            
            response["guidance"] = guidance
            
            # Add troubleshooting tips if something failed
            if successful_steps < len(results):
                response["troubleshooting"] = [
                    "Some steps failed - check individual step results",
                    "Verify target is connected and responsive",
                    "Check that symbols are properly loaded",
                    "Ensure the breakpoint location is valid"
                ]
            
            return response
            
        except Exception as e:
            enhanced_error = enhance_error("unexpected", 
                                         tool_name="breakpoint_and_continue", 
                                         original_error=str(e))
            error_dict = enhanced_error.to_dict()
            error_dict["partial_results"] = results
            error_dict["breakpoint"] = breakpoint
            return error_dict 