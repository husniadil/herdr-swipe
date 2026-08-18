"""A Herdr socket good enough to test navigation against.

Serves canned replies over a real AF_UNIX socket, one request per connection
and then close, because that is what Herdr does and it is the detail most
likely to break a client.
"""

import json
import os
import socket
import tempfile
import threading


class FakeHerdr:
    def __init__(self, replies):
        self.replies = replies          # method -> result dict
        self.calls = []                 # (method, params), in order
        self.path = os.path.join(tempfile.mkdtemp(), "herdr.sock")
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.path)
        self._server.listen(16)
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            with conn:
                data = b""
                while b"\n" not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                if not data:
                    continue
                req = json.loads(data.split(b"\n", 1)[0])
                self.calls.append((req["method"], req.get("params") or {}))
                result = self.replies.get(req["method"])
                if result is not None:
                    body = {"id": req["id"], "result": result}
                else:
                    missing = f"no canned reply for {req['method']}"
                    body = {"id": req["id"], "error": {"message": missing}}
                conn.sendall((json.dumps(body) + "\n").encode())

    def methods(self):
        return [m for m, _ in self.calls]

    def close(self):
        self._stop = True
        self._server.close()
