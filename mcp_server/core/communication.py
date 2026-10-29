"""
Core communication module for WinDbg MCP Extension.

This module provides all communication functionality including low-level protocols,
connection management, error handling, and diagnostic capabilities.
"""
import json
import time
import logging
import threading
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime
from dataclasses import dataclass
from contextlib import contextmanager
from enum import Enum
import win32pipe
import win32file
import win32api
import win32event
import pywintypes

from config import PIPE_NAME, PIPE_NAMES, BUFFER_SIZE, DEFAULT_TIMEOUT_MS, DebuggingMode, get_timeout_for_command

logger = logging.getLogger(__name__)


# Exception Classes
class CommunicationError(Exception):
    """Base exception for communication errors."""
    pass


class PipeTimeoutError(CommunicationError):
    """Raised when a command times out."""
    pass


# Backward compatibility alias
TimeoutError = PipeTimeoutError


class ConnectionError(CommunicationError):
    """Raised when connection to WinDbg extension fails."""
    pass


class NetworkDebuggingError(CommunicationError):
    """Raised when network debugging connection issues are detected."""
    pass


# Data Classes
@dataclass
class ConnectionHandle:
    """Represents a connection handle with metadata."""
    handle: Any
    created_at: datetime
    last_used: datetime
    in_use: bool = False
    use_count: int = 0
    thread_id: int = 0


@dataclass
class ConnectionHealth:
    """Represents the health status of the WinDbg connection."""
    is_connected: bool
    last_successful_command: Optional[datetime]
    consecutive_failures: int
    target_responsive: bool
    extension_responsive: bool
    last_error: Optional[str]


# Low-level Protocol Classes
class NamedPipeProtocol:
    """Handles low-level named pipe communication protocol."""
    
    @staticmethod
    def connect_to_pipe(pipe_name: str, timeout_ms: int) -> Any:
        """
        Connect to the WinDbg extension named pipe.
        
        Args:
            pipe_name: Name of the pipe to connect to
            timeout_ms: Connection timeout in milliseconds
            
        Returns:
            Handle to the connected pipe
            
        Raises:
            ConnectionError: If connection fails
        """
        try:
            handle = win32file.CreateFile(
                pipe_name,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0,
                None,
                win32file.OPEN_EXISTING,
                0,
                None
            )
            logger.debug(f"Connected to pipe: {pipe_name}")
            return handle
            
        except pywintypes.error as e:
            error_code = e.args[0]
            
            if error_code == 2:  # ERROR_FILE_NOT_FOUND
                raise ConnectionError("WinDbg extension not found. Make sure the extension is loaded in WinDbg.")
            elif error_code == 231:  # ERROR_PIPE_BUSY
                # Wait for pipe to become available
                start_time = time.time()
                while (time.time() - start_time) * 1000 < timeout_ms:
                    if win32pipe.WaitNamedPipe(pipe_name, min(5000, timeout_ms)):
                        try:
                            handle = win32file.CreateFile(
                                pipe_name,
                                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                                0,
                                None,
                                win32file.OPEN_EXISTING,
                                0,
                                None
                            )
                            logger.debug(f"Connected to pipe after waiting: {pipe_name}")
                            return handle
                        except pywintypes.error as retry_error:
                            if retry_error.args[0] != 231:
                                raise ConnectionError(f"Failed to connect after wait: {str(retry_error)}")
                            time.sleep(0.1)
                    else:
                        time.sleep(0.1)
                
                raise ConnectionError("WinDbg extension is busy and timeout exceeded.")
            else:
                raise ConnectionError(f"Failed to connect to WinDbg extension: {str(e)}")
    
    @staticmethod
    def write_to_pipe(handle: Any, data: bytes, timeout_ms: int):
        """Write data to the pipe."""
        try:
            win32file.WriteFile(handle, data)
            logger.debug(f"Successfully wrote {len(data)} bytes to pipe")
        except pywintypes.error as e:
            raise ConnectionError(f"Failed to write to pipe: {str(e)}")
    
    @staticmethod
    def read_from_pipe(handle: Any, timeout_ms: int) -> bytes:
        """Read response from the pipe."""
        start_time = datetime.now()
        response_data = b''
        
        while True:
            elapsed_ms = int((datetime.now() - start_time).total_seconds() * 1000)
            if elapsed_ms > timeout_ms:
                raise TimeoutError(f"Read operation timed out after {elapsed_ms}ms")
            
            try:
                hr, data = win32file.ReadFile(handle, BUFFER_SIZE)
                
                if data:
                    response_data += data
                    logger.debug(f"Read {len(data)} bytes, total: {len(response_data)} bytes")
                    
                    if response_data.endswith(b'\n'):
                        logger.debug("Found complete response")
                        break
                else:
                    time.sleep(0.01)
                    
            except pywintypes.error as e:
                error_code = e.args[0]
                if error_code == 109:  # ERROR_BROKEN_PIPE
                    if response_data:
                        logger.warning("Pipe broken but have partial data, using it")
                        break
                    raise ConnectionError("Pipe connection broken")
                elif error_code == 232:  # ERROR_NO_DATA
                    time.sleep(0.01)
                    continue
                else:
                    raise ConnectionError(f"Failed to read from pipe: {str(e)}")
        
        logger.debug(f"Successfully read complete response: {len(response_data)} bytes")
        return response_data
    
    @staticmethod
    def close_pipe(handle: Any):
        """Safely close a pipe handle."""
        if handle:
            try:
                win32file.CloseHandle(handle)
            except Exception as e:
                logger.warning(f"Error closing pipe handle: {e}")


class MessageProtocol:
    """Handles message-level protocol for MCP communication."""
    
    @staticmethod
    def create_command_message(command: str, timeout_ms: int, quiet: bool = False) -> Dict[str, Any]:
        """Create a command message for the WinDbg extension.

        quiet=True marks an INTERNAL probe (health check, mode detection, etc.)
        whose output must not be mirrored to the WinDbgX console. The extension
        will run it with DEBUG_OUTCTL_THIS_CLIENT so the human never sees the
        banner/list noise, while explicit user commands still appear.
        """
        msg = {
            "type": "command",
            "command": "execute_command",
            "id": int(time.time() * 1000),
            "args": {
                "command": command,
                "timeout_ms": timeout_ms
            }
        }
        if quiet:
            msg["args"]["quiet"] = True
        return msg
    
    @staticmethod
    def create_handler_message(handler_name: str, **kwargs) -> Dict[str, Any]:
        """Create a direct handler message for the WinDbg extension."""
        message = {
            "type": "command",
            "command": handler_name,
            "id": int(time.time() * 1000)
        }
        
        if kwargs:
            message["args"] = kwargs
            
        return message
    
    @staticmethod
    def serialize_message(message: Dict[str, Any]) -> bytes:
        """Serialize a message to bytes for transmission."""
        try:
            message_str = json.dumps(message) + "\n"
            return message_str.encode('utf-8')
        except (TypeError, ValueError) as e:
            raise CommunicationError(f"Failed to serialize message: {e}")
    
    @staticmethod
    def parse_response(response_data: bytes) -> Dict[str, Any]:
        """Parse the response data from the extension."""
        try:
            response_str = response_data.decode('utf-8').strip()
            return json.loads(response_str)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse response: {e}")
            logger.debug(f"Raw response: {response_data!r}")
            raise CommunicationError(f"Invalid response from WinDbg extension")
        except UnicodeDecodeError as e:
            logger.error(f"Failed to decode response: {e}")
            raise CommunicationError(f"Invalid response encoding from WinDbg extension")
    
    @staticmethod
    def validate_response(response: Dict[str, Any]) -> bool:
        """Validate that a response message has the expected structure."""
        try:
            if "status" not in response:
                return False
            
            status = response.get("status")
            if status not in ["success", "error"]:
                return False
            
            if status == "error" and "error" not in response:
                return False
            
            if status == "success" and "output" not in response:
                return False
            
            return True
            
        except Exception:
            return False
    
    @staticmethod
    def detect_network_debugging_error(error_message: str) -> bool:
        """Detect if an error message indicates network debugging issues."""
        network_error_indicators = [
            "retry sending", "transport connection", "lost", "network",
            "target windows seems lost", "resync with target"
        ]
        
        return any(phrase in error_message.lower() for phrase in network_error_indicators)


# Connection Pool Management
class ConnectionPool:
    """Thread-safe connection pool for named pipe connections."""
    
    def __init__(self, max_connections: int = 3):
        self._max_connections = max_connections
        self._connections: List[ConnectionHandle] = []
        self._lock = threading.RLock()
        self._pipe_name = PIPE_NAME
        self._active_requests = 0
        self._max_concurrent_requests = 10
        self._queue_condition = threading.Condition(self._lock)
    
    @contextmanager
    def get_connection(self, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        """Context manager to get a connection from the pool."""
        start_time = time.time()
        connection = None
        
        with self._lock:
            if self._active_requests >= self._max_concurrent_requests:
                while self._active_requests >= self._max_concurrent_requests:
                    elapsed = (time.time() - start_time) * 1000
                    if elapsed > timeout_ms:
                        raise TimeoutError(f"Request timed out in queue after {elapsed:.0f}ms")
                    
                    wait_time = min(0.1, (timeout_ms - elapsed) / 1000)
                    if not self._queue_condition.wait(wait_time):
                        continue
            
            self._active_requests += 1
        
        try:
            connection = self._acquire_connection(timeout_ms)
            yield connection.handle
        finally:
            if connection:
                self._release_connection(connection)
            
            with self._lock:
                self._active_requests -= 1
                self._queue_condition.notify_all()
    
    def _acquire_connection(self, timeout_ms: int) -> ConnectionHandle:
        """Acquire a connection from the pool."""
        current_thread = threading.get_ident()
        
        with self._lock:
            for conn in self._connections:
                if not conn.in_use:
                    conn.in_use = True
                    conn.last_used = datetime.now()
                    conn.use_count += 1
                    conn.thread_id = current_thread
                    logger.debug(f"Reusing connection (use count: {conn.use_count})")
                    return conn
            
            if len(self._connections) < self._max_connections:
                try:
                    handle = NamedPipeProtocol.connect_to_pipe(self._pipe_name, timeout_ms)
                    connection = ConnectionHandle(
                        handle=handle,
                        created_at=datetime.now(),
                        last_used=datetime.now(),
                        in_use=True,
                        use_count=1,
                        thread_id=current_thread
                    )
                    self._connections.append(connection)
                    logger.debug(f"Created new connection (total: {len(self._connections)})")
                    return connection
                except Exception as e:
                    logger.error(f"Failed to create connection: {e}")
                    raise ConnectionError(f"Unable to create connection: {e}")
            
            # Create temporary connection for high concurrency
            logger.debug("Creating temporary connection for high concurrency")
            try:
                handle = NamedPipeProtocol.connect_to_pipe(self._pipe_name, timeout_ms)
                return ConnectionHandle(
                    handle=handle,
                    created_at=datetime.now(),
                    last_used=datetime.now(),
                    in_use=True,
                    use_count=1,
                    thread_id=current_thread
                )
            except Exception as e:
                raise ConnectionError(f"Unable to acquire connection: {e}")
    
    def _release_connection(self, connection: ConnectionHandle):
        """Release connection back to pool."""
        with self._lock:
            connection.in_use = False
            connection.last_used = datetime.now()
            connection.thread_id = 0
            
            if connection not in self._connections:
                try:
                    NamedPipeProtocol.close_pipe(connection.handle)
                    logger.debug("Closed temporary connection")
                except Exception as e:
                    logger.warning(f"Error closing temporary connection: {e}")
    
    def get_stats(self) -> Dict[str, Any]:
        """Get connection pool statistics."""
        with self._lock:
            return {
                "total_connections": len(self._connections),
                "active_connections": sum(1 for conn in self._connections if conn.in_use),
                "active_requests": self._active_requests,
                "max_connections": self._max_connections
            }


# High-level Communication Manager
class CommunicationManager:
    """Main communication manager for WinDbg extension interaction."""
    
    def __init__(self):
        self._connection_health = ConnectionHealth(
            is_connected=True,
            last_successful_command=None,
            consecutive_failures=0,
            target_responsive=True,
            extension_responsive=True,
            last_error=None
        )
        self._health_lock = threading.Lock()
        self._connection_pool = ConnectionPool()

    # --- multi-instance support ---------------------------------------------
    def set_instance(self, pipe_name: str) -> None:
        """Select which WinDbgX instance (named pipe) commands go to."""
        self._connection_pool._pipe_name = pipe_name
        logger.info(f"WinDbg instance set to {pipe_name}")

    def get_instance(self) -> str:
        return self._connection_pool._pipe_name

    @property
    def _pipe_name(self) -> str:
        return self._connection_pool._pipe_name

    @_pipe_name.setter
    def _pipe_name(self, value: str) -> None:
        self._connection_pool._pipe_name = value
    
    def send_command(self, command: str, timeout_ms: int = DEFAULT_TIMEOUT_MS, quiet: bool = False) -> str:
        """Send a command to the WinDbg extension."""
        logger.debug(f"Sending command: {command}")
        
        message = MessageProtocol.create_command_message(command, timeout_ms, quiet)
        
        try:
            response = self._send_message(message, timeout_ms)
            
            if not MessageProtocol.validate_response(response):
                raise CommunicationError("Invalid response structure from WinDbg extension")
            
            if response.get("status") == "error":
                error_message = response.get("error", "Unknown error")
                
                if MessageProtocol.detect_network_debugging_error(error_message):
                    raise NetworkDebuggingError(f"Network debugging connection issue: {error_message}")
                
                raise CommunicationError(f"WinDbg command failed: {error_message}")
                
            self._update_health_on_success()
            return response.get("output", "")
            
        except (TimeoutError, ConnectionError, NetworkDebuggingError):
            self._update_health_on_failure(f"Command '{command}' failed")
            raise
        except Exception as e:
            self._update_health_on_failure(str(e))
            logger.error(f"Unexpected error executing command '{command}': {e}")
            raise CommunicationError(f"Error executing command: {str(e)}")
    
    def send_handler_command(self, handler_name: str, timeout_ms: int = DEFAULT_TIMEOUT_MS, **kwargs) -> Dict[str, Any]:
        """Send a direct handler command to the WinDbg extension."""
        logger.debug(f"Sending handler command: {handler_name}")
        
        message = MessageProtocol.create_handler_message(handler_name, **kwargs)
        
        try:
            response = self._send_message(message, timeout_ms)
            
            if not isinstance(response, dict):
                raise CommunicationError("Invalid response structure from WinDbg extension handler")
            
            if response.get("type") == "error":
                error_message = response.get("error_message", "Unknown error")
                raise CommunicationError(f"Handler '{handler_name}' failed: {error_message}")
                
            self._update_health_on_success()
            return response
            
        except (TimeoutError, ConnectionError):
            self._update_health_on_failure(f"Handler '{handler_name}' failed")
            raise
        except Exception as e:
            self._update_health_on_failure(str(e))
            logger.error(f"Unexpected error executing handler '{handler_name}': {e}")
            raise CommunicationError(f"Error executing handler: {str(e)}")
    
    def test_connection(self) -> bool:
        """Test if the connection to the WinDbg extension is working."""
        try:
            # Probe with .lastevent instead of the "version" HANDLER: version
            # runs dbgeng's `version`, which prints the entire startup banner
            # (Windows version, Product, Edition build lab, session/uptime,
            # copyright, EngHost command line, dbgeng/dbghelp paths, DIA
            # version, extension search path and DLL chain). That ran on every
            # health check, i.e. before every command.
            result = self.send_command(".lastevent", timeout_ms=get_timeout_for_command("version"), quiet=True)
            extension_responsive = bool(result and not result.startswith("Error:"))
            
            with self._health_lock:
                self._connection_health.extension_responsive = extension_responsive
                self._connection_health.is_connected = extension_responsive
                if extension_responsive:
                    self._connection_health.last_successful_command = datetime.now()
                    self._connection_health.consecutive_failures = 0
                    self._connection_health.last_error = None
                else:
                    self._connection_health.consecutive_failures += 1
            
            return extension_responsive
            
        except NetworkDebuggingError:
            logger.debug("Network debugging error during connection test - assuming connected")
            with self._health_lock:
                self._connection_health.extension_responsive = True
                self._connection_health.is_connected = True
                self._connection_health.last_successful_command = datetime.now()
                self._connection_health.consecutive_failures = 0
                self._connection_health.last_error = None
            return True
        except Exception as e:
            logger.debug(f"Connection test failed: {e}")
            with self._health_lock:
                self._connection_health.extension_responsive = False
                self._connection_health.is_connected = False
                self._connection_health.consecutive_failures += 1
                self._connection_health.last_error = str(e)
            return False
    
    def test_target_connection(self) -> Tuple[bool, str]:
        """Test if the debugging target is responsive."""
        try:
            # .lastevent answers with one line and still proves the target is
            # responsive; `version` re-emitted the whole startup banner.
            result = self.send_command(".lastevent", timeout_ms=get_timeout_for_command("version"), quiet=True)
            target_responsive = bool(result and not result.startswith("Error:"))
            
            with self._health_lock:
                self._connection_health.target_responsive = target_responsive
            
            if target_responsive:
                low = result.lower()
                # Prefer explicit user-mode markers: the WinDbg `version` banner
                # prints "Live user mode: <Local>" for a user-mode attach, but may
                # also mention "kernel" elsewhere, so check user mode FIRST.
                if "user mode" in low or "live user mode" in low:
                    return True, "User-mode debugging target connected"
                elif "kernel" in low:
                    return True, "Kernel debugging target connected"
                elif "process" in low:
                    return True, "User-mode debugging target connected"
                else:
                    return True, "Debugging target connected"
            else:
                return False, "No response from debugging target"
                
        except NetworkDebuggingError as e:
            with self._health_lock:
                self._connection_health.target_responsive = False
            return False, f"Network debugging issue: {str(e)}"
        except Exception as e:
            with self._health_lock:
                self._connection_health.target_responsive = False
            return False, f"Target test failed: {str(e)}"
    
    def diagnose_connection_issues(self) -> Dict[str, Any]:
        """Run comprehensive connection diagnostics."""
        diagnostics = {
            "extension_available": False,
            "target_connected": False,
            "recommendations": []
        }
        
        try:
            diagnostics["extension_available"] = self.test_connection()
            
            target_connected, target_status = self.test_target_connection()
            diagnostics["target_connected"] = target_connected
            diagnostics["target_status"] = target_status
            
            recommendations = []
            
            if not diagnostics["extension_available"]:
                recommendations.extend([
                    "Load the WinDbg extension with: .load path\\to\\windbgmcpExt.dll",
                    "Ensure WinDbg is running and the extension DLL is accessible"
                ])
            
            if not diagnostics["target_connected"]:
                recommendations.extend([
                    "Ensure a debugging target is connected",
                    "For kernel debugging, verify target VM configuration"
                ])
            
            diagnostics["recommendations"] = recommendations
            
        except Exception as e:
            logger.error(f"Failed to run diagnostics: {e}")
            diagnostics["error"] = str(e)
        
        return diagnostics
    
    def get_connection_health(self) -> ConnectionHealth:
        """Get the current connection health status."""
        with self._health_lock:
            return self._connection_health
    
    def get_connection_pool_stats(self) -> Dict[str, Any]:
        """Get connection pool statistics."""
        return self._connection_pool.get_stats()
    
    def _send_message(self, message: Dict[str, Any], timeout_ms: int) -> Dict[str, Any]:
        """Send a message to the WinDbg extension via named pipe."""
        try:
            with self._connection_pool.get_connection(timeout_ms) as handle:
                message_bytes = MessageProtocol.serialize_message(message)
                logger.debug(f"Sending {len(message_bytes)} bytes via pooled connection")
                
                NamedPipeProtocol.write_to_pipe(handle, message_bytes, timeout_ms)
                response_data = NamedPipeProtocol.read_from_pipe(handle, timeout_ms)
                
                return MessageProtocol.parse_response(response_data)
                
        except Exception as pool_error:
            logger.debug(f"Pooled connection failed, falling back to direct connection: {pool_error}")
            
            handle = None
            try:
                message_bytes = MessageProtocol.serialize_message(message)
                handle = NamedPipeProtocol.connect_to_pipe(self._pipe_name, timeout_ms)
                
                NamedPipeProtocol.write_to_pipe(handle, message_bytes, timeout_ms)
                response_data = NamedPipeProtocol.read_from_pipe(handle, timeout_ms)
                
                return MessageProtocol.parse_response(response_data)
                
            finally:
                if handle:
                    NamedPipeProtocol.close_pipe(handle)
    
    def _update_health_on_success(self):
        """Update connection health when a command succeeds."""
        with self._health_lock:
            self._connection_health.last_successful_command = datetime.now()
            self._connection_health.consecutive_failures = 0
            self._connection_health.last_error = None
            self._connection_health.extension_responsive = True
            self._connection_health.target_responsive = True
            self._connection_health.is_connected = True
    
    def _update_health_on_failure(self, error_message: str):
        """Update connection health when a failure occurs."""
        with self._health_lock:
            self._connection_health.consecutive_failures += 1
            self._connection_health.last_error = error_message
            
            if self._connection_health.consecutive_failures >= 3:
                self._connection_health.is_connected = False
                self._connection_health.extension_responsive = False


# Global Communication Manager Instance (per-instance registry)
_communication_managers: Dict[str, CommunicationManager] = {}
_current_instance: Optional[str] = None
# Thread-local selected instance so concurrent MCP calls don't clobber each
# other (each request can pick its own WinDbgX session).
_thread_local = threading.local()

# Per-MCP-client pin: {client_id -> pipe_name}. Two agents share one bridge, and
# requests for the same client may be served on different threads, so a
# thread-local alone is not enough to keep their selections apart.
_client_pins: Dict[str, str] = {}
_client_pins_lock = threading.Lock()

# RLock (not Lock): helpers nest (_selected_instance -> _discover_pipes,
# set_windbg_instance -> _get_communication_manager). Nesting is avoided
# where known, but RLock means a missed one degrades to reentrancy instead
# of deadlocking the entire bridge.
_manager_lock = threading.RLock()


def select_client_instance(client_id: Optional[str], pipe_name: str) -> str:
    """Pin a whole MCP client session to one WinDbgX pipe."""
    if not client_id:
        return pipe_name
    with _client_pins_lock:
        if pipe_name:
            _client_pins[client_id] = pipe_name
        else:
            _client_pins.pop(client_id, None)
    return pipe_name


def client_instance(client_id: Optional[str]) -> Optional[str]:
    if not client_id:
        return None
    with _client_pins_lock:
        return _client_pins.get(client_id)


def select_thread_instance(pipe_name: Optional[str]) -> None:
    """Bind the CURRENT THREAD (one MCP request) to a specific instance.

    run_command() calls this per request; the client pin above is the durable
    per-agent selection, this is the per-request override.
    """
    _thread_local.instance = pipe_name if pipe_name else None


def _make_manager(pipe_name: str) -> CommunicationManager:
    m = CommunicationManager()
    m.set_instance(pipe_name)
    return m


def _get_communication_manager(instance: Optional[str] = None) -> CommunicationManager:
    """Return the CommunicationManager for a given pipe (one per instance).

    With no argument, use the CURRENTLY SELECTED instance (default = the first
    configured pipe). This keeps every WinDbgX session independent so they can
    be driven concurrently.
    """
    global _current_instance
    with _manager_lock:
        name = instance or _selected_instance()
        m = _communication_managers.get(name)
        if m is None:
            m = _make_manager(name)
            _communication_managers[name] = m
        return m


def _selected_instance() -> str:
    """Instance for this call.

    Precedence: this thread's pin (run_command sets it per request) > this MCP
    client's pin (select_instance, survives across requests) > _current_instance
    (explicitly selected default).

    There is deliberately NO fallback from a named-but-dead instance to another
    live one, and NO implicit pick when nothing is selected: see the two
    RuntimeErrors below.
    """
    want = (getattr(_thread_local, "instance", None)
            or client_instance(getattr(_thread_local, "client_id", None))
            or _current_instance)
    live = _discover_pipes()
    if want and (not live or want in live):
        return want
    # NO SILENT FALLBACK to "some live pipe". Choosing a different session than
    # the caller named is how commands landed in another agent's debugger: the
    # caller believes it is driving its own target while it drives theirs, and
    # because the wrong session answers successfully the mistake is invisible.
    if want:
        raise RuntimeError(
            "instance %r is not live (live: %s). Name a live instance with "
            "instance=...; refusing to fall back to a different session."
            % (want, live))
    # NOTHING SELECTED. Picking live[0] here used to return whatever sorted
    # first -- with several kernel sessions and one userland session live,
    # `current` became a kernel session and unpinned calls silently drove a
    # KERNEL debugger while the caller believed it was on its own userland
    # target (measured: `kb` answered nt!DbgBreakPointWithStatus/kdnic for a
    # request meant for the attached user-mode process). The wrong session
    # answering successfully is exactly what makes this invisible, so refuse
    # instead of guessing.
    if len(live) > 1:
        raise RuntimeError(
            "no instance selected and %d are live (%s). Pin one with "
            "instance=.../select_instance, or set MCP_INSTANCE; refusing to guess."
            % (len(live), ", ".join(live)))
    if live:
        return live[0]
    return PIPE_NAMES[0] if PIPE_NAMES else PIPE_NAME


# --- multi-instance support ------------------------------------------------
def _discover_pipes() -> list:
    r"""Enumerate LIVE WinDbgX pipes by scanning the named-pipe namespace.

    The extension creates \\.\pipe\windbgmcp.<pid> when `.load`ed into a
    WinDbgX, and the pipe disappears when that WinDbgX exits. Scanning here
    means:
      * a session started AFTER the bridge came up is picked up automatically
      * a session that closed simply stops appearing (no stale entries)
    WINDBGMCP_PIPES, if set, is still honoured but only as an ADDITIONAL static
    list (useful when the bridge runs on another host / cannot see the pipes).
    """
    found = []
    try:
        import os as _os
        d = r"\\.\pipe"
        for name in _os.listdir(d):
            # Accept BOTH the per-session form (windbgmcp.<pid>) and the
            # bare default (windbgmcp). ResolveMcpPipeName() normally yields the
            # former, but if WINDBGMCP_PIPE is set, or the extension falls back
            # to Constants::DEFAULT_PIPE_NAME, the pipe is called plain
            # "windbgmcp" -- and a `startswith("windbgmcp.")` test then matched
            # nothing, so /list reported count:0 while the extension was in fact
            # loaded and serving. That reads exactly like "the MCP is dead".
            if name == "windbgmcp" or name.startswith("windbgmcp."):
                found.append(r"\\.\pipe" + "\\" + name)
    except Exception:
        pass
    # static fallback / additions
    for p in PIPE_NAMES:
        if p not in found:
            # only add a static pipe if it actually exists
            try:
                import os as _os
                leaf = p.rsplit("\\", 1)[-1]
                if leaf in _os.listdir(r"\\.\pipe"):
                    found.append(p)
            except Exception:
                pass
    # stable order: by pid ascending
    def _pid(p):
        try:
            return int(p.rsplit(".", 1)[-1])
        except Exception:
            return 0
    return sorted(found, key=_pid)


def _prune_dead_managers(live: list) -> None:
    """Drop managers whose pipe no longer exists (WinDbgX closed)."""
    with _manager_lock:
        for name in list(_communication_managers.keys()):
            if name not in live:
                _communication_managers.pop(name, None)


def list_windbg_instances() -> list:
    """Return the LIVE WinDbgX pipe names, refreshing on every call.

    Deliberately does NOT prune here: pruning takes _manager_lock, and /list must
    stay answerable even while another agent's pipe I/O is in flight. Pruning to
    one side also avoids a discovery-vs-lock cycle.
    """
    return _discover_pipes()


def set_windbg_instance(pipe_name: str, client_id: Optional[str] = None) -> str:
    """Pin an MCP client session (and this thread) to one WinDbgX instance.

    Two agents share one bridge, so selection must be per-client, not a
    process-wide global -- otherwise agent B's select_instance silently redirects
    agent A's commands (observed: both agents got the same session's output).

    `_current_instance` is still updated as the default for brand-new clients
    that never call select_instance.
    """
    global _current_instance
    name = (pipe_name or "").strip()
    if not name:
        return get_windbg_instance()
    select_thread_instance(name)                 # this thread
    if client_id:
        select_client_instance(client_id, name)  # this MCP client session
    with _manager_lock:
        _current_instance = name                 # default for un-pinned clients
    # Do NOT call _get_communication_manager() while holding _manager_lock: that
    # helper takes the SAME lock, so nesting self-deadlocks the request and wedges
    # the whole bridge (every later tool call hangs). Hoisted by AGENT-EDGE.
    _get_communication_manager(name)             # ensure it exists
    return name


def get_windbg_instance() -> str:
    return _selected_instance()


def interrupt_instance(name: str = "", timeout_ms: int = 15000) -> Dict[str, Any]:
    """Interrupt a RUNNING target (programmatic Ctrl+Break).

    Uses the extension's `interrupt` handler, which calls
    IDebugControl::SetInterrupt(DEBUG_INTERRUPT_ACTIVE). Unlike `.break`/`.breakin`
    (command strings that dbgeng refuses without a current scope), SetInterrupt is
    reentrant and works while the target is executing -- including a kernel target
    mid-boot.

    Returns {"instance", "broke", "output"} or {"instance","error"}.
    """
    name = (name or "").strip() or _selected_instance()
    mgr = _get_communication_manager(name)
    try:
        msg = MessageProtocol.create_handler_message("interrupt")
        resp = mgr._send_message(msg, timeout_ms)
        if not MessageProtocol.validate_response(resp):
            return {"instance": name, "error": "invalid response", "raw": resp}
        if resp.get("status") == "error":
            return {"instance": name, "error": resp.get("error") or resp.get("error_message"),
                    "raw": resp}
        # The extension returns the handler's fields at the TOP LEVEL of the
        # response ({"status":"success","broke":true,"output":"..."}), not
        # nested under data/result. Read either shape.
        top = resp if "broke" in resp else (resp.get("data") or resp.get("result") or {})
        if isinstance(top, str):
            return {"instance": name, "broke": "interrupt" in top, "output": top, "raw": resp}
        broke = bool(top.get("broke")) if isinstance(top, dict) else False
        out = top.get("output") if isinstance(top, dict) else top
        res = {"instance": name, "broke": broke, "output": out or "", "raw": resp}
        if not broke:
            res["error"] = "interrupt did not land"
        return res
    except Exception as e:
        return {"instance": name, "error": str(e)}


def instance_status(name: str, timeout_ms: int = 3000) -> Dict[str, Any]:
    """Live state of ONE session, cheap enough to call from /list.

    Returns a struct, not a bare string:

        {"id": "\\\\.\\pipe\\windbgmcp.NNNN",
         "running": bool,      # True  -> target free-running (a break will not fire)
         "halted":  bool,      # True  -> stopped at a prompt
         "responds": bool,     # False -> pipe exists but the engine does not answer
         "prompt": "0: kd>",   # the debugger's own prompt, when halted
         "error": "..."}       # only when responds is False

    Why this matters: a session can be PRESENT but free-running, and then no
    breakpoint fires no matter how correct it is. /list only saying "the pipe
    exists" hides exactly that, so every debugging round starts by guessing.
    """
    # No nulls. If the state cannot be determined we say so with a real value
    # (`responds: false` plus an error string) instead of returning None, which
    # reads as "some field I never filled in" and hides the failure.
    res: Dict[str, Any] = {"id": name, "running": False, "responds": False,
                           "prompt": "", "error": ""}
    try:
        # `.lastevent` is answered in BOTH states. A halted target returns a
        # real event string and a concrete prompt ("0: kd>"); a running one
        # returns "<no event>" and the indeterminate prompt "?: kd>".
# Probe with vertarget, NOT .lastevent.
        #
        # .lastevent keeps answering "<no event>" on a DEAD KD transport, so /list
        # happily reported running:true for four zombie EngHosts whose debugees
        # were gone. vertarget is the command that tells the truth: a live target
        # prints real uptime, a dead link prints "System Uptime: not available"
        # (plus "Kernel transport in use, packet write failed"). The Uptime line
        # cannot be fabricated by a stale cache.
        # .lastevent is answered in BOTH states and carries the prompt, which is
        # what tells a running target from a halted one.
        #
        # I briefly added a "zombie" heuristic here keyed on
        #   "System Uptime: not available" / "packet write failed"
        # and declared sessions dead. THAT WAS WRONG: "Uptime: not available" is
        # simply what a PAUSED target reports. The sessions were alive the whole
        # time and could be resumed. Do not invent liveness rules from output
        # text -- ask the engine (break_in works, `g` works) instead.
        out = send_command(".lastevent", timeout_ms=timeout_ms, instance=name, quiet=True) or ""
    except Exception as e:
        res["responds"] = False
        res["error"] = str(e)
        return res

    res["responds"] = True
    low = out.lower()
    # prompt is the first "N: kd>" / "?: kd>" token in the captured text
    for tok in out.split():
        if tok.endswith("kd>") or tok.endswith("cdb>"):
            res["prompt"] = tok
            break
    running = ("last event: <no event>" in low) or (res["prompt"] == "?:")
    res["running"] = running
    # Bugcheck detection, for /list.
    #
    # `running` alone cannot express the one state that matters most to an
    # agent: the target is HALTED, but it is halted because Windows died, so
    # nothing will ever resume it and every command describes a corpse. That
    # was indistinguishable from "stopped at my breakpoint" in /list, and the
    # difference decides whether the next move is `g` or a VM power cycle.
    #
    # Cheap test: a bugchecked kernel always has nt!KeBugCheck2 (or the
    # KiBugCheck* family) on the stack, because it is sitting in the bugcheck
    # path -- reached from DbgBreakPointWithStatus via
    # KiBugCheckDebugBreak/KeBugCheckEx. `kb` is a single cheap command, and
    # only issued when we already know the target is halted, so a free-running
    # session costs nothing extra.
    #
    # Deliberately NOT keyed on the "A fatal system error has occurred" text:
    # that string is also produced by a plain break on 80000003, so it would
    # report every operator Ctrl+Break as a crash.
    res["bugcheck"] = False
    if not running:
        try:
            kbo = send_command("kb", timeout_ms=timeout_ms, instance=name, quiet=True) or ""
            kbl = kbo.lower()
            if ("nt!kebugcheck2" in kbl or "nt!kibugcheck" in kbl
                    or "nt!kebugcheckex" in kbl):
                res["bugcheck"] = True
                # The stop code is the first 0xXXXXXXXX on the KeBugCheckEx
                # frame; surfacing it saves a second round-trip to ask.
                for _ln in kbo.splitlines():
                    if "kibugcheckdispatch" in _ln.lower():
                        res["bugcheck_frame"] = _ln.strip()[:160]
                        break
        except Exception:
            # kb failing must not turn /list into an error; the other fields
            # are still true and honest.
            pass
    res["halted"] = not running
    if "break instruction exception" in low or "breakpoint" in low:
        res["last_event"] = out.strip().splitlines()[-2:] and out.strip()[:200]
    return res



# Public API Functions
def send_command(command: str, timeout_ms: int = DEFAULT_TIMEOUT_MS,
                 instance: Optional[str] = None, quiet: bool = False) -> str:
    """
    Send a command to the WinDbg extension.
    
    Args:
        command: The WinDbg command to execute
        timeout_ms: Timeout in milliseconds
        instance: Optional WinDbgX pipe name; defaults to the selected instance
        
    Returns:
        The command output as a string
        
    Raises:
        ConnectionError: If connection to the extension fails
        TimeoutError: If the command times out
        CommunicationError: If the command fails
    """
    manager = _get_communication_manager(instance)
    # `quiet` must be forwarded: the manager accepts it (it routes output to the
    # callback only, so internal probes do not appear in the console). The module
    # wrapper used to drop it, so instance_status() -- which passes quiet=True --
    # raised "unexpected keyword argument 'quiet'" and the caller turned that into
    # all-null fields. A null that means "I did not check" is worse than an
    # honest error.
    return manager.send_command(command, timeout_ms, quiet)


async def send_command_async(command: str, timeout_ms: int = DEFAULT_TIMEOUT_MS,
                             instance: Optional[str] = None) -> str:
    """Awaitable send_command: runs the BLOCKING named-pipe I/O in a thread pool.

    MCP tool handlers are `async def`; calling the synchronous send_command()
    from them blocks the asyncio event loop, so one agent waiting on an
    unresponsive target froze every other session AND GET /list. Always use this
    from async code.
    """
    loop = _asyncio.get_event_loop()
    return await loop.run_in_executor(
        _pipe_executor(),
        lambda: send_command(command, timeout_ms=timeout_ms, instance=instance),
    )


def send_handler_command(handler_name: str, timeout_ms: int = DEFAULT_TIMEOUT_MS, **kwargs) -> Dict[str, Any]:
    """
    Send a direct handler command to the WinDbg extension.
    
    Args:
        handler_name: Name of the handler
        timeout_ms: Timeout in milliseconds
        **kwargs: Additional arguments
        
    Returns:
        The handler response as a dictionary
    """
    manager = _get_communication_manager()
    return manager.send_handler_command(handler_name, timeout_ms, **kwargs)


def test_connection() -> bool:
    """
    Test if the connection to the WinDbg extension is working.
    
    Returns:
        True if connection is working, False otherwise
    """
    manager = _get_communication_manager()
    return manager.test_connection()


def test_target_connection() -> Tuple[bool, str]:
    """
    Test if the debugging target is responsive.
    
    Returns:
        Tuple of (is_connected, status_message)
    """
    manager = _get_communication_manager()
    return manager.test_target_connection()


def diagnose_connection_issues() -> Dict[str, Any]:
    """
    Run comprehensive connection diagnostics.
    
    Returns:
        Dictionary containing diagnostic results and recommendations
    """
    manager = _get_communication_manager()
    return manager.diagnose_connection_issues() 