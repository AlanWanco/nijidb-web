"""Connection settings shared by news archive clients."""

from __future__ import annotations

from typing import Any


def pin_r2_client_to_ipv4(client: Any) -> None:
    """Bind this client's direct urllib3 connections to IPv4.

    Botocore's Config has no source-address option. Set the pool option before
    the first request so the SDK can skip unreachable AAAA addresses without
    changing process-wide DNS resolution, image requests, or host networking.
    """
    manager = client._endpoint.http_session._manager
    manager.connection_pool_kw["source_address"] = ("0.0.0.0", 0)
