# HP20 BLE Controller (Windows)

An **unofficial** Windows desktop controller for the ALIENTEK / 正点原子 **HP20** heating platform, written in Python (bleak + Tkinter).

> **Disclaimer**
> This project is **not affiliated with, endorsed by, or sponsored by** 广州市星翼电子科技有限公司 (ALIENTEK / 正点原子).
> It contains no vendor code, firmware, manual text or artwork. Protocol information here consists of byte layouts,
> field meanings and timing facts obtained by interoperability analysis of publicly available hardware behaviour.
> Use at your own risk; for commercial use, consult a lawyer.

## Features

- BLE scan / connect / **automatic reconnect** / **cancelable connect** (three-stage: cached address → direct address → rescan)
- Live monitoring: single chart with **dual Y axes** (temperature left, power right), hover crosshair readout, time-based X axis
- Heating control: channel selection T1–T3, custom target temperature, **start / stop** (stop = T4 stop channel, same as the official app)
- Reflow profile and repair-parameter read/write, device name read/write, **reset to factory defaults**
- Raw frame console with pause/resume auto-scroll, and a **diagnostic panel** that decodes any frame byte-by-byte (also lets you pin any frame from the console)
- CSV logging (full 14-column / compact 3-column) and **lossless PNG/SVG chart export**
- Input validation everywhere (temperatures 40–350 ℃; per-field limits for reflow/repair taken from the device manual)

## Requirements

- Windows 10/11 with a Bluetooth LE adapter
- Python 3.12+ and `bleak` (for source runs): `pip install bleak`
- Optional: `pillow` for chart PNG export (falls back to SVG without it)

## Run

```powershell
python winapp\atk_hp20.py                       # GUI, scan and pick your device
python winapp\atk_hp20.py --address AA:BB:CC:DD:EE:FF
python winapp\atk_hp20.py --probe   AA:BB:CC:DD:EE:FF   # read-only self check (exit 0/1/2)
python winapp\atk_hp20.py --sim                          # built-in simulated device, no hardware
python winapp\atk_hp20.py --selftest                     # 44-step end-to-end self test
python -m unittest discover -s winapp -p "test_*.py"     # unit tests
```

## Build a standalone exe

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

Runs tests and the self test first, then packages with PyInstaller into `dist\`
(`HP20上位机.exe` windowed + `hp20-cli.exe` console). See `packaging/README.md` for parameters and packaging pitfalls.

## Documentation

- `docs/hp20_protocol.md` — protocol spec (frame layout, commands, status fields, timing) — Chinese
- `docs/DELIVERY-zh.md` — full delivery notes: verified behaviour, known limitations, troubleshooting — Chinese
- `protocol/*.json` — machine-readable command / field tables

## Status fields (summary)

| Offset | Meaning |
|---|---|
| `[0:2]` | current temperature (i16, ℃) |
| `[2]` | supply power (W) |
| `[3:5]` | input voltage ×0.1 V |
| `[5]` | output state (`0x00` idle / `0x02` heating-mode running) |
| `[6]` | output power (W) |
| `W1=[7:9]` | `&0x3FF` target ℃; `>>10&3` channel; `>>12&7` mode (0 idle / 1 reflow / 2 heat / 3 setup / 4 repair / 8 home) |
| `W2=[9:11]` | heating timer, **base-64 packed MM:SS** (each minute = 64 units); `0x29BF` = timer off |

## License

MIT for the code in this repository (see `LICENSE`). Third-party dependencies keep their own licenses
(bleak — MIT; Pillow — HPND; PyInstaller — GPL with bootloader exception; Tcl/Tk — BSD-style).