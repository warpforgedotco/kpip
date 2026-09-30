import os
import sys

if sys.path[0] in ("", os.getcwd()):
    sys.path.pop(0)

if not __spec__ or __spec__.parent == "":
    path = os.path.dirname(os.path.dirname(__file__))
    sys.path.insert(0, path)

if __name__ == "__main__":
    # A compiled kpip is its own byte-compilation worker
    # (kpip.install._compile_worker), before anything a command needs.
    if sys.argv[1:2] == ["--kpip-compile-worker"]:
        from kpip.install._compile_worker import main

        main()

        sys.exit(0)

    from kpip.cli.entrypoint import console_main

    console_main(
        version=None,
        location=os.path.join(os.path.dirname(__file__), "__init__.py"),
    )
