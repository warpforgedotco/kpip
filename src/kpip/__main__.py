import os
import sys

# Run from source as ``python -m kpip``, the working directory comes first on
# sys.path, and kpip reads what is installed for the Python running it from
# sys.path: a directory of *.dist-info, as a test's workspace is, would pass
# for installed packages. As pip, it is taken off. The binary has no such
# entry.
if sys.path and sys.path[0] in ("", os.getcwd()):
    sys.path.pop(0)

if __name__ == "__main__":
    from kpip.cli.entrypoint import console_main

    console_main()
