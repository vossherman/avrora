"""Release-only entry point: all paths are relative to this portable folder."""
import os
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
os.chdir(ROOT)
os.environ["AURORA_CORE_DIR"] = str(ROOT / "core")
os.environ["AURORA_NEOSCRYPT_HARNESS"] = str(ROOT / "bin" / "harness_plen.exe")
os.environ["TCL_LIBRARY"] = str(ROOT / "runtime" / "tcl" / "tcl8.6")
os.environ["TK_LIBRARY"] = str(ROOT / "runtime" / "tcl" / "tk8.6")
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "core")]
if "--check" in sys.argv:
    runpy.run_path(str(ROOT / "tools" / "check_release.py"), run_name="__main__")
else:
    sys.argv[0] = str(ROOT / "app" / "launch.py")
    runpy.run_path(sys.argv[0], run_name="__main__")
