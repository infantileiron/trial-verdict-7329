"""最小 Redis 协议替身，仅支持本项目 worker 用到的命令。
不是真实 Redis，仅供本地无 Docker 环境验证队列行为。"""
import socket
import socketserver
import threading
import time


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        keys = self.server.keys
        lock = self.server.lock
        while True:
            line = self.rfile.readline()
            if not line:
                return
            if not line.startswith(b'*'):
                return
            n = int(line[1:])
            args = []
            for _ in range(n):
                header = self.rfile.readline().strip()
                if not header.startswith(b'$'):
                    raise ValueError(f'bad bulk header: {header!r}')
                ln = int(header[1:])
                args.append(self.rfile.read(ln))
                self.rfile.read(2)
            cmd = args[0].upper()
            with lock:
                if cmd == b'PING':
                    self.wfile.write(b'+PONG\r\n')
                elif cmd in (b'CLIENT', b'COMMAND', b'RESET', b'HELLO'):
                    self.wfile.write(b'+OK\r\n')
                elif cmd == b'RPUSH':
                    key = args[1]
                    keys.setdefault(key, []).extend(args[2:])
                    self.wfile.write(f':{len(keys[key])}\r\n'.encode())
                elif cmd == b'LPUSH':
                    key = args[1]
                    keys.setdefault(key, [])[0:0] = list(reversed(args[2:]))
                    self.wfile.write(f':{len(keys[key])}\r\n'.encode())
                elif cmd == b'BLPOP' or cmd == b'LPOP':
                    timeout = float(args[-1]) if cmd == b'BLPOP' else 0
                    qkeys = args[1:-1] if cmd == b'BLPOP' else [args[1]]
                    waited = 0.0
                    while True:
                        for k in qkeys:
                            if keys.get(k):
                                val = keys[k].pop(0)
                                self.wfile.write(b'*2\r\n$' + str(len(k)).encode() + b'\r\n' + k + b'\r\n$'
                                                 + str(len(val)).encode() + b'\r\n' + val + b'\r\n')
                                break
                        else:
                            if waited >= timeout:
                                self.wfile.write(b'*-1\r\n' if cmd == b'BLPOP' else b'$-1\r\n')
                                break
                            time.sleep(0.05)
                            waited += 0.05
                            continue
                        break
                elif cmd == b'LLEN':
                    self.wfile.write(f':{len(keys.get(args[1], []))}\r\n'.encode())
                elif cmd == b'FLUSHALL' or cmd == b'FLUSHDB':
                    keys.clear()
                    self.wfile.write(b'+OK\r\n')
                else:
                    self.wfile.write(b'-ERR unknown command\r\n')
            self.wfile.flush()


class FakeRedis(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, host='127.0.0.1', port=0):
        super().__init__((host, port), Handler)
        self.keys = {}
        self.lock = threading.Lock()

    @property
    def url(self):
        return f'redis://127.0.0.1:{self.server_address[1]}/0'


if __name__ == '__main__':
    srv = FakeRedis(port=16399)
    print('fake-redis on', srv.url, flush=True)
    srv.serve_forever()
