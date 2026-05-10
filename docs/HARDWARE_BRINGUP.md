# MicroJS8 — Hardware Bring-up Runbook

This document walks through first contact with a fresh M5Stack CardputerZero, from out-of-the-box to a JS8 transceiver running on the device.

It assumes you have:

- A CardputerZero with the M5Stack-shipped Debian image flashed
- SSH access (the M5Stack image enables `ssh` by default; root login may need to be enabled)
- A built `microjs8_*.deb` (from `python3 scripts/build_deb.py` or downloaded from a tagged GitHub Release)
- A USB radio interface (QDX, DigiRig, or similar — anything Hamlib supports)

Each step has a **validation** criterion. Don't move to the next step until the current one passes — `microjs8 --doctor` is the canonical "is everything wired up" check.

---

## 1. Initial network + SSH access

Find the device on your network and SSH in:

```bash
# Find by hostname (M5Stack image typically uses 'cardputerzero')
ssh pi@cardputerzero.local

# Or by IP from your router's DHCP leases
ssh pi@192.168.1.xxx
```

The default credentials vary by image revision; check M5Stack's wiki or the back of the device's box. Change the password on first login.

**Validation:** prompt shows `pi@cardputerzero $`. `cat /etc/os-release` reports a Debian-derivative.

---

## 2. Install runtime dependencies

MicroJS8 uses system Python with apt-installed dependencies (Phase 5 retired the bundled venv MiniJS8 needed). One-time setup:

```bash
sudo apt update
sudo apt install -y \
  python3 python3-pil python3-numpy python3-evdev \
  python3-sounddevice python3-serial \
  fonts-dejavu-core adduser systemd-sysv \
  hamlib-utils chrony gpsd evtest
```

Notes:
- `hamlib-utils` provides `rigctld` for the radio CAT side.
- `chrony` is the time source. Without it, MicroJS8 falls back to radio-derived consensus after ≥3 frames decoded — works but slower to first TX.
- `evtest` is for capturing Fn-key scancodes in step 5.

**Validation:** `python3 -c "import PIL, evdev, numpy, sounddevice, serial; print('ok')"` prints `ok`.

---

## 3. Install the .deb

Copy the `.deb` to the device (scp from your dev box) and install:

```bash
scp microjs8_0.0.1-1_all.deb pi@cardputerzero.local:~/

# On the CardputerZero
sudo dpkg -i ~/microjs8_0.0.1-1_all.deb
```

The postinst script:
1. Creates a system user `microjs8` with no shell, home `/var/lib/microjs8`
2. Adds `microjs8` to groups `audio dialout video i2c input`
3. Creates `/var/lib/microjs8/` (mode 0750, owned by `microjs8`)
4. Copies `/etc/microjs8/config.toml.default` → `/etc/microjs8/config.toml` (only on first install — preserves operator edits across upgrades)
5. `systemctl enable --now microjs8.service`

**Validation:**

```bash
systemctl status microjs8           # should show "active (running)"
journalctl -u microjs8 -n 50 --no-pager
```

The journal should show `MicroJS8 0.0.1 starting` and a series of `_start_*_best_effort` lines. Some will fail at this point (no callsign yet, no real radio yet) — that's expected.

---

## 4. Run the doctor

```bash
sudo -u microjs8 /usr/share/APPLaunch/bin/microjs8 --doctor
```

The doctor probes every subsystem and prints a structured report. Expected first-boot state on a fresh CardputerZero with no operator config yet:

| Subsystem | Expected status | Action |
|---|---|---|
| Display (Phase 5) | `[ OK ]` `fb<N>: fb_st7789v 320×170@16bpp` | none |
| Keyboard (Phase 3) | `[ OK ]` evdev present + `[WARN]` placeholder scancodes | proceed to step 5 |
| Battery (Phase 6) | `[ OK ]` BQ27220 percentage shown | none |
| Backlight (Phase 3) | `[ OK ]` brightness reported | none |
| Audio | `[ OK ]` device count, OR `[WARN]` if no radio plugged in | plug radio |
| Time source | `[ OK ]` chrony synced | none (assuming network access) |
| User identity | `[ OK ]` running as `microjs8` with all groups | none |
| Configuration | `[WARN]` callsign N0CALL + grid empty | proceed to step 6 |

**Anything red `[FAIL]` is an actionable issue — the doctor tells you what to do.**

**Validation:** all `[FAIL]` lines have been resolved or are clearly explained by the report.

---

## 5. Capture the real Fn+B and Fn+Q scancodes

Phase 3 left the `Fn+B` and `Fn+Q` evdev scancodes as placeholders (87/88 = KEY_F11/F12) because we couldn't read the real values without hardware. This step captures them.

```bash
# Find the keyboard's evdev device
ls -la /dev/input/by-path/ | grep i2c-event

# Run evtest, expect to need sudo unless you're in 'input' group
sudo evtest /dev/input/by-path/platform-3f804000.i2c-event
```

`evtest` enters interactive mode. Now:

1. **Press and release Fn+B once.** Look for an event line like:
   ```
   Event: ... type 1 (EV_KEY), code <NNN> (KEY_xxx), value 1
   ```
   The integer `<NNN>` is the scancode for `Fn+B`. Write it down.

2. **Press and release Fn+Q once.** Same — note the scancode.

3. Press `Ctrl+C` to exit `evtest`.

Now register the real values via a systemd drop-in:

```bash
sudo mkdir -p /etc/systemd/system/microjs8.service.d
sudo tee /etc/systemd/system/microjs8.service.d/scancodes.conf <<EOF
[Service]
Environment=MICROJS8_FN_B_KEYCODE=<scancode_for_fn_b>
Environment=MICROJS8_FN_Q_KEYCODE=<scancode_for_fn_q>
EOF
sudo systemctl daemon-reload
sudo systemctl restart microjs8
```

**Validation:** `microjs8 --doctor` now reports `[ OK ] Fn key overrides set: FN_B=<n>, FN_Q=<n>`.

---

## 6. Configure callsign and grid

Two paths:

**Via the Setup screen (recommended):** the daemon's UI is running. Navigate to Setup (the leftmost tile), edit `Call` to your real callsign, edit `Grid` to your Maidenhead grid (e.g. `EN82` or `EN82dj`), Enter to commit. The daemon writes `/etc/microjs8/config.toml` and the change takes effect immediately.

**Via the file directly (if the screen isn't up yet):**

```bash
sudo systemctl stop microjs8
sudo nano /etc/microjs8/config.toml      # set callsign and grid
sudo systemctl start microjs8
```

**Validation:** `microjs8 --doctor` reports `[ OK ] callsign: <yours>` and `[ OK ] grid: <yours>`.

---

## 7. Confirm the APPLaunch tile

The `.deb` registered MicroJS8 with the APPLaunch UI by dropping `/usr/share/APPLaunch/applications/microjs8.desktop`. After install, the launcher should show a MicroJS8 tile.

```bash
# Sanity-check the .desktop entry is present and parseable
cat /usr/share/APPLaunch/applications/microjs8.desktop
```

If the launcher UI doesn't show the tile after a refresh, reboot:

```bash
sudo reboot
```

After reboot, the systemd service restarts the daemon AND the APPLaunch UI rescans the applications directory.

**Validation:** the MicroJS8 tile appears in the APPLaunch menu. Tapping it focuses the running daemon's UI.

---

## 8. Plug in the radio and validate the audio path

Connect your radio interface (QDX, DigiRig, etc.) via USB. Wait ~5 seconds for udev to enumerate it.

```bash
# Discovery — sounddevice should now show the radio's audio
python3 -c "import sounddevice; print(sounddevice.query_devices())"

# Check that the daemon's audio capture starts
journalctl -u microjs8 -n 50 | grep -iE 'audio|capture|sound'
```

The journal should show `audio capture started: device=<idx> rate=12000` or similar. If audio fails to start:

- `[FAIL]` in `--doctor` will tell you what's missing
- Verify the radio's audio interface is recognised: `aplay -l` and `arecord -l` should both list it
- Check `dmesg | tail` for kernel-level USB enumeration errors

**Validation:** the daemon's HEARD list starts populating with decoded JS8 frames after a minute or two on an active band (try 7.078 MHz or 14.078 MHz daytime).

---

## 9. First TX

This is the moment of truth. Pre-conditions (the daemon's TX safety gate):

- Callsign set (step 6)
- Grid set (step 6)
- Time synced (chrony OK or ≥3 decoded frames for radio-consensus fallback)
- Battery not critical (BQ27 reports >5% OR is charging — see §6.11 of build spec)
- CAT control to the radio working (`systemctl status rigctld` shows active)

Trigger a manual TX:

1. Navigate to a heard station on the HEARD list, press Enter to focus it
2. The COMPOSE screen pops up with `TO=<their_call>` pre-populated
3. Tab to the TEXT field, type `TEST DE <YOUR_CALL>`
4. Tab to SEND, press Enter

Watch the journal:

```bash
journalctl -u microjs8 -f
```

You should see:
```
scheduler: enqueueing tx ...
tx_backend: PTT ON
tx_backend: ... bytes encoded
tx_backend: PTT OFF
```

**Validation:** the radio actually transmits (you can hear it relay through the speaker, or another receiver picks it up).

---

## Common issues

### Daemon refuses to start: "User=microjs8 unknown"

Postinst didn't create the user. Re-run:

```bash
sudo dpkg-reconfigure microjs8
```

If that fails, manually:

```bash
sudo addgroup --system microjs8
sudo adduser --system --ingroup microjs8 --no-create-home \
             --home /var/lib/microjs8 --shell /usr/sbin/nologin \
             --disabled-password microjs8
for g in audio dialout video i2c input; do sudo adduser microjs8 "$g"; done
sudo systemctl restart microjs8
```

### Display is blank / shows artefacts

The framebuffer driver isn't presenting a 320×170 16bpp panel. Check:

```bash
cat /proc/fb                       # should include 'fb_st7789v'
ls /sys/class/graphics/fb*/        # confirm sysfs attributes exist
cat /sys/class/graphics/fb<N>/virtual_size   # should be "320,170"
cat /sys/class/graphics/fb<N>/bits_per_pixel # should be "16"
```

If these don't match, the M5Stack DT overlay isn't loaded. This is a M5Stack image issue, not a MicroJS8 issue — file with M5Stack support.

### Battery row shows '--' even though the cap is connected

The kernel's BQ27 driver may not be auto-binding. Check:

```bash
ls /sys/class/power_supply/        # any bq27* entry?
sudo modprobe bq27xxx_battery_i2c
ls /dev/i2c-*                      # the BQ27 lives on i2c
sudo i2cdetect -y 0                # 0x55 is the BQ27220's typical address
```

Once `bq27xxx_battery_i2c` is loaded, the `BatteryReader`'s 30-second rediscovery loop will pick it up — no need to restart the daemon.

### Keyboard works but Fn+B doesn't toggle backlight, Fn+Q doesn't shutdown

Step 5 wasn't completed, or the captured scancodes are wrong. Re-run `evtest` and double-check the values.

### Logs are spammy or missing important info

Adjust journal verbosity:

```bash
sudo journalctl -u microjs8 -p err --since '5 min ago'    # errors only
sudo journalctl -u microjs8 -f                            # follow live
```

To make the daemon itself more verbose, set in `/etc/systemd/system/microjs8.service.d/`:

```
[Service]
Environment=PYTHONLOGLEVEL=DEBUG
```

---

## Glossary of paths

| Path | What |
|---|---|
| `/usr/share/APPLaunch/bin/microjs8` | Launcher shell script |
| `/usr/share/APPLaunch/lib/microjs8/` | Python source tree |
| `/usr/share/APPLaunch/applications/microjs8.desktop` | APPLaunch tile entry |
| `/usr/share/APPLaunch/share/images/microjs8.png` | Tile icon |
| `/lib/systemd/system/microjs8.service` | Systemd unit (distro-installed) |
| `/etc/systemd/system/microjs8.service.d/*.conf` | Operator overrides (drop-ins) |
| `/etc/microjs8/config.toml` | Live config (operator edits this) |
| `/etc/microjs8/config.toml.default` | Default config (read-only, ships in .deb) |
| `/var/lib/microjs8/` | State dir: mailbox.db, retention bookkeeping |
| `/proc/fb` | Framebuffer registry |
| `/sys/class/graphics/fb<N>/` | Framebuffer sysfs |
| `/sys/class/power_supply/bq27*/` | BQ27220 fuel gauge sysfs |
| `/sys/class/backlight/backlight/` | Backlight sysfs |
| `/dev/input/by-path/platform-3f804000.i2c-event` | TCA8418 keyboard evdev |
