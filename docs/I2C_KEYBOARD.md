# I2C keyboard link (CardKB v1.1)

v0.0.16+ supports the M5Stack CardKB v1.1 as a keyboard alongside
the USB and Cardputer-ADV-via-UART options. The CardKB is a
credit-card-sized 50-key QWERTY keyboard with a fixed I2C interface
at address `0x5F` -- smaller and cheaper than the Cardputer ADV when
you only need a keyboard (no display, no battery).

This document covers:

- Hardware: where to buy, what to wire
- Setup: enable I2C on the Pi and verify the bus
- Config: tell MicroJS8 to use the CardKB
- Verify: confirm it works
- Troubleshooting: common failure modes

## Hardware

- **Part:** M5Stack CardKB v1.1, SKU `U035-B`
- **Digi-Key:** `2221-U035-B-ND`
- **M5Stack store:** https://shop.m5stack.com/products/cardkb-mini-keyboard-programmable-unit-v1-1-mega8a
- **What's in the box:** the CardKB unit + a 20cm HY2.0-4P Grove cable

The CardKB and the Cardputer ADV's internal keyboard are NOT the same.
The Cardputer ADV uses a 56-key matrix scanned by an ESP32-S3 over
UART. The CardKB uses a 50-key matrix scanned by an ATMega8A over
I2C. Different protocols entirely; different MicroJS8 backends
(`uart_keyboard.py` vs `i2c_keyboard.py`).

## Setup

### 1. Enable I2C on the Pi

v0.0.16's postinst auto-enables I2C on fresh installs. If you're
running v0.0.16+ for the first time on a Pi that didn't have I2C
enabled before, you should see this line in the install transcript:

```
microjs8: enabled I2C in /boot/firmware/config.txt -- REBOOT REQUIRED for /dev/i2c-1
```

If so, reboot the Pi. After reboot, verify:

```bash
ls -l /dev/i2c-1
# Expect: crw-rw---- 1 root i2c ... /dev/i2c-1
```

If you're upgrading from an earlier MicroJS8 version and I2C was
NEVER enabled, the postinst handled it. If you previously
manually disabled I2C (commented out with `#dtparam=i2c_arm=on`),
the postinst respects that choice and leaves it alone -- enable it
yourself when ready:

```bash
sudo sed -i 's/^#dtparam=i2c_arm=on/dtparam=i2c_arm=on/' /boot/firmware/config.txt
sudo reboot
```

### 2. Wire the CardKB to the Pi

Power off the Pi first. The CardKB ships with a 20cm Grove cable;
strip the connector off one end and terminate the 4 wires to
female DuPont jumpers (or use the M5Stack Z076 Grove-to-Pi-header
adapter for a non-destructive option).

| CardKB wire | Signal | Pi physical pin | BCM GPIO |
|---|---|---|---|
| Red | 5V | **2** | (5V rail) |
| Black | GND | **6** | (ground) |
| White | SDA | **3** | BCM 2 |
| Yellow | SCL | **5** | BCM 3 |

**Important:** the Pi has a 3.3V pin at physical pin 1. Do NOT use
it -- the CardKB is a 5V device. Always use pin 2 (5V) for power.

Before powering on, multimeter-verify:

- Continuity from red wire to Pi pin 2: beep
- Continuity from black wire to Pi pin 6: beep
- NO continuity between SDA and SCL (would indicate a short)
- NO continuity from any signal wire to Pi pin 1 (wrong voltage)

### 3. Configure MicroJS8

Default `[hmi] keyboard = "auto"` already includes the I2C backend.
If you have an auto-mode config (the default since v0.0.13), the
CardKB will be discovered automatically alongside USB and UART
backends -- no config edit needed.

To explicitly use only the CardKB (skip USB / UART discovery), edit
the LIVE config:

```bash
sudo nano /var/lib/microjs8/config.toml
```

Add or update the `[hmi]` section:

```toml
[hmi]
keyboard = "i2c"
i2c_bus = 1          # /dev/i2c-1 (default; usually leave this)
i2c_address = 0x5F   # CardKB default; only change if you have a
                     # firmware-modified CardKB at a different address
```

Then restart the daemon:

```bash
sudo systemctl restart microjs8
```

**Reminder:** edit the LIVE config at `/var/lib/microjs8/config.toml`,
NOT the shipped default at `/etc/microjs8/config.toml`. The default
is only read on first install; the daemon reads `/var/lib/microjs8/`
at every restart. See `docs/CARDPUTER_LINK.md` for more on the
two-files distinction.

### 4. Verify

After daemon restart, the journal should show:

```
i2c keyboard thread started: bus=/dev/i2c-1 address=0x5F poll=30ms
```

Tail the journal while pressing a few CardKB keys:

```bash
sudo journalctl -u microjs8 -f
```

Each keypress should drive UI navigation just like the USB or UART
keyboards do.

## Troubleshooting

### `/dev/i2c-1` doesn't exist after reboot

The dtparam may not be parsing. Check:

```bash
grep -E 'i2c_arm|^\[' /boot/firmware/config.txt
```

If the `dtparam=i2c_arm=on` line lands UNDER a non-applicable section
header (like `[cm4]`), it won't apply to your Pi. Move it under `[all]`
or before any section header.

Also check `/etc/modules` contains `i2c-dev`:

```bash
grep i2c-dev /etc/modules
```

If missing:

```bash
echo 'i2c-dev' | sudo tee -a /etc/modules
sudo modprobe i2c-dev
```

### `i2cdetect -y 1` doesn't show 0x5F

Hardware issue, almost always wiring. Re-check:

- 5V (red) -> Pi pin 2, NOT pin 1
- GND (black) -> Pi pin 6 (or any GND pin)
- SDA (white) -> Pi pin 3
- SCL (yellow) -> Pi pin 5
- Wires fully seated in DuPont housings

Cable continuity: probe each Grove wire end-to-end. The Grove
connectors are HY2.0 -- not as durable as DuPont. A bent pin can
cause intermittent contact.

If multiple I2C devices share the bus, check for address conflicts:
the CardKB is at 0x5F; if another device claims that address, one
of them needs to move.

### Daemon journal shows "smbus2 not installed"

The postinst pip-install of smbus2 failed. Install manually:

```bash
sudo pip install --break-system-packages smbus2
sudo systemctl restart microjs8
```

### Daemon journal shows "failed to open /dev/i2c-1"

The microjs8 service user doesn't have access. The postinst adds
the user to the `i2c` group, but on existing installs from older
versions this may not have happened. Fix:

```bash
sudo adduser microjs8 i2c
sudo systemctl restart microjs8
```

### CardKB works but keys feel sluggish

The default poll rate is 33 Hz (30 ms interval). To increase
responsiveness, decrease the interval. There isn't a config setting
for this yet (planned for v0.0.17); the default is tuned for low
CPU. If you really need it faster, edit
`/usr/share/APPLaunch/lib/microjs8/microjs8/input/i2c_keyboard.py`
and change `DEFAULT_POLL_INTERVAL_S = 0.030` to `0.015` (66 Hz),
then `sudo systemctl restart microjs8`. Note that this edit will be
overwritten on the next `apt install`.

### Fn+key combinations don't work

The CardKB's Fn modifier produces special bytes in the 0x80-0x9F
range. MicroJS8's I2C backend currently drops these silently --
they don't correspond to MicroJS8 navigation gestures. If you need
a specific Fn+key combination mapped, please open a GitHub issue
with the exact byte value (use the `i2cget` poll loop in the
PI-2W-TEST bring-up procedure to capture it).
