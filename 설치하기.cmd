@echo off
setlocal
rem Housecatch blog tool installer: runs install\install.ps1 next to this file.
rem Keep this file ASCII with CRLF line endings; cmd reads batch files in the console code page.
set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%~dp0install\install.ps1" goto notextracted
"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0install\install.ps1" %*
set "CODE=%ERRORLEVEL%"
pause
exit /b %CODE%

:notextracted
rem The zip was opened without extracting it. The Korean notice is written as \u escapes to keep this file ASCII.
"%PS%" -NoProfile -Command "Write-Host ([regex]::Unescape('\uC124\uCE58 \uD30C\uC77C\uC744 \uCC3E\uC9C0 \uBABB\uD588\uC2B5\uB2C8\uB2E4. \uC555\uCD95 \uD30C\uC77C\uC744 \uB9C8\uC6B0\uC2A4 \uC624\uB978\uCABD \uB2E8\uCD94\uB85C \uB204\uB974\uACE0 \u2018\uBAA8\uB450 \uC555\uCD95 \uD480\uAE30\u2019\uB97C \uACE0\uB978 \uB4A4, \uD480\uB9B0 housecatch \uD3F4\uB354\uC5D0\uC11C \uC124\uCE58\uD558\uAE30.cmd\uB97C \uB2E4\uC2DC \uB450 \uBC88 \uB20C\uB7EC \uC8FC\uC138\uC694.')) -ForegroundColor Yellow"
pause
exit /b 1
