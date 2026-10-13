"""A minimal HTTP/1.1 keep-alive server answering every POST with a canned Converse JSON body."""
import asyncio, sys
BODY = b'{"output": {"message": {"role": "assistant", "content": [{"text": "{\\"A\\": 0.7, \\"B\\": 0.2, \\"C\\": 0.1}"}]}}, "stopReason": "end_turn", "usage": {"inputTokens": 512, "outputTokens": 12, "totalTokens": 524}, "metrics": {"latencyMs": 50}}'
RESP = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nx-amzn-RequestId: r\r\nContent-Length: %d\r\n\r\n%s" % (len(BODY), BODY)
class P(asyncio.Protocol):
    def connection_made(self, t): self.t = t; self.buf = b""
    def data_received(self, d):
        self.buf += d
        while True:
            i = self.buf.find(b"\r\n\r\n")
            if i < 0: return
            head = self.buf[:i].decode("latin1").lower()
            n = 0
            for line in head.split("\r\n"):
                if line.startswith("content-length:"): n = int(line.split(":")[1])
            if len(self.buf) < i + 4 + n: return
            self.buf = self.buf[i + 4 + n:]
            self.t.write(RESP)
async def main():
    srv = await asyncio.get_running_loop().create_server(P, "127.0.0.1", int(sys.argv[1]))
    await srv.serve_forever()
asyncio.run(main())
