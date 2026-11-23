"""打包入口：给 PyInstaller 用，源码里直接 `python -m muyan` 也行。"""

import sys

from muyan.__main__ import main

if __name__ == "__main__":
    sys.exit(main() or 0)
