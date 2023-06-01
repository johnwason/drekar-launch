import subprocess
import sys
import os
from pathlib import Path

def test_simple_launch_quit():
    res_dir = Path(__file__).parent / "res"
    subprocess.check_call([sys.executable, "-msimple_launch", "--config=simple-launch-quit.yaml"],
                          cwd=res_dir, close_fds=True)
    
def test_simple_launch_quit_j2():
    res_dir = Path(__file__).parent / "res"
    subprocess.check_call([sys.executable, "-msimple_launch", "--config-j2=simple-launch-quit-j2.yaml.j2"],
                          cwd=res_dir, close_fds=True)

def test_simple_launch_quit_err():
    res_dir = Path(__file__).parent / "res"
    res = subprocess.call([sys.executable, "-msimple_launch", "--config=simple-launch-quit-err.yaml"],
                          cwd=res_dir, close_fds=True)
    assert res == 42, "Expected return code 42, got " + str(res)