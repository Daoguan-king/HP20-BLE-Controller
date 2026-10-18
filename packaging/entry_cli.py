"""打包入口：CLI（console，能看到输出）。

给 `--probe` / `--selftest` / `--sim` / `--scan-only` 用；与 GUI 同一套 winapp 代码。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_WINAPP = os.path.join(os.path.dirname(_HERE), "winapp")
if os.path.isdir(_WINAPP) and _WINAPP not in sys.path:
    sys.path.insert(0, _WINAPP)


def main() -> int:
    import atk_hp20
    return atk_hp20.main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
