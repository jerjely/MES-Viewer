"""
Runs the self-test of a built or installed MES Viewer and fails (exit code 1) unless it passed.

    python install/run_selftest.py <path to MES-Viewer executable> <work folder>

Used by .github/workflows/build-installers.yml; works the same on Windows, macOS and Linux.
The app writes <work folder>/selftest.txt whose first line starts with "OK" only if every check
passed; its exit code must be 0 as well.
"""
import os
import subprocess
import sys


def _utf8_console():
    """The report contains µ and Δ; Windows consoles default to cp1252, which cannot print them."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(app, folder):
    _utf8_console()
    os.makedirs(folder, exist_ok=True)
    report = os.path.join(folder, "selftest.txt")
    if os.path.exists(report):
        os.remove(report)
    try:
        code = subprocess.run([os.path.abspath(app), "--selftest", os.path.abspath(folder)],
                              timeout=900).returncode
    except subprocess.TimeoutExpired:
        sys.exit("The MES Viewer self-test did not finish within 15 minutes")
    text = open(report, encoding="utf-8").read() if os.path.exists(report) else \
        f"(no selftest.txt was written; exit code {code})"
    print(text)
    if code != 0 or not text.startswith("OK"):
        sys.exit(f"The MES Viewer self-test FAILED (exit code {code})")
    print("Self-test passed.")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
