"""Windows command protection: run a command at Low integrity, inside a job object.

    python -m xharness.winlow --cwd <dir> -- <program> [args...]

What it does, with the APIs Windows already has (no driver, no admin rights):

- Starts the command with a restricted copy of the caller's token: every
  privilege but SeChangeNotify removed, Administrators made deny-only, and the
  integrity level lowered to Low. A Low process cannot write to anything at
  Medium or above -- the person's documents, desktop, settings, most of the
  registry -- however its ACLs read.
- Labels the working directory Low (inherited by what is created in it), so the
  command can write there and nowhere else; TEMP and TMP point at a Low folder
  under %LOCALAPPDATA%\\Temp\\Low.
- Puts the process in a job object that kills the whole tree when the launcher
  goes (a timeout in the bash tool kills the launcher).

What it does not do: cut the network. Windows has no per-process switch for
that without AppContainer and firewall rules, so `allow_network = false` is
refused on Windows rather than silently ignored.

The launcher keeps the command's exit status and passes its standard handles
straight through.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404 - icacls to label the working directory
import sys

LOW_SID = "S-1-16-4096"
ADMINISTRATORS_SID = "S-1-5-32-544"


def launcher_argv(cwd: str) -> list[str]:
    """How to start this launcher: the packaged app runs itself (it has no separate Python)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--sandbox-launch", "--cwd", cwd, "--"]
    return [sys.executable, "-m", "xharness.winlow", "--cwd", cwd, "--"]


def label_low(path: str) -> None:
    """Mark `path` Low integrity, inherited by files and folders made in it."""
    icacls = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "icacls.exe")
    done = subprocess.run([icacls, path, "/setintegritylevel", "(OI)(CI)low", "/Q"],  # nosec B603 - fixed argv
                          capture_output=True, timeout=30)
    if done.returncode != 0:
        raise OSError(f"cannot label {path} Low: {done.stderr.decode('utf-8', 'replace').strip() or done.returncode}")


def low_temp() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    path = os.path.join(base, "Temp", "Low", "xharness")
    os.makedirs(path, exist_ok=True)
    label_low(path)
    return path


def run(argv: list[str], cwd: str) -> int:  # pragma: no cover - Windows only, exercised by tests on Windows
    import ctypes
    from ctypes import wintypes as w

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def check(ok: int, what: str) -> None:
        if not ok:
            raise OSError(f"{what} failed: {ctypes.WinError(ctypes.get_last_error())}")

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", w.DWORD)]

    class TOKEN_MANDATORY_LABEL(ctypes.Structure):
        _fields_ = [("Label", SID_AND_ATTRIBUTES)]

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [("cb", w.DWORD), ("lpReserved", w.LPWSTR), ("lpDesktop", w.LPWSTR), ("lpTitle", w.LPWSTR),
                    ("dwX", w.DWORD), ("dwY", w.DWORD), ("dwXSize", w.DWORD), ("dwYSize", w.DWORD),
                    ("dwXCountChars", w.DWORD), ("dwYCountChars", w.DWORD), ("dwFillAttribute", w.DWORD),
                    ("dwFlags", w.DWORD), ("wShowWindow", w.WORD), ("cbReserved2", w.WORD),
                    ("lpReserved2", ctypes.c_void_p), ("hStdInput", w.HANDLE), ("hStdOutput", w.HANDLE),
                    ("hStdError", w.HANDLE)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", w.HANDLE), ("hThread", w.HANDLE), ("dwProcessId", w.DWORD), ("dwThreadId", w.DWORD)]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]

    class BASIC_LIMIT(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", w.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", w.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD)]

    class EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [("Basic", BASIC_LIMIT), ("Io", IO_COUNTERS), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    for fn, args, res in (
        (advapi32.OpenProcessToken, [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)], w.BOOL),
        (advapi32.ConvertStringSidToSidW, [w.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)], w.BOOL),
        (advapi32.CreateRestrictedToken, [w.HANDLE, w.DWORD, w.DWORD, ctypes.POINTER(SID_AND_ATTRIBUTES), w.DWORD,
                                          ctypes.c_void_p, w.DWORD, ctypes.c_void_p, ctypes.POINTER(w.HANDLE)], w.BOOL),
        (advapi32.GetLengthSid, [ctypes.c_void_p], w.DWORD),
        (advapi32.GetTokenInformation, [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)], w.BOOL),
        (advapi32.InitializeAcl, [ctypes.c_void_p, w.DWORD, w.DWORD], w.BOOL),
        (advapi32.AddAccessAllowedAce, [ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.c_void_p], w.BOOL),
        (advapi32.SetTokenInformation, [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
        (advapi32.CreateProcessAsUserW, [w.HANDLE, w.LPCWSTR, w.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, w.BOOL,
                                         w.DWORD, ctypes.c_void_p, w.LPCWSTR, ctypes.POINTER(STARTUPINFOW),
                                         ctypes.POINTER(PROCESS_INFORMATION)], w.BOOL),
        (kernel32.GetCurrentProcess, [], w.HANDLE),
        (kernel32.GetStdHandle, [w.DWORD], w.HANDLE),
        (kernel32.SetHandleInformation, [w.HANDLE, w.DWORD, w.DWORD], w.BOOL),
        (kernel32.CreateJobObjectW, [ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
        (kernel32.SetInformationJobObject, [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
        (kernel32.AssignProcessToJobObject, [w.HANDLE, w.HANDLE], w.BOOL),
        (kernel32.ResumeThread, [w.HANDLE], w.DWORD),
        (kernel32.WaitForSingleObject, [w.HANDLE, w.DWORD], w.DWORD),
        (kernel32.GetExitCodeProcess, [w.HANDLE, ctypes.POINTER(w.DWORD)], w.BOOL),
        (kernel32.TerminateProcess, [w.HANDLE, w.UINT], w.BOOL),
        (kernel32.CloseHandle, [w.HANDLE], w.BOOL),
    ):
        fn.argtypes, fn.restype = args, res

    TOKEN_ACCESS = 0x0001 | 0x0002 | 0x0008 | 0x0080   # assign primary, duplicate, query, adjust default
    DISABLE_MAX_PRIVILEGE, TOKEN_INTEGRITY_LEVEL, SE_GROUP_INTEGRITY = 0x1, 25, 0x20
    CREATE_SUSPENDED, CREATE_UNICODE_ENVIRONMENT, STARTF_USESTDHANDLES = 0x4, 0x400, 0x100
    KILL_ON_JOB_CLOSE, DIE_ON_UNHANDLED_EXCEPTION, EXTENDED_LIMIT_INFO = 0x2000, 0x400, 9

    token, restricted = w.HANDLE(), w.HANDLE()
    check(advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_ACCESS, ctypes.byref(token)), "OpenProcessToken")
    admins, low = ctypes.c_void_p(), ctypes.c_void_p()
    check(advapi32.ConvertStringSidToSidW(ADMINISTRATORS_SID, ctypes.byref(admins)), "ConvertStringSidToSid")
    check(advapi32.ConvertStringSidToSidW(LOW_SID, ctypes.byref(low)), "ConvertStringSidToSid")
    deny = SID_AND_ATTRIBUTES(admins, 0)
    check(advapi32.CreateRestrictedToken(token, DISABLE_MAX_PRIVILEGE, 1, ctypes.byref(deny), 0, None, 0, None,
                                         ctypes.byref(restricted)), "CreateRestrictedToken")
    # Run elevated (an administrator's SSH session), the token's owner and default DACL name
    # Administrators -- which is deny-only now, so the process could not open its own token.
    # Make the person the owner, and give the default DACL to them and SYSTEM.
    size = w.DWORD()
    advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))           # TokenUser
    user_buffer = ctypes.create_string_buffer(size.value)
    check(advapi32.GetTokenInformation(token, 1, user_buffer, size, ctypes.byref(size)), "GetTokenInformation")
    user_sid = ctypes.cast(user_buffer, ctypes.POINTER(SID_AND_ATTRIBUTES)).contents.Sid
    system = ctypes.c_void_p()
    check(advapi32.ConvertStringSidToSidW("S-1-5-18", ctypes.byref(system)), "ConvertStringSidToSid")
    owner = ctypes.c_void_p(user_sid)
    check(advapi32.SetTokenInformation(restricted, 4, ctypes.byref(owner), ctypes.sizeof(owner)), "set TokenOwner")
    acl_size = 8 + 2 * (8 + 68)
    acl = ctypes.create_string_buffer(acl_size)
    check(advapi32.InitializeAcl(acl, acl_size, 2), "InitializeAcl")
    for sid in (user_sid, system):
        check(advapi32.AddAccessAllowedAce(acl, 2, 0x10000000, sid), "AddAccessAllowedAce")   # GENERIC_ALL
    dacl = ctypes.c_void_p(ctypes.addressof(acl))
    check(advapi32.SetTokenInformation(restricted, 6, ctypes.byref(dacl), ctypes.sizeof(dacl)), "set TokenDefaultDacl")

    label = TOKEN_MANDATORY_LABEL(SID_AND_ATTRIBUTES(low, SE_GROUP_INTEGRITY))
    check(advapi32.SetTokenInformation(restricted, TOKEN_INTEGRITY_LEVEL, ctypes.byref(label),
                                       ctypes.sizeof(label) + advapi32.GetLengthSid(low)), "SetTokenInformation")

    label_low(cwd)
    temp = low_temp()
    env = {**os.environ, "TEMP": temp, "TMP": temp}
    block = "".join(f"{k}={v}\0" for k, v in env.items()) + "\0"
    env_buffer = ctypes.create_unicode_buffer(block, len(block))

    startup = STARTUPINFOW()
    startup.cb = ctypes.sizeof(startup)
    startup.dwFlags = STARTF_USESTDHANDLES
    for field, which in (("hStdInput", -10), ("hStdOutput", -11), ("hStdError", -12)):
        handle = kernel32.GetStdHandle(which & 0xFFFFFFFF)
        if handle:
            kernel32.SetHandleInformation(handle, 1, 1)       # inheritable
        setattr(startup, field, handle)

    job = kernel32.CreateJobObjectW(None, None)
    check(job, "CreateJobObject")
    limits = EXTENDED_LIMIT()
    limits.Basic.LimitFlags = KILL_ON_JOB_CLOSE | DIE_ON_UNHANDLED_EXCEPTION
    check(kernel32.SetInformationJobObject(job, EXTENDED_LIMIT_INFO, ctypes.byref(limits), ctypes.sizeof(limits)),
          "SetInformationJobObject")

    command = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
    info = PROCESS_INFORMATION()
    check(advapi32.CreateProcessAsUserW(restricted, None, command, None, None, True,
                                        CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT, env_buffer, cwd,
                                        ctypes.byref(startup), ctypes.byref(info)), "CreateProcessAsUser")
    if not kernel32.AssignProcessToJobObject(job, info.hProcess):
        kernel32.TerminateProcess(info.hProcess, 1)
        check(0, "AssignProcessToJobObject")
    kernel32.ResumeThread(info.hThread)
    kernel32.WaitForSingleObject(info.hProcess, 0xFFFFFFFF)
    code = w.DWORD()
    kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    for handle in (info.hThread, info.hProcess, restricted, token, job):
        kernel32.CloseHandle(handle)
    return int(code.value)


def main(args: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if args is None else args)
    if len(args) < 4 or args[0] != "--cwd" or args[2] != "--":
        print("usage: python -m xharness.winlow --cwd <dir> -- <program> [args...]", file=sys.stderr)
        return 2
    if sys.platform != "win32":
        print("xharness.winlow runs on Windows only", file=sys.stderr)
        return 2
    try:
        return run(args[3:], os.path.realpath(args[1]))
    except OSError as error:
        print(f"xharness sandbox: {error}", file=sys.stderr)
        return 126


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
