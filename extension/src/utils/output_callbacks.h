/**
 * @file output_callbacks.h
 * @brief Defines a class to capture WinDbg debugger output.
 */
#pragma once

#include "pch.h"
#include <Windows.h>
#include <DbgEng.h>
#include <string>
#include <mutex>

// Maximum size for command output to prevent excessive memory usage
constexpr size_t MAX_OUTPUT_SIZE = 1024 * 1024; // 1MB

// Maximum size of the FULL (untruncated) console transcript. The per-command
// reply is capped at MAX_OUTPUT_SIZE so a stray `!process 0 0` cannot bloat a
// JSON response, but a debugger session is often needed WHOLE -- e.g. `lm`
// followed by many `dt` calls, or a log you want to read after the fact. The
// console buffer is therefore retained up to this (larger) cap and can be
// fetched on demand with the `get_console_log` handler.
constexpr size_t MAX_CONSOLE_LOG_SIZE = 16 * 1024 * 1024; // 16MB

/**
 * @class OutputCallbacks
 * @brief Implements IDebugOutputCallbacks to capture debugger output.
 * 
 * This class is used to intercept and store output from WinDbg commands.
 */
class OutputCallbacks : public IDebugOutputCallbacks {
public:
    OutputCallbacks();
    virtual ~OutputCallbacks();

    // IUnknown methods
    STDMETHOD(QueryInterface)(
        __in REFIID InterfaceId,
        __out PVOID* Interface
    );
    STDMETHOD_(ULONG, AddRef)();
    STDMETHOD_(ULONG, Release)();

    // IDebugOutputCallbacks methods
    STDMETHOD(Output)(
        __in ULONG Mask,
        __in PCSTR Text
    );

    /**
     * @brief Get the captured output for the CURRENT command.
     * @return A string containing the captured output, truncated to
     *         MAX_OUTPUT_SIZE.
     */
    [[nodiscard]] std::string GetOutput() const;

    /**
     * @brief Reset the per-command output buffer.
     *
     * Called before every execute. It clears ONLY m_output -- the accumulated
     * console transcript is deliberately left alone, otherwise the "log" would
     * hold exactly one command (the reply) instead of a session transcript.
     * It also records where this command's transcript begins, which is what
     * GetLastCommandLog() slices out.
     */
    void Clear();

    /**
     * @brief Get the FULL captured console transcript, untruncated.
     *
     * Unlike GetOutput(), which is scoped to the command that just ran (the
     * buffer is cleared before each execute), this returns the accumulated log.
     *
     * @param maxBytes Cap on the returned size; 0 means MAX_CONSOLE_LOG_SIZE.
     * @param truncated Set to true when the cap was hit (the tail is kept,
     *        because the end of a debugger session is what you usually want).
     * @return The console transcript.
     */
    [[nodiscard]] std::string GetConsoleLog(size_t maxBytes = 0, bool* truncated = nullptr) const;

    /**
     * @brief Get the console transcript written by the MOST RECENT command.
     *
     * GetConsoleLog() returns the whole session; the per-command reply is
     * capped. This is the middle ground: the transcript slice belonging to the
     * last `execute_command`, uncapped -- i.e. "this command's full output".
     *
     * @param maxBytes Cap on the returned size; 0 means MAX_CONSOLE_LOG_SIZE.
     * @param truncated Set to true when the cap was hit.
     * @return The last command's transcript, including its echoed input line.
     */
    [[nodiscard]] std::string GetLastCommandLog(size_t maxBytes = 0, bool* truncated = nullptr) const;

private:
    // m_output is written by dbgeng's output-callback thread and read by the
    // MCP command thread. Without this mutex the two race, and a read that lands
    // mid-write returns an empty or truncated string -- which surfaces as
    // "command returned no output" (reported to the client as the useless
    // "Unknown error") for perfectly valid commands like `lm`.
    mutable std::mutex m_outputMutex;
    std::string m_output;        ///< Buffer storing output of the current command
    std::string m_consoleLog;    ///< Accumulated transcript, NOT cleared per command
    size_t m_lastCommandLogStart{0}; ///< Offset in m_consoleLog where the last command began
    size_t m_consoleLogDropped{0}; ///< Bytes discarded to stay within the cap
    LONG m_refCount;             ///< Reference count for COM interface
    bool m_extensionError{false}; ///< Flag indicating if an extension error occurred
    bool m_exportError{false};    ///< Flag indicating if an export error occurred
}; 