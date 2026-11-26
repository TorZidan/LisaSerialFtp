#!/usr/bin/env python3
"""Audit both Lisa clients (src/client/LisaSerialFtpClient.text and
LosSerialFtpClient.text, with the files they include) for 8-character
identifier-prefix collisions.

Lisa Pascal truncates identifiers to 8 characters and folds case, so two
identifiers with the same 8-char prefix (case-insensitive) are the same
identifier.  The audit itself is the Lisa Pascal cross-compiler's
pascal/audit_8char.py (https://github.com/TorZidan/LisaPascalCrossCompiler):
it follows the {$I ...} includes and checks against the units each program
uses, found through the boot volume's INTRINSIC.LIB.  src/client/make.sh runs
the same check before it compiles each client.

Not a test of the server: a check to run after changing a client.
Usage:
  python3 src/server/tests/audit_8char.py
Environment: LISA_PASCAL, the LisaPascalCrossCompiler folder (default: next to
this repository); LISA_BOOT, the boot volume (default: its libs/workshop3.0).
"""
import os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
CLIENT = os.path.normpath(os.path.join(HERE, '..', '..', 'client'))
LISA_PASCAL = os.environ.get('LISA_PASCAL') or os.path.normpath(
    os.path.join(HERE, '..', '..', '..', '..', 'LisaPascalCrossCompiler'))
AUDIT = os.path.join(LISA_PASCAL, 'pascal', 'audit_8char.py')
BOOT = os.environ.get('LISA_BOOT') or os.path.join(LISA_PASCAL, 'libs', 'workshop3.0')

if __name__ == '__main__':
    if not os.path.isfile(AUDIT):
        sys.exit(f'no {AUDIT} (set LISA_PASCAL)')
    status = 0
    for prog in ('LisaSerialFtpClient', 'LosSerialFtpClient'):
        print(f'=== {prog}.text')
        status |= subprocess.call([sys.executable, AUDIT, '--boot', BOOT, prog], cwd=CLIENT)
    sys.exit(status)
