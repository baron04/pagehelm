"""Loopback TCP relay from the Agent's network namespace to the evaluation provider gateway.

The product accepts a plain-HTTP provider only on loopback. The evaluation gateway, which
owns the attempt ledger and the provider credential, runs on the evaluation host. This relay
shares the Agent container's network namespace and forwards bytes unchanged, so the product
keeps its ordinary provider path and the harness keeps every request. It parses nothing,
retries nothing and logs nothing.
"""
from __future__ import annotations

import asyncio
import os
import sys


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _serve(listen_port: int, target_host: str, target_port: int) -> None:
    async def handle(client_reader, client_writer):
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                target_host, target_port)
        except OSError:
            client_writer.close()
            return
        await asyncio.gather(_pipe(client_reader, upstream_writer),
                             _pipe(upstream_reader, client_writer))

    server = await asyncio.start_server(handle, "127.0.0.1", listen_port)
    async with server:
        await server.serve_forever()


def main() -> int:
    listen = int(os.environ.get("P74_RELAY_LISTEN_PORT", "18080"))
    host = os.environ.get("P74_GATEWAY_HOST", "host.docker.internal")
    port = int(os.environ["P74_GATEWAY_PORT"])
    print(f"relay 127.0.0.1:{listen} -> {host}:{port}", flush=True)
    asyncio.run(_serve(listen, host, port))
    return 0


if __name__ == "__main__":
    sys.exit(main())
