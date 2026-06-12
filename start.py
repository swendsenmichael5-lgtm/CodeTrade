#!/usr/bin/env python3
"""
One-command launcher: starts the trading bot AND the pixel dashboard,
then opens the dashboard in your browser.

    python3 start.py

Press Ctrl+C once to stop everything cleanly.
"""

import signal
import subprocess
import sys
import time
import webbrowser

DASHBOARD_PORT = 8000


def _on_terminate(*_):
    # Treat a polite kill (SIGTERM) like Ctrl+C so children get cleaned up.
    raise KeyboardInterrupt


def main() -> None:
    signal.signal(signal.SIGTERM, _on_terminate)
    here = sys.path[0]  # folder this script lives in
    python = sys.executable or "python3"

    print("[start] launching bot...")
    bot = subprocess.Popen([python, "bot.py"], cwd=here)

    print("[start] launching dashboard...")
    dash = subprocess.Popen([python, "dashboard.py", str(DASHBOARD_PORT)],
                            cwd=here)

    time.sleep(1.5)  # give the server a moment before opening the browser
    url = f"http://localhost:{DASHBOARD_PORT}"
    print(f"[start] opening {url} (if it doesn't open, paste it into your "
          f"browser)")
    webbrowser.open(url)

    print("[start] running — press Ctrl+C to stop both.")
    try:
        # If either process dies on its own, shut the other one down too.
        while True:
            if bot.poll() is not None:
                print("[start] bot exited; stopping dashboard")
                break
            if dash.poll() is not None:
                print("[start] dashboard exited; stopping bot")
                break
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[start] stopping...")
    finally:
        for proc in (bot, dash):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        print("[start] all stopped — your progress is saved in state.json.")


if __name__ == "__main__":
    main()
