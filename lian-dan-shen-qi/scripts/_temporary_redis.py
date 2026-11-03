"""仅供验收的独立 Redis 子进程；随机端口、临时目录，不接触已有 Redis 数据。"""
from contextlib import contextmanager
from pathlib import Path
import shutil
import socket
import subprocess
from tempfile import TemporaryDirectory
import time

from redis import Redis


@contextmanager
def temporary_redis():
    server=shutil.which('redis-server')
    if server is None:raise RuntimeError('验证需要本机 Redis server 可执行程序。')
    with TemporaryDirectory(prefix='codex_redis_stream_') as temporary:
        directory=Path(temporary).resolve()
        with socket.socket() as port_socket:
            port_socket.bind(('127.0.0.1',0));port=port_socket.getsockname()[1]
        config=directory/'redis.conf'
        config.write_text('bind 127.0.0.1\nport '+str(port)+'\nprotected-mode yes\nsave ""\nappendonly no\n'
            +'dir "'+directory.as_posix()+'"\nlogfile "'+(directory/'redis.log').as_posix()+'"\n',encoding='utf-8')
        process=subprocess.Popen([server,str(config)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        client=Redis(host='127.0.0.1',port=port,decode_responses=True,socket_connect_timeout=1,socket_timeout=2)
        verified=False
        try:
            deadline=time.monotonic()+8
            while time.monotonic()<deadline:
                if process.poll() is not None:raise RuntimeError('临时 Redis 未能启动。')
                try:
                    info=client.info('server')
                    if int(info['process_id'])!=process.pid:raise RuntimeError('随机端口被其他进程占用。')
                    verified=True;break
                except OSError:time.sleep(.05)
                except Exception as error:
                    if type(error).__name__ not in {'ConnectionError','TimeoutError'}:raise
                    time.sleep(.05)
            if not verified:raise RuntimeError('临时 Redis 启动超时。')
            yield {'url':f'redis://127.0.0.1:{port}/0','client':client,'version':info['redis_version']}
        finally:
            # 只关闭本函数启动且已通过 PID 核对的 Redis，不对任何现有服务发 SHUTDOWN。
            if verified and process.poll() is None:
                try:client.shutdown(nosave=True)
                except Exception:pass
            client.close()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.terminate();process.wait(timeout=5)
