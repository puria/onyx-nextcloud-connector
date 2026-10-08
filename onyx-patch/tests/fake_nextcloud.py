"""Throwaway fake Nextcloud WebDAV server for connector end-to-end testing.

Serves a tiny in-memory tree on port 8899 (PROPFIND Depth 1 + GET). Run inside
the `fake-nextcloud` container on the Onyx compose network; the connector then
uses `http://fake-nextcloud:8899` as its server URL.
"""

import hashlib
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NC_PREFIX = "/remote.php/dav/files/me"
FILES: dict[str, dict] = {}


def add(path: str, content: bytes, fileid: str) -> None:
    FILES[path] = {
        "content": content,
        "etag": hashlib.md5(content).hexdigest(),
        "fileid": fileid,
    }


def multistatus(rel: str) -> bytes:
    direct = [p for p in FILES if p.rsplit("/", 1)[0] == rel]
    folders = set()
    for path in FILES:
        if rel and not path.startswith(rel + "/"):
            continue
        rest = path if not rel else path[len(rel) + 1 :]
        segment, _, more = rest.partition("/")
        if more:
            folders.add(f"{rel}/{segment}" if rel else segment)
    parts = [
        '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
    ]
    for folder in sorted(folders):
        parts.append(
            f"<d:response><d:href>{NC_PREFIX}/{folder}/</d:href><d:propstat><d:prop>"
            "<d:resourcetype><d:collection/></d:resourcetype></d:prop>"
            "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        )
    for path in sorted(direct):
        entry = FILES[path]
        parts.append(
            f"<d:response><d:href>{NC_PREFIX}/{path}</d:href><d:propstat><d:prop>"
            "<d:getlastmodified>Mon, 01 Jan 2024 10:00:00 GMT</d:getlastmodified>"
            f'<d:getetag>"{entry["etag"]}"</d:getetag>'
            f"<d:getcontentlength>{len(entry['content'])}</d:getcontentlength>"
            f"<d:resourcetype/><oc:fileid>{entry['fileid']}</oc:fileid>"
            "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        )
    parts.append("</d:multistatus>")
    return "".join(parts).encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code: int, body: bytes, content_type: str = "text/xml") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PROPFIND(self) -> None:
        rel = urllib.parse.unquote(self.path.split(NC_PREFIX, 1)[-1]).strip("/")
        exists = rel == "" or any(path == rel or path.startswith(rel + "/") for path in FILES)
        if not exists:
            self._send(404, b"not found", "text/plain")
            return
        self._send(207, multistatus(rel))

    def do_GET(self) -> None:
        rel = urllib.parse.unquote(self.path.split(NC_PREFIX, 1)[-1]).strip("/")
        entry = FILES.get(rel)
        if entry is None:
            self._send(404, b"gone", "text/plain")
            return
        self._send(200, entry["content"], "application/octet-stream")


if __name__ == "__main__":
    add("Documents/readme.md", b"# Fake Nextcloud\nconnector e2e test content", "901")
    add("Documents/notes/idea.txt", b"an idea worth indexing", "902")
    add("Documents/report.txt", b"quarterly numbers placeholder", "903")
    server = ThreadingHTTPServer(("0.0.0.0", 8899), Handler)
    print("fake nextcloud ready on :8899", flush=True)
    server.serve_forever()
