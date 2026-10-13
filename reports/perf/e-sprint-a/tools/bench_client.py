import asyncio, json, sys, time, uuid
import aiohttp, httpx, orjson
from aiobotocore.session import AioSession
from aiobotocore.config import AioConfig
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import ReadOnlyCredentials
PORT = int(sys.argv[1]); N = int(sys.argv[2]); C = 15
URL = f"http://127.0.0.1:{PORT}"
TEXT = "x" * 1800
PARAMS = {"modelId": "us.anthropic.claude-haiku-5-5", "messages": [{"role": "user", "content": [{"text": "<app_question>" + TEXT + "</app_question>"}, {"text": "Reply."}]}], "inferenceConfig": {"maxTokens": 100}, "system": [{"text": "Estimate how likely each option is"}], "outputConfig": {"effort": "low"}}
CREDS = ReadOnlyCredentials("AKIDEXAMPLE", "secret", "token" * 50)
def sign(url, body):
    req = AWSRequest(method="POST", url=url, data=body, headers={"Content-Type": "application/json"})
    SigV4Auth(CREDS, "bedrock", "us-east-1").add_auth(req)
    return dict(req.headers.items())
def lean_body():
    p = {k: v for k, v in PARAMS.items() if k != "modelId"}
    return json.dumps(p).encode()
async def run(label, one):
    for _ in range(50): await one()
    sem = asyncio.Semaphore(C)
    async def task():
        async with sem: await one()
    t0, w0 = time.process_time(), time.perf_counter()
    await asyncio.gather(*(task() for _ in range(N)))
    cpu = time.process_time() - t0
    print(f"{label:28s} cpu/call {cpu / N * 1e6:7.1f} us   wall {time.perf_counter() - w0:.2f}s")
async def main():
    s = AioSession()
    cfg = AioConfig(signature_version="v4", retries={"total_max_attempts": 1, "mode": "standard"}, max_pool_connections=64)
    async with s.create_client("bedrock-runtime", region_name="us-east-1", endpoint_url=URL, aws_access_key_id="AKIDEXAMPLE", aws_secret_access_key="secret", aws_session_token="token" * 50, config=cfg) as c:
        await run("aiobotocore converse", lambda: c.converse(**PARAMS))
    url = URL + "/model/us.anthropic.claude-haiku-5-5/converse"
    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=64)) as hc:
        async def h():
            b = lean_body(); r = await hc.post(url, content=b, headers=sign(url, b)); orjson.loads(r.content)
        await run("httpx post+sign+json", h)
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=64), skip_auto_headers={"CONTENT-TYPE"}, auto_decompress=False) as ac:
        async def a():
            b = lean_body()
            async with ac.post(url, data=b, headers=sign(url, b)) as r:
                orjson.loads(await r.read())
        await run("aiohttp post+sign+json", a)
        async def a2():
            b = orjson.dumps({"x": 1})
            async with ac.post(url, data=b, headers={"Content-Type": "application/json"}) as r:
                await r.read()
        await run("aiohttp post bare", a2)
    t0 = time.process_time()
    for _ in range(N): sign(url, lean_body())
    print(f"{'sign+json only':28s} cpu/call {(time.process_time() - t0) / N * 1e6:7.1f} us")
    t0 = time.process_time()
    for _ in range(N): lean_body()
    print(f"{'json.dumps only':28s} cpu/call {(time.process_time() - t0) / N * 1e6:7.1f} us")
asyncio.run(main())
