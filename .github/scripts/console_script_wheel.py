"""Write a wheel whose console script says how wide a pointer its Python has.

Used by CI to check that a console script kpip installs starts the Python it
was installed for, whatever that Python's bitness.
"""

from __future__ import annotations

import base64
import hashlib
import sys
import zipfile
from pathlib import Path

NAME = "console_script_probe"
VERSION = "1.0"
DIST_INFO = f"{NAME}-{VERSION}.dist-info"

FILES = {
    f"{NAME}.py": (
        "import struct\n"
        "import sys\n\n\n"
        "def main():\n"
        '    print(struct.calcsize("P") * 8, sys.executable)\n'
    ),
    f"{DIST_INFO}/METADATA": (
        f"Metadata-Version: 2.1\nName: console-script-probe\nVersion: {VERSION}\n"
    ),
    f"{DIST_INFO}/WHEEL": (
        "Wheel-Version: 1.0\nGenerator: ci\nRoot-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    ),
    f"{DIST_INFO}/entry_points.txt": (
        f"[console_scripts]\nconsole-script-probe = {NAME}:main\n"
    ),
}


def record_line(path: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return f"{path},sha256={digest.decode()},{len(data)}"


def main() -> None:
    destination = Path(sys.argv[1])
    destination.mkdir(parents=True, exist_ok=True)
    wheel = destination / f"{NAME}-{VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        lines = []
        for path, text in FILES.items():
            data = text.encode()
            archive.writestr(path, data)
            lines.append(record_line(path, data))
        lines.append(f"{DIST_INFO}/RECORD,,")
        archive.writestr(f"{DIST_INFO}/RECORD", "\n".join(lines) + "\n")
    print(wheel)


if __name__ == "__main__":
    main()
