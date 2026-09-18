"""Build the dashboard into an executable, and prove the build serves a page.

    python tools/build_exe.py                        # dist/c4x/c4x(.exe)
    python tools/build_exe.py --bundle               # dist/bundle/c4x/: exe + node + the tools
    python tools/build_exe.py --smoke --db <store>   # run the built exe against a store
    python tools/build_exe.py --smoke --bundle --db <store>   # run the BUNDLE, with a bare PATH
    python tools/build_exe.py --check-icon           # the exe's icon is the app's (Windows)
    python tools/build_exe.py --self-test            # the argv builder and the smoke plan, no build

WHAT THE EXE REPLACES: Python. Not node. The hooks and the harvester are node, the store they
write is what the exe serves, and c4x/store.py shells out to tools/*.mjs for window math. The exe
runs from inside an install and refuses to run anywhere else; see c4x/paths.py.

SO THE DOWNLOAD CARRIES NODE TOO. `--bundle` assembles dist/bundle/c4x/: the exe, node/node.exe
fetched from nodejs.org and checked against the release's own SHASUMS256, and the tools and hooks
the exe and Claude run. That folder IS an install (it holds tools/harvest.mjs, which is the marker
c4x/paths.py looks for), so it can be unzipped anywhere and needs neither Python nor node. The
plain build stays what it was: an exe for a checkout that already has node.

THE ICON IS THE APP'S, READ AT BUILD TIME. The user wants the exe to match the Claude desktop app
in a folder listing, and that icon is Anthropic's, so it is not committed to this public repo:
`find_app_icon` reads it out of the app installed on the building machine (PyInstaller's --icon
takes FILE.exe,N), the Store build first, then the non-Store one, then `C4X_ICON` for anything
else, and a build with no app gets PyInstaller's own icon, which is what CI gets. A version
resource names the file for Explorer and Task Manager.

WHY A SMOKE, NOT A BUILD LOG. PyInstaller reports success when it wrote an exe, and an exe that
imports dash lazily through `import app` (c4x/api/main.py, `_app()`) can be missing half of
plotly and still start. So the check here is the page: the shell served from the bundle, the tab
list (which imports app.py, so dash is in), and one rendered pane (which draws with plotly). A
build that passes this has everything the hook-started server has.

PYINSTALLER IS IMPORTED INSIDE build() ONLY. The suite runs --self-test on every CI leg, and the
legs install requirements.txt and requirements-dev.txt, not requirements-build.txt; a module-level
import would fail all three for a package only the build job needs. The self-test asserts that.
"""
import base64
import glob
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "c4x"
DESCRIPTION = "c4x dashboard (Claude Code context capture)"
ENTRY = Path("c4x") / "api" / "__main__.py"
# Where the desktop app's exe is, per install kind; the icon is read out of it.
APP_EXES = (
    r"C:\Program Files\WindowsApps\Claude_*\app\Claude.exe",
    r"%LOCALAPPDATA%\Programs\Claude\Claude.exe",
    r"%LOCALAPPDATA%\Programs\claude-desktop\Claude.exe",
)
# Packages the dashboard never imports that PyInstaller would still walk into on a machine that
# happens to have them: plotly and pandas import several of these optionally, and the analysis
# follows an optional import as far as the interpreter allows. Measured here: an interpreter
# with 543 packages spent over fifteen minutes analysing pyspark before this list existed. CI
# installs none of them, so the exclusion changes nothing there and keeps a developer build
# the same shape as the shipped one.
EXCLUDES = ("pyspark", "torch", "tensorflow", "matplotlib", "IPython", "ipykernel", "jupyter",
            "jupyter_client", "notebook", "nbformat", "scipy", "sklearn", "numba", "polars",
            "sympy", "PIL", "xarray", "dask", "kaleido", "statsmodels", "seaborn", "bokeh",
            "PyQt5", "PyQt6", "PySide2", "PySide6", "tkinter", "pytest", "mypy", "ruff",
            # Not pinned in constraints-ci.txt, so CI runs pandas without it and the local build
            # should not carry a hundred megabytes CI never sees.
            "pyarrow")
# WHICH NODE THE DOWNLOAD CARRIES. Pinned, because the bundle is reproducible or it is not: a
# floating "latest" would mean two zips built a week apart run different interpreters over the same
# transcripts. Long-term support line, which is what the hooks are tested against.
NODE_VERSION = "24.14.1"
NODE_BASE = "https://nodejs.org/dist"
# What goes in the folder beside the exe: everything the hooks, the harvester and the server shell
# out to, and nothing else. No tests, no frontend source, no .git.
RUNTIME_DIRS = ("tools", "hooks")
RUNTIME_FILES = ("package.json", "LICENSE", "README.md")

# The routes the smoke asks for, in order, and what each proves.
SMOKE_PLAN = (
    ("GET", "/__health__", "the server answers for the store and the port it was given"),
    ("GET", "/", "the shell is served from the bundle, not from a checkout path"),
    ("GET", "/api/tabs", "app.py imported, so dash and every tab module are in the bundle"),
    ("GET", "/api/tab/{first}/render", "one pane rendered, so plotly's package data is in"),
    ("GET", "/api/health", "the API's own health shape"),
    ("POST", "/__shutdown__", "the token in the announce line stops it"),
)


def exe_path(root: Path = ROOT, platform: str = sys.platform) -> Path:
    return root / "dist" / NAME / (f"{NAME}.exe" if platform.startswith("win") else NAME)


APPX_QUERY = "(Get-AppxPackage -Name 'Claude*' | Select-Object -First 1).InstallLocation"


def store_package_dir() -> str | None:
    """Where the Store build of the app is installed, from the package registry.

    NOT A GLOB. `C:\\Program Files\\WindowsApps` refuses to be LISTED by an ordinary process
    (PermissionError 5 here), so a pattern with the package name as a wildcard finds nothing, while
    the package directory itself, once named, opens and reads fine. Windows only; None elsewhere or
    when no package answers.
    """
    if not sys.platform.startswith("win"):
        return None
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", APPX_QUERY],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    where = (r.stdout or "").strip().splitlines()
    return where[-1].strip() if r.returncode == 0 and where and where[-1].strip() else None


def find_app_icon(env: Mapping[str, str] | None = None,
                  exists: Callable[[str], bool] = os.path.exists,
                  glob_fn: Callable[[str], list[str]] = glob.glob,
                  appx: Callable[[], str | None] = store_package_dir) -> list[str]:
    """Icon sources to try, in order: `C4X_ICON`, the installed app's exe (as `path,0`, the
    first icon group; the Store package by its registered location first, then the glob, then the
    non-Store installs), then the tray icon beside it. Empty when there is no app on this machine.

    A LIST, because the exe under `WindowsApps` can be listed and still refuse to be opened by an
    ordinary process; `build()` tries each source in turn and falls back to none.
    """
    env = os.environ if env is None else env
    out: list[str] = []
    override = env.get("C4X_ICON")
    if override:
        if exists(override):
            out.append(f"{override},0" if override.lower().endswith(".exe") else override)
        return out
    package = appx()
    patterns = ([os.path.join(package, "app", "Claude.exe")] if package else []) + list(APP_EXES)
    for pattern in patterns:
        expanded = pattern.replace("%LOCALAPPDATA%", env.get("LOCALAPPDATA", ""))
        hits = glob_fn(expanded) if "*" in expanded else ([expanded] if exists(expanded) else [])
        for exe in sorted(hits):
            if not exists(exe):
                continue
            out.append(f"{exe},0")
            tray = os.path.join(os.path.dirname(exe), "resources", "Tray-Win32.ico")
            if exists(tray):
                out.append(tray)
            return out
    return out


def describe_to_version(text: str) -> str:
    """`git describe --tags --always` folded to the four numbers a version resource takes:
    `v0.1.0-12-gabc` -> `0.1.0.12`, the exact tag `v0.1.0` -> `0.1.0.0`, a hash -> `0.1.0.0`."""
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)(?:-(\d+)-g[0-9a-f]+)?$", str(text or "").strip())
    if not m:
        return "0.1.0.0"
    major, minor, patch, ahead = m.groups()
    return f"{int(major)}.{int(minor)}.{int(patch)}.{int(ahead or 0)}"


def version_file_text(version: str, name: str = NAME, description: str = DESCRIPTION) -> str:
    """The `VSVersionInfo` block PyInstaller's --version-file reads (an eval'd Python literal)."""
    numbers = ", ".join(version.split("."))
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({numbers}), prodvers=({numbers}), mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', ''),
      StringStruct('FileDescription', '{description}'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', '{name}'),
      StringStruct('OriginalFilename', '{name}.exe'),
      StringStruct('ProductName', '{name}'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def pyinstaller_args(root: Path = ROOT, platform: str = sys.platform, icon: str | None = None,
                     version_file: str | None = None) -> list[str]:
    """The whole PyInstaller command line, pure, so the self-test reads it without a build.

    `--add-data` separates source and destination with `;` on Windows and `:` elsewhere, which is
    `os.pathsep` and easy to write the wrong way round. One-dir, because one-file extracts the
    bundle on every launch and the extraction directory dies with the process, so `install_root`
    would have nothing stable to walk up from.
    """
    sep = ";" if platform.startswith("win") else ":"
    work = root / "tmp" / "pyinstaller"
    return [
        "--noconfirm", "--clean", "--onedir", "--console", "--name", NAME,
        *(["--icon", icon] if icon else []),
        *(["--version-file", version_file] if version_file else []),
        "--paths", str(root),
        "--collect-submodules", "c4x",
        "--hidden-import", "app",
        "--collect-all", "dash",
        "--collect-all", "plotly",
        "--collect-submodules", "uvicorn",
        *[arg for name in EXCLUDES for arg in ("--exclude-module", name)],
        "--add-data", f"{root / 'frontend' / 'dist'}{sep}frontend/dist",
        "--add-data", f"{root / 'c4x' / 'prices.json'}{sep}c4x",
        "--workpath", str(work), "--specpath", str(work),
        "--distpath", str(root / "dist"),
        str(root / ENTRY),
    ]


def build(root: Path = ROOT) -> int:
    shell = root / "frontend" / "dist" / "index.html"
    if not shell.is_file():
        print(f"no built page at {shell}: run `npm run build --prefix frontend` first, or check "
              "out the tracked frontend/dist")
        return 2
    from importlib.metadata import version

    from PyInstaller.__main__ import run as pyinstaller_run
    started = time.monotonic()
    sha, described = "unknown", ""
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                             cwd=root, timeout=20).stdout.strip() or sha
        described = subprocess.run(["git", "describe", "--tags", "--always"], capture_output=True,
                                   text=True, cwd=root, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    work = root / "tmp" / "pyinstaller"
    work.mkdir(parents=True, exist_ok=True)
    version_path = work / "version.txt"
    version_path.write_text(version_file_text(describe_to_version(described)), encoding="utf-8")
    # The icon sources, each tried in turn: the app's exe under WindowsApps can refuse to open
    # to an ordinary process, and PyInstaller opens it with no handler of its own.
    icon_used: str | None = None
    for source in [*find_app_icon(), None]:
        try:
            pyinstaller_run(pyinstaller_args(root, icon=source, version_file=str(version_path)))
            icon_used = source
            break
        except OSError as exc:
            if source is None:
                raise
            print(f"icon source {source} could not be used ({exc}); trying the next")
    exe = exe_path(root)
    if not exe.is_file():
        print(f"PyInstaller returned and {exe} does not exist")
        return 1
    total = sum(p.stat().st_size for p in exe.parent.rglob("*") if p.is_file())
    stamp = {
        "name": NAME, "git": sha, "version": describe_to_version(described),
        "icon": icon_used, "python": sys.version.split()[0],
        "pyinstaller": version("pyinstaller"),
        "index_sha256": hashlib.sha256(shell.read_bytes()).hexdigest(),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "bytes": total, "seconds": round(time.monotonic() - started, 1),
    }
    (exe.parent / "BUILD.json").write_text(json.dumps(stamp, indent=1) + "\n", encoding="utf-8")
    print(f"built {exe} ({total / (1 << 20):.0f} MB, {stamp['seconds']} s); BUILD.json beside it")
    return 0


# ---------------------------------------------------------------------------
# The icon check: the built exe's icon is the app's, pixel for pixel.
# ---------------------------------------------------------------------------
# The path is spelled INTO the command: with -Command there are no $args, and a first version that
# read $args[0] extracted nothing from either exe while reporting a tidy failure.
ICON_PS = (
    "Add-Type -AssemblyName System.Drawing; "
    "$i = [System.Drawing.Icon]::ExtractAssociatedIcon('{path}'); "
    "$m = New-Object System.IO.MemoryStream; "
    "$i.ToBitmap().Save($m, [System.Drawing.Imaging.ImageFormat]::Png); "
    "[Convert]::ToBase64String($m.ToArray())"
)


def icon_png(path: str) -> bytes | None:
    """The 32 by 32 icon of an exe as PNG bytes, through PowerShell; None when it cannot be read."""
    try:
        if "'" in path:
            return None
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ICON_PS.format(path=path)],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return base64.b64decode(r.stdout.strip().splitlines()[-1])
    except ValueError:
        return None


def check_icon(root: Path = ROOT) -> int:
    """PASS when the built exe's icon and the app's are the same image."""
    if not sys.platform.startswith("win"):
        print("ICON CHECK SKIPPED: Windows only (exe icons)")
        return 0
    exe = exe_path(root)
    sources = [s for s in find_app_icon() if s.lower().endswith(",0")]
    if not exe.is_file():
        print(f"ICON CHECK FAIL: no exe at {exe}")
        return 1
    if not sources:
        print("ICON CHECK SKIPPED: no Claude desktop app on this machine to compare with")
        return 0
    app = sources[0][:-2]
    ours, theirs = icon_png(str(exe)), icon_png(app)
    if not ours or not theirs:
        print(f"ICON CHECK FAIL: could not read an icon (exe: {bool(ours)}, app: {bool(theirs)})")
        return 1
    same = hashlib.sha256(ours).hexdigest() == hashlib.sha256(theirs).hexdigest()
    print(f"ICON CHECK {'PASS' if same else 'FAIL'}: {exe.name} against {app} "
          f"({len(ours)} and {len(theirs)} PNG bytes)")
    return 0 if same else 1


# ---------------------------------------------------------------------------
# The smoke.
# ---------------------------------------------------------------------------
def pick_free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def token_from(text: str) -> str | None:
    """The shutdown token out of the announce line c4x/server.py prints."""
    found = re.search(r"X-C4X-Shutdown:\s*([^\"\s]+)", text or "")
    return found.group(1) if found else None


def _get(url: str, timeout: float = 5.0):
    """(status, content type, body bytes); status 0 when nothing answered."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, "", b""
    except (urllib.error.URLError, OSError, ValueError):
        return 0, "", b""


def _post(url: str, headers: dict, timeout: float = 5.0) -> int:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, method="POST", headers=headers, data=b"")
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError, ValueError):
        return 0


def bare_path_env(root: Path, db: str | None = None) -> dict:
    """The environment the download is actually met in: no node, no Python, no developer PATH.

    A bundle that forgot node.exe still passes a smoke run on a machine that has node, because the
    hooks and the tools find one anyway. Stripping PATH to Windows itself is what makes the check
    able to fail: anything the folder does not carry is simply not there.
    """
    keep = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
            "PROGRAMDATA", "COMSPEC", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE"}
    system = os.environ.get("SYSTEMROOT", r"C:\Windows")
    env = {name: value for name, value in os.environ.items() if name.upper() in keep}
    env["PATH"] = os.pathsep.join([system + r"\System32", system])
    # The store the smoke was given, not one the folder does not have: importing the module that
    # reads it is the first thing the exe does, and a missing store is an exception, not a page.
    # RESOLVED, because the child runs in the folder and not here: `--db tmp/fixture.db` names a
    # store relative to the checkout, and the harvester read it relative to the bundle and refused.
    env["C4X_DB"] = str(Path(db).resolve()) if db else str(root / "data" / "context.db")
    return env


def smoke(db: str, port: int | None = None, root: Path = ROOT, startup_s: float = 120.0,
          bundle: bool = False) -> int:
    """Run the built exe against `db` and walk SMOKE_PLAN. Non-zero on the first miss; the child
    is killed on any path out, so a failed CI smoke never leaves an exe running.

    `bundle=True` runs the ASSEMBLED FOLDER instead, with a stripped PATH, and asks it two things
    a developer machine would answer by accident: that node is the one in the folder, and that the
    harvester runs at all.
    """
    where = bundle_dir(root) if bundle else exe_path(root).parent
    exe = where / exe_path(root).name
    checks: list[tuple[str, bool, str]] = []
    add = checks.append
    if not exe.is_file():
        add(("the exe exists", False, str(exe)))
        return _report(checks, "SMOKE")
    env = bare_path_env(where, db) if bundle else None
    if bundle:
        node = where / "node" / "node.exe"
        add(("the folder carries its own node", node.is_file(), str(node)))
        add(("the folder is an install: it holds the marker paths.py looks for",
             (where / "tools" / "harvest.mjs").is_file(), str(where / "tools" / "harvest.mjs")))
        if all(ok for _, ok, _ in checks):
            # THE EXE IS THIS SOURCE'S. A stale build in dist/ once got copied into the folder and
            # answered the checks below by accident, so the first question is whether this program
            # even knows what a verb is.
            said = subprocess.run([str(exe), "--version"], capture_output=True, text=True,
                                  cwd=where, env=env, timeout=120)
            add(("`c4x.exe --version` names the build", said.returncode == 0
                 and said.stdout.strip().startswith("c4x "), (said.stdout + said.stderr)[-200:]))
            # THE PROOF THAT THE FOLDER NEEDS NOTHING ELSE: the harvester, run by the node in the
            # folder, with a PATH that has no node on it. `--stats` prints the store's own numbers,
            # which nothing else in this program prints.
            ran = subprocess.run([str(exe), "harvest", "--stats"], capture_output=True,
                                 text=True, cwd=where, env=env, timeout=600)
            add(("`c4x.exe harvest --stats` runs the harvester through the bundled node",
                 ran.returncode == 0 and '"sessions"' in ran.stdout,
                 (ran.stdout + ran.stderr)[-300:]))
            told = subprocess.run([str(exe), "status"], capture_output=True, text=True,
                                  cwd=where, env=env, timeout=300)
            add(("`c4x.exe status` runs the installer through it too",
                 "install root" in (told.stdout + told.stderr),
                 (told.stdout + told.stderr)[-300:]))
        if any(not ok for _, ok, _ in checks):
            return _report(checks, "SMOKE")
    store = Path(db).resolve()
    add(("the store exists", store.is_file(), str(store)))
    port = port or pick_free_port()
    base = f"http://127.0.0.1:{port}"
    status, _, _ = _get(f"{base}/__health__", timeout=1.0)
    add((f"nothing answers on {port} before the launch", status == 0, f"status {status}"))
    own = subprocess.run([str(exe), "--self-test"], capture_output=True, text=True,
                         cwd=where if bundle else root, env=env, timeout=120)
    add(("the exe's own self-test passes", "SELF-TEST PASS" in own.stdout,
         (own.stdout + own.stderr)[-300:]))
    if any(not ok for _, ok, _ in checks):
        return _report(checks, "SMOKE")

    lines: list[str] = []
    token: dict = {}
    child = subprocess.Popen([str(exe), "--db", str(store), "--port", str(port)],
                             env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             cwd=root, bufsize=1)

    def drain():
        assert child.stdout is not None
        for line in child.stdout:
            lines.append(line.rstrip())
            if "token" not in token:
                found = token_from(line)
                if found:
                    token["token"] = found

    threading.Thread(target=drain, daemon=True).start()
    try:
        deadline = time.monotonic() + startup_s
        answered = None
        while time.monotonic() < deadline and child.poll() is None:
            status, _, body = _get(f"{base}/__health__", timeout=2.0)
            if status == 200:
                try:
                    answered = json.loads(body)
                except ValueError:
                    answered = None
                if isinstance(answered, dict) and answered.get("ok"):
                    break
            time.sleep(0.5)
        theirs = os.path.normcase(str(Path(str((answered or {}).get("db", ""))).resolve()))
        add((SMOKE_PLAN[0][2], isinstance(answered, dict) and answered.get("port") == port
             and theirs == os.path.normcase(str(store)),
             f"exit {child.poll()} answer {answered!r} log {lines[-5:]}"))
        if not checks[-1][1]:
            return _report(checks, "SMOKE")
        status, ctype, body = _get(f"{base}/")
        add((SMOKE_PLAN[1][2], status == 200 and "text/html" in ctype and b'id="root"' in body,
             f"status {status} type {ctype} bytes {len(body)}"))
        status, _, body = _get(f"{base}/api/tabs", timeout=60.0)
        tabs = []
        try:
            tabs = json.loads(body) if status == 200 else []
        except ValueError:
            tabs = []
        add((SMOKE_PLAN[2][2], status == 200 and isinstance(tabs, list) and len(tabs) > 0,
             f"status {status} tabs {len(tabs)} log {lines[-3:]}"))
        first = tabs[0]["id"] if tabs and isinstance(tabs[0], dict) else "none"
        status, _, body = _get(f"{base}/api/tab/{first}/render", timeout=120.0)
        add((SMOKE_PLAN[3][2].replace("one pane", f"pane {first}"), status == 200 and len(body) > 0,
             f"status {status} bytes {len(body)} log {lines[-3:]}"))
        status, _, body = _get(f"{base}/api/health")
        add((SMOKE_PLAN[4][2], status == 200 and b'"ok"' in body, f"status {status}"))
        waited = time.monotonic() + 5
        while "token" not in token and time.monotonic() < waited:
            time.sleep(0.1)
        status = _post(f"{base}/__shutdown__", {"X-C4X-Shutdown": token.get("token", "")})
        add((SMOKE_PLAN[5][2], status == 200, f"status {status} token seen {'token' in token}"))
        try:
            child.wait(timeout=15)
            add(("the process exited after the shutdown", True, f"exit {child.returncode}"))
        except subprocess.TimeoutExpired:
            add(("the process exited after the shutdown", False, "still running after 15 s"))
    finally:
        if child.poll() is None:
            child.kill()
    return _report(checks, "SMOKE")


def _report(checks: list, label: str) -> int:
    bad = 0
    for what, ok, detail in checks:
        if not ok:
            bad += 1
            print(f"  FAIL  {what}  [{detail}]")
        else:
            print(f"  ok    {what}")
    print(f"{label} {'PASS' if not bad else 'FAIL'} ({len(checks)} checks)")
    return 1 if bad else 0


# ---------------------------------------------------------------------------
def self_test() -> int:
    """The argv builder, the smoke plan and the helpers. No build, no PyInstaller."""
    win = pyinstaller_args(Path("X:/r"), platform="win32")
    nix = pyinstaller_args(Path("/r"), platform="linux")
    with_icon = pyinstaller_args(Path("X:/r"), "win32", icon="X:/a.exe,0")
    # Paths the finder builds with os.path.join carry the platform's separator; the checks read
    # them with forward slashes so the same assertions hold on every leg.
    STORE_EXE = "X:/wa/Claude_1_x64__abc/app/Claude.exe"
    USER_EXE = "X:/la/Programs/Claude/Claude.exe"
    slashed = lambda found: [f.replace(chr(92), "/") for f in found]  # noqa: E731
    source = Path(__file__).read_text(encoding="utf-8")
    top_level = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    checks = [
        ("the entry script is the API's own __main__", win[-1].endswith(str(ENTRY))),
        ("one-dir, never one-file", "--onedir" in win and "--onefile" not in win),
        ("the page is added from frontend/dist with the Windows separator",
         any(a.endswith(";frontend/dist") for a in win)),
        ("and with the posix separator elsewhere", any(a.endswith(":frontend/dist") for a in nix)),
        ("prices.json rides in under c4x/, where pricing.py looks",
         any(a.endswith(";c4x") and "prices.json" in a for a in win)),
        ("dash and plotly are collected whole, because app.py imports them lazily",
         win.count("--collect-all") == 2 and "dash" in win and "plotly" in win),
        ("root app.py is a hidden import",
         "app" in win and win[win.index("app") - 1] == "--hidden-import"),
        ("every c4x submodule is collected",
         win[win.index("--collect-submodules") + 1] == "c4x"),
        ("the work and spec paths are under tmp/, which every gate skips",
         all("tmp" in Path(win[win.index(f) + 1]).parts for f in ("--workpath", "--specpath"))),
        ("the output lands in dist/", win[win.index("--distpath") + 1].endswith("dist")),
        ("the optional heavyweights are excluded, pyspark and torch among them",
         all(name in win and win[win.index(name) - 1] == "--exclude-module"
             for name in ("pyspark", "torch"))),
        ("and nothing the dashboard needs is",
         not any(name in EXCLUDES
                 for name in ("pandas", "numpy", "plotly", "dash", "fastapi", "uvicorn",
                              "psutil"))),
        ("the exe path follows the platform", exe_path(Path("X:/r"), "win32").name == "c4x.exe"
         and exe_path(Path("/r"), "linux").name == "c4x"),
        ("the name is c4x and the directory dist/c4x",
         NAME == "c4x" and exe_path(Path("X:/r"), "win32").parent.name == "c4x"),
        ("an icon source is added as --icon and only when found",
         "--icon" not in win and with_icon[with_icon.index("--icon") + 1] == "X:/a.exe,0"),
        ("the version file rides in the same way",
         "--version-file" in pyinstaller_args(Path("X:/r"), "win32", version_file="X:/v.txt")),
        ("the app's exe is found by glob, first, as path,0, with the tray icon after it",
         slashed(find_app_icon({"LOCALAPPDATA": "X:/la"}, exists=lambda p: True, appx=lambda: None,
                               glob_fn=lambda pat: [STORE_EXE] if "*" in pat else []))
         == [STORE_EXE + ",0", "X:/wa/Claude_1_x64__abc/app/resources/Tray-Win32.ico"]),
        ("the Store package's registered location is tried before the glob, which cannot list it",
         slashed(find_app_icon({"LOCALAPPDATA": "X:/la"}, exists=lambda p: True,
                               appx=lambda: "X:/wa/Claude_1_x64__abc", glob_fn=lambda pat: []))[0]
         == STORE_EXE + ",0"),
        ("the non-Store install is found when the Store one is not, and no tray beside it",
         slashed(find_app_icon({"LOCALAPPDATA": "X:/la"}, glob_fn=lambda pat: [], appx=lambda: None,
                               exists=lambda p: p.replace(chr(92), "/") == USER_EXE))
         == [USER_EXE + ",0"]),
        ("C4X_ICON wins, bare for an .ico",
         find_app_icon({"C4X_ICON": "X:/mine.ico"}, exists=lambda p: True, appx=lambda: "z",
                       glob_fn=lambda pat: ["z"]) == ["X:/mine.ico"]),
        ("and no app means no icon",
         find_app_icon({"LOCALAPPDATA": "X:/la"}, exists=lambda p: False, appx=lambda: None,
                       glob_fn=lambda pat: []) == []),
        ("git describe folds to four numbers in all three shapes",
         describe_to_version("v0.1.0-12-gabc1234") == "0.1.0.12"
         and describe_to_version("v0.1.0") == "0.1.0.0"
         and describe_to_version("abc1234") == "0.1.0.0"),
        ("the version text carries the name, the description and the version",
         all(s in version_file_text("0.1.0.12") for s in
             ("FileDescription', 'c4x dashboard", "OriginalFilename', 'c4x.exe'",
              "filevers=(0, 1, 0, 12)", "ProductVersion', '0.1.0.12'"))),
        # The plan pins the routes that prove the bundle: the tab list (dash) and a rendered pane
        # (plotly). Without these two a smoke could pass on an exe that serves a page over nothing.
        ("the smoke plan asks for the tab list", any(p[1] == "/api/tabs" for p in SMOKE_PLAN)),
        ("and renders a pane", any(p[1].endswith("/render") for p in SMOKE_PLAN)),
        ("and stops the process it started", SMOKE_PLAN[-1][1] == "/__shutdown__"),
        ("a free port is a port", isinstance(pick_free_port(), int) and pick_free_port() > 0),
        ("the token is read out of the announce line",
         token_from('  stop it with: curl -X POST http://127.0.0.1:1/__shutdown__ '
                    '-H "X-C4X-Shutdown: abc_DEF-1"') == "abc_DEF-1"),
        ("no token in an ordinary line", token_from("c4x api on http://127.0.0.1:8059") is None),
        # The gate for the class: a module-level PyInstaller import would fail every CI leg's
        # self-test, and nothing but this line would say why.
        ("PyInstaller is not imported at module level (gate can fail)",
         not any("PyInstaller" in line for line in top_level)),
        ("and is not loaded by running the self-test", "PyInstaller" not in sys.modules),
    ]
    # ---------------------------------------------------------------- the download's folder
    runtime = runtime_files(ROOT)
    names = {rel.as_posix() for rel in runtime}
    bare = bare_path_env(ROOT / "dist" / "bundle" / "c4x")
    system = os.environ.get("SYSTEMROOT", r"C:\Windows").lower()
    url, sums = node_url("24.14.1", "win32")
    checks += [
        ("the folder carries the harvester, which also marks it as an install",
         "tools/harvest.mjs" in names),
        ("and the hooks Claude runs",
         "hooks/event-hook.mjs" in names and "hooks/compact-hook.mjs" in names),
        ("and the tools the server shells out to",
         {"tools/mirror.mjs", "tools/segments.mjs", "tools/breakdown.mjs"} <= names),
        ("and says what it is", "README.md" in names and "LICENSE" in names),
        # NOT THE SOURCE. A download is the program, not the workshop.
        ("and carries no frontend source, which is already inside the exe",
         not any(name.startswith("frontend/") for name in names)),
        ("every runtime file is a real file in this checkout",
         all((ROOT / rel).is_file() for rel in runtime)),
        ("node is fetched from the official release, pinned to a version",
         url == "https://nodejs.org/dist/v24.14.1/node-v24.14.1-win-x64.zip"
         and sums == "https://nodejs.org/dist/v24.14.1/SHASUMS256.txt"),
        ("the checksum file is read for the archive's own line",
         sha_for("abc123  node-v24.14.1-win-x64.zip\ndef456  other.zip",
                 "node-v24.14.1-win-x64.zip") == "abc123"),
        ("a checksum file that does not mention it yields nothing rather than a guess",
         sha_for("def456  other.zip", "node-v24.14.1-win-x64.zip") is None),
        ("a starred name (binary mode) is the same name",
         sha_for("abc123 *node-v24.14.1-win-x64.zip", "node-v24.14.1-win-x64.zip") == "abc123"),
        # THE CHECK THAT MAKES THE BUNDLE SMOKE ABLE TO FAIL: on a developer's machine a folder
        # that forgot node.exe still works, because the machine has node.
        ("the bundle smoke runs with a PATH holding Windows and nothing else",
         all(part.lower().startswith(system) for part in bare["PATH"].split(os.pathsep))),
        ("so a folder that forgot node cannot be rescued by the machine's own",
         not any("nodejs" in part.lower() for part in bare["PATH"].split(os.pathsep))),
        # THE DECISION THAT KEEPS A STALE EXE OUT OF THE FOLDER, which is what this whole change
        # was nearly shipped without.
        ("no exe at all means build", needs_build(None, 1.0)),
        ("an exe older than its source means build again", needs_build(1.0, 2.0)),
        ("an exe newer than its source is kept", not needs_build(2.0, 1.0)),
        ("the same instant is not stale", not needs_build(2.0, 2.0)),
        ("compiled bytecode is not source: a test run must not force a rebuild",
         not is_source(ROOT / "c4x" / "__pycache__" / "store.cpython-314.pyc")
         and not is_source(ROOT / "c4x" / "store.pyc")
         and is_source(ROOT / "c4x" / "store.py")),
        ("the bundle is assembled beside the plain build, not inside it",
         bundle_dir(ROOT).as_posix().endswith("dist/bundle/c4x")
         and bundle_dir(ROOT) != exe_path(ROOT).parent),
    ]

    bad = 0
    for what, ok in checks:
        if not ok:
            bad += 1
            print(f"  FAIL  {what}")
    print(f"SELF-TEST {'PASS' if not bad else 'FAIL'} ({len(checks)} checks)")
    return 1 if bad else 0


def bundle_dir(root: Path = ROOT) -> Path:
    """Where the download is assembled. Beside dist/c4x/ rather than inside it: the zip is this."""
    return root / "dist" / "bundle" / NAME


def node_url(version: str = NODE_VERSION, platform: str = sys.platform) -> tuple[str, str]:
    """The archive to fetch and the checksum file that names it."""
    if not platform.startswith("win"):
        raise SystemExit("the bundle is Windows only for now: node is fetched as the win-x64 zip")
    return (f"{NODE_BASE}/v{version}/node-v{version}-win-x64.zip",
            f"{NODE_BASE}/v{version}/SHASUMS256.txt")


def sha_for(text: str, filename: str) -> str | None:
    """The digest SHASUMS256.txt gives for one file, or None when it does not mention it."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == filename:
            return parts[0].lower()
    return None


def fetch_node(into: Path, version: str = NODE_VERSION, fetch=None) -> Path:
    """Put `node.exe` and its licence in `into`, from the official release, checksum verified.

    CHECKED, NOT TRUSTED. This binary ends up in a zip somebody downloads and runs, so the bytes
    are compared against the digest the release publishes beside them, and a mismatch stops the
    build rather than shipping. The archive is fetched once and cached under tmp/, because a build
    that re-downloads 30 MB on every run is a build nobody runs twice.
    """
    import io
    import zipfile

    get = fetch if fetch is not None else _fetch_bytes
    archive_url, sums_url = node_url(version)
    name = archive_url.rsplit("/", 1)[-1]
    cache = ROOT / "tmp" / "node" / name
    if cache.is_file():
        blob = cache.read_bytes()
    else:
        blob = get(archive_url)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(blob)
    sums = get(sums_url).decode("utf-8", "replace")
    want = sha_for(sums, name)
    got = hashlib.sha256(blob).hexdigest()
    if want is None:
        raise SystemExit(f"SHASUMS256.txt for node v{version} does not mention {name}")
    if got != want:
        cache.unlink(missing_ok=True)
        raise SystemExit(f"node archive digest {got} does not match the published {want}")
    into.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as zipped:
        for member in zipped.namelist():
            tail = member.split("/")[-1]
            if tail in ("node.exe", "LICENSE"):
                (into / tail).write_bytes(zipped.read(member))
    exe = into / "node.exe"
    if not exe.is_file():
        raise SystemExit(f"node.exe was not in {name}")
    return exe


def _fetch_bytes(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=300) as answer:  # noqa: S310  (a pinned https URL)
        return bytes(answer.read())


def runtime_files(root: Path = ROOT) -> list[Path]:
    """Every file the folder must carry beside the exe, as paths relative to the root.

    The hooks and the harvester Claude runs, the tools the server shells out to, and the two files
    that say what this is. Deliberately NOT the tests, the frontend source or the store: the page
    is already inside the exe, and a store belongs to the machine, not to the download.
    """
    out: list[Path] = []
    for folder in RUNTIME_DIRS:
        here = root / folder
        if not here.is_dir():
            continue
        out.extend(sorted(p.relative_to(root) for p in here.glob("*.mjs")))
    out.extend(Path(name) for name in RUNTIME_FILES if (root / name).is_file())
    return out


def is_source(path: Path) -> bool:
    """Whether a file is something the exe is built FROM.

    Compiled bytecode is not: `__pycache__` is written by running the tests, so counting it made
    every test run look like a source change and forced a three-minute rebuild for nothing.
    """
    if path.suffix in (".pyc", ".pyo"):
        return False
    return "__pycache__" not in path.parts


def newest_source(root: Path = ROOT) -> float:
    """When the newest thing the exe is built from changed."""
    seen = [0.0]
    for folder in ("c4x", "frontend/dist"):
        here = root / folder
        if here.is_dir():
            seen.extend(f.stat().st_mtime for f in here.rglob("*") if f.is_file() and is_source(f))
    for name in ("app.py", "requirements.txt"):
        if (root / name).is_file():
            seen.append((root / name).stat().st_mtime)
    return max(seen)


def needs_build(exe_mtime: float | None, newest: float) -> bool:
    """Whether to build: there is no exe, or the one there is predates its own source.

    THE GATE FOR THIS PR'S FOUNDING DEFECT. Reusing whatever exe happened to be in dist/ shipped a
    program built before the verbs existed, and the smoke then passed a check the old exe answered
    by accident. An independent review pointed out that the fix itself had no check; this is it,
    kept as a pure decision so it can have one.
    """
    return exe_mtime is None or exe_mtime < newest


def assemble(root: Path = ROOT, fetch=None) -> Path:
    """Build the exe when it is missing OR older than the source, then assemble the folder.

    THE STALE BUILD IS THE DEFECT THIS GUARDS. Reusing whatever exe happened to be in dist/ put a
    program built before the verbs existed into the folder, and the smoke then passed a check that
    the old exe answered by accident. A download that is a week behind its own source is worse than
    a build that takes five minutes.
    """
    import shutil

    exe = exe_path(root)
    when = exe.stat().st_mtime if exe.is_file() else None
    if needs_build(when, newest_source(root)):
        if when is not None:
            print(f"{exe.name} is older than the source it is built from; building again")
        code = build()
        if code:
            raise SystemExit(code)
    out = bundle_dir(root)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    shutil.copytree(exe.parent, out, dirs_exist_ok=True)
    for rel in runtime_files(root):
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / rel, target)
    fetch_node(out / "node", fetch=fetch)
    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(f"bundle at {out} ({size / (1 << 20):.0f} MB): c4x.exe, node v{NODE_VERSION}, "
          f"{len(runtime_files(root))} runtime files")
    return out


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--self-test" in argv:
        return self_test()
    if "--check-icon" in argv:
        return check_icon()
    if "--smoke" in argv:
        db = ""
        if "--db" in argv and argv.index("--db") + 1 < len(argv):
            db = argv[argv.index("--db") + 1]
        if not db:
            print("--smoke needs --db <store>: the exe refuses to run without one it can find")
            return 2
        port = None
        if "--port" in argv and argv.index("--port") + 1 < len(argv):
            port = int(argv[argv.index("--port") + 1])
        return smoke(db, port, bundle="--bundle" in argv)
    if "--bundle" in argv:
        assemble()
        return 0
    return build()


if __name__ == "__main__":
    sys.exit(main())
