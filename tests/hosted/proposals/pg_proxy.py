"""Disposable loopback test proxy; never used by the hosted runtime."""
import socket
import socketserver
import threading
import os


UPSTREAM_HOST = os.environ.get("DRYDOCK_TEST_DB_HOST", "db")
UPSTREAM_PORT = int(os.environ.get("DRYDOCK_TEST_DB_PORT", "5432"))


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        upstream = socket.create_connection((UPSTREAM_HOST, UPSTREAM_PORT))

        def copy(source, target):
            while data := source.recv(65536):
                target.sendall(data)

        thread = threading.Thread(target=copy, args=(self.request, upstream))
        thread.start()
        copy(upstream, self.request)
        thread.join()


socketserver.ThreadingTCPServer(("0.0.0.0", 55432), Handler).serve_forever()
