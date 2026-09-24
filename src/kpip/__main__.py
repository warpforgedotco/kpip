import os
import sys

if sys.path[0] in ("", os.getcwd()):
    sys.path.pop(0)

if not __spec__ or __spec__.parent == "":
    path = os.path.dirname(os.path.dirname(__file__))
    sys.path.insert(0, path)

if __name__ == "__main__":
    from kpip.cli.entrypoint import console_main

    console_main(
        version=None,
        location=os.path.join(os.path.dirname(__file__), "__init__.py"),
    )
