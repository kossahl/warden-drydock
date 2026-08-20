"""Disposable loopback test proxy; never used by the hosted runtime."""
import socket
import socketserver
import threading


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        upstream = socket.create_connection(("db", 5432))

        def copy(source, target):
            while data := source.recv(65536):
                target.sendall(data)

        thread = threading.Thread(target=copy, args=(self.request, upstream))
        thread.start()
        copy(upstream, self.request)
        thread.join()


socketserver.ThreadingTCPServer(("0.0.0.0", 55432), Handler).serve_forever()
