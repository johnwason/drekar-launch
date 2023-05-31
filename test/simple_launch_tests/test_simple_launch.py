import subprocess
import os
from pathlib import Path
import sys
import time
import appdirs
import shutil

if sys.platform == "win32":
    import win32gui
    import win32con
    import win32process

def _launch_http_servers():

    # Get current file location
    res_dir = Path(__file__).parent / "res"
    proc = subprocess.Popen([sys.executable, "-msimple_launch"], cwd=res_dir, close_fds=True,\
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0)

    return proc

def _send_shutdown_signal(proc):
    if sys.platform == "win32":
        # proc.send_signal(subprocess.signal.CTRL_C_EVENT)
        # Send WM_CLOSE to the window

        # Find all windows for process proc using pywin32
        

        windows = []
        print(proc.pid)
        # Use FindWindowsEx to find all WM_MESSAGE windows for the process
        hwnd_child_after = 0
        while True:
            hWnd = win32gui.FindWindowEx(win32con.HWND_MESSAGE, hwnd_child_after, None, None)
            if hWnd == 0:
                break
            pid = win32process.GetWindowThreadProcessId(hWnd)[1]
            if pid == proc.pid:
                windows.append(hWnd)
            hwnd_child_after = hWnd

        # Send WM_CLOSE to all windows
        print(windows)
        for hwnd in windows:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    
        proc.send_signal(subprocess.signal.CTRL_C_EVENT)
    else:
        proc.send_signal(subprocess.signal.SIGINT)

def _wait_proc_exit(proc):
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        assert False, "Process did not exit in time"
        

def _assert_proc_returncode(proc):
    assert proc.returncode == 0, f"Process return code is {proc.returncode}"

def _get_logdir_base(name):
    log_dir = Path(appdirs.user_log_dir(appname="simple-launch")).joinpath(name)
    print("Log dir is ", log_dir)
    return log_dir

def _clear_logs(name):
    # Clear logs
    basedir = _get_logdir_base(name)
    if basedir.exists():
        # glob for name
        for f in basedir.glob(f"{name}*"):
            print("Removing old test log file ", f)
            shutil.rmtree(f)

def _assert_logs_exist(name):
    basedir = _get_logdir_base(name)
    if not basedir.exists():
        assert False, f"Log dir {basedir} does not exist"
    # glob for name
    for f in basedir.glob(f"{name}*"):
        print("Found test log directory ", f)
        assert (f / "test_http_server_1.log").is_file(), "test_http_server_1.log does not exist"
        assert (f / "test_http_server_1.stderr.log").is_file(), "test_http_server_1.stderr.log does not exist"
        assert (f / "test_http_server_2.log").is_file(), "test_http_server_2.log does not exist"
        assert (f / "test_http_server_2.stderr.log").is_file(), "test_http_server_2.stderr.log does not exist"
        return
        
    assert False

def test_services():
    _clear_logs("test_simple_launch")
    launch_proc = _launch_http_servers()

    # TODO: check servers status

    time.sleep(5)

    _send_shutdown_signal(launch_proc)
    print("Sent shutdown")
    _wait_proc_exit(launch_proc)
    print("Process exited")
    _assert_proc_returncode(launch_proc)
    print("Process return code is 0")
    _assert_logs_exist("test_simple_launch")