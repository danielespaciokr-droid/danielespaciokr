@echo off
rem PS Remover folder watcher for Windows: double-click to open it.
rem It watches the photo folder saved in the window and removes the common area
rem from every photo that arrives. Close the window to stop.
call "%~dp0run_windows.bat" watch %*
