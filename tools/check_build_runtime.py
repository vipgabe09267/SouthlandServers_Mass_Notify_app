"""Reject stale interpreters before installing build dependencies."""
import json
import platform
import re
import ssl
import struct
import sys

EXPECTED_PYTHON = (3, 14, 7)
MIN_OPENSSL = (3, 5, 7)
MIN_RELEASE_OPENSSL = (3, 5, 8)


def main():
    if sys.version_info[:3] != EXPECTED_PYTHON or platform.python_implementation() != "CPython":
        raise SystemExit("Build requires CPython 3.14.7; install it explicitly or update the reviewed build policy.")
    if sys.platform != "win32" or struct.calcsize("P") != 8 or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise SystemExit("Build requires Windows x64.")
    match = re.match(r"OpenSSL (\d+)\.(\d+)\.(\d+)\b", ssl.OPENSSL_VERSION)
    # CPython exposes OpenSSL 3.5.7 as (3,5,0,7,0), retaining the old OpenSSL
    # tuple layout. Compare the actual modern semantic version string.
    if match is None or tuple(map(int, match.groups())) < MIN_OPENSSL:
        raise SystemExit("Build requires supported OpenSSL 3.5.7 or later; verify the interpreter distribution.")
    if "--release" in sys.argv and tuple(map(int, match.groups())) < MIN_RELEASE_OPENSSL:
        raise SystemExit("Production release requires OpenSSL 3.5.8 or later. The official Python 3.14.7 runtime bundles 3.5.7; update the reviewed interpreter/runtime policy before releasing.")
    print(json.dumps({"python": platform.python_version(), "openssl": ssl.OPENSSL_VERSION, "platform": platform.platform()}))


if __name__ == "__main__":
    main()
