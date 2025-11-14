"""Command-line interface for running the MyTube web server."""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import logging
import socket
from typing import Iterable

import uvicorn
from zeroconf import ServiceInfo, Zeroconf

from . import create_app

logger = logging.getLogger(__name__)


def _iter_candidate_addresses(host: str) -> Iterable[str]:
    if host and host not in {"0.0.0.0", "::"}:
        yield host
    try:
        hostname = socket.gethostbyname(socket.gethostname())
        if hostname:
            yield hostname
    except OSError:
        pass
    try:
        with socket.create_connection(("8.8.8.8", 80), timeout=1) as sock:
            yield sock.getsockname()[0]
    except OSError:
        pass
    yield "127.0.0.1"


def _resolve_mdns_addresses(host: str) -> list[bytes]:
    addresses: list[bytes] = []
    for candidate in _iter_candidate_addresses(host):
        try:
            addr = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if addr.is_unspecified:
            continue
        packed = addr.packed
        if packed not in addresses:
            addresses.append(packed)
    return addresses


def _register_mdns_service(host: str, port: int) -> tuple[Zeroconf, ServiceInfo] | None:
    addresses = _resolve_mdns_addresses(host)
    if not addresses:
        logger.warning("Could not determine an address to advertise via mDNS.")
        return None

    zeroconf = Zeroconf()
    info = ServiceInfo(
        type_="_http._tcp.local.",
        name="mytube._http._tcp.local.",
        addresses=addresses,
        port=port,
        server="mytube.local.",
    )

    try:
        zeroconf.register_service(info, allow_name_change=False)
        logger.info("Advertised MyTube at mytube.local via mDNS on port %s", port)
    except Exception:
        logger.exception("Failed to register mDNS advertisement for MyTube.")
        zeroconf.close()
        return None

    return zeroconf, info


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the MyTube Chromecast remote web server.")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface to bind.")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on.")
    args = parser.parse_args()

    app = create_app()
    registration = _register_mdns_service(args.host, args.port)
    try:
        uvicorn.run(app, host=args.host, port=args.port, workers=1)
    finally:
        if registration:
            zeroconf, info = registration
            with contextlib.suppress(Exception):
                zeroconf.unregister_service(info)
            zeroconf.close()


if __name__ == "__main__":  # pragma: no cover - direct execution guard
    main()
