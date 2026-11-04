"""Run the SavabYar bot with a small HTTP health endpoint for Runflare."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import subprocess
import sys
import time


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/health", "/healthz"):
            self.send_error(404)
            return
        body = b"SavabYar is running\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def main():
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    server.timeout = 1
    process = subprocess.Popen([sys.executable, "savabyar/bot.py"])
    try:
        while process.poll() is None:
            server.handle_request()
            time.sleep(0.05)
        return process.returncode
    finally:
        server.server_close()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
