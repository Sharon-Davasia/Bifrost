"""LAN address detection, pairing URLs and the terminal startup banner.

IP selection order
------------------
1. ``BIFROST_HOST_IP`` manual override (VPN / Hyper-V / WSL / multi-adapter).
2. UDP route detection towards ``10.255.255.255`` (no packets are sent).
3. ``socket.gethostbyname(socket.gethostname())``.
4. ``127.0.0.1`` with a warning that remote devices may not be reachable.

The pairing URL always carries the PIN in the **fragment**, never the query
string, so the PIN never reaches the HTTP server or its logs.
"""

from __future__ import annotations

import ipaddress
import socket
import sys
from typing import TextIO

from config import Settings

LOOPBACK_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})

#: Sent to by the UDP probe; nothing is transmitted, it only selects an interface.
_ROUTE_PROBE_TARGET = ("10.255.255.255", 1)


def is_usable_remote_ip(value: str) -> bool:
    """True when ``value`` is a routable, non-loopback, non-link-local IPv4/IPv6."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (
        address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
        or address.is_reserved
    )


def _udp_detect() -> str | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(0.5)
        sock.connect(_ROUTE_PROBE_TARGET)
        candidate = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()
    return candidate if is_usable_remote_ip(candidate) else None


def _hostname_detect() -> str | None:
    try:
        candidate = socket.gethostbyname(socket.gethostname())
    except OSError:
        return None
    return candidate if is_usable_remote_ip(candidate) else None


def detect_lan_ip(override: str | None = None) -> tuple[str, list[str]]:
    """Return ``(advertised_ip, warnings)`` following the documented priority."""
    warnings: list[str] = []

    if override:
        if not is_usable_remote_ip(override):
            warnings.append(
                f"BIFROST_HOST_IP={override} is not a routable address; "
                "remote devices may not be able to connect."
            )
        return override, warnings

    detected = _udp_detect()
    if detected:
        return detected, warnings

    detected = _hostname_detect()
    if detected:
        return detected, warnings

    warnings.append(
        "No LAN address could be detected. Remote devices will not be able to "
        "connect. Set BIFROST_HOST_IP to this machine's LAN IP address."
    )
    return "127.0.0.1", warnings


def format_host(ip: str) -> str:
    """Bracket IPv6 literals so they can be embedded in a URL."""
    if ":" in ip and not ip.startswith("["):
        return f"[{ip}]"
    return ip


def build_urls(ip: str, port: int, pin: str) -> dict[str, str]:
    """Return the URLs advertised in the banner and QR code."""
    host = format_host(ip)
    localhost = "localhost"
    return {
        "local": f"http://{localhost}:{port}/",
        "lan": f"http://{host}:{port}/",
        "pairing": f"http://{host}:{port}/#pin={pin}",
    }


def render_qr(text: str, *, border: int = 1) -> str:
    """Render ``text`` as a compact terminal QR code using half blocks."""
    import qrcode
    from qrcode.constants import ERROR_CORRECT_L

    qr = qrcode.QRCode(border=border, error_correction=ERROR_CORRECT_L)
    qr.add_data(text)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    width = len(matrix[0]) if matrix else 0
    lines: list[str] = []
    for y in range(0, len(matrix), 2):
        top_row = matrix[y]
        bottom_row = matrix[y + 1] if y + 1 < len(matrix) else [False] * width
        chars: list[str] = []
        for top, bottom in zip(top_row, bottom_row, strict=False):
            if top and bottom:
                chars.append("\u2588")  # full block
            elif top:
                chars.append("\u2580")  # upper half block
            elif bottom:
                chars.append("\u2584")  # lower half block
            else:
                chars.append(" ")
        lines.append("".join(chars))
    return "\n".join(lines)


def _banner_lines(
    settings: Settings,
    *,
    pin: str,
    ip: str,
    urls: dict[str, str],
    warnings: list[str],
    qr_block: str | None,
) -> list[str]:
    max_upload = _human_bytes(settings.max_upload_bytes)
    rule = "=" * 62
    lines = [
        "",
        rule,
        "  BIFROST - local file & text sharing".ljust(len(rule) - 2) + "  ",
        rule,
        "  Status          : running (1 worker, plain HTTP)",
        f"  On this machine : {urls['local']}",
        f"  On your network : {urls['lan']}",
        f"  Pairing PIN     : {pin}",
        f"  Pairing URL     : {urls['pairing']}",
        f"  Storage folder  : {settings.data_dir}",
        f"  Max upload size : {max_upload}",
    ]
    if warnings:
        lines.append("  Warnings:")
        for warning in warnings:
            lines.append(f"    ! {warning}")
    if qr_block:
        lines.extend(
            [
                "",
                "  Scan this code with your phone camera to pair:",
                "",
            ]
        )
        lines.extend(f"  {line}" for line in qr_block.splitlines())
    lines.extend(
        [
            "",
            "  Keep this terminal open while other devices are connected.",
            "  Press Ctrl+C to stop Bifrost.",
            rule,
            "",
        ]
    )
    return lines


def _human_bytes(value: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.0f} {unit}" if size >= 10 else f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def print_banner(
    settings: Settings,
    *,
    pin: str,
    ip: str,
    warnings: list[str] | None = None,
    stream: TextIO | None = None,
) -> None:
    """Print the startup banner (never routed through the logging system)."""
    stream = stream or sys.stdout
    warnings = warnings or []
    urls = build_urls(ip, settings.port, pin)

    qr_block: str | None = None
    try:
        qr_block = render_qr(urls["pairing"])
    except ImportError:  # pragma: no cover - qrcode is a hard dependency
        qr_block = None
    except Exception:
        # Unicode rendering problems must never prevent the server from running.
        qr_block = None

    try:
        text = "\n".join(
            _banner_lines(
                settings, pin=pin, ip=ip, urls=urls, warnings=warnings, qr_block=qr_block
            )
        )
        stream.write(text + "\n")
        stream.flush()
    except (UnicodeEncodeError, OSError):
        # Console cannot render the block glyphs: plain text fallback.
        fallback = [
            "",
            "BIFROST is running.",
            f"  Local URL   : {urls['local']}",
            f"  Network URL : {urls['lan']}",
            f"  Pairing PIN : {pin}",
            f"  Pairing URL : {urls['pairing']}",
            "",
        ]
        fallback.extend(f"  ! {warning}" for warning in warnings)
        fallback.append("")
        try:
            stream.write("\n".join(fallback) + "\n")
            stream.flush()
        except (UnicodeEncodeError, OSError):  # pragma: no cover - last resort
            pass
