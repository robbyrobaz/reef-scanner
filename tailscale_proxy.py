#!/usr/bin/env python3
"""
Tiny reverse proxy for Tailscale HTTPS URL.
- /reef/*  → reef scanner on port 8891
- /*       → openclaw gateway on port 18789
"""
import asyncio
from asyncio import start_server

REEF_PORT = 8891
GATEWAY_PORT = 18789
PROXY_PORT = 7891

async def pipe(reader, writer):
    try:
        while True:
            data = await reader.read(8192)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()

async def handle(client_reader, client_writer):
    try:
        # Read and parse request line
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

        # Route /reef/* to reef scanner
        if path.startswith(b"/reef"):
            target_path = path[len(b"/reef"):] or b"/"
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

        # Reconstruct request
        new_req = method + b" " + target_path + b" " + version + b"\r\n"
        new_req += b"".join(headers_list)
        if not any(h.lower().startswith(b"host:") for h in headers_list):
            new_req += b"Host: 127.0.0.1:" + str(target_port).encode() + b"\r\n"
        new_req += b"\r\n"
        if body:
            new_req += body

        # Send to target
        target_writer.write(new_req)
        await target_writer.drain()

        # Stream response back
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
    print(f"  /reef/*  -> reef scanner   (:{REEF_PORT})")
    print(f"  /*       -> openclaw gateway (:{GATEWAY_PORT})")
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    asyncio.run(main())
