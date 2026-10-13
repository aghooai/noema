"""files.upload with file objects: the sync client streams a seekable file in chunks (and resends all of it on a
retry); the async client never reads on the event loop. Multipart fields always precede the file part."""

from __future__ import annotations

import io
import re
import threading

import httpx
import respx

from aghoo import Aghoo, AsyncAghoo
from support import respond

CONTENT = b"%PDF-" + bytes(range(256)) * 600  # 153 605 bytes: several httpx chunks


class Recording(io.BytesIO):
    """A seekable file that records the size of every read and the thread it ran on."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.sizes: list[int | None] = []
        self.threads: set[int] = set()

    def read(self, size: int | None = -1, /) -> bytes:
        self.sizes.append(size)
        self.threads.add(threading.get_ident())
        return super().read(size)


class Pipe(io.RawIOBase):
    """A non-seekable stream (e.g. a pipe): it can be read only once."""

    def __init__(self, data: bytes) -> None:
        self._left = data

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: memoryview) -> int:  # type: ignore[override]
        n = min(len(buffer), len(self._left))
        buffer[:n], self._left = self._left[:n], self._left[n:]
        return n


def parts(request: httpx.Request) -> tuple[list[str], bytes]:
    body = request.read()
    names = re.findall(rb'Content-Disposition: form-data; name="([^"]+)"', body)
    return [n.decode() for n in names], body


def test_a_seekable_file_is_streamed_and_resent_whole_on_retry(
    mock: respx.MockRouter, client: Aghoo, sleeps: list[float]
) -> None:
    route = mock.post("/v1/files").mock(side_effect=[respond("createTurn.500"), respond("uploadFile.202")])
    f = Recording(CONTENT)
    f.seek(100)  # sent from offset 0 regardless
    out = client.files.upload(f, filename="a.pdf", session_id="ses_1", media_type="application/pdf")
    assert out.status == "quarantined"
    assert route.call_count == 2
    assert len(sleeps) == 1
    keys = {c.request.headers["idempotency-key"] for c in route.calls}
    assert len(keys) == 1
    for call in route.calls:
        names, body = parts(call.request)
        assert names == ["purpose", "session_id", "file"]
        assert CONTENT in body
    assert -1 not in f.sizes  # read in bounded chunks, never all at once
    assert None not in f.sizes
    assert max(s for s in f.sizes if s is not None) < len(CONTENT)


def test_a_non_seekable_file_is_read_once_and_retried_from_memory(
    mock: respx.MockRouter, client: Aghoo, sleeps: list[float]
) -> None:
    route = mock.post("/v1/files").mock(side_effect=[respond("createTurn.500"), respond("uploadFile.202")])
    client.files.upload(io.BufferedReader(Pipe(CONTENT)), filename="a.pdf")
    assert route.call_count == 2
    assert all(CONTENT in parts(c.request)[1] for c in route.calls)


async def test_async_upload_reads_the_file_off_the_event_loop(mock: respx.MockRouter, aclient: AsyncAghoo) -> None:
    route = mock.post("/v1/files").mock(side_effect=lambda r: respond("uploadFile.202"))
    f = Recording(CONTENT)
    await aclient.files.upload(f, filename="a.pdf", session_id="ses_1")
    assert threading.get_ident() not in f.threads  # read in a worker thread
    names, body = parts(route.calls[0].request)
    assert names == ["purpose", "session_id", "file"]
    assert CONTENT in body
