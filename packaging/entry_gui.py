"""打包入口：GUI（windowed，无控制台窗口）。

只做两件事：①把 winapp/ 加进 sys.path（源码直跑用；打包后模块已在包内）；
②windowed 模式下 stdout/stderr 是 None，兜个空设备，免得任何 print 炸掉。
业务逻辑一律不在这里，全部仍由 winapp/atk_hp20.py 提供。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_WINAPP = os.path.join(os.path.dirname(_HERE), "winapp")
if os.path.isdir(_WINAPP) and _WINAPP not in sys.path:
    sys.path.insert(0, _WINAPP)

for _name in ("stdout", "stderr"):
    if getattr(sys, _name, None) is None:          # --windowed 没有控制台
        setattr(sys, _name, open(os.devnull, "w", encoding="utf-8"))


def main() -> int:
    import atk_hp20
    return atk_hp20.main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
