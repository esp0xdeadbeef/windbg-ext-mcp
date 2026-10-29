/**
 * @file types.cpp
 * @brief Implementation of the WinDbg extension initialization and command handlers.
 */
#include "pch.h"
#include <Windows.h>
#define KDEXT_64BIT
#include <DbgEng.h>
#include <WDBGEXTS.H>
#include <atlcomcli.h>
#include <memory>
#include <atomic>
#include "ipc/mcp_server.h"
#include "command/command_handlers.h"
#include "utils/constants.h"

#pragma comment(lib, "dbgeng")

// Define the ExtensionApis object here
WINDBG_EXTENSION_APIS64 ExtensionApis{ sizeof(ExtensionApis) };

// Global MCP server instance
std::unique_ptr<MCPServer> g_mcpServer;

// Flag to track if the DLL is being unloaded
std::atomic<bool> g_dllUnloading(false);

// Event to signal clean shutdown
HANDLE g_shutdownEvent = nullptr;

/**
 * @brief Initialize the debug extension.
 * 
 * This function is called when the extension is loaded by WinDbg.
 * It initializes the MCP server and registers command handlers.
 * 
 * @param version Pointer to a version number to be set.
 * @param flags Pointer to flags to be set.
 * @return HRESULT indicating success or failure.
 */
#include <cstdio>  // _snprintf_s

// --- per-instance pipe name resolution ---
// Priority: WINDBGMCP_PIPE env var > "\\.\pipe\windbgmcp.<pid>".
static std::string ResolveMcpPipeName() {
	char buf[512] = {0};
	DWORD n = GetEnvironmentVariableA("WINDBGMCP_PIPE", buf, sizeof(buf));
	if (n > 0 && n < sizeof(buf)) {
		std::string want(buf, n);
		// Callers may pass a BARE name ("windbgmcp.kdbg.target01"). CreateNamedPipeA
		// needs the object-manager path, and without the prefix it fails with
		// error 123 ERROR_INVALID_NAME, so the pipe is never created even though
		// the extension prints "MCP server started on pipe: ...".
		if (want.rfind("\\", 0) != 0 && want.rfind("//", 0) != 0) {
			if (want.rfind("pipe\\", 0) == 0) want = "\\\\.\\" + want;
			else                                want = "\\\\.\\pipe\\" + want;
		}
		return want;
	}
	char name[160];
	_snprintf_s(name, sizeof(name), _TRUNCATE,
	            "\\\\.\\pipe\\windbgmcp.%lu", (unsigned long)GetCurrentProcessId());
	return std::string(name);
}

// Record the pipe name to a file so the Python bridge can discover it
// deterministically (named-pipe enumeration via Python is unreliable).
static void WriteMcpPipeRecord(const std::string& pipeName) {
	// 1) WinDbgX package LocalState (writable by the WinDbgX AppContainer)
	char base[MAX_PATH] = {0};
	DWORD n = GetEnvironmentVariableA("LOCALAPPDATA", base, sizeof(base));
	if (n > 0 && n < sizeof(base)) {
		char dir[MAX_PATH];
		_snprintf_s(dir, sizeof(dir), _TRUNCATE,
		            "%s\\Packages\\Microsoft.WinDbg_8wekyb3d8bbwe\\LocalState", base);
		CreateDirectoryA(dir, nullptr);
		char path[MAX_PATH];
		_snprintf_s(path, sizeof(path), _TRUNCATE, "%s\\mcp.%lu.pipe",
		            dir, (unsigned long)GetCurrentProcessId());
		HANDLE h = CreateFileA(path, GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
		if (h != INVALID_HANDLE_VALUE) {
			DWORD w = 0; DWORD m = (DWORD)pipeName.size();
			WriteFile(h, pipeName.c_str(), m, &w, nullptr);
			CloseHandle(h);
		}
	}
	// 2) C:\ProgramData\wbmcp (also writable by the Edge-renderer AppContainer)
	CreateDirectoryA("C:\\ProgramData\\wbmcp", nullptr);
	char ppath[MAX_PATH];
	_snprintf_s(ppath, sizeof(ppath), _TRUNCATE,
	            "C:\\ProgramData\\wbmcp\\mcp.%lu.pipe", (unsigned long)GetCurrentProcessId());
	HANDLE h2 = CreateFileA(ppath, GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
	if (h2 != INVALID_HANDLE_VALUE) {
		DWORD w = 0; DWORD m = (DWORD)pipeName.size();
		WriteFile(h2, pipeName.c_str(), m, &w, nullptr);
		CloseHandle(h2);
	}
}

HRESULT __stdcall DebugExtensionInitialize(PULONG version, PULONG flags) {
	if (!version || !flags) {
		return E_POINTER;
	}

	CComPtr<IDebugClient> client;
	HRESULT hr = DebugCreate(__uuidof(IDebugClient), (void**)&client);
	if (FAILED(hr)) {
		return hr;
	}

	CComQIPtr<IDebugControl> control(client);
	hr = control->GetWindbgExtensionApis64(&ExtensionApis);
	if (FAILED(hr)) {
		return hr;
	}

	*version = DEBUG_EXTENSION_VERSION(1, 0);
	*flags = 0;

	// Create shutdown event for internal synchronization
	g_shutdownEvent = CreateEvent(nullptr, TRUE, FALSE, nullptr);
	if (g_shutdownEvent == nullptr) {
		dprintf("Warning: Failed to create shutdown event\n");
	}

	// Initialize MCP server
	g_mcpServer = std::make_unique<MCPServer>();
	
	// Register command handlers
	CommandHandlers::RegisterHandlers(*g_mcpServer);
	
	// Start the server
	std::string pipeName = ResolveMcpPipeName();
	if (!g_mcpServer->Start(pipeName)) {
		dprintf("Failed to start MCP server\n");
	} else {
		dprintf("MCP server started on pipe: %s\n", pipeName.c_str());
		WriteMcpPipeRecord(pipeName);
	}

	return S_OK;
}

/**
 * @brief Display help for the extension commands.
 * 
 * @param client Pointer to the debug client.
 * @param args Command arguments.
 * @return HRESULT indicating success or failure.
 */
STDAPI help(IDebugClient* client, PCSTR args) {
	dprintf("WinDBG MCP Extension Help\n");
	dprintf("  help - show this help\n");
	dprintf("  hello - display a test message\n");
	dprintf("  objecttypes [name] - display object types filtered by 'name'\n");
	dprintf("  mcpstart - start the MCP server if not already running\n");
	dprintf("  mcpstop - stop the MCP server if running\n");
	dprintf("  mcpstatus - show MCP server status\n");

	return S_OK;
}

/**
 * @brief Display kernel object types.
 * 
 * @param client Pointer to the debug client.
 * @param args Command arguments.
 * @return HRESULT indicating success or failure.
 */
STDAPI objecttypes(IDebugClient* client, PCSTR args) {
	auto types = GetExpression("nt!ObpObjectTypes");
	if (types == 0) {
		dprintf("Failed to locate nt!ObpObjectTypes\n");
		return E_UNEXPECTED;
	}

	int count = 0;
	ULONG nameOffset;
	GetFieldOffset("nt!_OBJECT_TYPE", "Name", &nameOffset);

	while (types) {
		ULONG64 type;
		if (!ReadPointer(types, &type)) {
			break;
		}

		if (type == 0) {
			break;
		}

		ULONG totalObjects, totalHandles;
		ULONG peakObjects, peakHandles;
		UCHAR index;
		GetFieldValue(type, "nt!_OBJECT_TYPE", "Index", index);
		GetFieldValue(type, "nt!_OBJECT_TYPE", "TotalNumberOfObjects", totalObjects);
		GetFieldValue(type, "nt!_OBJECT_TYPE", "TotalNumberOfHandles", totalHandles);
		GetFieldValue(type, "nt!_OBJECT_TYPE", "HighWaterNumberOfObjects", peakObjects);
		GetFieldValue(type, "nt!_OBJECT_TYPE", "HighWaterNumberOfHandles", peakHandles);

		CComQIPtr<IDebugControl> control(client);

		control->ControlledOutput(DEBUG_OUTCTL_DML, DEBUG_OUTPUT_NORMAL,
			"%-33msu <link cmd=\"dt nt!_OBJECT_TYPE %p\">0x%p</link> %3d %8u %8u %8u %8u\n",
			type + nameOffset, type, type, index, totalObjects, totalHandles, peakObjects, peakHandles);

		types += sizeof(void*);
		count++;
	}
	dprintf("Total objects: %d\n", count);

	return S_OK;
}

/**
 * @brief Start the MCP server.
 * 
 * @param client Pointer to the debug client.
 * @param args Command arguments.
 * @return HRESULT indicating success or failure.
 */
STDAPI mcpstart(IDebugClient* client, PCSTR args) {
	if (!g_mcpServer) {
		g_mcpServer = std::make_unique<MCPServer>();
		CommandHandlers::RegisterHandlers(*g_mcpServer);
	}
	
	if (g_mcpServer->IsRunning()) {
		dprintf("MCP server is already running\n");
		return S_OK;
	}
	
	std::string pipeName = ResolveMcpPipeName();
	if (args && *args) { const char* a = args; while (*a == ' ') ++a; if (*a) pipeName = std::string(a); }
	if (!g_mcpServer->Start(pipeName)) {
		dprintf("Failed to start MCP server\n");
		return E_FAIL;
	}
	
	dprintf("MCP server started on pipe: %s\n", pipeName.c_str());
	WriteMcpPipeRecord(pipeName);
	return S_OK;
}

/**
 * @brief Stop the MCP server.
 * 
 * @param client Pointer to the debug client.
 * @param args Command arguments.
 * @return HRESULT indicating success or failure.
 */
STDAPI mcpstop(IDebugClient* client, PCSTR args) {
	if (!g_mcpServer || !g_mcpServer->IsRunning()) {
		dprintf("MCP server is not running\n");
		return S_OK;
	}
	
	dprintf("Stopping MCP server...\n");
	g_mcpServer->Stop();
	dprintf("MCP server stopped\n");
	return S_OK;
}

/**
 * @brief Display the MCP server status.
 * 
 * @param client Pointer to the debug client.
 * @param args Command arguments.
 * @return HRESULT indicating success or failure.
 */
STDAPI mcpstatus(IDebugClient* client, PCSTR args) {
	if (!g_mcpServer) {
		dprintf("MCP server has not been initialized\n");
		return S_OK;
	}
	
	if (g_mcpServer->IsRunning()) {
		dprintf("MCP server is running on pipe: %s\n", Constants::DEFAULT_PIPE_NAME);
	} else {
		dprintf("MCP server is not running\n");
	}

	return S_OK;
}

/**
 * @brief Uninitialize the debug extension.
 * 
 * This function is called when the extension is unloaded by WinDbg.
 * It cleans up resources used by the extension.
 */
extern "C"
void CALLBACK DebugExtensionUninitialize(void) {
	// Set flag to indicate unloading
	g_dllUnloading.store(true);
	
	// Wait for a short time to let other threads complete operations
	const DWORD CLEANUP_TIMEOUT_MS = 5000; // 5 seconds
	dprintf("WinDbg MCP Extension: Uninitializing...\n");
	
	// Stop the MCP server if it's running
	if (g_mcpServer && g_mcpServer->IsRunning()) {
		dprintf("Stopping MCP server...\n");
		g_mcpServer->Stop();
	}
	
	// Free the MCP server
	g_mcpServer.reset();
	
	// Signal the shutdown event and clean up
	if (g_shutdownEvent != nullptr) {
		SetEvent(g_shutdownEvent);
		
		// Wait for potential cleanup operations to complete
		WaitForSingleObject(g_shutdownEvent, CLEANUP_TIMEOUT_MS);
		
		CloseHandle(g_shutdownEvent);
		g_shutdownEvent = nullptr;
	}
	
	dprintf("WinDbg MCP Extension: Uninitialized\n");
}

/**
 * @brief Test function to verify the extension is working.
 */
extern "C"
void CALLBACK hello(_In_ HANDLE, _In_ HANDLE, _In_ ULONG, _In_ PCSTR /*args*/) {
	dprintf("Hello from WinDbg MCP Extension!\n");
}


