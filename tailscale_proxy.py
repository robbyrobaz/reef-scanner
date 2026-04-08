#!/usr/bin/env python3
"""
Tailscale reverse proxy — strips /reef prefix before forwarding to reef dashboard.
- /reef/* -> reef scanner (port 8891), strips /reef prefix so /reef/api -> /api
- /*      -> openclaw gateway (port 18789)
"""
import asyncio
from asyncio import start_server

REEF_PORT = 8891
GATEWAY_PORT = 18789
PROXY_PORT = 7891
REEF_PREFIX = b"/reef"

async def handle(client_reader, client_writer):
    try:
        # Read request line
        line = await client_reader.readline()
        if not line:
            client_writer.close()
            return
        parts = line.rstrip().split(b" ")
        if len(parts) != 3:
            client_writer.close()
            return
        method, path, version = parts

        # Read headers
        headers_list = []
        content_length = 0
        while True:
            h = await client_reader.readline()
            if h == b"\r\n" or not h:
                break
            headers_list.append(h)
            if h.lower().startswith(b"content-length:"):
                content_length = int(h.split(b":", 1)[1].strip())

        # Read body
        body = b""
        if content_length > 0:
            body = await client_reader.readexactly(content_length)

        # Route: strip /reef prefix for reef dashboard, pass through for gateway
        if path.startswith(REEF_PREFIX):
            target_path = path[len(REEF_PREFIX):] or b"/"
            target_port = REEF_PORT
        else:
            target_path = path
            target_port = GATEWAY_PORT

        # Connect to target
        try:
            target_reader, target_writer = await asyncio.open_connection("127.0.0.1", target_port)
        except Exception:
            client_writer.close()
            return

        # Forward request (with potentially modified path)
        new_req = method + b" " + target_path + b" " + version + b"\r\n"
        new_req += b"".join(headers_list)
        if not any(h.lower().startswith(b"host:") for h in headers_list):
            new_req += b"Host: 127.0.0.1:" + str(target_port).encode() + b"\r\n"
        new_req += b"\r\n"
        if body:
            new_req += body

        target_writer.write(new_req)
        await target_writer.drain()

        # Forward response back to client (streaming)
        try:
            while True:
                data = await target_reader.read(8192)
                if not data:
                    break
                client_writer.write(data)
                await client_writer.drain()
        except Exception:
            pass
        finally:
            target_writer.close()
    except Exception:
        pass
    finally:
        client_writer.close()

async def main():
    server = await start_server(handle, "127.0.0.1", PROXY_PORT)
    addr = server.sockets[0].getsockname()
    print(f"Proxy running on http://{addr[0]}:{addr[1]}")
    print(f"  /reef/*  -> reef scanner   (:{REEF_PORT}), strips /reef prefix")
    print(f"  /*       -> openclaw gateway (:{GATEWAY_PORT})")
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    asyncio.run(main())
