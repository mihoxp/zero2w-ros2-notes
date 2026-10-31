"""Minimal MJPEG-over-HTTP server for apps such as V7RC that show a live camera URL.

Every path serves the multipart stream (so any URL form an app expects works), except
/snapshot, which returns one JPEG. Frames are only encoded while at least one client is
connected, so an idle server costs nothing.
"""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BOUNDARY = b"frame"


class MjpegServer:
    def __init__(self, port, logger=None):
        self.cond = threading.Condition()
        self.jpeg = None
        self.clients = 0
        self.logger = logger
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep the ROS log clean
                pass

            def do_GET(self):
                if self.path.startswith("/snapshot"):
                    jpg = server.wait_frame(timeout=3.0)
                    if jpg is None:
                        self.send_error(503, "no frame yet")
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(jpg)))
                    self.end_headers()
                    self.wfile.write(jpg)
                    return
                self.send_response(200)
                self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY.decode()}")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                server._client(+1, self.client_address[0])
                try:
                    while True:
                        jpg = server.wait_frame(timeout=5.0)
                        if jpg is None:
                            continue
                        self.wfile.write(b"--" + BOUNDARY + b"\r\nContent-Type: image/jpeg\r\n"
                                         + f"Content-Length: {len(jpg)}\r\n\r\n".encode() + jpg + b"\r\n")
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    pass
                finally:
                    server._client(-1, self.client_address[0])

        self.httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def _client(self, delta, addr):
        with self.cond:
            self.clients += delta
            count = self.clients
        if self.logger:
            self.logger.info(f"MJPEG client {'connected' if delta > 0 else 'left'}: {addr} ({count} watching)")

    @property
    def wanted(self):
        """True while some request is waiting for a frame (stream client or snapshot)."""
        return self.waiting > 0

    waiting = 0

    def wait_frame(self, timeout):
        with self.cond:
            self.waiting += 1
            try:
                self.cond.wait(timeout=timeout)
                return self.jpeg
            finally:
                self.waiting -= 1

    def publish(self, jpeg_bytes):
        with self.cond:
            self.jpeg = jpeg_bytes
            self.cond.notify_all()

    def close(self):
        self.httpd.shutdown()
