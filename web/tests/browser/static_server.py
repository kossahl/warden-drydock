import os
from dataclasses import replace
from pathlib import Path
import sys
from threading import Thread
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[3]
DIST = ROOT / "web" / "dist"
sys.path.insert(0, str(ROOT))

from warden_drydock.hosted.http.application import SliceApplication, SyntheticProvider
from warden_drydock.hosted.operations.server import Handler


class BrowserTestHandler(Handler):
    def do_POST(self):
        request = urlsplit(self.path)
        if request.path == "/__test_hide_atlas_record__":
            query = parse_qs(request.query, keep_blank_values=True)
            try:
                campaign_id = query["campaign_id"][0]
                revision_id = query["revision_id"][0]
                record_id = query["record_id"][0]
            except (KeyError, IndexError):
                self.send_error(400)
                return
            bundle = self.application.atlas_repository.get(campaign_id, revision_id)
            self.application.atlas_repository.replace(replace(
                bundle,
                records=tuple(record for record in bundle.records if record.record_id != record_id),
                edges=tuple(
                    edge for edge in bundle.edges
                    if edge.source_record_id != record_id and edge.target_record_id != record_id
                ),
            ))
            self.send_response(204)
            self.end_headers()
            return
        if request.path != "/__test_shutdown__":
            return super().do_POST()
        self.send_response(204)
        self.end_headers()
        Thread(target=self.server.shutdown, daemon=True).start()


if __name__ == "__main__":
    from http.server import ThreadingHTTPServer

    os.environ["DRYDOCK_ALLOWED_HOSTS"] = "127.0.0.1:4173"
    os.environ["DRYDOCK_STATIC"] = str(DIST)
    BrowserTestHandler.application = SliceApplication(provider=SyntheticProvider())
    BrowserTestHandler.csrf_secret = "a" * 64
    ThreadingHTTPServer(("127.0.0.1", 4173), BrowserTestHandler).serve_forever()
