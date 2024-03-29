import argparse
from contextlib import suppress
from ctypes import ArgumentError
import asyncio
import gc
import importlib
import shutil
import yaml
from typing import NamedTuple, List, Dict
from enum import Enum
import threading
import traceback
import appdirs
from pathlib import Path
import sys
from datetime import datetime
import os
import time
import signal
import subprocess
import uuid
import jinja2
import drekar_launch_process


class DrekarTask(NamedTuple):
    name: str
    program: str
    cwd: str
    args: List[str]
    restart: bool
    restart_backoff: float
    tags: List[str]
    environment: Dict[str,str]
    start_delay: float
    quit_on_terminate: bool

# Based on MS Windows service states
class ProcessState(Enum):
    STOPPED = 0x1
    START_PENDING = 0x2
    STOP_PENDING = 0x3
    RUNNING = 0x4
    CONTINUE_PENDING = 0x5
    PAUSE_PENDING = 0x6
    PAUSED = 0x7

class DrekarProcess:
    def __init__(self, parent, task_launch, log_dir, loop):
        self.parent = parent
        self.task_launch = task_launch
        self.log_dir = log_dir
        self.loop = loop
        self._keep_going = True
        self._process = None
        self._term_attempts = 0
        self.screen = parent.screen
        self.exit_status = -1
    
    async def run(self):
        s = self.task_launch
        stdout_log_fname = self.log_dir.joinpath(f"{s.name}.log")
        stderr_log_fname = self.log_dir.joinpath(f"{s.name}.stderr.log")
        with open(stdout_log_fname,"w") as stdout_log, open(stderr_log_fname,"w") as stderr_log:
            if s.start_delay > 0:
                stderr_log.write(f"Delaying starting {s.name} for {s.start_delay} seconds...\n")
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.parent.exit_event.wait(),timeout=s.start_delay)
                if not self._keep_going:
                    return
            while self._keep_going:
                try:
                    self.parent.process_state_changed(s.name,ProcessState.START_PENDING)
                    stderr_log.write(f"Starting process {s.name}...\n")
                    python_exe = sys.executable
                    self._process = await create_subprocess_exec(s.program, s.args, s.environment, s.cwd, self.parent.cgroup)
                    # print(f"process pid: {self._process.pid}")
                    stderr_log.write(f"Process {s.name} started\n\n")                   
                    self.parent.process_state_changed(s.name,ProcessState.RUNNING)
                    stdout_read_task = asyncio.ensure_future(self._process.stdout.readline())
                    stderr_read_task = asyncio.ensure_future(self._process.stderr.readline())
                    while True: #self._keep_going:
                        wait_tasks = list(filter(lambda x: x is not None, [stdout_read_task, stderr_read_task]))
                        if len(wait_tasks) == 0:
                            break
                        done, pending = await asyncio.wait(wait_tasks,return_when=asyncio.FIRST_COMPLETED)
                        if stderr_read_task in done:
                            stderr_line = await stderr_read_task
                            if len(stderr_line) == 0:
                                stderr_read_task = None
                            else:
                                stderr_log.write(stderr_line.decode("utf-8")) 
                                stderr_log.flush()
                                stderr_read_task = asyncio.ensure_future(self._process.stderr.readline())
                                if self.screen:
                                    print(f"[{self.task_launch.name}]  " + stderr_line.decode("utf-8"),end="",file=sys.stderr)
                        if stdout_read_task in done:
                            stdout_line = await stdout_read_task
                            if len(stdout_line) == 0:
                                stdout_read_task = None
                            else:
                                stdout_log.write(stdout_line.decode("utf-8"))
                                stdout_log.flush()
                                stdout_read_task = asyncio.ensure_future(self._process.stdout.readline())
                                if self.screen:
                                    print(f"[{self.task_launch.name}]  " + stdout_line.decode("utf-8"),end="",file=sys.stdout)
                        if stdout_read_task is None and stderr_read_task is None:
                            break
                    await self._process.wait()
                    self.exit_status = self._process.get_exit_status()
                    if self.exit_status != 0:
                        stderr_log.write(f"Process {s.name} exited with status {self.exit_status}\n")
                        if self.screen:
                            print(f"[{self.task_launch.name}]  Process {s.name} exited with status {self.exit_status}",file=sys.stderr)
                    self.parent.process_state_changed(s.name,ProcessState.STOPPED)
                except:
                    self._process = None
                    self.parent.process_state_changed(s.name,ProcessState.STOPPED)
                    traceback.print_exc()
                    stderr_log.write(f"\nProcess {s.name} error:\n")
                    stderr_log.write(traceback.format_exc())
                self._process = None
                if s.quit_on_terminate:
                    self.parent.exit_event.set()
                    break
                if not s.restart:
                    break
                if self._keep_going:
                    with suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self.parent.exit_event.wait(), timeout=s.restart_backoff)

    @property
    def process_state(self):
        pass

    @property
    def stopped(self):
        return self._process == None

    def close(self):
        self._keep_going = False
        if self._process:
            self._process.send_term(self._term_attempts)
            self._term_attempts += 1
    
    def kill(self):
        p = self._process
        if p is None:
            return
        try:
            self._process.kill()
        except:
            traceback.print_exc()

class DrekarCore:
    def __init__(self, name, task_launches, exit_event, log_dir, screen, loop):
        self.name = name
        self.task_launches = dict()
        self._closed = False
        for s in task_launches:
            self.task_launches[s.name] = s
        self.log_dir = log_dir
        self.loop = loop
        self.screen=screen

        self._subprocesses = dict()
        self._lock = threading.RLock()
        self.exit_event = exit_event
        self.cgroup = None

        if sys.platform == "linux":
            self.cgroup = _linux_cgroupv2_launch_scope()
            self.cgroup.create_launcher_cgroup()


    def _do_start(self,s):
        p = DrekarProcess(self, s, self.log_dir, self.loop)
        self._subprocesses[s.name] = p
        self.loop.create_task(p.run())

    def start_all(self):
        with self._lock:
            for name,s in self.task_launches.items():
                if name not in self._subprocesses:
                    self._do_start(s)

    def start(self, name):
        with self._lock:
            if self._closed:
                assert False, "Already closed"
            try:
                s = self.task_launches[name]
            except KeyError:
                raise ArgumentError(f"Invalid service requested: {name}")
            if name not in self._subprocesses:
                self._do_start(s)

    def process_state_changed(self, process_name, state):
        print(f"Process changed {process_name} {state}")
        if self._closed:
            if state == ProcessState.STOPPED:
                with self._lock:
                    if process_name in self._subprocesses:
                        del self._subprocesses[process_name]

    def check_deps_status(self, deps):
        return True

    def stop_all(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True

            for p in self._subprocesses.values():
                try:
                    p.close()
                except Exception:
                    traceback.print_exc()
                    pass

    async def wait_all_stopped(self):
        try:
            t1 = time.time()
            t_last_sent_close = 0
            while True:
                t_diff = time.time() - t1
                if t_diff > 15:
                    break
                running_count = 0
                with self._lock:
                    for p in self._subprocesses.values():
                        if not p.stopped:
                            running_count += 1
                if running_count == 0:
                    break
                await asyncio.sleep(0.1)
                if t_diff > t_last_sent_close + 1:
                    t_last_sent_close = t_diff
                    with self._lock:
                        for p in self._subprocesses.values():
                            if not p.stopped:
                                try:
                                    p.close()
                                except Exception:
                                    traceback.print_exc()
                                    pass
            
            running_count = 0
            with self._lock:
                for p in self._subprocesses.values():
                    if not p.stopped:
                        running_count += 1
                        try:
                            p.kill()
                        except Exception:
                            traceback.print_exc()
                        
            if running_count != 0:
                print("Sending processes still running SIGKILL")                
                time.sleep(2)               

            #self._loop.stop()
        except:
            traceback.print_exc()

    def get_exit_status(self):
        exit_status = 0
        with self._lock:
            for p in self._subprocesses.values():
                if p.exit_status != 0:
                    exit_status = p.exit_status
        return exit_status
    
    def close(self):
        if sys.platform == "linux":
            self.cgroup.close()


async def create_subprocess_exec(process, args, env, cwd, launcher_cgroup=None):
    if sys.platform == "win32":
        job_handle = subprocess_impl_win32.win32_create_job_object()

        process = await asyncio.create_subprocess_exec(process,*args, \
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,\
            env=env, cwd=cwd, creationflags=subprocess_impl_win32.CREATE_SUSPENDED \
            | subprocess.CREATE_NEW_PROCESS_GROUP 
            ,close_fds=True)

        subprocess_impl_win32.win32_attach_job_and_resume_process(process, job_handle)

        return DrekarSubprocessImpl(process,job_handle)

    else:
        #TODO: Use "start_new_session=True" arg for new process
        process = await asyncio.create_subprocess_exec(process,*args, \
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,\
            env=env, cwd=cwd, close_fds=True, start_new_session=True )
        if sys.platform == "linux" and launcher_cgroup is not None:
            task_cgroup = launcher_cgroup.create_task_cgroup(process.pid)
            return DrekarSubprocessImpl(process, task_cgroup = task_cgroup)
        return DrekarSubprocessImpl(process)


class DrekarSubprocessImpl:
    def __init__(self, asyncio_subprocess, job_handle = None, task_cgroup = None):
        self._process = asyncio_subprocess
        self._job_handle = job_handle
        self._task_cgroup = task_cgroup
        # TODO: Linux

    @property
    def process(self):
        return self._process

    @property
    def stdout(self):
        return self._process.stdout

    @property
    def stderr(self):
        return self._process.stderr

    @property
    def pid(self):
        return self._process.pid

    def wait(self):
        return self._process.wait()

    def kill(self):
        self._process.kill()

    def send_term(self, attempt_count):
        if sys.platform == "win32":
            if attempt_count > 3:
                subprocess_impl_win32._win32_send_ctrl_c_event(self._process.pid)
            else:
                subprocess_impl_win32.win32_send_job_wm_close(self._job_handle)
        else:
            import signal
            pid = self._process.pid
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGINT)

    def close(self):
        if sys.platform == "win32":            
            subprocess_impl_win32.win32_close_job_object(self._job_handle)
        elif sys.platform == "linux" and self._task_cgroup:            
            self._task_cgroup.close()
        else:
            with suppress(Exception):
                self._process.kill()

    def get_exit_status(self):
        return self._process.returncode


if sys.platform == "win32":
    import ctypes.wintypes
    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
                ('PerProcessUserTimeLimit', ctypes.wintypes.LARGE_INTEGER),
                ('PerJobUserTimeLimit', ctypes.wintypes.LARGE_INTEGER),
                ('LimitFlags', ctypes.wintypes.DWORD),
                ('MinimumWorkingSetSize', ctypes.c_size_t),
                ('MaximumWorkingSetSize', ctypes.c_size_t),
                ('ActiveProcessLimit', ctypes.wintypes.DWORD),  
                ('Affinity', ctypes.POINTER(ctypes.c_ulong)),
                ('PriorityClass', ctypes.wintypes.DWORD),
                ('SchedulingClass', ctypes.wintypes.DWORD)
            ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ('ReadOperationCount',ctypes.c_ulonglong),
            ('WriteOperationCount',ctypes.c_ulonglong),
            ('OtherOperationCount',ctypes.c_ulonglong),
            ('ReadTransferCount',ctypes.c_ulonglong),
            ('WriteTransferCount',ctypes.c_ulonglong),
            ('OtherTransferCount',ctypes.c_ulonglong),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ('BasicLimitInformation',_JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ('IoInfo', _IO_COUNTERS),
            ('ProcessMemoryLimit', ctypes.c_size_t),
            ('JobMemoryLimit', ctypes.c_size_t),
            ('PeakProcessMemoryUsed', ctypes.c_size_t),
            ('PeakJobMemoryUsed', ctypes.c_size_t)
        ]

    class _THREADENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_ulong),
            ("cntUsage", ctypes.c_ulong),
            ("th32ThreadID", ctypes.c_ulong),
            ("th32OwnerProcessID", ctypes.c_ulong),
            ("tpBasePri", ctypes.c_ulong),
            ("tpDeltaPri", ctypes.c_ulong),
            ("dwFlags", ctypes.c_ulong)
        ]

    class _JOBOBJECT_BASIC_PROCESS_ID_LIST(ctypes.Structure):
        _fields_ = [
            ("NumberOfAssignedProcesses", ctypes.wintypes.DWORD),
            ("NumberOfProcessIdsInList", ctypes.wintypes.DWORD),
            ("ProcessIdList", ctypes.c_size_t*16384)
        ]
    class subprocess_impl_win32:
        
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL,ctypes.wintypes.HWND,ctypes.wintypes.LPARAM)


        JobObjectBasicLimitInformation = 2
        JobObjectBasicProcessIdList = 3
        JobObjectExtendedLimitInformation = 9
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
        PROCESS_SET_QUOTA = 0x0100
        PROCESS_TERMINATE = 0x0001
        CREATE_SUSPENDED = 0x00000004

        TH32CS_SNAPTHREAD = 0x00000004
        THREAD_SUSPEND_RESUME = 0x0002

        HWND_MESSAGE = ctypes.wintypes.HWND(-3)
        WM_CLOSE = 16

        def win32_create_job_object():
            job = ctypes.windll.kernel32.CreateJobObjectW(None, None)
            job_limits = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            res = ctypes.windll.kernel32.QueryInformationJobObject(job, subprocess_impl_win32.JobObjectExtendedLimitInformation, ctypes.pointer(job_limits), ctypes.sizeof(job_limits), None)
            assert "Internal error, could not query win32 job object information"
            job_limits.BasicLimitInformation.LimitFlags |= subprocess_impl_win32.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            res = ctypes.windll.kernel32.SetInformationJobObject(job, subprocess_impl_win32.JobObjectExtendedLimitInformation, ctypes.pointer(job_limits), ctypes.sizeof(job_limits))
            assert res, "Internal error, could not set win32 job object information"
            #current_process = ctypes.windll.kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, ctypes.windll.kernel32.GetCurrentProcessId())
            #res = ctypes.windll.kernel32.AssignProcessToJobObject(job, current_process)
            #assert res, "Internal error, could not assign win32 process to job"
            return job

        def win32_attach_job_and_resume_process(asyncio_process, job):
            
            h = ctypes.windll.kernel32.OpenProcess(subprocess_impl_win32.PROCESS_SET_QUOTA | subprocess_impl_win32.PROCESS_TERMINATE, False, asyncio_process.pid)
            res = ctypes.windll.kernel32.AssignProcessToJobObject(job, h)
            assert res, "Internal error, could not assign win32 process to job"
            ctypes.windll.kernel32.CloseHandle(h)

            subprocess_impl_win32.win32_resume_process(asyncio_process.pid)

        def win32_close_job_object(handle):
            if handle is None:
                return
            ctypes.windll.kernel32.CloseHandle(handle)

        def win32_get_thread_ids(pid):

            thread_ids = []

            hThreadSnap = ctypes.windll.kernel32.CreateToolhelp32Snapshot(subprocess_impl_win32.TH32CS_SNAPTHREAD, pid)
            try:
                te32 = _THREADENTRY32()
                te32.dwSize = ctypes.sizeof(_THREADENTRY32)
                if ctypes.windll.kernel32.Thread32First(hThreadSnap, ctypes.byref(te32)) == 0:
                    pass

                else:
                    while True:
                        if pid == te32.th32OwnerProcessID:
                            thread_ids.append(te32.th32ThreadID)

                        if ctypes.windll.kernel32.Thread32Next(hThreadSnap, ctypes.byref(te32)) == 0:
                            break
            finally:
                ctypes.windll.kernel32.CloseHandle(hThreadSnap)
            return sorted(thread_ids)

        def win32_resume_process(pid):
            thread_ids = subprocess_impl_win32.win32_get_thread_ids(pid)
            for thread_id in thread_ids:
                thread_h = ctypes.windll.kernel32.OpenThread(subprocess_impl_win32.THREAD_SUSPEND_RESUME, False, thread_id)
                ctypes.windll.kernel32.ResumeThread(thread_h)
                ctypes.windll.kernel32.CloseHandle(thread_h)

        def win32_send_job_wm_close(job):
            win32_thread_info = _JOBOBJECT_BASIC_PROCESS_ID_LIST()
            res = ctypes.windll.kernel32.QueryInformationJobObject(job, subprocess_impl_win32.JobObjectBasicProcessIdList, ctypes.pointer(win32_thread_info), ctypes.sizeof(win32_thread_info), None)
            if not res:
                return
            pids = []
            for i in range(win32_thread_info.NumberOfProcessIdsInList):
                pids.append(win32_thread_info.ProcessIdList[i])

            subprocess_impl_win32.win32_send_pid_wm_close(pids)

        def win32_send_pid_wm_close(pid):        
            subprocess_impl_win32._win32_send_wm_close_hwnd_message(pid)
            subprocess_impl_win32._win32_send_ctrl_c_event(pid)

        
        def _win32_find_message_hwnds(pids):
            if not isinstance(pids, list):
                pids = [pids]
            hWnd_child_after = 0
            hwnds = []

            while True:
                hWnd = ctypes.windll.user32.FindWindowExW(subprocess_impl_win32.HWND_MESSAGE, hWnd_child_after, None, None)
                # print(hWnd)    
                if hWnd == 0:
                    break
                process_id = ctypes.wintypes.DWORD()
                ctypes.windll.user32.GetWindowThreadProcessId(hWnd,ctypes.byref(process_id))
                if process_id.value in pids:
                    hwnds.append(hWnd)
                    #ctypes.windll.user32.PostMessageW(hWnd,subprocess_impl_win32.WM_CLOSE,0,0)
                hWnd_child_after = hWnd
            return hwnds
        
        def _win32_find_main_hwnds(pids):
            if not isinstance(pids, list):
                pids = [pids]

            hwnds = []

            # Create list of top level windows
            
            def worker(hWnd, lParam):
                process_id = ctypes.wintypes.DWORD()
                ctypes.windll.user32.GetWindowThreadProcessId(hWnd,ctypes.byref(process_id))
                if lParam == process_id.value:
                    hwnds.append(hWnd)
                return True
            
            cb_worker = subprocess_impl_win32.WNDENUMPROC(worker)
            if not ctypes.windll.user32.EnumWindows(cb_worker, pids[0]):
                return hwnds
            
            # Filter out windows that are children of other windows
            hwnds = [hWnd for hWnd in hwnds if not ctypes.windll.user32.GetParent(hWnd)]
            return hwnds
        
        def _win32_send_wm_close_hwnd_message(pid):
            # check for main window first, then send to message windows
            hwnds = subprocess_impl_win32._win32_find_main_hwnds(pid)
            if not hwnds:
                hwnds = subprocess_impl_win32._win32_find_message_hwnds(pid)
            for hWnd in hwnds:
                ctypes.windll.user32.PostMessageW(hWnd,subprocess_impl_win32.WM_CLOSE,0,0)

        def _win32_send_ctrl_c_event(pid):
            if isinstance(pid, list):
                for p in pid:
                    subprocess_impl_win32._win32_send_ctrl_c_event(p)
                return
            ctypes.windll.kernel32.GenerateConsoleCtrlEvent(0,pid)

if sys.platform == "linux":

    class _linux_cgroupv2_launch_scope:

        @staticmethod
        def cgroupv2_supported():
            if Path("/sys/fs/cgroup/cgroup.controllers").exists():
                return True
            return False

        def __init__(self):
            self._pid = os.getpid()
            self.cgroup_parent_path = None
            self.cgroup_path = None

            if not self.cgroupv2_supported():
                return
            
            self.cgroup_parent_path = _linux_cgroupv2_launch_scope.read_proc_cgroup(self._pid)

            self.sentinel_process = None

        @staticmethod
        def read_proc_cgroup(pid):
            cgroup_path = None
            try:
                # read cgroup path from /proc/{pid}/cgroup
                with open(f"/proc/{pid}/cgroup","r") as f:
                    for line in f:
                        if line.startswith("0::/"):
                            path1 = line.split(":")[2]
                            if path1.strip() == "/":
                                # Not currently assigned to a cgroup
                                break
                            cgroup_path = Path("/sys/fs/cgroup") / Path(path1.strip().strip("/"))
                            break
            except:
                # TODO: log error
                traceback.print_exc()
                pass

            return cgroup_path
        
        def create_launcher_cgroup(self):
            if not self.cgroupv2_supported():
                return
            
            # create a new cgroup scope for this launcher with the name drekar-launch-{random_uuid}.scope
            try:
                cgroup_name = f"drekar-launch-{uuid.uuid4().hex}.scope"
                cgroup_path = self.cgroup_parent_path / cgroup_name
                # TODO: log
                # print(f"Creating cgroup {cgroup_path}")
                cgroup_path.mkdir()
                self.cgroup_path = cgroup_path

                enable_sentinel_environ = os.environ.get("DREKAR_LAUNCH_ENABLE_SENTINEL", "1").strip().lower()
                if enable_sentinel_environ == "1" or enable_sentinel_environ == "true":
                    self.start_sentinel()
            except:
                traceback.print_exc()
                pass

        def create_task_cgroup(self, task_pid):
            task_name = f"task-{task_pid}"
            if self.cgroup_path is None:
                return _linux_cgroupv2_task_scope(None, task_name, task_pid)
            
            task_cgroup = _linux_cgroupv2_task_scope(self.cgroup_path, task_name, task_pid)
            task_cgroup.create_task_cgroup()
            return task_cgroup
        
        @staticmethod
        def close_cgroup_path(cgroup_path):
            try:
                #Iterate through all subdirectories and recursively close them
                for subpath in cgroup_path.iterdir():
                    if subpath.is_dir():
                        _linux_cgroupv2_launch_scope.close_cgroup_path(subpath)
                cgroup_kill_path = cgroup_path / "cgroup.kill"
                if cgroup_kill_path.exists():
                    with open(cgroup_kill_path,"w") as f:
                        f.write("1")
                    pass
                    time.sleep(0.1)
                cgroup_path.rmdir()
            except:
                traceback.print_exc()
                pass

        def close(self):
            if self.cgroup_path is not None:
                # Iterate through all subdirectories depth first

                self.close_cgroup_path(self.cgroup_path)

                self.cgroup_path = None

                if self.sentinel_process is not None:
                    self.stop_sentinel()

        def start_sentinel(self):
            if self.sentinel_process is not None:
                return
            
            sentinel_environ = os.environ.copy()
            sentinel_environ["DREKAR_LAUNCH_ENABLE_SENTINEL"] = "0"
            self.sentinel_process=subprocess.Popen([sys.executable, "-m", "drekar_launch", "--sentinel", str(os.getpid()), str(self.cgroup_path)], 
                                                    env=sentinel_environ, close_fds=True, start_new_session=True)
            
        def stop_sentinel(self):
            if self.sentinel_process is None:
                return
            try:                
                os.killpg(self.sentinel_process.pid, signal.SIGTERM)
            except:
                pass

        def __enter__(self):
            self.create_launcher_cgroup()
            return self
        
        def __exit__(self, exc_type, exc_value, traceback):
            self.close()


    class _linux_cgroupv2_task_scope:
        def __init__(self, cgroup_path, task_name, task_pid):
            self.cgroup_path = cgroup_path
            self.task_name = task_name
            self.task_pid = task_pid
            self.task_cgroup_path = None

        def create_task_cgroup(self):
            if self.cgroup_path is None:
                return
            
            self.task_cgroup_path = self.cgroup_path / f"{self.task_name}.scope"
            self.task_cgroup_path.mkdir()
            # Move task to new cgroup
            with open(self.task_cgroup_path / "cgroup.procs","w") as f:
                f.write(str(self.task_pid))
            
        def close(self):
            if self.task_cgroup_path is not None:
                task_cgroup_kill_path = self.task_cgroup_path / "cgroup.kill"
                if task_cgroup_kill_path.exists():
                    with open(task_cgroup_kill_path,"w") as f:
                        f.write("1")
                    pass
                self.task_cgroup_path.rmdir()
                self.task_cgroup_path = None

        def __enter__(self):
            self.create_task_cgroup()
            return self
        
        def __exit__(self, exc_type, exc_value, traceback):
            self.close()

    def _sentinel_main():
        if not _linux_cgroupv2_launch_scope.cgroupv2_supported():
            return
        assert len(sys.argv) >= 4
        assert sys.argv[1] == "--sentinel"
        parent_pid = int(sys.argv[2])
        parent_cgroup_path = Path(sys.argv[3])
        parent_pid_proc_path = Path(f"/proc/{parent_pid}")
        evt = threading.Event()
        drekar_launch_process.wait_exit_callback(lambda: evt.set())
        while True:
            evt.wait(15)
            if evt.is_set():
                break
            if not parent_cgroup_path.exists():
                return
            
            # Check if parent process is still alive
            if not parent_pid_proc_path.exists():
                break            
            
        # Parent process is dead, close cgroup
        if not parent_cgroup_path.exists():
            return
        time.sleep(10)
        if not parent_cgroup_path.exists():
            return
        _linux_cgroupv2_launch_scope.close_cgroup_path(Path(parent_cgroup_path))


def parse_task_launch_from_yaml(yaml_dict, cwd):
    # parse yaml_dict into DrekarTask tuple
    name = yaml_dict["name"]
    program = yaml_dict["program"]
    cwd = yaml_dict.get("cwd", cwd)
    args = yaml_dict.get("args", None)
    if args is None:
        args = []
    else:
        if isinstance(args,str):
            args = args.split()
        if isinstance(args,bool) or isinstance(args,int) or isinstance(args,float):
            # Corner case where args is another yaml type
            args = [str(args)]
        else:
            assert isinstance(args,list), "process args must be a string or list"
            args = [str(a) for a in args]
    restart = yaml_dict.get("restart", False)
    restart_backoff = yaml_dict.get("restart-backoff", 5)
    tags = yaml_dict.get("tags", [])
    env = os.environ.copy()
    env.update(yaml_dict.get("environment", {}))
    start_delay = yaml_dict.get("start-delay", 0)
    quit_on_terminate = yaml_dict.get("quit-on-terminate", False)

    if "env-file" in yaml_dict:
        env_file = yaml_dict["env-file"]
        with open(env_file, "r") as f:
            env = dict()
            env_line = f.readline()
            while env_line:
                env_line = env_line.strip()
                if env_line:
                    # handle comments
                    if env_line[0] == "#":
                        # remove trailing comment
                        env_line = env_line.split("#", 1)[0]
                    env_line_split = env_line.split("=", 1)
                    if len(env_line_split) == 2:
                        env[env_line_split[0]] = env_line_split[1]
                env_line = f.readline()

    if(Path(program).name == program):
        program = shutil.which(program, path=env["PATH"])
        if program is None:
            raise Exception("Could not find program: {}".format(program))

    return DrekarTask(
        name=name,
        program=program,
        cwd=cwd,
        args=args,
        restart=restart,
        restart_backoff=restart_backoff,
        tags=tags,
        environment=env,
        start_delay=start_delay,
        quit_on_terminate=quit_on_terminate,
    )




def parse_task_launches_from_yaml(f, cwd):
    yaml_dict = yaml.safe_load(f)
    return parse_task_launches_from_yaml_dict(yaml_dict, cwd)

def parse_task_launches_from_yaml_dict(yaml_dict, cwd):
    yaml_tasks = yaml_dict["tasks"]
    name = yaml_dict.get("name",None)
    task_launches = []
    for t in yaml_tasks:
        task_launches.append(parse_task_launch_from_yaml(t, cwd))
    return name, task_launches


class DrekarGui:

    def __init__(self, name, core, exit_event):
        self.name = name
        self.core = core
        self.log_dir = core.log_dir
        self.exit_event = exit_event
        self.root = None

    def _create_root(self):
        tk = importlib.import_module("tkinter")

        root = tk.Tk()
        root.title(self.name + " Drekar Launch")
        root.geometry("600x200")

        root.protocol("WM_DELETE_WINDOW", self._set_exit_event)

        # Create a window that displays status of tasks and has a "Stop All" button
        # that sets the exit_event

        label = tk.Label(root, fg = "black", justify=tk.LEFT, wraplength=600)
        # Fill window with label
        label.grid(row=0, column=0, sticky=tk.NSEW)
        # label.grid(row=0, column=0)        
        label.config(text=f"Running Launch:\n{self.name}\n\nLog Directory:\n{self.log_dir}\n\nPress \"Stop All\" to exit\n")

        button = tk.Button(root, text="Stop All", command=self._set_exit_event, width=50, height=2)
        button.grid(row=1, column=0, sticky=tk.S)  

        root.bind("<<exit>>", self._close)

        self.root = root

    def _set_exit_event(self):
        self.core.loop.call_soon_threadsafe(self.exit_event.set)

    def start(self):
        # run in thread
        self._thread = threading.Thread(target=self._run)
        # self._thread.daemon = True
        self._thread.start()

    def _run(self):
        self._create_root()
        self.root.mainloop()
        self.root.quit()
        self.root.tk.quit()
        # self.root.tk.destroy()
        self.root = None
        time.sleep(1)

    def _close(self, *args):
        self.root.destroy()
               

    def close(self):
        self.root.event_generate("<<exit>>")
        self._thread.join()

def parse_task_launches_from_jinja2_config(config, config_fname, cwd, extra_process_args):

    config_absolute_path = os.path.abspath(config_fname)
    config_dir = os.path.dirname(config_absolute_path)
    config_text = config.read()

    jinja2_env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(config_dir),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )

    # create vars from extract args starting with --var-
    vars = dict()
    for a in extra_process_args:
        if a.startswith("--var-"):
            var_name = a.split("=",1)[0][6:]
            var_value = a.split("=",1)[1]
            vars[var_name] = var_value

    extra_args = {
        "configdir": config_dir,
        "configpath": config_absolute_path,
        "env": os.environ,
        "vars": vars,
        "platform": sys.platform
    }

    config_text = jinja2_env.from_string(config_text).render(**extra_args)
    yaml_dict = yaml.safe_load(config_text)
    name, task_launches = parse_task_launches_from_yaml_dict(yaml_dict, cwd)

    return name, task_launches

def main():

    # Run the sentinel if requsted on linux
    if sys.platform == "linux" and "--sentinel" in sys.argv:
        _sentinel_main()
        return

    core = None
    try:
        parser = argparse.ArgumentParser("PyRI Core Launcher")
        parser.add_argument("--config", type=str, default=None, help="Configuration file")
        parser.add_argument("--config-j2", type=str, default=None, help="Configuration file (jinja2 template)")
        parser.add_argument("--cwd", type=str, default=".", help="Working directory")
        parser.add_argument("--name", type=str, default=None, help="Name of the launch")
        parser.add_argument("--quiet", action="store_true", help="Echo output to screen")
        parser.add_argument("--gui", action="store_true", help="Run GUI")

        parser_results, remaining_args = parser.parse_known_args()

        
        # check that both config and config-j2 are not specified
        if parser_results.config is not None and parser_results.config_j2 is not None:
            raise Exception("Only one of --config or --config-j2 can be specified")
        
        # check that config or config-j2 is specified
        if parser_results.config_j2 is not None:
            with open(parser_results.config_j2, "r") as f:                
                name, task_launch = parse_task_launches_from_jinja2_config(f, parser_results.config_j2, parser_results.cwd, remaining_args)
        elif parser_results.config is not None:
            with open (parser_results.config, "r") as f:
                name, task_launch = parse_task_launches_from_yaml(f, parser_results.cwd)
        else:
            # use default config drekar-launch.yaml
            with open("drekar-launch.yaml", "r") as f:
                name, task_launch = parse_task_launches_from_yaml(f, parser_results.cwd)

        name = parser_results.name if parser_results.name is not None else name
        if name is None:
            name = "drekar-launch"

        timestamp = datetime.now().strftime("-%Y-%m-%d--%H-%M-%S")
        log_dir = Path(appdirs.user_log_dir(appname="drekar-launch")).joinpath(name).joinpath(name + timestamp)
        log_dir.mkdir(parents=True, exist_ok=True)
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)        
                        
        exit_event = asyncio.Event()
        core = DrekarCore(parser_results.name, task_launch, exit_event, log_dir, not parser_results.quiet, loop)
        gui = None
        if parser_results.gui:
            gui = DrekarGui(name, core, exit_event)
            gui.start()
        loop.call_soon(lambda: core.start_all())
        def ctrl_c_pressed():
            loop.call_soon_threadsafe(lambda: exit_event.set())
        drekar_launch_process.wait_exit_callback(ctrl_c_pressed)
        print("Press Ctrl-C to exit")        
        loop.run_until_complete(exit_event.wait())
        print("Exit received, closing")
        core.stop_all()
        loop.run_until_complete(core.wait_all_stopped())
        #pending = asyncio.all_tasks(loop)
        # pending = asyncio.all_tasks()
        #loop.run_until_complete(asyncio.gather(*pending))
        if gui is not None:
            gui.close()
        print("Exiting!")
        exit_status = core.get_exit_status()
        if exit_status != 0:
            print(f"Exit status: {exit_status}")
        sys.exit(exit_status)
    except Exception:
        traceback.print_exc()
        raise
    finally:
        if core is not None:
            core.close()
    


if __name__ == "__main__":
    main()