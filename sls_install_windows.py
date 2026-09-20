"""Small Windows API boundary; installer tests mock these side effects."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import base64
import json
import subprocess
from pathlib import Path
import time

TRUSTED_WRITERS = {
    "S-1-5-18", "S-1-5-32-544",
    "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464",  # TrustedInstaller
}
# Write/add/delete contents, delete object, change permissions/owner, generic write/all.
WRITE_ACCESS = 0x00000156 | 0x000D0000 | 0x50000000


def executable_identity(path: Path) -> dict[str, str]:
    """Read PE version resources without executing legacy application code."""
    version = ctypes.WinDLL("version", use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    version.GetFileVersionInfoW.restype = wintypes.BOOL
    version.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    version.VerQueryValueW.restype = wintypes.BOOL
    unused = wintypes.DWORD()
    size = version.GetFileVersionInfoSizeW(str(path), ctypes.byref(unused))
    if not size or size > 1024 * 1024:
        raise ValueError(f"Missing or oversized executable version resources: {path}")
    data = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, data):
        raise ctypes.WinError(ctypes.get_last_error())
    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    if not version.VerQueryValueW(data, r"\VarFileInfo\Translation", ctypes.byref(pointer), ctypes.byref(length)) or length.value < 4:
        raise ValueError(f"Missing executable translation resources: {path}")
    language, codepage = (wintypes.WORD * 2).from_address(pointer.value)
    fields = {}
    for name in ("ProductName", "CompanyName", "FileVersion", "OriginalFilename"):
        key = f"\\StringFileInfo\\{language:04x}{codepage:04x}\\{name}"
        if not version.VerQueryValueW(data, key, ctypes.byref(pointer), ctypes.byref(length)) or not length.value:
            raise ValueError(f"Missing executable identity {name}: {path}")
        fields[name] = ctypes.wstring_at(pointer, length.value).rstrip("\x00")
    return fields


def shortcut_identity(path: Path) -> dict:
    """Read a shell link using Windows COM; never execute its target."""
    encoded_path = base64.b64encode(str(path).encode("utf-8")).decode("ascii")
    script = ("$ErrorActionPreference='Stop'; "
              "$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + encoded_path + "')); "
              "$s=New-Object -ComObject WScript.Shell; $l=$s.CreateShortcut($p); "
              "@{target=$l.TargetPath;arguments=$l.Arguments}|ConvertTo-Json -Compress")
    command = system_directory() / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    result = subprocess.run([str(command), "-NoProfile", "-NonInteractive", "-EncodedCommand",
                             base64.b64encode(script.encode("utf-16le")).decode("ascii")],
                            check=True, capture_output=True, text=True, timeout=20,
                            cwd=str(system_directory()), creationflags=subprocess.CREATE_NO_WINDOW)
    return json.loads(result.stdout)


def system_directory() -> Path:
    if os.name != "nt":
        raise OSError("Windows system directory is unavailable.")
    output = ctypes.create_unicode_buffer(32768)
    if not ctypes.windll.kernel32.GetSystemDirectoryW(output, len(output)):
        raise ctypes.WinError()
    return Path(output.value)


def program_files_directory() -> Path:
    """Use the shell API, not a caller-controlled ProgramFiles environment value."""
    output = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 0x26, None, 0, output) != 0:
        raise OSError("Windows Program Files folder could not be resolved.")
    return Path(output.value)


def sid_string(sid) -> str:
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    convert = advapi.ConvertSidToStringSidW
    convert.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    convert.restype = wintypes.BOOL
    output = wintypes.LPWSTR()
    if not convert(sid, ctypes.byref(output)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return output.value
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(output, ctypes.c_void_p))


def verify_protected_directory(path: Path, *, allow_shell_delete: bool = False) -> None:
    """Reject any effective allow ACE granting untrusted principals write access.

    This conservative check does not attempt to subtract deny ACEs. An ambiguous
    or unsupported writable ACL fails closed rather than proving safety wrongly.
    """
    class ACL(ctypes.Structure):
        _fields_ = [("revision", ctypes.c_ubyte), ("reserved", ctypes.c_ubyte),
                    ("size", wintypes.WORD), ("count", wintypes.WORD), ("reserved2", wintypes.WORD)]

    class ACE_HEADER(ctypes.Structure):
        _fields_ = [("kind", ctypes.c_ubyte), ("flags", ctypes.c_ubyte), ("size", wintypes.WORD)]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    read = advapi.GetNamedSecurityInfoW
    read.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
                    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                    ctypes.POINTER(ctypes.c_void_p)]
    read.restype = wintypes.DWORD
    owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    result = read(str(path), 1, 0x1 | 0x4, ctypes.byref(owner), None,
                  ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if result:
        raise ctypes.WinError(result)
    try:
        if sid_string(owner) not in TRUSTED_WRITERS:
            raise PermissionError(f"Installation directory has an untrusted owner: {path}")
        if not dacl.value:
            raise PermissionError(f"Installation directory permits unrestricted writes: {path}")
        info = ctypes.cast(dacl, ctypes.POINTER(ACL)).contents
        get_ace = advapi.GetAce
        get_ace.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
        get_ace.restype = wintypes.BOOL
        for index in range(info.count):
            pointer = ctypes.c_void_p()
            if not get_ace(dacl, index, ctypes.byref(pointer)):
                raise ctypes.WinError(ctypes.get_last_error())
            header = ctypes.cast(pointer, ctypes.POINTER(ACE_HEADER)).contents
            if header.flags & 0x08 or header.kind == 1:  # Inherit-only or deny ACE.
                continue
            if header.kind != 0:
                raise PermissionError(f"Unsupported installation directory ACL: {path}")
            mask = wintypes.DWORD.from_address(pointer.value + 4).value
            # Windows lets users remove shared Start Menu shortcuts. Deletion
            # alone does not let them write link contents or create replacements.
            # Code/runtime locations always retain the stricter default policy.
            writes = WRITE_ACCESS & ~0x10040 if allow_shell_delete else WRITE_ACCESS
            if mask & writes and sid_string(ctypes.c_void_p(pointer.value + 8)) not in TRUSTED_WRITERS:
                raise PermissionError(f"Installation directory is writable by an untrusted principal: {path}")
    finally:
        ctypes.windll.kernel32.LocalFree(descriptor)


def protect_new_directory(path: Path) -> None:
    """Set explicit machine ACLs on an installer-created, previously empty folder.

    Python's mkdtemp uses a private user ACL on Windows; that is unsuitable for
    a machine installer staging code which will execute with elevated rights.
    """
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
                        ctypes.POINTER(wintypes.DWORD)]
    convert.restype = wintypes.BOOL
    descriptor = ctypes.c_void_p()
    sddl = "O:BAG:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1200a9;;;BU)"
    if not convert(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        apply = advapi.SetFileSecurityW
        apply.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
        apply.restype = wintypes.BOOL
        if not apply(str(path), 0x80000007, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        ctypes.windll.kernel32.LocalFree(descriptor)
    verify_protected_directory(path)


def normalized_image_path(path: Path | str) -> str:
    """Expand Windows 8.3 names without following user-controlled junctions."""
    raw = os.path.abspath(str(path))
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetLongPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetLongPathNameW.restype = wintypes.DWORD
    output = ctypes.create_unicode_buffer(32768)
    size = kernel.GetLongPathNameW(raw, output, len(output))
    return os.path.normcase(output.value if 0 < size < len(output) else raw)


def exact_processes(executable: Path) -> list[int]:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    processes = (wintypes.DWORD * 65536)()
    used = wintypes.DWORD()
    if not psapi.EnumProcesses(processes, ctypes.sizeof(processes), ctypes.byref(used)):
        raise ctypes.WinError(ctypes.get_last_error())
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    matched = []
    expected = normalized_image_path(executable)
    for pid in processes[:used.value // ctypes.sizeof(wintypes.DWORD)]:
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            continue
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(buffer))
            if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                if normalized_image_path(buffer.value) == expected:
                    matched.append(int(pid))
        finally:
            kernel.CloseHandle(handle)
    return matched


def request_window_close(pids: set[int]) -> None:
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def close_window(window, _):
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(window, ctypes.byref(pid))
        if pid.value in pids:
            user.PostMessageW(window, 0x0010, 0, 0)  # WM_CLOSE; no forced termination.
        return True

    user.EnumWindows(close_window, 0)


def process_parents() -> dict[int, int]:
    """Parent IDs determine shutdown order only, never authorization to kill."""
    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                    ("pid", wintypes.DWORD), ("heap", ctypes.c_size_t),
                    ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                    ("parent", wintypes.DWORD), ("priority", wintypes.LONG),
                    ("flags", wintypes.DWORD), ("name", wintypes.WCHAR * 260)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = kernel.Process32FirstW.argtypes
    kernel.Process32NextW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(0x2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = PROCESSENTRY32()
        entry.size = ctypes.sizeof(entry)
        found = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        parents = {}
        while found:
            parents[int(entry.pid)] = int(entry.parent)
            found = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
            raise ctypes.WinError(ctypes.get_last_error())
        return parents
    finally:
        kernel.CloseHandle(snapshot)


def children_first(pids, parents: dict[int, int]) -> list[int]:
    candidates = set(pids)
    def depth(pid):
        seen = set()
        while pid in candidates and pid not in seen:
            seen.add(pid)
            pid = parents.get(pid)
        return len(seen)
    return sorted(candidates, key=lambda pid: (-depth(pid), pid))


def terminate_exact_process(pid: int, executable: Path) -> bool:
    """Hold a handle and re-check its image before terminating that exact process.

    No name-based taskkill, process-tree kill, PID-only kill or inherited
    parent relationship can authorize terminating a different executable.
    """
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                               wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000 | 0x100000 | 0x1, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # Process already exited.
            return False
        raise PermissionError(f"Windows denied access to stop app process {pid}: {ctypes.WinError(error)}")
    try:
        if kernel.WaitForSingleObject(handle, 0) == 0:
            return False
        image = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(image))
        if not kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(length)):
            if kernel.WaitForSingleObject(handle, 0) == 0:
                return False
            raise ctypes.WinError(ctypes.get_last_error())
        if normalized_image_path(image.value) != normalized_image_path(executable):
            return False  # PID was reused, or the image is from another location.
        if not kernel.TerminateProcess(handle, 0):
            if kernel.WaitForSingleObject(handle, 0) == 0:
                return False
            raise ctypes.WinError(ctypes.get_last_error())
        if kernel.WaitForSingleObject(handle, 5000) != 0:
            raise RuntimeError(f"Windows has not released app process {pid} after shutdown.")
        return True
    finally:
        kernel.CloseHandle(handle)


def close_exact_processes(executable: Path, *, timeout: float = 8,
                          force: bool = False, progress=None) -> None:
    executable = executable.resolve()
    pids = set(exact_processes(executable))
    if not pids:
        return
    report = progress or (lambda _message: None)
    report(f"Closing the installed app (processes {', '.join(map(str, sorted(pids)))}).")
    request_window_close(pids)
    deadline = time.monotonic() + timeout
    while True:
        remaining = set(exact_processes(executable))
        if not remaining:
            report("The installed app has stopped.")
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(0.2)
    if not force:
        raise RuntimeError(f"The app did not exit: {executable}; process IDs {sorted(remaining)}.")
    report("The app did not finish closing. Ending the remaining processes from this installation.")
    for pid in children_first(remaining, process_parents()):
        terminate_exact_process(pid, executable)
        # A onefile launcher normally exits and cleans its own extraction after
        # the child ends. Give it that opportunity before considering its PID.
        time.sleep(0.3)
    deadline = time.monotonic() + 5
    while True:
        remaining = exact_processes(executable)
        if not remaining:
            report("The installed app has stopped; continuing setup.")
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f"The app restarted or could not be stopped: {executable}; process IDs {remaining}. "
                               "Check any service or task that automatically restarts it.")
        time.sleep(0.2)


def delete_after_reboot(path: Path) -> None:
    move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    move.restype = wintypes.BOOL
    if not move(str(path), None, 0x4):
        raise ctypes.WinError(ctypes.get_last_error())
