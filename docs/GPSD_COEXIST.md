# Running MicroJS8 alongside gpsd

If your station uses both a USB GPS receiver (u-Blox 6/7/8/9, etc.)
and a DigiRig or other CP210x-based USB-serial radio interface, you
will hit a resource conflict on first install unless gpsd is
configured to leave the DigiRig alone.

This guide documents:
  - What the conflict is and how it manifests
  - The recommended gpsd config (USBAUTO=false + DEVICES pin)
  - How to apply the fix on an existing install
  - Why MicroJS8's postinst does NOT modify your gpsd config
    automatically

## Symptom

When MicroJS8 is configured with a DigiRig + radio (any radio
that uses RTS for PTT -- (tr)uSDX, G90 with PTT cable, FM walkies
on the DigiRig audio path) and you also have gpsd running for
GPS time/position:

  - On boot, the radio's PTT keys up immediately as soon as the
    DigiRig is enumerated
  - The radio stays in TX continuously
  - MicroJS8's PTT operations report "CAT disconnected; cannot
    key PTT" in the journal
  - `sudo lsof /dev/ttyUSB0` shows gpsd holding the port

## Root cause

Standard Debian/Raspberry Pi OS gpsd packages ship
`/etc/default/gpsd` with `USBAUTO="true"`. With this setting,
gpsd's udev hotplug helper grabs every USB-serial device that
enumerates, opens it, and sends probe characters to test whether
it's a GPS receiver speaking NMEA.

The DigiRig's CP2102N USB-UART chip has RTS asserted high by
default at the silicon level. When gpsd opens the port, RTS goes
high, which the DigiRig's optoisolator translates to PTT-asserted
on the radio. Even after gpsd decides "not a GPS" and releases
the port, the CP2102N can latch RTS high until a USB device reset.

Meanwhile, MicroJS8 tries to open the same `/dev/ttyUSB0` for its
own RTS-PTT control, finds gpsd has it, and reports the port as
unusable. The radio is stuck in TX because the OS itself is keying
it.

## The recommended fix

Edit `/etc/default/gpsd` to:

  1. Pin your actual GPS receiver in `DEVICES` so gpsd always
     knows about it at startup, even when socket-activated.
  2. Set `USBAUTO="false"` so gpsd doesn't auto-grab any other
     USB-serial device.

### Step-by-step

```bash
# 1. Identify your GPS by stable by-id path (survives reboots
#    and enumeration order changes)
ls /dev/serial/by-id/ | grep -i 'u-blox\|garmin\|gps\|gnss'
# Example output:
#   usb-u-blox_AG_-_www.u-blox.com_u-blox_7_-_GPS_GNSS_Receiver-if00

# 2. Back up the existing config
sudo cp /etc/default/gpsd /etc/default/gpsd.bak-$(date +%Y%m%d)

# 3. Write the new config
sudo tee /etc/default/gpsd > /dev/null << 'EOF'
# Devices gpsd should collect to at boot time.
DEVICES="/dev/serial/by-id/usb-u-blox_AG_-_www.u-blox.com_u-blox_7_-_GPS_GNSS_Receiver-if00"

# Other options you want to pass to gpsd
GPSD_OPTIONS=""

# Disable USB autograb so gpsd doesn't accidentally open the
# DigiRig (or any other CP210x-based radio interface) thinking
# it might be a GPS receiver.
USBAUTO="false"
EOF

# 4. Restart gpsd
sudo systemctl restart gpsd.socket gpsd.service

# 5. Verify gpsd is reading your GPS
gpspipe -w -n 5 -x 10 2>&1 | head -20
# Expect:
#   {"class":"VERSION","release":"3.22", ...}
#   {"class":"DEVICES","devices":[{"path":"/dev/serial/by-id/usb-u-blox..."}]}
#   {"class":"WATCH","enable":true, ...}
#   {"class":"TPV","mode":3,"lat":...,"lon":...}

# 6. Confirm the DigiRig is NOT being touched by gpsd
sudo lsof /dev/ttyUSB0 2>&1
# Expect: NO gpsd entries (only microjs8 if running)

# 7. Restart MicroJS8 to reconnect with the now-correct port state
sudo systemctl restart microjs8 2>/dev/null \
  || sudo microjs8-launch
```

## How MicroJS8's postinst handles this

The postinst section 7.5 detects the common conflict condition and
auto-applies a conservative fix. Specifically, it checks `/etc/default/gpsd`
and, if EITHER of these is true:

  - `USBAUTO="true"` (the Debian default), OR
  - `DEVICES=""` (the Debian default)

then it:

  1. Backs up the file to `/etc/default/gpsd.pre-microjs8-<timestamp>`
  2. Sets `USBAUTO="false"`
  3. If `DEVICES` was empty, sets it to `/dev/ttyACM0` (the typical
     u-Blox CDC-ACM path on Pi OS)
  4. Restarts gpsd if it's currently running

If your GPS device is at a different path (e.g., a USB GPS that
enumerates as `/dev/ttyUSB1`, or you prefer the by-id path for
robustness), the postinst's hardcoded `/dev/ttyACM0` may not match
and you'll need to override. The original config is preserved in
the timestamped backup file so you can compare and restore.

## Recommended: use a stable by-id path

The postinst's `/dev/ttyACM0` is correct in many cases but is NOT
stable across reboots if you ever plug additional devices in.
Prefer the by-id path:

```bash
# Find the stable name
ls /dev/serial/by-id/ | grep -i 'u-blox\|garmin\|gps\|gnss'

# Edit /etc/default/gpsd and replace the DEVICES line with the
# by-id path. Example:
sudo nano /etc/default/gpsd
# Change:
#   DEVICES="/dev/ttyACM0"
# To:
#   DEVICES="/dev/serial/by-id/usb-u-blox_AG_-_www.u-blox.com_u-blox_7_-_GPS_GNSS_Receiver-if00"

# Restart gpsd
sudo systemctl restart gpsd.socket gpsd.service
```

## Reverting

To restore the original config:

```bash
sudo cp /etc/default/gpsd.bak-YYYYMMDD /etc/default/gpsd
sudo systemctl restart gpsd.socket gpsd.service
```

(Backup files are named with the date you applied the fix.)

## Related: udev defense-in-depth

MicroJS8's udev rule (`50-microjs8-digirig.rules` since v0.0.18)
also sets `ID_GPSD_IGNORE=1` and `ID_MM_DEVICE_IGNORE=1` on the
DigiRig's port. These flags are honored by gpsd's hotplug helper
and ModemManager IF those tools respect the udev properties --
which has been unreliable across recent versions. The
`/etc/default/gpsd` change above is the reliable defense; the
udev rule is belt-and-suspenders.

## Quick check: am I affected?

Run this on your host:

```bash
# Are you running gpsd?
systemctl is-active gpsd 2>/dev/null
# If "inactive", you're not affected.

# Is USBAUTO enabled?
grep '^USBAUTO=' /etc/default/gpsd 2>/dev/null
# If "USBAUTO=\"false\"" or not present, you're not affected.

# Do you have a CP210x?
lsusb | grep -i 'CP210\|Silicon Labs CP'
# If empty, you're not affected.
```

If all three return positive answers, you should apply the fix
above before running MicroJS8 with a DigiRig.
