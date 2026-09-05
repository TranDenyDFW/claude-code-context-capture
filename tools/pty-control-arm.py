"""TTY control arm: does a configured statusLine actually run when the session
IS interactive?

The desktop host produces zero genuine samples. That is an ABSENCE, and an
absence cannot separate "this entrypoint declines to run the script" from "the
script is broken on this machine". This is the paired positive control, and the
README's status-line section depends on it.

Procedure:
  1. spawn `claude` on a real ConPTY, so process.stdout.isTTY is true and the
     interactivity predicate takes the interactive branch
  2. answer the workspace trust dialog (option 1 is preselected, Enter confirms).
     Interactive sessions get NO automatic session-trust grant, unlike
     non-interactive ones, so skipping this produces a false zero that looks
     exactly like the finding
  3. let the prompt screen mount and sit there. No prompt is sent: the status
     line runs once at session start, so a turn is not needed and the API cost
     stays at zero
  4. kill the child unconditionally, then read the capture file

  python tools/pty-control-arm.py              run the arm
  python tools/pty-control-arm.py --self-test  check the screen matcher only

Requires pywinpty. Reads happen on a daemon thread because pywinpty's read()
blocks; the main thread owns the deadline and the kill. Budget: one spawn, 75
seconds, killed by pid in the finally block whatever happens.

Exit 0 if the status line ran, 1 if it did not, 2 if the arm could not run.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAPTURE = os.path.join(ROOT, 'data', 'raw', 'statusline.ndjson')
TRUST_WAIT = 20
SETTLE_WAIT = 35
HARD_DEADLINE = 75
ESC = chr(27)


def strip_ansi(s):
    s = re.sub(ESC + r'\[[0-9;?]*[a-zA-Z]', '', s)
    s = re.sub(ESC + r'\][^\x07]*\x07', '', s)
    return re.sub(ESC + r'[=>()][0-9A-Za-z]?', '', s)


def matches(blob, *needles):
    """Whitespace-insensitive search of a pty screen.

    Strips ANSI BEFORE collapsing whitespace. An earlier version searched the
    RAW stream, where escape sequences sit between the words, so it reported
    "not found" for text that was plainly rendered. The run then silently
    skipped the trust step this gates and recorded a confident zero. A check
    that fails in the "could not run" direction while printing a negative is
    the failure mode this whole file exists to avoid, so it is self-tested.
    """
    flat = re.sub(r'\s+', '', strip_ansi(blob))
    return all(re.sub(r'\s+', '', n) in flat for n in needles)


def self_test():
    checks = []

    def add(name, ok, detail=''):
        checks.append((name, ok, detail))

    sample = (ESC + '[1t' + ESC + '[c' + ESC + '[?1004h' + '\xbb1.'
              + ESC + '[32mYes, I ' + ESC + '[0mtrust' + ESC + '[1m this folder'
              + ESC + '[0m\r\n2. No, exit')

    add('finds text split by ANSI codes', matches(sample, 'trust this folder'))
    add('finds text across a line break', matches(sample, 'No, exit'))
    add('tolerates collapsed whitespace', matches(sample, 'trustthisfolder'))
    # Negative control: a matcher that always returned True would pass every
    # check above, so assert it can say no.
    add('reports absent text as absent (gate can fail)',
        not matches(sample, 'banana not on this screen'))
    add('an empty screen matches nothing', not matches('', 'trust this folder'))
    # The exact regression: the old matcher searched the raw stream.
    old = re.sub(r'\s+', '', 'trust this folder') in re.sub(r'\s+', '', sample)
    add('the OLD raw-stream matcher would have missed it', not old,
        'if this fails the fixture no longer reproduces the bug')

    bad = 0
    for name, ok, detail in checks:
        if not ok:
            bad += 1
        print('{}  {}{}'.format('PASS' if ok else 'FAIL', name,
                            '' if ok else '  [' + detail + ']'))
    print(f'SELF-TEST PASS ({len(checks)} checks)' if bad == 0
          else f'SELF-TEST FAIL ({bad}/{len(checks)} failed)')
    return 0 if bad == 0 else 1


def run_arm():
    try:
        from winpty import PtyProcess
    except ImportError:
        print('pywinpty is not installed; the arm cannot run.')
        return 2

    chunks = []
    stop = threading.Event()

    def pump(proc):
        while not stop.is_set():
            try:
                data = proc.read()
            except (EOFError, OSError):
                return
            if data:
                chunks.append(data)
            else:
                time.sleep(0.05)

    # COUNT THE ROWS, DO NOT TEST FOR THE FILE. This arm exists to answer whether a desktop-style
    # session ever invokes the status line, and the file it watches ALREADY EXISTS on any machine
    # that has run the status line even once. So `os.path.exists` was true before the spawn, the
    # settle loop broke instantly, and the run printed "capture file APPEARED" and exited 0 having
    # measured nothing. The existence test drove the exit code too, so the experiment reported
    # success on every machine with a sample file.
    before = _rows()
    print('genuine rows BEFORE:', before)
    started = time.time()
    p = PtyProcess.spawn(['claude'], cwd=ROOT, dimensions=(40, 130))
    child_pid = p.pid
    print('spawned claude, pid', child_pid)

    threading.Thread(target=pump, args=(p,), daemon=True).start()

    try:
        trusted = False
        while time.time() - started < TRUST_WAIT:
            if matches(''.join(chunks), 'trust this folder'):
                time.sleep(0.7)
                p.write('\r')
                trusted = True
                print('trust dialog answered at t+%.1fs' % (time.time() - started))
                break
            time.sleep(0.25)
        if not trusted:
            print(f'trust dialog never appeared within {TRUST_WAIT}s '
                  '(already trusted, or a different first screen)')

        settle_until = time.time() + SETTLE_WAIT
        while time.time() < settle_until and time.time() - started < HARD_DEADLINE:
            if _rows() > before:
                print('a NEW genuine row appeared at t+%.1fs' % (time.time() - started))
                time.sleep(2)
                break
            time.sleep(0.5)
    finally:
        stop.set()
        try:
            p.terminate(force=True)
        except PermissionError as exc:
            # WinError 5 on this machine. Reported and then handled: the taskkill below is the
            # fallback, and the liveness check after it is what proves the child actually went.
            print('terminate raised, falling back to taskkill:', exc)
        except Exception as exc:
            print('terminate raised, falling back to taskkill:', exc)
        # Belt and braces: terminate did not take on the first run here and
        # left an orphaned claude.exe behind.
        subprocess.run(['taskkill', '/PID', str(child_pid), '/T', '/F'],
                       capture_output=True)
        time.sleep(1)
        alive = subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             f"(Get-Process -Id {child_pid} -ErrorAction SilentlyContinue) -ne $null"],
            capture_output=True, encoding='utf8')
        print('child still alive after kill:', alive.stdout.strip())

    plain = strip_ansi(''.join(chunks))
    print('=== last screen text ===')
    for line in [row.rstrip() for row in plain.replace('\r\n', '\n').split('\n')][-25:]:
        if line.strip():
            print('  ' + line[:126])

    print('=== result ===')
    after = _rows()
    print('genuine rows AFTER:', after, '(before:', before, ')')
    if after > before:
        print(f'{after - before} NEW genuine row(s). The status line ran in this arm.')
        return 0
    print('NO NEW GENUINE ROW. The status line did not run in this arm.')
    return 1


def _rows():
    """Genuine status-line samples currently on disk.

    Genuine, not total: `probe: true` marks a row this repo's own tooling wrote, and counting those
    would let the experiment satisfy itself. Missing file is zero, which is a real answer here.
    """
    try:
        with open(CAPTURE, encoding='utf-8') as fh:
            n = 0
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    if json.loads(line).get('probe') is False:
                        n += 1
                except ValueError:
                    continue
            return n
    except OSError:
        return 0


def main(argv=None):
    """Arguments, because ANY argument used to run the experiment.

    `sys.exit(self_test() if '--self-test' in sys.argv else run_arm())` meant `--help` spawned a
    real Claude Code session and answered its trust dialog. A tool whose help text is a side effect
    is a tool nobody can safely inspect.
    """
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--run', action='store_true',
                    help='SPAWNS A REAL CLAUDE CODE SESSION in a pty and answers its trust dialog')
    ap.add_argument('--self-test', action='store_true', help='check the parsing, spawn nothing')
    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    if args.run:
        return run_arm()
    ap.print_help()
    print()
    print('Nothing was run. This experiment spawns a real session, so it needs --run.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
