@echo off
rem Same as "Start Beam Profiler.bat", but other computers on the lab network
rem can open the page at  http://<this PC's IP address>:8765/
rem There is no password: use this on a trusted network only.
call "%~dp0Start Beam Profiler.bat" --lan
