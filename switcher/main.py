# main.py - compatibility entry point.
# The real daemon lives in daemon.py. Both spellings work:
#     python -m switcher.main
#     python -m switcher.daemon
from .daemon import main

if __name__ == "__main__":
    main()
