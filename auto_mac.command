#!/bin/bash
# PS Remover folder watcher for macOS: double-click to open it.
# It watches the photo folder saved in the window and removes the common area
# from every photo that arrives. Close the window to stop.
exec /bin/bash "$(dirname "$0")/run_mac.command" watch "$@"
