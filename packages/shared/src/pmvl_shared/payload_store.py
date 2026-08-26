"""Content-addressed storage for raw source payloads.

Why raw payloads are kept at all
--------------------------------
A normalised value is a parser's opinion of a payload. Every factor definition
written in the future will want a field today's parser did not extract, or will
want to know whether a field was absent or merely unparsed - and neither is
recoverable from a stored float.

The alternative is reconstructing history from whatever the current parser
happens to produce, which is retroactively inventing the past. Storing the bytes
costs a few hundred kilobytes per station-day and removes that failure entirely.

Why content-addressed
---------------------
The path IS the hash, so:

* **immutability is structural.** Writing the same content twice writes the same
  path with the same bytes. Writing *different* content produces a different
  path, so nothing can be overwritten in place - there is no filename to reuse.
* **integrity is checkable without a manifest.** Re-hash the file, compare to
  the path it came from.
* **deduplication is free.** An unchanged NWS gridpoint polled hourly stores
  once, which matters when the whole point is polling often.

Why not object storage
----------------------
Because a directory tree satisfies every requirement above, and this repository
runs from a checkout with no infrastructure. A future move to S3 changes
:meth:`PayloadStore.put` and nothing else; ``raw_payload_ref`` already carries an
opaque string rather than a filesystem path.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

#: Reference scheme. Present so a stored ref remains interpretable if the
#: backing store ever changes: ``sha256/ab/cd/abcd...`` is resolvable by any
#: implementation that knows the algorithm.
REF_SCHEME = "sha256"

#: Refuse to store anything larger than this. A payload this size is a bug in
#: the caller - a full market dump rather than one observation - and silently
#: absorbing it would fill the disk over a weekend of polling.
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024


class PayloadTooLargeError(ValueError):
    """A payload exceeded the per-object ceiling."""


class PayloadIntegrityError(RuntimeError):
    """Stored bytes do not hash to the reference that named them."""


@dataclass(frozen=True)
class StoredPayload:
    """What a write produced. ``ref`` is what belongs in the database."""

    ref: str
    sha256: str
    size_bytes: int
    #: False when the content was already present. Not an error - it is the
    #: expected result of polling a source that has not changed, and the count
    #: of duplicates is itself a measure of source update cadence.
    written: bool


class PayloadStore:
    """A content-addressed store over a directory tree.

    Files are gzipped: source payloads are JSON, they compress by roughly 10x,
    and the hash is taken over the **uncompressed** bytes so the reference is a
    property of the content rather than of the compression settings.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def _path_for(self, digest: str) -> Path:
        # Two levels of fan-out. A single flat directory with a hundred thousand
        # entries is slow to list on every filesystem that matters, and polling
        # seven stations hourly reaches that within a year.
        return self._root / digest[:2] / digest[2:4] / f"{digest}.json.gz"

    @staticmethod
    def ref_for(digest: str) -> str:
        return f"{REF_SCHEME}:{digest}"

    @staticmethod
    def digest_of(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    def put(self, payload: bytes) -> StoredPayload:
        """Store bytes and return their reference.

        The write is atomic: content goes to a temporary file in the same
        directory and is then renamed. A crash mid-write therefore leaves either
        nothing or a complete object, never a truncated one that would fail
        verification later and be indistinguishable from corruption.
        """
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise PayloadTooLargeError(
                f"payload is {len(payload)} bytes, above the "
                f"{MAX_PAYLOAD_BYTES} ceiling; store a single observation, not a "
                "whole response batch"
            )

        digest = self.digest_of(payload)
        target = self._path_for(digest)
        if target.exists():
            # Already stored. Content-addressed, so the existing bytes ARE these
            # bytes; rewriting would be a no-op that risks a partial write.
            return StoredPayload(
                ref=self.ref_for(digest),
                sha256=digest,
                size_bytes=len(payload),
                written=False,
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        try:
            with os.fdopen(handle, "wb") as raw:
                # mtime=0 so identical content produces identical files. A
                # timestamp in the gzip header would make byte-for-byte
                # comparison of two stores fail for no reason.
                with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
                    gz.write(payload)
            os.replace(temp_name, target)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
        return StoredPayload(
            ref=self.ref_for(digest),
            sha256=digest,
            size_bytes=len(payload),
            written=True,
        )

    def get(self, ref: str) -> bytes:
        """Read back the payload a reference names, verifying it round-trips.

        Verification is not optional and not a debug mode. A silently corrupted
        payload would be reconstructed into a plausible normalised value, and the
        research built on it would be wrong in a way nothing else could detect.
        """
        digest = self.digest_from_ref(ref)
        path = self._path_for(digest)
        if not path.exists():
            raise FileNotFoundError(f"no payload stored for {ref}")
        with gzip.open(path, "rb") as gz:
            payload = gz.read()
        actual = self.digest_of(payload)
        if actual != digest:
            raise PayloadIntegrityError(
                f"payload at {ref} hashes to {actual[:16]}...; the stored bytes "
                "are not the bytes that were written"
            )
        return payload

    @staticmethod
    def digest_from_ref(ref: str) -> str:
        scheme, _, digest = ref.partition(":")
        if scheme != REF_SCHEME or len(digest) != 64:
            raise ValueError(f"malformed payload ref {ref!r}")
        return digest

    def exists(self, ref: str) -> bool:
        try:
            return self._path_for(self.digest_from_ref(ref)).exists()
        except ValueError:
            return False

    def verify_all(self) -> list[str]:
        """Re-hash every stored object. Empty means the store is intact.

        Reports rather than repairs: a payload that no longer matches its hash
        cannot be recovered from itself, and quietly deleting it would remove the
        evidence that anything was lost.
        """
        problems: list[str] = []
        for path in sorted(self._root.rglob("*.json.gz")):
            expected = path.stem.replace(".json", "")
            try:
                with gzip.open(path, "rb") as gz:
                    actual = self.digest_of(gz.read())
            except OSError as exc:
                problems.append(f"{path.name}: unreadable ({exc})")
                continue
            if actual != expected:
                problems.append(f"{path.name}: hashes to {actual[:16]}...")
        return problems

    def size_on_disk(self) -> int:
        return sum(p.stat().st_size for p in self._root.rglob("*.json.gz"))

    def count(self) -> int:
        return sum(1 for _ in self._root.rglob("*.json.gz"))

    def prune_unreferenced(self, referenced: set[str]) -> int:
        """Delete objects nothing points at. Callers must pass EVERY reference.

        Deliberately awkward to call, and deliberately not scheduled. Payload
        retention is the one thing in this system that cannot be rebuilt, so
        deletion is a manual act with an explicit list, never a background job
        that decides for itself what is unreachable.
        """
        digests = {self.digest_from_ref(r) for r in referenced}
        removed = 0
        for path in sorted(self._root.rglob("*.json.gz")):
            if path.stem.replace(".json", "") not in digests:
                path.unlink()
                removed += 1
        return removed

    def clear(self) -> None:
        """Remove the whole store. Tests only."""
        if self._root.exists():
            shutil.rmtree(self._root)
