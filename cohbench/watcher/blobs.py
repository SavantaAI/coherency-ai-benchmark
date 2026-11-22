"""Content-addressed blob store: blobs/<first two hex>/<sha256>."""

from __future__ import annotations

import hashlib
import os
import tempfile


class BlobStore:
    def __init__(self, out_dir: str) -> None:
        self.dir = os.path.join(out_dir, "blobs")
        os.makedirs(self.dir, exist_ok=True)

    def path(self, sha: str) -> str:
        return os.path.join(self.dir, sha[:2], sha)

    def has(self, sha: str) -> bool:
        return os.path.exists(self.path(sha))

    def get(self, sha: str) -> bytes:
        with open(self.path(sha), "rb") as f:
            return f.read()

    def put(self, sha: str, data: bytes) -> None:
        dest = self.path(sha)
        if os.path.exists(dest):
            return
        if hashlib.sha256(data).hexdigest() != sha:
            raise ValueError("blob content does not match its hash")
        d = os.path.dirname(dest)
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, dest)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
