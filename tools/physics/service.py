"""Single-operator loopback physics service, binary poses and durable commands."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import time
from pathlib import Path

from aiohttp import web

from tools.physics.runtime import Conflict, Runtime


def make_app(bundle: Path, database: Path, port=8793, dist: Path | None = None):
    runtime = Runtime(bundle, database)
    sockets = set()
    health = {"error": None}
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {f"http://{host}" for host in hosts} | {"http://127.0.0.1:5173", "http://localhost:5173", "http://127.0.0.1:8787", "http://localhost:8787"}

    @web.middleware
    async def boundary(request, handler):
        if request.host not in hosts or (request.headers.get("Origin") and request.headers["Origin"] not in origins):
            raise web.HTTPForbidden(text="Use the local physics workspace")
        if request.method == "POST" and request.headers.get("X-OI-Physics") != "1":
            raise web.HTTPForbidden(text="Explicit physics client header required")
        try:
            response = await handler(request)
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response
        except Conflict as exc:
            return web.json_response({"error": str(exc)}, status=409)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    app = web.Application(middlewares=[boundary], client_max_size=16_384)

    async def manifest(_request):
        return web.json_response(runtime.manifest)

    async def status(_request):
        return web.json_response({**runtime.status(), "workerError": health["error"]})

    async def command(request):
        if health["error"]:
            raise ValueError("Worker stopped; inspect the service and restart before commanding")
        return web.json_response(runtime.command(await request.json()))

    async def frame(request):
        return web.Response(body=runtime.packet(int(request.match_info["step"])), content_type="application/octet-stream")

    async def history(request):
        return web.json_response(runtime.history(int(request.query.get("after", "0"))))

    async def replay(request):
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"start", "end"}:
            raise ValueError("Replay requires start and end steps")
        # A separate SQLite reader/snapshot and MjData keep replay off the worker.
        def verify():
            reader = Runtime(bundle, database)
            try:
                return reader.replay(body["start"], body["end"])
            finally:
                reader.close()
        if app["replaying"]:
            raise ValueError("A verification is already running")
        app["replaying"] = True
        try:
            return web.json_response(await asyncio.to_thread(verify))
        finally:
            app["replaying"] = False

    async def stream(request):
        if len(sockets) >= 8:
            raise web.HTTPServiceUnavailable(text="Viewer limit reached")
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1024)
        await ws.prepare(request)
        sockets.add(ws)
        try:
            await ws.send_bytes(runtime.packet())
            async for _message in ws:
                await ws.close(code=1008, message=b"Read-only pose stream; use commands API")
        finally:
            sockets.discard(ws)
        return ws

    async def asset(request):
        name = request.match_info["name"]
        # No user-provided MJCF uploads, remote fetches, or arbitrary filesystem paths.
        if name not in runtime.manifest["checksums"] or not (name.endswith(".glb") or name in ("tileset.json", "provenance.json")):
            raise web.HTTPNotFound()
        return web.FileResponse(bundle / name)

    async def page(request):
        if dist is None or not (dist / "index.html").is_file():
            return web.Response(text="Build the workbench with npm run build, or open http://127.0.0.1:5173/?physics", status=503)
        name = request.match_info.get("name", "")
        path = (dist / name).resolve()
        if not path.is_relative_to(dist.resolve()):
            raise web.HTTPNotFound()
        if path.is_file():
            return web.FileResponse(path)
        return web.FileResponse(dist / "index.html")

    async def worker():
        previous, debt = time.monotonic(), 0.0
        while True:
            await asyncio.sleep(.01)
            now = time.monotonic()
            try:
                state = runtime.status()
                if state["running"]:
                    debt += now-previous
                    steps = min(100, math.floor(debt / runtime.model.opt.timestep))
                    if steps:
                        runtime.tick(steps)
                        debt -= steps * runtime.model.opt.timestep
                else:
                    debt = 0
                previous = now
            except Exception as exc:
                health["error"] = str(exc)
                return

    async def publish():
        last = -1
        while True:
            await asyncio.sleep(1/30)
            packet = runtime.packet()
            step = int.from_bytes(packet[8:12], "little")
            if step == last:
                continue
            last = step
            for ws in tuple(sockets):
                try:
                    await asyncio.wait_for(ws.send_bytes(packet), timeout=.1)
                except (TimeoutError, ConnectionError, RuntimeError):
                    sockets.discard(ws)
                    await ws.close()

    async def lifecycle(_app):
        tasks = [asyncio.create_task(worker()), asyncio.create_task(publish())]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for ws in tuple(sockets):
                await ws.close(code=1001, message=b"Service stopping")
            runtime.close()

    app["replaying"] = False
    app.cleanup_ctx.append(lifecycle)
    prefix = "/physics/api"
    app.router.add_get(prefix + "/manifest", manifest)
    app.router.add_get(prefix + "/status", status)
    app.router.add_get(prefix + "/history", history)
    app.router.add_get(prefix + "/frames/{step}", frame)
    app.router.add_get(prefix + "/stream", stream)
    app.router.add_post(prefix + "/commands", command)
    app.router.add_post(prefix + "/replay", replay)
    app.router.add_get("/physics/bundle/{name:.*}", asset)
    app.router.add_get("/{name:.*}", page)
    return app


@contextlib.contextmanager
def exclusive_worker(database):
    """Only one wall-clock worker per database; the OS releases on hard exit."""
    path = Path(str(database.resolve()) + ".worker-lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt
                if path.stat().st_size == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("Another physics worker owns this database") from exc
        yield


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--database", type=Path, default=Path(".astra/physics/runtime.sqlite"))
    parser.add_argument("--port", type=int, default=8793)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Port must be 1024..65535")
    with exclusive_worker(args.database):
        app = make_app(args.bundle.resolve(), args.database.resolve(), args.port, Path(__file__).resolve().parents[2] / "dist")
        web.run_app(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
