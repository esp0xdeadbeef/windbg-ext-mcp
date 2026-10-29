#include "pch.h"
#include "command/command_utilities.h"
#include "utils/output_callbacks.h"
#include <sstream>
#include <atlcomcli.h>
#include <future>
#include <chrono>
#include <algorithm>
#include <thread>
#include <mutex>
#include <condition_variable>
#include <iomanip>
#include <random>
#include <ctime>

// ---------------------------------------------------------------------------
// Process-wide debug client + output callback.
//
// dbgeng emits its startup banner (version, dbgeng/dbghelp paths, extension
// chain, session/uptime) when an IDebugClient attaches its first output
// callback. Creating a client per command therefore replayed that banner before
// every reply, so every reply began with 40+ lines of noise.
//
// There is exactly one debugger per process, so one client and one callback
// serve every command: the banner is emitted once, at first use (drained there),
// and never again. The buffer is cleared between commands instead of
// reinstalling the callback -- reinstalling is what re-triggers the banner.
// ---------------------------------------------------------------------------
// Commands the bridge fires internally (health probes, metadata, mode
// detection). They must NOT be mirrored to the WinDbgX console, because their
// output is noise (the full `version` banner, a giant `lm` list, etc.) that the
// user never asked to see. A human typing `.echo foo` or `k` should see it; the
// bridge asking for the effective machine should not.
static bool IsInternalCommand(const std::string& cmd) {
    std::string c = cmd;
    for (auto& ch : c) ch = (char)std::tolower((unsigned char)ch);
    // trim leading dots/whitespace
    size_t i = 0;
    while (i < c.size() && (c[i] == '.' || c[i] == ' ' || c[i] == '\t')) ++i;
    std::string t = c.substr(i);
    static const char* kInternal[] = {
        "version", "lastevent", "effmach", "target", "effmach",
        // the bridge's own health probes
        "check_connection",
    };
    // exact prefix match on the first token
    std::string first = t.substr(0, t.find_first_of(" \t"));
    for (const char* k : kInternal) {
        if (first == k) return true;
    }
    return false;
}

static CComPtr<IDebugClient>    g_mcpClient;
static CComPtr<OutputCallbacks> g_mcpCallbacks;
static std::mutex               g_mcpClientMutex;

// Non-static now: InterruptHandler needs the SAME singleton client. A fresh
// DebugCreate has no target, and creating a second client kills the session.
HRESULT CommandUtilities::GetOrCreateClient(IDebugClient** outClient, OutputCallbacks** outCallbacks)
{
    std::lock_guard<std::mutex> lock(g_mcpClientMutex);
    if (!g_mcpClient) {
        CComPtr<IDebugClient> client;
        HRESULT hr = DebugCreate(__uuidof(IDebugClient), (void**)&client);
        if (FAILED(hr)) return hr;

        CComPtr<OutputCallbacks> cbs = new OutputCallbacks();
        hr = client->SetOutputCallbacks(cbs);
        if (FAILED(hr)) return hr;

        // Do NOT "drain" by executing an empty command: dbgeng prints a bare
        // prompt for each one, and several of those produced the repeated
        // "0:033>0:033>0:033>" lines in the console. Just discard whatever the
        // first attach emitted.
        cbs->Clear();

        // ----------------------------------------------------------------
        // MASK THE ENGINE'S BACKGROUND NOTIFICATION SPAM.
        //
        // dbgeng emits unsolicited NOTIFICATION output for debuggee events --
        // `ModLoad:`, `ModUnload:`, `DllLoad:`, thread create/exit -- and in a
        // KD session that is constant: every module the debugee touches while
        // running produces a burst. It goes to EVERY client (the WinDbgX console
        // included), so the console pane fills with lines nobody typed and the
        // commands that WERE typed scroll away.
        //
        // These are not command output: they are not in our reply either (the
        // `g` reply is clean), which is why filtering command text would never
        // have worked -- there is no command to attach them to.
        //
        // DEBUG_OUTPUT_DEBUGGEE is the category for exactly this. Clearing it
        // leaves NORMAL / ERROR / WARNING / PROMPT / EXTENSION_WARNING /
        // SYMBOLS / STATUS intact, so:
        //   * commands still echo and return their results,
        //   * errors and warnings still show,
        //   * requested symbol diagnostics (`!sym noisy`) still show,
        //   * the ModLoad/DllLoad notification flood does not.
        //
        // Deliberately NOT DEBUG_ENGOPT_KD_QUIET_MODE: that is the legacy
        // KDQUIET switch and it also swallows symbol-loading diagnostics, which
        // we DO want when a symbol problem is why we are looking. Mask one
        // output class instead of changing the engine's whole verbosity.
        //
        // Get/SetOutputMask live on IDebugCLIENT, not IDebugControl (the control
        // interface only has Get/SetOtherOutputMask, which needs the target
        // client's handle). `client` is already an IDebugClient, so call it there.
        // ----------------------------------------------------------------
        ULONG outMask = 0;
        if (SUCCEEDED(client->GetOutputMask(&outMask))) {
            client->SetOutputMask(outMask & ~DEBUG_OUTPUT_DEBUGGEE);
        }

        g_mcpClient = client;
        g_mcpCallbacks = cbs;
    }
    g_mcpClient.CopyTo(outClient);
    g_mcpCallbacks.CopyTo(outCallbacks);
    return S_OK;
}

// Global variables for tracking performance and health (protected by mutex)
std::mutex CommandUtilities::s_staticMembersMutex;

// Static member definitions
std::chrono::steady_clock::time_point CommandUtilities::g_lastCommandTime = std::chrono::steady_clock::now();
std::string CommandUtilities::g_sessionId;
double CommandUtilities::g_lastExecutionTime = 0.0;

// CommandExecutor class implementation
class CommandExecutor {
public:
    struct CommandResult {
        std::string output;
        HRESULT hr;
        bool hasTimedOut;
        
        CommandResult() : output(""), hr(S_OK), hasTimedOut(false) {}
        CommandResult(const std::string& out, HRESULT result = S_OK, bool timedOut = false)
            : output(out), hr(result), hasTimedOut(timedOut) {}
    };

    static CommandResult ExecuteWithTimeout(const std::string& command, unsigned int timeoutMs, bool quiet = false) {
        CommandResult result;
        
        // Create a promise/future for the command execution
        std::promise<CommandResult> promise;
        auto future = promise.get_future();
        
        // Shared pointer to control interface for interrupt capability
        std::shared_ptr<CComQIPtr<IDebugControl>> sharedControl = std::make_shared<CComQIPtr<IDebugControl>>();
        
        // Launch the command execution in a separate thread
        std::thread executionThread([&promise, command, sharedControl, quiet]() {
            bool promiseSet = false;
            try {
                // ONE debug client + callback for the process lifetime (see
                // GetOrCreateClient above). DebugCreate per command made dbgeng
                // replay its startup banner on every call.
                IDebugClient* rawClient = nullptr;
                OutputCallbacks* callbacks = nullptr;
                HRESULT hr = CommandUtilities::GetOrCreateClient(&rawClient, &callbacks);
                if (FAILED(hr)) {
                    promise.set_value(CommandResult("Failed to create debug client", hr));
                    promiseSet = true;
                    return;
                }
                CComPtr<IDebugClient> client;
                client.Attach(rawClient);          // keep the singleton alive
                
                CComQIPtr<IDebugControl> control(client);
                if (!control) {
                    promise.set_value(CommandResult("Failed to get debug control interface", E_FAIL));
                    promiseSet = true;
                    return;
                }
                
                // Store the control interface for potential interrupt
                *sharedControl = control;
                
                // Reuse the singleton callback: clear it instead of reinstalling,
                // which would make dbgeng re-emit the banner.
                callbacks->Clear();
                
                // Execute the command.
                //
                // ALL_CLIENTS is the ONLY way output reaches the WinDbgX
                // console -- that is the same channel that prints the
                // "0:006>" prompt. THIS_CLIENT writes to our private callback
                // only, which is why the console showed prompts but no commands
                // and no results.
                //
                // The problem with ALL_CLIENTS was never the routing, it was
                // that dbgeng ALSO replays its startup banner there. That banner
                // is emitted once, when a debug client first attaches an output
                // callback (GetOrCreateClient), and is discarded there. Here we
                // only reset our buffer so the reply contains this command's
                // output and nothing else.
                //
                // ECHO makes dbgeng print "<prompt><command>" itself, which is
                // the line the console was missing.
                callbacks->Clear();

                // ORIGIN decides visibility -- NOT the command text.
                //
                // `quiet` is set by the MCP bridge for commands IT issues. There
                // are exactly two cases and nothing else should be special-cased:
                //
                //   quiet == true   the command came from an internal MCP probe
                //                   (health check, discovery). Output goes to our
                //                   callback only: the user never typed it, so the
                //                   console must not pretend they did.
                //   quiet == false  the command came from the user via MCP. Send
                //                   it to ALL_CLIENTS so it renders exactly as if
                //                   typed, and let dbgeng do the echoing.
                //
                // The earlier implementation decided this by matching a hardcoded
                // list of "internal" command names. That is fragile -- any probe
                // missing from the list leaks into the console -- and wrong in
                // principle: whether output belongs on screen depends on WHO
                // issued it, not on what the text says. It also could not
                // distinguish "an internal call that happens to run .lastevent"
                // from "the user typed .lastevent".
                unsigned long outctl = quiet ? DEBUG_OUTCTL_THIS_CLIENT
                                             : DEBUG_OUTCTL_ALL_CLIENTS;

                // ECHO THE COMMAND **WITH ITS PROMPT**.
                //
                // DEBUG_EXECUTE_ECHO alone prints the bare command text
                // (`vertarget`) with no prompt, so the console and the MCP reply
                // were a stream of commands and outputs with no way to tell which
                // output belonged to which command:
                //
                //     g
                //     Break instruction exception - code 80000003 (first chance)
                //     Breakpoint 0 hit
                //     win32kbase!HMAllocObject:
                //     ffffbc0e`4a47c6e0 48895c2420  mov qword ptr [rsp+20h],rbx
                //     vertarget
                //     Windows 10 Kernel Version 19041 ...
                //
                // What is wanted is the debugger's own input line:
                //
                //     0: kd> vertarget
                //     Windows 10 Kernel Version 19041 ...
                //
                // IDebugControl::OutputPrompt does exactly that: it formats the
                // prompt dbgeng would show (processor/thread numbers, `0: kd>` or
                // `?: kd>` for a free-running target -- so the prompt also tells
                // you the run state at a glance) followed by the format string.
                // Passing the command as that format string gives one atomic
                // engine call, in order, on the same pass -- no `.echo` race.
                //
                // So use OutputPrompt(command) and do NOT set DEBUG_EXECUTE_ECHO
                // (that would print the command a second time).
                //
                // Only for non-quiet commands: an internal MCP probe was never
                // typed by anyone, so the console must not show it as input.
                // Outctl for the prompt matches the command's, so both go to the
                // same places (console + our callback).
                //
                // Echoed input line, kept for the reply (see the race note below):
                // " ", command, "\n". Empty for quiet commands, which must not
                // look typed.
                std::string echoedLine;
                unsigned long execFlags = DEBUG_EXECUTE_DEFAULT;
                if (!quiet) {
                    // OutputPrompt uses printf-style formatting, so a command that
                    // contains a '%' (e.g. `%` in a dd format spec, or a literal
                    // percent in a symbol) would be misread as a format directive.
                    // Escape it: the command must appear verbatim.
                    std::string echoed;
                    echoed.reserve(command.size() * 2);
                    for (char c : command) {
                        if (c == '%') echoed += '%';
                        echoed += c;
                    }
                    // OutputPrompt writes the prompt itself but NEITHER the space
                    // that normally follows it NOR a newline. Measured result of
                    // passing only "%s":
                    //
                    //     ?: kd>vertargetWARNING: The debugger does not have...
                    //
                    // so the command ran straight into the first line of output.
                    // Put the space in the format string and terminate the line.
                    // DEBUG_OUTCTL_* alone adds neither.
                    //
                    // Exactly ONE newline terminates the echoed command, so the
                    // transcript matches what WinDbg's own console shows:
                    //
                    //     ?: kd> vertarget
                    //     Windows 10 Kernel Version 19041 MP (2 procs)...
                    //
                    // Earlier this emitted "\n\n" (a blank line after the
                    // command) to make the input line "stand alone". Measured
                    // in use, that reads as a cosmetic bug rather than a
                    // separator: the real console never inserts a blank line
                    // after a typed command, and the doubled newline also broke
                    // up multi-command output. Do not reintroduce it.
                    //
                    // DO NOT PUT THE COMMAND IN OutputPrompt'S FORMAT STRING.
                    //
                    // That was the original implementation and it silently lost
                    // the input line for extension commands (`!heap`, `!pte`,
                    // `!analyze`, `!drvobj`, ...). The MCP reply still contained
                    // them because our callback captures ALL_CLIENTS output, but
                    // the WinDbgX console never showed the `0: kd> !heap` line --
                    // the command appeared to be missing from the transcript
                    // while its results were there.
                    //
                    // Cause: OutputPrompt renders the prompt through dbgeng's
                    // per-context *prompt format*, and the engine resets that
                    // format (empties it) while dispatching an extension
                    // command. So the prompt template the next OutputPrompt call
                    // uses is the empty one, and everything the command path
                    // passes as the format string is dropped -- prompt AND
                    // command text. It depends on which command ran previously,
                    // which is why it looked intermittent: `!irql` right after
                    // `vertarget` echoed fine, while `!pte` after `!kuser` did
                    // not.
                    //
                    // Fix, and it is deliberately not a `!`-prefix check (a
                    // hardcoded list of extension commands would break on any
                    // other command whose dispatch touches the prompt format):
                    // ask the engine for the prompt ONCE with an empty format,
                    // so only the real prompt is rendered (`0: kd>`, `?: kd>` --
                    // still engine-generated, still tells you the run state),
                    // then print the command as ordinary TEXT with Output().
                    // Plain output is never subject to prompt formatting, so the
                    // input line is emitted verbatim for every command kind:
                    // engine commands, dot commands and `!` extension commands.
                    //
                    // Output() takes a printf format too, so the `%`-escaping of
                    // the command text still applies -- print the escaped string
                    // as a `%s` argument rather than as the format itself.
                    // ONE output call, prompt included.
                    //
                    // Two separate calls (OutputPrompt then Output) leak the
                    // engine's prompt state into the transcript. OutputPrompt
                    // renders dbgeng's per-context prompt FORMAT, and after an
                    // extension command that format is reset to empty, so the
                    // prompt silently vanishes and the line reads
                    //
                    //     !sysinfo smbios          <-- no "0: kd>" prefix
                    //
                    // while the command text still appears. Measured back to
                    // back on one session: `!sysinfo cpuinfo` rendered
                    // `0: kd> !sysinfo cpuinfo`, and the very next command,
                    // `!sysinfo smbios`, rendered a bare `!sysinfo smbios`. A
                    // missing prompt is easy to miss in a screenshot and invisible
                    // in the HTTP reply, which is why it survived several rounds
                    // of "verified".
                    //
                    // Prompt and command go out as a SINGLE Output() so the line
                    // cannot be interleaved with the command's own output, and so
                    // the callback buffer receives it atomically.
                    //
                    // The prompt is built from the engine's run state, which is
                    // exactly what it encodes: `0: kd>` once broken in, `?: kd>`
                    // while free-running. Asking OutputPrompt for it is unreliable
                    // (that is the bug), so only the run state is queried.
                    std::string promptText;
                    {
                        ULONG exState = 0;
                        HRESULT sh = control->GetExecutionStatus(&exState);
                        const bool isRunning = SUCCEEDED(sh) && exState != DEBUG_STATUS_BREAK;
                        promptText = isRunning ? "?: kd> " : "0: kd> ";
                    }
                    control->Output(outctl, "%s\n", (promptText + command).c_str());

                    // Keep the EXACT text of the input line for the reply.
                    //
                    // The console copy above is what the human sees, but the
                    // copy in OUR buffer can still be lost: dbgeng appends to the
                    // callback from its own thread, and `callbacks->Clear()`
                    // (both at the top of this function and any the bridge does
                    // between calls) can land between the two Output calls and
                    // the first chunk of command output. When that happens the
                    // reply starts at the command's RESULT and the input line is
                    // simply absent -- measured on `!sysinfo cpuinfo`,
                    // `!sysinfo smbios` and `!pte`, intermittently, on the same
                    // session that echoed `!sysinfo machineid` correctly a second
                    // earlier. Intermittent is the tell: it is a race, not a
                    // property of any command.
                    //
                    // So the reply no longer TRUSTS the buffer to contain the
                    // echo. We remember the line and, after reading the buffer,
                    // put it back if it is missing.
                    echoedLine = promptText + command + "\n";
                }

                hr = control->Execute(outctl, command.c_str(), execFlags);

                // No flush, no event pump.
                //
                // GetOutput() reads the callback buffer that dbgeng has already
                // filled during the synchronous Execute() above.
                //
                // I added a "flush before reading" loop calling
                // WaitForEvent(0, 0) twice. That is wrong for two reasons:
                //   1. it is a BUSY POLL with a zero timeout, so it almost
                //      always returns S_FALSE and flushes nothing, and
                //   2. dbgeng's output for a synchronous Execute() has already
                //      been delivered to the callback by the time Execute
                //      returns -- pumping the event loop from the SAME thread
                //      that owns the engine can wedge it (symptom: every later
                //      command hangs, `g` appears to do nothing, and the target
                //      stops making progress).
                // OutputCallbacks is already synchronised, so just read it.

                // Get the output for MCP
                std::string output = callbacks->GetOutput();

                // Make the reply START with the full `0: kd> <command>` line.
                //
                // Containment is not enough: a reply whose first line is a bare
                // `!sysinfo smbios` (prompt missing) is the exact defect this
                // change is about, and a `find(command)` test passes for it. So
                // compare the first line against the echoed line and repair it.
                //
                // An EMPTY buffer is left empty: the command produced nothing, and
                // synthesising a lone echo would misreport that as output.
                if (!echoedLine.empty() && !output.empty()) {
                    const std::string want = echoedLine.substr(0, echoedLine.size() - 1); // no trailing \n
                    const size_t firstNl = output.find('\n');
                    const std::string firstLine =
                        output.substr(0, (firstNl == std::string::npos) ? output.size() : firstNl);
                    if (firstLine != want) {
                        if (firstLine.find(command) != std::string::npos) {
                            // The echo is there but its prompt is wrong/absent:
                            // replace that line wholesale.
                            output = want +
                                ((firstNl == std::string::npos) ? std::string() : "\n" + output.substr(firstNl + 1));
                        } else {
                            // The buffer starts at the command's output: prepend.
                            output = want + "\n" + output;
                        }
                    }
                }

                // Do NOT uninstall the callback: it belongs to the process-wide
                // client and is reused. SetOutputCallbacks(nullptr) forces a
                // reinstall next time, which re-triggers the banner.
                client.Detach();   // drop our reference, keep the singleton
                
                promise.set_value(CommandResult(output, hr));
                promiseSet = true;
                
            } catch (const std::exception& e) {
                if (!promiseSet) {
                    promise.set_value(CommandResult(std::string("Exception: ") + e.what(), E_FAIL));
                    promiseSet = true;
                }
            } catch (...) {
                if (!promiseSet) {
                    promise.set_value(CommandResult("Unknown exception", E_FAIL));
                    promiseSet = true;
                }
            }
            
            // Final safety net - ensure promise is always set
            if (!promiseSet) {
                promise.set_value(CommandResult("Internal error: Promise not set", E_FAIL));
            }
        });
        
        // Wait for either completion or timeout
        auto status = future.wait_for(std::chrono::milliseconds(timeoutMs));
        
        if (status == std::future_status::timeout) {
            result.hasTimedOut = true;
            result.output = "Command timed out";
            result.hr = E_ABORT;
            
            // Try to interrupt the command gracefully using SetInterrupt
            if (*sharedControl) {
                (*sharedControl)->SetInterrupt(DEBUG_INTERRUPT_ACTIVE);
            }
            
            // Give the command a brief opportunity to respond to the interrupt
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
            
            // Check if the command completed after interrupt
            auto interruptStatus = future.wait_for(std::chrono::milliseconds(500));
            if (interruptStatus == std::future_status::ready) {
                // Command completed after interrupt, get the result
                auto interruptedResult = future.get();
                result.output += " (interrupted)";
                
                // Wait for thread to complete safely since command finished
                if (executionThread.joinable()) {
                    executionThread.join();
                }
            } else {
                // Command still running despite interrupt, detach thread to avoid blocking
                if (executionThread.joinable()) {
                    executionThread.detach();
                }
            }
        } else {
            // Command completed normally
            result = future.get();
            
            // Wait for thread to complete safely
            if (executionThread.joinable()) {
                executionThread.join();
            }
        }
        
        return result;
    }
};

std::string CommandUtilities::ExecuteWinDbgCommand(const std::string& command, unsigned int timeoutMs, bool quiet) {
    // Validate command
    if (command.empty()) {
        throw std::invalid_argument("Command cannot be empty");
    }
    
    try {
        auto result = CommandExecutor::ExecuteWithTimeout(command, timeoutMs, quiet);
        
        if (result.hasTimedOut) {
            throw std::runtime_error("Command timed out after " + std::to_string(timeoutMs) + " ms");
        }
        
        if (FAILED(result.hr)) {
            std::ostringstream oss;
            oss << "Command failed with HRESULT: 0x" << std::hex << result.hr;
            if (!result.output.empty()) {
                oss << " - " << result.output;
            }
            throw std::runtime_error(oss.str());
        }
        
        return result.output;
    }
    catch (const std::exception& e) {
        throw std::runtime_error(std::string("Command execution failed: ") + e.what());
    }
}

json CommandUtilities::CreateSuccessResponse(int id, const std::string& command, const std::string& output) {
    return {
        {"type", "response"},
        {"id", id},
        {"status", "success"},
        {"command", command},
        {"output", output},
        {"timestamp", GetCurrentTimestamp()}
    };
}

json CommandUtilities::CreateSuccessResponseWithMetadata(int id, const std::string& command, const std::string& output, 
                                                      double execution_time, const std::string& debugging_mode) {
    // Parse modules from output if it contains module listing
    json modules = json::array();
    std::string modulesOutput = output;
    
    // Basic module parsing for commands like 'lm'
    if (command.find("lm") == 0 || command.find("modules") != std::string::npos) {
        std::istringstream moduleStream(modulesOutput);
        std::string line;
        while (std::getline(moduleStream, line)) {
            if (!line.empty() && line.find("start    end") == std::string::npos) {
                modules.push_back(line);
            }
        }
    }
    
    return {
        {"type", "response"},
        {"id", id},
        {"status", "success"},
        {"command", command},
        {"output", output},
        {"metadata", {
            {"execution_time", execution_time},
            {"debugging_mode", debugging_mode},
            {"modules", modules},
            {"timestamp", GetCurrentTimestamp()}
        }}
    };
}

json CommandUtilities::CreateEnhancedErrorResponse(int id, const std::string& command, 
                                                 const std::string& error,
                                                 ErrorCategory category,
                                                 const std::string& suggestion) {
    return {
        {"type", "response"},
        {"id", id},
        {"status", "error"},
        {"command", command},
        {"error", error},
        {"error_category", GetErrorCategoryString(category)},
        {"suggestion", suggestion},
        {"timestamp", GetCurrentTimestamp()}
    };
}

json CommandUtilities::CreateErrorResponse(int id, const std::string& command, const std::string& error) {
    return {
        {"type", "response"},
        {"id", id},
        {"status", "error"},
        {"command", command},
        {"error", error},
        {"timestamp", GetCurrentTimestamp()}
    };
}

json CommandUtilities::CreateDetailedErrorResponse(
    int id,
    const std::string& command,
    const std::string& error,
    ErrorCategory category,
    HRESULT errorCode,
    const std::string& suggestion) {
    
    return {
        {"type", "response"},
        {"id", id},
        {"status", "error"},
        {"command", command},
        {"error", error},
        {"error_category", GetErrorCategoryString(category)},
        {"error_code", static_cast<unsigned int>(errorCode)},
        {"suggestion", suggestion},
        {"timestamp", GetCurrentTimestamp()}
    };
}

ErrorCategory CommandUtilities::ClassifyError(const std::string& errorMessage, HRESULT errorCode) {
    // Convert to lowercase for case-insensitive comparison
    std::string lowerError = errorMessage;
    std::transform(lowerError.begin(), lowerError.end(), lowerError.begin(), 
                   [](char c) { return static_cast<char>(std::tolower(static_cast<unsigned char>(c))); });
    
    // Check for specific HRESULT codes
    if (errorCode == E_INVALIDARG) {
        return ErrorCategory::CommandSyntax;
    } else if (errorCode == E_ACCESSDENIED) {
        return ErrorCategory::PermissionDenied;
    } else if (errorCode == E_OUTOFMEMORY) {
        return ErrorCategory::ResourceExhaustion;
    } else if (errorCode == RPC_E_DISCONNECTED || errorCode == RPC_E_SERVER_DIED) {
        return ErrorCategory::ConnectionLost;
    }
    
    // Check for specific error message patterns
    if (lowerError.find("syntax error") != std::string::npos ||
        lowerError.find("invalid command") != std::string::npos ||
        lowerError.find("unknown command") != std::string::npos) {
        return ErrorCategory::CommandSyntax;
    }
    
    if (lowerError.find("access denied") != std::string::npos ||
        lowerError.find("permission denied") != std::string::npos) {
        return ErrorCategory::PermissionDenied;
    }
    
    if (lowerError.find("out of memory") != std::string::npos ||
        lowerError.find("memory allocation") != std::string::npos) {
        return ErrorCategory::ResourceExhaustion;
    }
    
    if (lowerError.find("connection") != std::string::npos ||
        lowerError.find("disconnect") != std::string::npos ||
        lowerError.find("rpc") != std::string::npos) {
        return ErrorCategory::ConnectionLost;
    }
    
    if (lowerError.find("timeout") != std::string::npos ||
        lowerError.find("timed out") != std::string::npos) {
        return ErrorCategory::Timeout;
    }
    
    if (lowerError.find("process") != std::string::npos ||
        lowerError.find("thread") != std::string::npos ||
        lowerError.find("context") != std::string::npos) {
        return ErrorCategory::ExecutionContext;
    }
    
    return ErrorCategory::Unknown;
}

std::string CommandUtilities::GetErrorCategoryString(ErrorCategory category) {
    switch (category) {
        case ErrorCategory::CommandSyntax: return "CommandSyntax";
        case ErrorCategory::PermissionDenied: return "PermissionDenied";
        case ErrorCategory::ResourceExhaustion: return "ResourceExhaustion";
        case ErrorCategory::ConnectionLost: return "ConnectionLost";
        case ErrorCategory::Timeout: return "Timeout";
        case ErrorCategory::ExecutionContext: return "ExecutionContext";
        case ErrorCategory::InternalError: return "InternalError";
        case ErrorCategory::Unknown: return "Unknown";
        default: return "Unknown";
    }
}

std::string CommandUtilities::GetSuggestionForError(ErrorCategory category, const std::string& command, HRESULT errorCode) {
    switch (category) {
        case ErrorCategory::CommandSyntax:
            return "Check the command syntax. Use '.help " + command.substr(0, command.find(' ')) + "' for help.";
        
        case ErrorCategory::PermissionDenied:
            return "The command requires elevated privileges. Ensure WinDbg is running as administrator.";
        
        case ErrorCategory::ResourceExhaustion:
            return "The system is low on resources. Close unnecessary applications and try again.";
        
        case ErrorCategory::ConnectionLost:
            return "The connection to the debugger was lost. Try reconnecting to the target.";
        
        case ErrorCategory::Timeout:
            return "The command timed out. Try increasing the timeout or breaking the command into smaller parts.";
        
        case ErrorCategory::ExecutionContext:
            return "The command failed due to execution context. Ensure you are in the correct process/thread context.";
        
        case ErrorCategory::InternalError:
            return "An internal error occurred. Check the debugger state and try again.";
        
        case ErrorCategory::Unknown:
        default:
            return "An unknown error occurred. Check the command syntax and execution context.";
    }
}

TimeoutCategory CommandUtilities::CategorizeCommand(const std::string& command) {
    std::string lowerCommand = command;
    std::transform(lowerCommand.begin(), lowerCommand.end(), lowerCommand.begin(), 
                   [](char c) { return static_cast<char>(std::tolower(static_cast<unsigned char>(c))); });
    
    // Quick commands (usually complete in <5 seconds)
    if (lowerCommand.find("version") == 0 ||
        lowerCommand.find("r") == 0 ||
        lowerCommand.find("k") == 0 ||
        lowerCommand.find("u") == 0 ||
        lowerCommand.find("db") == 0 ||
        lowerCommand.find("dd") == 0 ||
        lowerCommand.find("dw") == 0 ||
        lowerCommand.find("dq") == 0) {
        return TimeoutCategory::Quick;
    }
    
    // Analysis commands (can take 30-60 seconds)
    if (lowerCommand.find("!analyze") == 0 ||
        lowerCommand.find("!pool") == 0 ||
        lowerCommand.find("!heap") == 0 ||
        lowerCommand.find("!handle") == 0) {
        return TimeoutCategory::Analysis;
    }
    
    // Bulk operations (can take several minutes)
    if (lowerCommand.find("!for_each") == 0 ||
        lowerCommand.find("lm") == 0 ||
        lowerCommand.find("!process 0 0") == 0) {
        return TimeoutCategory::Bulk;
    }
    
    // Slow commands (10-30 seconds)
    if (lowerCommand.find("!process") == 0 ||
        lowerCommand.find("!thread") == 0 ||
        lowerCommand.find("!dlls") == 0 ||
        lowerCommand.find("!address") == 0) {
        return TimeoutCategory::Slow;
    }
    
    // Default to normal timeout
    return TimeoutCategory::Normal;
}

unsigned int CommandUtilities::GetTimeoutForCategory(TimeoutCategory category) {
    switch (category) {
        case TimeoutCategory::Quick: return 5000;    // 5 seconds
        case TimeoutCategory::Normal: return 15000;  // 15 seconds
        case TimeoutCategory::Slow: return 30000;    // 30 seconds
        case TimeoutCategory::Analysis: return 60000; // 1 minute
        case TimeoutCategory::Bulk: return 300000;   // 5 minutes
        default: return 15000;
    }
}

std::string CommandUtilities::GetCurrentTimestamp() {
    auto now = std::chrono::system_clock::now();
    auto time_t = std::chrono::system_clock::to_time_t(now);
    
    struct tm tm_info;
    #ifdef _WIN32
    localtime_s(&tm_info, &time_t);
    #else
    localtime_r(&time_t, &tm_info);
    #endif
    
    std::ostringstream oss;
    oss << std::put_time(&tm_info, "%Y-%m-%d %H:%M:%S");
    return oss.str();
}

std::string CommandUtilities::GenerateSessionId() {
    auto now = std::chrono::system_clock::now();
    auto time_t = std::chrono::system_clock::to_time_t(now);
    
    struct tm tm_info;
    #ifdef _WIN32
    localtime_s(&tm_info, &time_t);
    #else
    localtime_r(&time_t, &tm_info);
    #endif
    
    std::ostringstream oss;
    oss << "windbg_session_" << std::put_time(&tm_info, "%Y%m%d_%H%M%S");
    
    // Add random component
    std::random_device rd;
    std::mt19937 gen(rd());
    std::uniform_int_distribution<> dis(1000, 9999);
    oss << "_" << dis(gen);
    
    return oss.str();
}

void CommandUtilities::UpdateGlobalPerformanceMetrics(double execution_time) {
    std::lock_guard<std::mutex> lock(s_staticMembersMutex);
    g_lastCommandTime = std::chrono::steady_clock::now();
    g_lastExecutionTime = execution_time;
}

double CommandUtilities::GetLastExecutionTime() {
    std::lock_guard<std::mutex> lock(s_staticMembersMutex);
    return g_lastExecutionTime;
}

std::string CommandUtilities::GetSessionId() {
    std::lock_guard<std::mutex> lock(s_staticMembersMutex);
    if (g_sessionId.empty()) {
        g_sessionId = GenerateSessionId();
    }
    return g_sessionId;
}

std::chrono::steady_clock::time_point CommandUtilities::GetLastCommandTime() {
    std::lock_guard<std::mutex> lock(s_staticMembersMutex);
    return g_lastCommandTime;
}

std::string CommandUtilities::GetDebuggingMode() {
    try {
        // .effmach answers in one line and does NOT print the banner
        std::string output = ExecuteWinDbgCommand(".effmach", 5000);
        // .effmach reports "Effective machine: x64 (AMD64)" for user mode and
        // "... x64 kernel ..." for kernel mode.
        if (output.find("kernel") != std::string::npos ||
            output.find("Kernel") != std::string::npos) {
            return "Kernel Mode";
        }
        return "User Mode";
        
        if (output.find("kernel") != std::string::npos || output.find("Kernel") != std::string::npos) {
            return "Kernel Mode";
        } else if (output.find("user") != std::string::npos || output.find("User") != std::string::npos) {
            return "User Mode";
        } else if (output.find("dump") != std::string::npos || output.find("Dump") != std::string::npos) {
            return "Dump Analysis";
        } else {
            return "Unknown";
        }
    } catch (...) {
        return "Unknown";
    }
}

std::string CommandUtilities::GetExtensionVersion() {
    // Return the extension version information
    return "WinDbg MCP Extension v1.0.0";
}

std::string CommandUtilities::GetWinDbgVersion() {
    try {
        // Execute version command to get WinDbg version
        std::string output = ExecuteWinDbgCommand(".effmach", 5000);
        
        // Extract version information from output
        std::istringstream iss(output);
        std::string line;
        while (std::getline(iss, line)) {
            if (line.find("Microsoft") != std::string::npos && line.find("Debugging") != std::string::npos) {
                return line;
            }
        }
        
        return "WinDbg (version unknown)";
    } catch (...) {
        return "WinDbg (version unavailable)";
    }
}

CommandResult CommandUtilities::ExecuteWithTimeout(const std::string& command, unsigned int timeoutMs) {
    try {
        auto result = CommandExecutor::ExecuteWithTimeout(command, timeoutMs);
        
        // Convert CommandExecutor::CommandResult to global CommandResult
        CommandResult utilsResult;
        utilsResult.output = result.output;
        utilsResult.hr = result.hr;
        utilsResult.hasTimedOut = result.hasTimedOut;
        utilsResult.executionTime = 0.0;  // CommandExecutor doesn't track this
        
        return utilsResult;
    } catch (const std::exception& e) {
        CommandResult errorResult;
        errorResult.output = std::string("Exception: ") + e.what();
        errorResult.hr = E_FAIL;
        errorResult.hasTimedOut = false;
        errorResult.executionTime = 0.0;
        return errorResult;
    }
}

void CommandUtilities::EnsureSessionId() {
    std::lock_guard<std::mutex> lock(s_staticMembersMutex);
    if (g_sessionId.empty()) {
        g_sessionId = GenerateSessionId();
    }
} 