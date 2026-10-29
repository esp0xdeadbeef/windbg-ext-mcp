"""
Server initialization module for WinDbg MCP Server.

Simplified initialization with essential functionality only.
"""
import logging
from typing import Tuple, Optional
from dataclasses import dataclass

from core.communication import test_connection, test_target_connection, diagnose_connection_issues

logger = logging.getLogger(__name__)


@dataclass
class ConnectionTestResult:
    """Results from connection testing during initialization."""
    extension_connected: bool
    target_connected: bool
    target_status: str
    debugging_mode: str
    error_message: Optional[str] = None


@dataclass
class InitializationConfig:
    """Configuration for server initialization."""
    test_connection: bool = True
    verbose_logging: bool = True


class ServerInitializer:
    """Handles the initialization sequence for the WinDbg MCP Server."""
    
    def __init__(self, config: Optional[InitializationConfig] = None):
        self.config = config or InitializationConfig()
    
    def initialize(self) -> ConnectionTestResult:
        """
        Run the initialization sequence.
        
        Returns:
            ConnectionTestResult containing the initialization results
        """
        logger.info("Starting WinDbg MCP Server initialization")
        
        try:
            # Test connections
            connection_result = self._test_connections()
            
            # Log results
            self._log_connection_summary(connection_result)
            
            logger.info("Server initialization completed successfully")
            return connection_result
            
        except Exception as e:
            logger.error(f"Server initialization failed: {e}")
            return ConnectionTestResult(
                extension_connected=False,
                target_connected=False,
                target_status="Initialization failed",
                debugging_mode="unknown",
                error_message=str(e)
            )
    
    def _test_connections(self) -> ConnectionTestResult:
        """
        Test connections to WinDbg extension and debugging target.
        
        Returns:
            ConnectionTestResult with test results
        """
        logger.info("Testing connection to WinDbg extension...")
        logger.info("=" * 50)
        
        extension_connected = False
        target_connected = False
        target_status = "Not tested"
        error_message = None
        
        if not self.config.test_connection:
            logger.info("⚠ Connection testing disabled in configuration")
            return ConnectionTestResult(
                extension_connected=True,  # Assume connected
                target_connected=True,
                target_status="Connection testing disabled",
                debugging_mode="unknown"
            )
        
        try:
            # BOUNDED, NON-FATAL.
            #
            # These probes talk to the extension pipe. If a WinDbgX session is
            # halted at a breakpoint, or the pipe is busy, the probe can block
            # for a very long time -- and because this runs BEFORE the HTTP
            # server binds, the bridge never comes up at all. Symptom: the
            # supervisor logs "no bridge answering", restarts the child, the child
            # hangs in this same probe, forever; `python -m mcp_server.server` is
            # alive but never listens on :8001.
            #
            # A session being wedged is NOT a reason to refuse to serve: /list and
            # every per-call `instance=` pin still work, and that is precisely when
            # you need the bridge most.
            def _bounded(fn, *a, **k):
                import concurrent.futures as _cf
                with _cf.ThreadPoolExecutor(max_workers=1) as ex:
                    return ex.submit(fn, *a, **k).result(timeout=5)
            try:
                extension_connected = _bounded(test_connection)
            except Exception as _e:
                logger.warning("extension probe skipped/timed out: %s", _e)
                extension_connected = False
            try:
                target_connected, target_status = _bounded(test_target_connection)
            except Exception as _e:
                logger.warning("target probe skipped/timed out: %s", _e)
                target_connected, target_status = False, "probe timed out"
            
            # Log results
            if extension_connected:
                logger.info("✓ Connected to WinDbg extension")
            else:
                logger.info("✗ Not connected to WinDbg extension")
                error_message = "Extension connection failed"
            
            if target_connected:
                logger.info(f"✓ Target connection: {target_status}")
            else:
                logger.info(f"⚠ Target issue: {target_status}")
                if not error_message:
                    error_message = f"Target connection failed: {target_status}"
                
        except Exception as e:
            logger.info(f"✗ Connection test failed: {e}")
            error_message = str(e)
        
        # If either connection failed, run diagnostics for more information
        if not extension_connected or not target_connected:
            self._run_connection_diagnostics()
        
        # Detect debugging mode
        debugging_mode = self._detect_debugging_mode(target_connected, target_status)
        logger.info(f"Detected debugging mode: {debugging_mode}")
        
        return ConnectionTestResult(
            extension_connected=extension_connected,
            target_connected=target_connected,
            target_status=target_status,
            debugging_mode=debugging_mode,
            error_message=error_message
        )
    
    def _run_connection_diagnostics(self):
        """Run connection diagnostics when connection fails."""
        logger.info("\n" + "=" * 50)
        logger.info("Running detailed connection diagnostics...")
        
        try:
            diagnostics = diagnose_connection_issues()
            logger.info(f"📋 Diagnostic Results:")
            logger.info(f"  - Extension available: {diagnostics['extension_available']}")
            logger.info(f"  - Target connected: {diagnostics['target_connected']}")
            
            if diagnostics.get("target_status"):
                logger.info(f"  - Target status: {diagnostics['target_status']}")
            
            if diagnostics.get("recommendations"):
                logger.info("\n💡 Recommendations:")
                for rec in diagnostics["recommendations"]:
                    logger.info(f"  • {rec}")
            
            logger.info("=" * 50)
        except Exception as e:
            logger.warning(f"Failed to run diagnostics: {e}")
    
    def _detect_debugging_mode(self, target_connected: bool, target_status: str) -> str:
        """
        Detect the current debugging mode.
        
        Args:
            target_connected: Whether target connection was successful
            target_status: Target connection status message
            
        Returns:
            "kernel", "user", or "unknown"
        """
        if not target_connected:
            logger.info(f"Target not connected: {target_status}")
            return "unknown"
        
        # Detect mode from target status
        ts = target_status.lower()
        if "user" in ts:
            return "user"
        elif "kernel" in ts:
            return "kernel"
        else:
            # For an attached browser renderer this is user-mode; don't assume kernel.
            return "user"
    
    def _log_connection_summary(self, connection_result: ConnectionTestResult):
        """Log a summary of connection status."""
        logger.info("Connection status summary:")
        logger.info(f"  - Extension available: {connection_result.extension_connected}")
        logger.info(f"  - Target connected: {connection_result.target_connected}")
        logger.info(f"  - Target status: {connection_result.target_status}")
        logger.info(f"  - Debugging mode: {connection_result.debugging_mode}")
        if connection_result.error_message:
            logger.info(f"  - Error: {connection_result.error_message}") 