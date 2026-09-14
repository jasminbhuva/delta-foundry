"""Bounded execution of untrusted and oracle processes.

Every process runs in its own session, optionally as a dedicated unprivileged
UID, under prlimit(1) resource limits, with a scrubbed environment, only
descriptors 0-2 open, stdout redirected to a supervisor-opened file and
stderr drained through a capped pipe. Wall time is measured with the
supervisor's monotonic clock from just before spawn until the process has
exited. Afterwards every surviving process of the UID is killed and counted.
"""

import os
import signal
import subprocess
import threading
import time

PRLIMIT = "/usr/bin/prlimit"


class Limits:
    def __init__(self, wall=120.0, address_space=2 << 30, file_size=512 << 20,
                 nofile=256, nproc=64, cpu=None, stderr_cap=1 << 16):
        self.wall = wall
        self.address_space = address_space
        self.file_size = file_size
        self.nofile = nofile
        self.nproc = nproc
        self.cpu = cpu
        self.stderr_cap = stderr_cap

    def prlimit_args(self):
        a = [PRLIMIT, "--core=0", "--as=%d" % self.address_space,
             "--fsize=%d" % self.file_size, "--nofile=%d" % self.nofile]
        if self.nproc:
            a.append("--nproc=%d" % self.nproc)
        if self.cpu:
            a.append("--cpu=%d" % self.cpu)
        return a + ["--"]


def base_env(home, tmpdir):
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": home,
        "TMPDIR": tmpdir,
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
    }


def _drain(pipe, cap, sink):
    kept = bytearray()
    total = 0
    while True:
        b = pipe.read(65536)
        if not b:
            break
        total += len(b)
        if len(kept) < cap:
            kept += b[:cap - len(kept)]
    sink["data"] = bytes(kept)
    sink["total"] = total


def pids_of_uid(uid):
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open("/proc/%s/status" % d) as f:
                for line in f:
                    if line.startswith("Uid:"):
                        if int(line.split()[1]) == uid:
                            out.append(int(d))
                        break
        except OSError:
            pass
    return out


def kill_uid(uid, rounds=50):
    """SIGKILL every process whose real UID is `uid`. Returns how many were found."""
    if uid is None or uid == 0:
        return 0
    found = set()
    for _ in range(rounds):
        pids = [p for p in pids_of_uid(uid) if p != os.getpid()]
        if not pids:
            break
        for p in pids:
            found.add(p)
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass
        time.sleep(0.02)
    return len(found)


def clean_sysv_ipc(uid):
    """Remove SysV shared memory/semaphores/queues owned by `uid`. Returns count."""
    n = 0
    for kind, flag in (("shm", "-m"), ("sem", "-s"), ("msg", "-q")):
        try:
            with open("/proc/sysvipc/" + kind) as f:
                lines = f.read().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            cols = line.split()
            # key id perms ... uid is column 7 for shm, 4 for sem, 7 for msg
            ucol = {"shm": 7, "sem": 4, "msg": 7}[kind]
            try:
                if int(cols[ucol]) == uid:
                    subprocess.run(["ipcrm", flag, cols[1]], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
                    n += 1
            except (IndexError, ValueError):
                pass
    return n


def run(argv, *, cwd, env, limits, stdin_path=None, stdout_path=None,
        stdout_pipe_reader=None, uid=None, gid=None):
    """Run argv under limits. Returns a result dict (never raises for child failure).

    stdout goes to `stdout_path` (opened here, truncated) or, when
    `stdout_pipe_reader` is given, to a pipe consumed by that callable
    (called in a thread with the pipe file object; its return value is stored
    in result['reader']).
    """
    stdin_f = open(stdin_path, "rb") if stdin_path else open(os.devnull, "rb")
    out_f = None
    if stdout_path:
        out_f = open(stdout_path, "wb")
        stdout_arg = out_f
    elif stdout_pipe_reader:
        stdout_arg = subprocess.PIPE
    else:
        stdout_arg = subprocess.DEVNULL
    kwargs = {}
    if uid is not None and os.geteuid() == 0:
        kwargs.update(user=uid, group=gid, extra_groups=[])
    full = limits.prlimit_args() + list(argv)
    res = {"argv": list(argv), "timed_out": False, "returncode": None,
           "signal": None, "wall": None, "survivors": 0, "ipc": 0}
    t0 = time.monotonic()
    try:
        p = subprocess.Popen(full, cwd=cwd, env=env, stdin=stdin_f, stdout=stdout_arg,
                             stderr=subprocess.PIPE, close_fds=True,
                             start_new_session=True, **kwargs)
    except OSError as e:
        stdin_f.close()
        if out_f:
            out_f.close()
        res["error"] = "spawn failed: %s" % e
        res["wall"] = time.monotonic() - t0
        res["stderr"] = b""
        return res
    err_sink = {}
    th = threading.Thread(target=_drain, args=(p.stderr, limits.stderr_cap, err_sink))
    th.daemon = True
    th.start()
    reader_box = {}
    rth = None
    if stdout_pipe_reader:
        def _rd():
            try:
                reader_box["value"] = stdout_pipe_reader(p.stdout)
            except Exception as e:  # reader errors are recorded, never raised
                reader_box["error"] = "%s: %s" % (type(e).__name__, e)
            finally:
                try:
                    while p.stdout.read(1 << 16):
                        pass
                except (OSError, ValueError):
                    pass
        rth = threading.Thread(target=_rd)
        rth.daemon = True
        rth.start()
    try:
        rc = p.wait(timeout=limits.wall)
        t1 = time.monotonic()
    except subprocess.TimeoutExpired:
        res["timed_out"] = True
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        rc = p.wait()
        t1 = time.monotonic()
    res["wall"] = t1 - t0
    if rth:
        rth.join(limits.wall + 5)
        res["reader"] = reader_box.get("value")
        if "error" in reader_box:
            res["reader_error"] = reader_box["error"]
    th.join(10)
    res["stderr"] = err_sink.get("data", b"")
    res["stderr_total"] = err_sink.get("total", 0)
    stdin_f.close()
    if out_f:
        out_f.close()
    if rc < 0:
        res["signal"] = -rc
    res["returncode"] = rc
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        pass
    if uid is not None and os.geteuid() == 0:
        res["survivors"] = kill_uid(uid)
        res["ipc"] = clean_sysv_ipc(uid)
    return res


def describe_failure(res, what):
    if res.get("error"):
        return "%s: %s" % (what, res["error"])
    if res["timed_out"]:
        return "%s: exceeded %.0f s wall limit" % (what, res.get("limit", 0) or 0)
    if res["signal"]:
        name = {signal.SIGXFSZ: "SIGXFSZ (output file limit)",
                signal.SIGKILL: "SIGKILL", signal.SIGSEGV: "SIGSEGV",
                signal.SIGABRT: "SIGABRT", signal.SIGXCPU: "SIGXCPU"}.get(res["signal"], str(res["signal"]))
        return "%s: killed by signal %s" % (what, name)
    if res["returncode"]:
        tail = res.get("stderr", b"")[-300:].decode("utf-8", "replace").replace("\n", " | ")
        return "%s: exit %d: %s" % (what, res["returncode"], tail)
    return None
