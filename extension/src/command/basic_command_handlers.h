#pragma once

#include "pch.h"
#include "../include/json.hpp"
#include "command_utilities.h"  // Include for utility functions and types
#include <Windows.h>
#include <DbgEng.h>
#include <WDBGEXTS.H>
#include <string>
#include <vector>

using json = nlohmann::json;

// Forward declarations
class MCPServer;

/**
 * @brief Basic command handlers for fundamental WinDbg operations.
 * 
 * This class provides handlers for basic WinDbg operations including:
 * - Connection checking and status
 * - Version information
 * - Basic metadata retrieval
 * - Module listing
 * - Type display
 * - Memory display
 */
class BasicCommandHandlers {
public:
    /**
     * @brief Register basic command handlers with the MCP server.
     * @param server The MCP server instance.
     */
    static void RegisterHandlers(MCPServer& server);
    
    // Basic command handlers
    
    /**
     * @brief Check if the WinDbg connection is active.
     * @param message JSON message (no parameters required).
     * @return JSON response with connection status.
     */
    static json CheckConnectionHandler(const json& message);
    static json InterruptHandler(const json& message);
    
    /**
     * @brief Get WinDbg version information.
     * @param message JSON message (no parameters required).
     * @return JSON response with version information.
     */
    static json VersionHandler(const json& message);
    
    /**
     * @brief Get basic debugging session metadata.
     * @param message JSON message (no parameters required).
     * @return JSON response with session metadata.
     */
    static json GetMetadataHandler(const json& message);
    
    /**
     * @brief List loaded modules.
     * @param message JSON message with optional timeout parameter.
     * @return JSON response with module list.
     */
    static json ListModulesHandler(const json& message);
    
    /**
     * @brief Display type information.
     * @param message JSON message with type_name and optional address.
     * @return JSON response with type information.
     */
    static json DisplayTypeHandler(const json& message);
    
    /**
     * @brief Display memory contents.
     * @param message JSON message with address and optional length.
     * @return JSON response with memory contents.
     */
    static json DisplayMemoryHandler(const json& message);

    /**
     * @brief Return the console transcript.
     *
     * The per-command reply (run_command) is capped and covers ONE command.
     * This returns the accumulated session transcript instead, so a caller can
     * read back everything the debugger printed (each command echoed with its
     * prompt).
     *
     * Args:
     *   max_bytes (int, 0 = the log's own cap)  Hard cap on the returned text.
     *   last_command (bool, default false)      Return only the most recent
     *        command's transcript -- the uncapped form of its reply.
     *
     * There is deliberately NO clear/reset option: the transcript is evidence,
     * and an accidental wipe (script, retry loop, another agent on the pipe)
     * destroys exactly what was wanted. Do not add one.
     *
     * @return JSON response with `output`, `bytes`, `truncated`, `last_command`.
     */
    static json GetConsoleLogHandler(const json& message);

private:
    // Basic command handlers are straightforward and don't need helper methods
}; 