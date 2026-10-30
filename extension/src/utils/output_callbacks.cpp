/**
 * @file output_callbacks.cpp
 * @brief Implementation of OutputCallbacks class for capturing WinDbg output.
 */
#include "pch.h"
#include "utils/output_callbacks.h"
#include <algorithm>

OutputCallbacks::OutputCallbacks() : m_refCount(1) {
}

OutputCallbacks::~OutputCallbacks() = default;

// IUnknown methods
STDMETHODIMP OutputCallbacks::QueryInterface(
    __in REFIID InterfaceId,
    __out PVOID* Interface
) {
    if (!Interface) {
        return E_POINTER;
    }

    *Interface = nullptr;

    if (IsEqualIID(InterfaceId, __uuidof(IUnknown)) ||
        IsEqualIID(InterfaceId, __uuidof(IDebugOutputCallbacks))) {
        *Interface = this;
        AddRef();
        return S_OK;
    }

    return E_NOINTERFACE;
}

STDMETHODIMP_(ULONG) OutputCallbacks::AddRef() {
    return InterlockedIncrement(&m_refCount);
}

STDMETHODIMP_(ULONG) OutputCallbacks::Release() {
    LONG retVal = InterlockedDecrement(&m_refCount);
    if (retVal == 0) {
        delete this;
    }
    return retVal;
}

// IDebugOutputCallbacks methods
STDMETHODIMP OutputCallbacks::Output(
    __in ULONG Mask,
    __in PCSTR Text
) {
    // Append the output text to our buffer
    if (!Text) {
        return S_OK;
    }

    const std::string textStr(Text);

    // Serialise against GetOutput()/Clear(): this runs on dbgeng's callback
    // thread while those run on the MCP command thread.
    std::lock_guard<std::mutex> lock(m_outputMutex);

    // ONE append point for both buffers.
    //
    // Every branch used to write m_output += ... and m_consoleLog += ... by
    // hand, in parallel. That is two chances per branch to update one buffer and
    // not the other, and the failure would be silent: the reply would look right
    // and the transcript would quietly be missing lines. So all branches below
    // only COMPUTE the text, and this lambda is the only thing that stores it.
    auto emit = [this](const std::string& text) {
        m_output += text;

        // Accumulate the transcript, then trim its OLDEST bytes if it is over
        // the cap. Keeping the tail is deliberate: the most recent output is
        // what a caller wants from a long session. The trim is by a large chunk
        // rather than per-append so this stays O(1) amortised.
        m_consoleLog += text;
        if (m_consoleLog.size() > MAX_CONSOLE_LOG_SIZE) {
            const size_t keep = MAX_CONSOLE_LOG_SIZE - (MAX_CONSOLE_LOG_SIZE / 8);
            const size_t drop = m_consoleLog.size() - keep;
            m_consoleLogDropped += drop;
            m_consoleLog.erase(0, drop);
            // Dropping bytes shifts every offset in the log. If the start of the
            // last command fell inside the dropped range its slice is gone, so
            // clamp to 0 and report what survives -- an out-of-range offset
            // would make GetLastCommandLog() return the whole log instead.
            m_lastCommandLogStart = (m_lastCommandLogStart >= drop)
                                        ? (m_lastCommandLogStart - drop) : 0;
        }
    };

    // Check for various types of messages
    if (textStr.find("WARNING: .cache forcedecodeuser is not enabled") != std::string::npos) {
        // This is a common warning, not a fatal error - log but continue
        emit("Note: " + textStr + "\n");
    }
    else if (textStr.find("is not extension gallery command") != std::string::npos) {
        // Extract the command name
        const size_t pos = textStr.find(" is not extension gallery command");
        if (pos != std::string::npos) {
            const std::string cmdName = textStr.substr(0, pos);
            if (!m_extensionError) {
                // Provide a more helpful error message
                if (cmdName == "modinfo") {
                    emit("Note: The !modinfo command is not available. Using alternative lmv command instead.\n");
                } else {
                    emit("Error: Command '" + cmdName + "' is not available. Make sure the required extension is loaded.\n");
                }
                m_extensionError = true;
            }
        }
        else {
            emit(textStr);
        }
    }
    else if (textStr.find("No export") != std::string::npos && textStr.find("found") != std::string::npos) {
        // Handle "No export" errors
        const size_t pos = textStr.find(" found");
        if (pos != std::string::npos) {
            const std::string cmdName = textStr.substr(9, pos - 9);  // Extract name after "No export "
            if (!m_exportError) {
                emit("Note: Command '" + cmdName + "' is not available in the current debugging context.\n");
                m_exportError = true;
            }
        }
        else {
            emit(textStr);
        }
    }
    else {
        emit(textStr);
    }
    
    return S_OK;
}

std::string OutputCallbacks::GetOutput() const {
    // Take the lock: dbgeng may still be appending from its callback thread.
    std::lock_guard<std::mutex> lock(m_outputMutex);
    // If the output exceeds a reasonable size, truncate it and add a note
    if (m_output.size() > MAX_OUTPUT_SIZE) {
        std::string truncated = m_output.substr(0, MAX_OUTPUT_SIZE);
        truncated += "\n[Output truncated. Result too large (exceeded " + 
                     std::to_string(MAX_OUTPUT_SIZE) + " bytes)]";
        return truncated;
    }
    return m_output;
}

std::string OutputCallbacks::GetConsoleLog(size_t maxBytes, bool* truncated) const {
    std::lock_guard<std::mutex> lock(m_outputMutex);
    const size_t cap = (maxBytes == 0 || maxBytes > MAX_CONSOLE_LOG_SIZE)
                           ? MAX_CONSOLE_LOG_SIZE : maxBytes;
    bool cut = false;
    std::string out;
    if (m_consoleLog.size() > cap) {
        // Keep the TAIL: the last thing the debugger printed is the interesting
        // part when a session has been running for a while.
        out = m_consoleLog.substr(m_consoleLog.size() - cap);
        cut = true;
    } else {
        out = m_consoleLog;
    }
    if (truncated) *truncated = cut;
    if (cut || m_consoleLogDropped > 0) {
        std::string note = "\n[console log truncated at " + std::to_string(cap) + " bytes";
        if (m_consoleLogDropped > 0) {
            note += "; " + std::to_string(m_consoleLogDropped) +
                    " earlier bytes were dropped to stay within the " +
                    std::to_string(MAX_CONSOLE_LOG_SIZE) + " byte cap";
        }
        note += "]\n";
        out += note;
    }
    return out;
}

std::string OutputCallbacks::GetLastCommandLog(size_t maxBytes, bool* truncated) const {
    std::lock_guard<std::mutex> lock(m_outputMutex);
    // m_lastCommandLogStart is maintained by Clear(): it is stamped with the
    // current log length at the moment the per-command buffer is reset, i.e.
    // after the previous command finished and before this one emits anything.
    // LogAppend... (see Output()'s emit lambda) keeps it in range when a trim
    // drops bytes underneath it, but clamp here too so a stale value can never
    // make substr() throw.
    const size_t start = (m_lastCommandLogStart <= m_consoleLog.size())
                             ? m_lastCommandLogStart : m_consoleLog.size();
    std::string out = m_consoleLog.substr(start);
    const size_t cap = (maxBytes == 0 || maxBytes > MAX_CONSOLE_LOG_SIZE)
                           ? MAX_CONSOLE_LOG_SIZE : maxBytes;
    bool cut = false;
    if (out.size() > cap) {
        out = out.substr(0, cap);   // head: this is one command's output, which
                                    // reads front-to-back, unlike a transcript
        cut = true;
    }
    if (truncated) *truncated = cut;
    if (cut) {
        out += "\n[output truncated at " + std::to_string(cap) + " bytes]\n";
    }
    return out;
}

void OutputCallbacks::Clear() {
    std::lock_guard<std::mutex> lock(m_outputMutex);
    m_output.clear();
    // NOTE: m_consoleLog is deliberately NOT cleared here. Clear() runs before
    // every command, so clearing the log here would make it hold exactly one
    // command -- which is the reply, not a transcript.
    //
    // What IS recorded is where this command's transcript begins, so the
    // uncapped "output of the last command" can be sliced out of the log.
    m_lastCommandLogStart = m_consoleLog.size();
    m_extensionError = false;
    m_exportError = false;
} 