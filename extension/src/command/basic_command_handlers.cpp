#include "pch.h"
#include "command/basic_command_handlers.h"
#include "command/command_utilities.h"
#include "../ipc/mcp_server.h"  // Include MCPServer definition
#include "../utils/constants.h"
#include <atlbase.h>   // CComPtr / CComQIPtr (interrupt handler)

void BasicCommandHandlers::RegisterHandlers(MCPServer& server) {
    // Register basic command handlers
    server.RegisterHandler("check_connection", CheckConnectionHandler);
    server.RegisterHandler("version", VersionHandler);
    server.RegisterHandler("get_metadata", GetMetadataHandler);
    server.RegisterHandler("interrupt", InterruptHandler);   // programmatic Ctrl+Break
    server.RegisterHandler("lm", ListModulesHandler);
    server.RegisterHandler("dt", DisplayTypeHandler);
    server.RegisterHandler("dd", DisplayMemoryHandler);
    server.RegisterHandler("get_console_log", GetConsoleLogHandler);   // full transcript
}

json BasicCommandHandlers::CheckConnectionHandler(const json& message) {
    return CommandUtilities::CreateSuccessResponse(
        message.value("id", 0),
        "check_connection",
        "Connection verified successfully"
    );
}

json BasicCommandHandlers::VersionHandler(const json& message) {
    // Get WinDBG version
    std::string version = "WinDbg Extension v1.0.0";
    
    return CommandUtilities::CreateSuccessResponse(
        message.value("id", 0),
        "version",
        version
    );
}

json BasicCommandHandlers::GetMetadataHandler(const json& message) {
    // Get basic metadata about the debugging session
    std::string targetInfo;
    std::string modules;
    
    // Maximum output size to prevent excessive memory usage (64KB)
    const size_t MAX_OUTPUT_SIZE = 65536;
    
    // Try to execute basic WinDBG commands to gather metadata
    try {
        auto args = message.value("args", json::object());
        unsigned int timeout = args.value("timeout_ms", Constants::ANALYSIS_TIMEOUT_MS); // Use analysis timeout for metadata
        
        targetInfo = CommandUtilities::ExecuteWinDbgCommand("!target", timeout);
        modules = CommandUtilities::ExecuteWinDbgCommand("lm", timeout);
        
        // Limit output size to prevent excessive memory usage
        if (targetInfo.size() > MAX_OUTPUT_SIZE) {
            targetInfo = targetInfo.substr(0, MAX_OUTPUT_SIZE) + 
                        "\n... [Output truncated due to size limit]";
        }
        
        if (modules.size() > MAX_OUTPUT_SIZE) {
            modules = modules.substr(0, MAX_OUTPUT_SIZE) + 
                     "\n... [Output truncated due to size limit]";
        }
        
        // Create combined metadata output
        std::string metadata = "Target Information:\n" + targetInfo + 
                              "\n\nModules:\n" + modules;
        
        return CommandUtilities::CreateSuccessResponse(
            message.value("id", 0),
            "get_metadata",
            metadata
        );
    }
    catch (const std::exception& e) {
        return CommandUtilities::CreateErrorResponse(
            message.value("id", 0),
            "get_metadata",
            std::string("Failed to get metadata: ") + e.what()
        );
    }
}

json BasicCommandHandlers::ListModulesHandler(const json& message) {
    try {
        auto args = message.value("args", json::object());
        unsigned int timeout = args.value("timeout_ms", Constants::BULK_TIMEOUT_MS); // Use bulk timeout for module listing
        
        // Maximum output size to prevent excessive memory usage (64KB)
        const size_t MAX_OUTPUT_SIZE = 65536;
        
        std::string output = CommandUtilities::ExecuteWinDbgCommand("lm", timeout);
        
        // Limit output size to prevent excessive memory usage
        if (output.size() > MAX_OUTPUT_SIZE) {
            output = output.substr(0, MAX_OUTPUT_SIZE) + 
                    "\n... [Output truncated due to size limit - use more specific lm options for full listing]";
        }
        
        return CommandUtilities::CreateSuccessResponse(
            message.value("id", 0),
            "lm",
            output
        );
    }
    catch (const std::exception& e) {
        return CommandUtilities::CreateErrorResponse(
            message.value("id", 0),
            "lm",
            std::string("Command failed: ") + e.what()
        );
    }
}

json BasicCommandHandlers::DisplayTypeHandler(const json& message) {
    try {
        auto args = message.value("args", json::object());
        std::string typeName = args.value("type_name", "");
        std::string address = args.value("address", "");
        unsigned int timeout = args.value("timeout_ms", Constants::ANALYSIS_TIMEOUT_MS); // Use analysis timeout for type display
        
        if (typeName.empty()) {
            return CommandUtilities::CreateErrorResponse(
                message.value("id", 0),
                "dt",
                "Type name is required"
            );
        }
        
        std::string command = "dt " + typeName;
        if (!address.empty()) {
            command += " " + address;
        }
        
        std::string output = CommandUtilities::ExecuteWinDbgCommand(command, timeout);
        return CommandUtilities::CreateSuccessResponse(
            message.value("id", 0),
            "dt",
            output
        );
    }
    catch (const std::exception& e) {
        return CommandUtilities::CreateErrorResponse(
            message.value("id", 0),
            "dt",
            std::string("Command failed: ") + e.what()
        );
    }
}

json BasicCommandHandlers::DisplayMemoryHandler(const json& message) {
    try {
        auto args = message.value("args", json::object());
        std::string address = args.value("address", "");
        int length = args.value("length", 32);
        unsigned int timeout = args.value("timeout_ms", Constants::SLOW_TIMEOUT_MS); // Use slow timeout for memory display
        
        if (address.empty()) {
            return CommandUtilities::CreateErrorResponse(
                message.value("id", 0),
                "dd",
                "Address is required"
            );
        }
        
        std::string command = "dd " + address + " " + std::to_string(length);
        std::string output = CommandUtilities::ExecuteWinDbgCommand(command, timeout);
        return CommandUtilities::CreateSuccessResponse(
            message.value("id", 0),
            "dd",
            output
        );
    }
    catch (const std::exception& e) {
        return CommandUtilities::CreateErrorResponse(
            message.value("id", 0),
            "dd",
            std::string("Command failed: ") + e.what()
        );
    }
}


// ---------------------------------------------------------------------------
// InterruptHandler -- programmatic Ctrl+Break.
//
// ".break" / ".breakin" are COMMAND STRINGS: dbgeng refuses them while the
// target runs because there is no current scope ("'.break outside of scope").
// IDebugControl::SetInterrupt() is different -- per dbgeng.h it "registers a
// user interrupt" and is explicitly reentrant, so it can be called from any
// client/thread while the engine is blocked in WaitForEvent. It is the API
// behind Ctrl+Break and works on a free-running (kernel) target.
// ---------------------------------------------------------------------------
json BasicCommandHandlers::InterruptHandler(const json& message) {
    const int id = message.value("id", 0);

    // Use the PROCESS-WIDE client, not a fresh DebugCreate.
    //
    // A newly created IDebugClient has no target attached. SetInterrupt() on it
    // either fails or interrupts nothing, and — worse — creating a second client
    // while the singleton is mid-command perturbs the engine so the session dies
    // (observed: /list goes empty and the UDP listener frees right after an
    // interrupt, i.e. the whole WinDbgX/EngHost went away).
    //
    // GetOrCreateClient() returns the same client the command path uses, so the
    // interrupt lands on the engine that actually owns the target.
    CComPtr<IDebugClient> client;
    CComPtr<OutputCallbacks> cbs;
    HRESULT hr = CommandUtilities::GetOrCreateClient(&client, &cbs);
    if (FAILED(hr))
        return CommandUtilities::CreateErrorResponse(id, "interrupt", "GetOrCreateClient failed");

    CComQIPtr<IDebugControl> control(client);
    if (!control)
        return CommandUtilities::CreateErrorResponse(id, "interrupt", "no IDebugControl");

    ULONG before = 0;
    control->GetExecutionStatus(&before);

    hr = control->SetInterrupt(DEBUG_INTERRUPT_ACTIVE);
    if (FAILED(hr))
        return CommandUtilities::CreateErrorResponse(id, "interrupt", "SetInterrupt failed");

    bool broke = false;
    for (int i = 0; i < 50; ++i) {
        Sleep(100);
        ULONG st = 0;
        control->GetExecutionStatus(&st);
        if (st == DEBUG_STATUS_BREAK) { broke = true; break; }
    }

    std::string out = broke ? "target interrupted (break)"
                            : "interrupt requested; target not yet reported break";
    json resp = CommandUtilities::CreateSuccessResponse(id, "interrupt", out);
    resp["broke"] = broke;
    resp["was_running"] = (before != DEBUG_STATUS_BREAK);
    return resp;
}
json BasicCommandHandlers::GetConsoleLogHandler(const json& message) {
    // Return the ACCUMULATED console transcript -- every command echoed with its
    // prompt plus all of its output -- rather than the single-command reply the
    // `execute_command` path produces.
    //
    // This exists because run_command is deliberately capped (MAX_OUTPUT_SIZE)
    // and scoped to one command: after a long session the transcript is exactly
    // what is wanted for a report or a post-mortem, and it cannot be
    // reconstructed from a series of per-command replies once any of them was
    // truncated.
    //
    // READ-ONLY BY DESIGN: there is no clear/reset argument and no wipe path.
    // A transcript is evidence; an accidental clear (a script, a retry loop,
    // another agent sharing the pipe) destroys precisely what was wanted, and
    // nothing here needs one -- the log caps itself and trims its oldest bytes.
    const int id = message.value("id", 0);
    try {
        auto args = message.value("args", json::object());
        // last_command: return just the most recent command's transcript, which
        // is the UNCAPPED form of the run_command reply (see GetLastCommandLog).
        const bool lastOnly = args.value("last_command", false);
        // 0 / negative => use the callback's own (larger) cap.
        size_t maxBytes = 0;
        if (args.contains("max_bytes") && args["max_bytes"].is_number_integer()) {
            const long long v = args["max_bytes"].get<long long>();
            if (v > 0) maxBytes = static_cast<size_t>(v);
        }

        // The callback belongs to the process-wide client; reuse it, do NOT
        // create a second one (that kills the session -- see InterruptHandler).
        CComPtr<IDebugClient> client;
        CComPtr<OutputCallbacks> cbs;
        HRESULT hr = CommandUtilities::GetOrCreateClient(&client, &cbs);
        if (FAILED(hr) || !cbs)
            return CommandUtilities::CreateErrorResponse(
                id, "get_console_log", "GetOrCreateClient failed");

        bool truncated = false;
        std::string log = lastOnly ? cbs->GetLastCommandLog(maxBytes, &truncated)
                                  : cbs->GetConsoleLog(maxBytes, &truncated);

        json resp = CommandUtilities::CreateSuccessResponse(id, "get_console_log", log);
        resp["bytes"] = log.size();
        resp["truncated"] = truncated;
        resp["last_command"] = lastOnly;
        return resp;
    }
    catch (const std::exception& e) {
        return CommandUtilities::CreateErrorResponse(
            id, "get_console_log", std::string("Failed to read console log: ") + e.what());
    }
}
