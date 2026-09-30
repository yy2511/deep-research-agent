"""不同于命令用户的守护进程；只有可信控制端可以更新租期。"""
from pathlib import Path
import sys
import time

expires=time.monotonic()+min(172800,max(1,float(sys.argv[1])))
lease=Path('/run/dra/lease')
while True:
    try: expires=float(lease.read_text())
    except FileNotFoundError: pass
    if time.monotonic()>=expires: break
    time.sleep(min(.2,max(.01,expires-time.monotonic())))
