"""单次命令的进程组控制，作为 Python 代码随 Docker exec 发送，无须更新沙箱镜像。"""

# 控制文件的锁覆盖检查取消、启动子进程与登记 PID，消除“取消先于启动”的竞态。
# 实际命令及其普通子进程在独立进程组中运行。PID 保留到控制端收完输出，
# 避免父命令先退出、后台子进程仍占着输出管道时，取消找不到进程组。
RUN = r'''
import fcntl, subprocess, sys
path = sys.argv[1]
with open(path, "a+") as control:
    fcntl.flock(control, fcntl.LOCK_EX)
    control.seek(0)
    if control.read().startswith("cancelled"):
        from pathlib import Path
        Path(path).unlink()
        sys.exit(143)
    child = subprocess.Popen(sys.argv[2:], start_new_session=True)
    control.seek(0)
    control.truncate()
    control.write(str(child.pid))
    control.flush()
code = child.wait()
sys.exit(code if code >= 0 else 128 - code)
'''

STOP = r'''
import fcntl, os, signal, sys
with open(sys.argv[1], "a+") as control:
    fcntl.flock(control, fcntl.LOCK_EX)
    control.seek(0)
    pid = control.read()
    control.seek(0)
    control.truncate()
    control.write("cancelled")
    control.flush()
    if pid and pid != "cancelled":
        try:
            os.killpg(int(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.unlink(sys.argv[1])
'''

CLEANUP = r'''
from pathlib import Path
import sys
Path(sys.argv[1]).unlink(missing_ok=True)
'''
