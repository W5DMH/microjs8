# MicroJS8 + Cardputer Link

Operator setup for running MicroJS8 with an **M5Stack Cardputer ADV**
as the keyboard + battery source for the Pi.

This is one of two supported HMI configurations:

| HMI mode | Keyboard | Battery | Display |
|---|---|---|---|
| `keyboard = "usb"` (default) | Any USB HID keyboard | External (Pi USB-C) | Waveshare 320×170 SPI on Pi |
| `keyboard = "uart"` (this doc) | Cardputer ADV keyboard | Cardputer 1750 mAh | Waveshare 320×170 SPI on Pi |

In the UART configuration, the Cardputer ADV runs the companion
firmware **microjs8-cardputer-link** (separate repository) which
scans its 56-key matrix and emits one keystroke event per line over
its EXT 14-pin header's hardware UART. The Pi reads those lines from
`/dev/serial0` and the daemon's new `UartKeyboardThread` translates
them into the same `KeyEvent` objects the existing USB backend
produces, so the rest of the UI stack is unchanged.

## Hardware overview

```
┌──────────────────────────────────────┐
│   M5Stack Cardputer ADV              │
│                                      │
│   ┌─ 1750 mAh battery ─┐             │
│   │  boost 3.7→5V →─────────────► 5V OUT (EXT pin 6)
│   └────────────────────┘             │
│                                      │
│   ┌─ ESP32-S3 firmware ┐             │
│   │  scans keyboard    │             │
│   │  emits CHAR:k\n    ─────────────► UART_TX (EXT pin 14, GPIO 15)
│   └────────────────────┘             │
└──────────────────────────────────────┘
                    │
                    │  4-wire bundle through EXT 2.54-14P
                    │
                    ▼
┌──────────────────────────────────────┐
│   Raspberry Pi Zero 2 W              │
│                                      │
│   GPIO header pin 2  ◄────── 5V IN   │
│   GPIO header pin 39 ◄────── GND     │
│   GPIO header pin 10 ◄────── UART RX │ (BCM 15)
│   GPIO header pin 8  ──────► UART TX │ (BCM 14, currently unused)
│                                      │
│   MicroJS8 daemon:                   │
│     uart_keyboard.py reads /dev/serial0
│     parses CHAR:k → KeyEvent(char='k')
│     same router consumes either backend
└──────────────────────────────────────┘
```

## EXT 14-pin pinout (Cardputer ADV)

Source: M5 official docs, https://docs.m5stack.com/en/core/Cardputer-Adv

| EXT pin | Function | Notes |
|---|---|---|
| 1 | RESET (G3) | |
| 2 | 5V IN | external charge input |
| 3 | INT (G4) | |
| 4 | **GND** | use this |
| 5 | BUSY (G6) | |
| 6 | **5V OUT** | use this for powering Pi |
| 7 | SCK (G40) | |
| 8 | I2C SDA (G8) | |
| 9 | MOSI (G14) | |
| 10 | I2C SCL (G9) | |
| 11 | MISO (G39) | |
| 12 | UART_RX (G13) | optional — Pi→Cardputer ack channel |
| 13 | CS (G5) | |
| 14 | **UART_TX (G15)** | use this for Cardputer→Pi keystrokes |

The four pins we use (4, 6, 12, 14) are all on the same column of the
2×7 header — convenient for a clean cable.

## Wiring

| Cardputer EXT pin | Wire color (suggested) | Pi 40-pin header | Pi function |
|---|---|---|---|
| 4 (GND) | black | pin 39 | GND |
| 6 (5V OUT) | red | pin 2 | 5V input |
| 14 (UART_TX) | yellow | pin 10 (BCM 15) | UART RXD |
| 12 (UART_RX) | white | pin 8 (BCM 14) | UART TXD (unused; reserve) |

**Important:** identify EXT pin 4 (GND) with a multimeter continuity
check BEFORE wiring. The connector orientation is not visually obvious
on the device. Probe each pin against a known GND (e.g. a Grove cable's
black wire) until you find the beep — that's pin 4. Every other pin's
position falls out from the table once you have pin 4 located.

Logic levels are 3.3V on both ends. No level shifter needed.

## Pi-side setup

### 1. Install MicroJS8 v0.0.12 or later

If you're on v0.0.10 or earlier, the `[hmi]` config section and the
`uart_keyboard.py` backend don't exist yet. Upgrade first:

```bash
scp microjs8_0.0.12-1_all.deb pi@PI-2W-TEST:/tmp/
ssh pi@PI-2W-TEST
sudo apt install -y /tmp/microjs8_0.0.12-1_all.deb
```

### 2. Enable the Pi's hardware UART

The Pi Zero 2W's primary UART (`/dev/serial0` → `/dev/ttyAMA0`) is by
default reserved for the serial console. We need it for the Cardputer.

```bash
# Stop microjs8 while we reconfigure
sudo systemctl stop microjs8.service

# Remove the serial console from cmdline.txt
sudo sed -i 's/console=serial0,115200 //' /boot/firmware/cmdline.txt

# Enable the UART hardware and free PL011 from Bluetooth
sudo bash -c 'cat >> /boot/firmware/config.txt' << 'EOF'

# Enable hardware UART for microjs8-cardputer-link
enable_uart=1
dtoverlay=miniuart-bt
EOF

# Verify the changes look right
grep -E '^enable_uart|^dtoverlay' /boot/firmware/config.txt
cat /boot/firmware/cmdline.txt | grep -v console=serial0   # should match — no console=serial0

# Reboot
sudo reboot
```

After reboot:

```bash
ssh pi@PI-2W-TEST
ls -la /dev/serial0          # should symlink to ttyAMA0
groups | tr ' ' '\n' | grep dialout    # microjs8 user already added by postinst
```

### 3. Switch the HMI config

Edit `/etc/microjs8/config.toml` and add (or update) the `[hmi]`
section:

```toml
[hmi]
keyboard = "uart"
uart_device = "/dev/serial0"
uart_baud = 115200
```

Defaults if you omit fields: `device="/dev/serial0"`, `baud=115200`.

### 4. Start MicroJS8

```bash
sudo systemctl start microjs8.service
journalctl -fu microjs8.service
```

Look for a line like:

```
INFO microjs8.input.uart_keyboard: reading from /dev/serial0 @ 115200 baud
```

That confirms the backend started. If you see a `pyserial not installed`
error, install it with `sudo apt install python3-serial` (the v0.0.12
postinst should have done this automatically).

## Testing the link

The simplest end-to-end check — type on the Cardputer keyboard and
watch the daemon's journal:

```bash
journalctl -fu microjs8.service | grep -i 'key\|input'
```

You should see key events being routed (look for entries from
`microjs8.input.router` or similar). Or simply navigate the screen
ring with `← →` arrows — if the screens cycle, the link is alive.

To diagnose at the byte level (before MicroJS8 consumes them), stop
the daemon temporarily and `cat` the serial port:

```bash
sudo systemctl stop microjs8.service
stty -F /dev/serial0 115200 cs8 -cstopb -parenb raw -echo
cat /dev/serial0
# type on Cardputer; expect: CHAR:k, ENTER, UP, etc.
# Ctrl+C to stop
sudo systemctl start microjs8.service
```

## Power budget

Battery-only runtime with the Cardputer powering the Pi:

| Load | Approx draw | Source |
|---|---|---|
| Cardputer firmware (idle backlight, scanning) | ~80 mA @ 3.7V = 0.3 W | bench measured |
| Pi Zero 2W under MicroJS8 (audio, occasional TX) | ~250 mA @ 5V = 1.25 W | typical |
| Boost converter 3.7→5V loss | ~15% | typical |
| **Combined runtime from 1750 mAh** | **~3.8 hours** | calculated |

For longer ops, plug a USB-C power bank into the Cardputer's USB-C
port — the bank tops up the internal battery while everything runs.
With a 10000 mAh USB-C bank, expect 30+ hours continuous.

## Troubleshooting

**No keystrokes reach the daemon**

1. Confirm the Pi UART path: `sudo systemctl stop microjs8.service; cat /dev/serial0` and type on Cardputer. If chars appear → daemon-side issue (skip to next section). If nothing appears → wiring or firmware issue (see [microjs8-cardputer-link README](https://github.com/W5DMH/microjs8-cardputer-link)).
2. Confirm `/dev/serial0` exists: `ls -la /dev/serial0` (must symlink to ttyAMA0).
3. Confirm the microjs8 user is in `dialout`: `groups microjs8` (should list dialout).
4. Confirm `[hmi] keyboard = "uart"` in `/etc/microjs8/config.toml`.

**`pyserial not installed` in the journal**

```bash
sudo apt install python3-serial
sudo systemctl restart microjs8.service
```

**Pi keeps brown-out rebooting once 5V wire connected**

The Cardputer's boost converter can't sustain the Pi's current draw
(Pi 4 won't work; Pi Zero 2W should). Falls into one of two camps:

- **Inrush at boot:** add a 220-470 µF, ≥6.3V electrolytic cap across
  Pi's 5V and GND pins to buffer the spike.
- **Sustained draw too high:** keep the Pi on its own USB-C power.
  The Cardputer remains the keyboard but doesn't power the Pi.

**Battery drains abnormally fast**

Check `top` on the Pi — if a runaway process is consuming CPU, the
Pi's draw is higher than expected. Expected drain at idle MicroJS8
load is around 0.5-1% / minute.

**Want to switch back to USB keyboard**

Just edit `/etc/microjs8/config.toml`:

```toml
[hmi]
keyboard = "usb"
```

Then `sudo systemctl restart microjs8.service`. The UART backend is
not started and the existing USB HID code takes over.

You can leave the EXT cable connected — the Cardputer keystrokes will
just be ignored. Or unplug it; doesn't matter.

## Architecture notes

The two backends (`KeyboardThread` for USB HID, `UartKeyboardThread`
for UART) are mutually exclusive at runtime — the daemon instantiates
exactly one based on `[hmi] keyboard`. Both emit identical `KeyEvent`
objects into the same `InputRouter`, so the screen ring, COMPOSE
buffer, and all other UI code is backend-agnostic.

Why not both at once? Two reasons:

1. **Determinism.** With one source, an event is what the operator
   pressed. With two, you have to choose which wins on simultaneous
   presses, and the "merge" semantics are surprising.
2. **Cardputer firmware already resolves modifiers.** Mixing CHAR:&
   from the Cardputer with raw scancodes from a USB keyboard would
   require a third layer that knows which source pre-resolved Shift.

If you want both keyboards available physically, plug both in — the
configured backend will work, the other will sit idle. Switching is
a config-edit + service-restart.

## See also

- [microjs8-cardputer-link firmware](https://github.com/W5DMH/microjs8-cardputer-link) — what runs on the Cardputer
- `docs/HARDWARE_BRINGUP.md` — bare-Pi-2W + Waveshare wiring (USB keyboard variant)
- M5 official Cardputer ADV docs — https://docs.m5stack.com/en/core/Cardputer-Adv
