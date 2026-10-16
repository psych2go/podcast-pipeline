#!/usr/bin/env python3
"""Run a command under a PTY so tools that require a TTY work headlessly."""
import os
import pty
import select
import sys
import time
import shutil


def main(argv):
    timeout = 0
    logfile = ''
    args = list(argv)
    while args and args[0].startswith('--'):
        if args[0] == '--':
            args = args[1:]
            break
        if args[0].startswith('--timeout='):
            timeout = int(args[0].split('=',1)[1])
            args = args[1:]
            continue
        if args[0] == '--timeout':
            timeout = int(args[1])
            args = args[2:]
            continue
        if args[0].startswith('--logfile='):
            logfile = args[0].split('=',1)[1]
            args = args[1:]
            continue
        if args[0] == '--logfile':
            logfile = args[1]
            args = args[2:]
            continue
        # unknown flag -> treat rest as command
        break
    if args and args[0] == '--':
        args = args[1:]
    if not args:
        print('usage: run_with_pty.py [--timeout N] [--logfile PATH] -- CMD...', file=sys.stderr)
        return 2
    # resolve executable
    exe = args[0]
    if '/' not in exe:
        found = shutil.which(exe)
        if not found:
            print(f'executable not found: {exe}', file=sys.stderr)
            return 127
        args[0] = found
    log = open(logfile, 'wb') if logfile else None
    pid, fd = pty.fork()
    if pid == 0:
        os.environ.setdefault('TERM', 'xterm-256color')
        os.execvp(args[0], args)
    start = time.time()
    status = 0
    try:
        while True:
            if timeout and (time.time() - start) > timeout:
                try:
                    os.kill(pid, 9)
                except ProcessLookupError:
                    pass
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass
                print('\n[run_with_pty] timeout', file=sys.stderr)
                return 124
            r, _, _ = select.select([fd], [], [], 0.5)
            if fd in r:
                try:
                    chunk = os.read(fd, 16384)
                except OSError:
                    break
                if not chunk:
                    break
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
                if log:
                    log.write(chunk)
                    log.flush()
            wpid, st = os.waitpid(pid, os.WNOHANG)
            if wpid:
                status = st
                while True:
                    r, _, _ = select.select([fd], [], [], 0.05)
                    if fd not in r:
                        break
                    try:
                        chunk = os.read(fd, 16384)
                    except OSError:
                        break
                    if not chunk:
                        break
                    sys.stdout.buffer.write(chunk)
                    if log:
                        log.write(chunk)
                break
    finally:
        if log:
            log.close()
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
