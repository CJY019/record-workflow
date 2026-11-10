"""手机/平板远程访问：用 SSH 反向隧道把本机服务映射到 VPS（127.0.0.1:remote_port），
VPS 上的 nginx 再以 HTTPS + 密码对外提供。由启动器拉起，启动器关闭时隧道随之结束。"""
import ctypes
import os
import shutil
import subprocess
import sys
import threading
import time
from ctypes import wintypes

from .config import CFG

CREATE_NO_WINDOW = 0x08000000


def bind_children_to_self():
    """把当前进程放进「关闭即杀死」的 Windows 作业对象：启动器窗口一关，
    ssh 等子进程跟着结束，不会残留在后台。"""
    if sys.platform != "win32":
        return

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("Basic", BASIC), ("Io", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    info = EXTENDED()
    info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    k32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info))
    k32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(k32.GetCurrentProcess()))
    globals()["_JOB"] = job  # 句柄保持打开，进程退出时由系统关闭 → 子进程被结束


def _cmd(port: int):
    r = CFG.get("relay") or {}
    ssh = shutil.which("ssh") or r"C:\Windows\System32\OpenSSH\ssh.exe"
    return [ssh, "-N", "-T", "-i", os.path.expanduser(r.get("key", "~/.ssh/id_ed25519")),
            "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3",
            "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15",
            "-R", f"127.0.0.1:{r.get('remote_port', 18765)}:127.0.0.1:{port}", r["ssh"]]


def _loop(port: int):
    r = CFG["relay"]
    wait, announced = 5, False
    while True:
        p = subprocess.Popen(_cmd(port), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
        t0 = time.time()
        while p.poll() is None and time.time() - t0 < 5:
            time.sleep(0.2)
        if p.poll() is None:
            if announced:
                print("手机 / 平板访问中转已重新连上")
            else:
                print(f"手机 / 平板访问：{r.get('url', '')}")
                if r.get("user"):
                    print(f"　　用户名：{r['user']}（密码请查看本地配置）")
            announced, wait = True, 5
        err = p.stderr.read().decode("utf-8", "replace").strip().splitlines()
        p.wait()
        if time.time() - t0 > 60:
            wait = 5  # 跑过一阵才断的（网络波动/休眠），尽快重连
        msg = err[-1] if err else f"退出码 {p.returncode}"
        print(f"手机访问中转断开（{msg}），{wait} 秒后重连…")
        time.sleep(wait)
        wait = min(wait * 2, 60)


def start(port: int):
    if not (CFG.get("relay") or {}).get("enabled"):
        return
    threading.Thread(target=_loop, args=(port,), daemon=True).start()
