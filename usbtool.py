#!/usr/bin/env python3
"""
Talk to the camera at the USB level.

The camera exposes mass storage over Bulk-Only Transport (subclass 6 = SCSI
transparent, protocol 0x50 = BOT), which means we can speak SCSI to it directly
by writing the transport ourselves over libusb -- no kext, no IOKit.

That gives us a command channel into the firmware, which is the most promising
route to flipping it into the webcam personality the manual claims exists.

Safety: this only issues read-only/informational SCSI commands by default.
Vendor opcode probing is opt-in and skips anything in DANGEROUS_OPCODES, because
a blind sweep on a device like this can hit firmware-write or format commands.

    python3 usbtool.py info      # descriptors + standard SCSI identity
    python3 usbtool.py vpd       # vendor/EVPD inquiry pages
    python3 usbtool.py probe     # careful vendor opcode probe (read-only shapes)
"""

from __future__ import annotations

import struct
import subprocess
import sys
import time

import usb.backend.libusb1
import usb.core
import usb.util

VID, PID = 0x1B3F, 0x8301
LIBUSB = "/opt/homebrew/lib/libusb-1.0.dylib"

CBW_SIG = 0x43425355  # 'USBC'
CSW_SIG = 0x53425355  # 'USBS'

# Never send these blind: they erase, overwrite, or reflash.
DANGEROUS_OPCODES = {
    0x04,  # FORMAT UNIT
    0x0A, 0x2A, 0x8A, 0xAA,  # WRITE(6/10/16/12)
    0x2E, 0xAE,  # WRITE AND VERIFY
    0x41,  # WRITE SAME
    0x42,  # UNMAP
    0x3B,  # WRITE BUFFER  <- classic firmware-flash path
    0x3F,  # WRITE LONG
    0x51,  # ERASE
    0x2C, 0x2D,  # ERASE(10/12)
}


def get_device():
    backend = usb.backend.libusb1.get_backend(find_library=lambda _: LIBUSB)
    dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
    if dev is None:
        sys.exit("camera not found on the USB bus -- is it plugged in?")
    return dev


def decode_swapped(text: str) -> str:
    """Undo the firmware's mangled string descriptors.

    The strings come back transposed *and* shifted by one byte -- dropping the
    leading byte and swapping adjacent pairs turns
    'ЉeGenir cSU BaMssS otaregD veci' back into
    'Generic USB Mass Storage Device'.
    """
    body = text[1:]
    out = []
    for i in range(0, len(body) - 1, 2):
        out.append(body[i + 1])
        out.append(body[i])
    if len(body) % 2:
        out.append(body[-1])
    return "".join(out)


def probe_control(dev) -> None:
    """Sweep vendor control requests on endpoint 0.

    Endpoint 0 needs no interface claim, so this works even while macOS's
    mass-storage driver owns the device. Read-direction only (0xC0/0xA1) -- we
    are looking for a request the firmware answers, not poking state.
    """
    print("\nvendor control-request sweep (read direction, endpoint 0)")
    hits = []
    for req_type in (0xC0, 0xA1, 0x80):
        for request in range(0x00, 0x100):
            try:
                data = dev.ctrl_transfer(req_type, request, 0, 0, 64, timeout=400)
            except usb.core.USBError:
                continue
            except Exception:
                continue
            if data:
                raw = bytes(data)
                printable = "".join(chr(b) if 32 <= b < 127 else "." for b in raw)
                print(f"  ANSWERED type=0x{req_type:02x} req=0x{request:02x}: "
                      f"{raw[:24].hex()} |{printable[:24]}|")
                hits.append((req_type, request))
    print(f"\n  {len(hits)} vendor control requests answered")
    if not hits:
        print("  (endpoint 0 exposes nothing beyond the standard requests)")


class BOT:
    """USB Bulk-Only Transport: CBW out, data phase, CSW in."""

    def __init__(self, dev):
        self.dev = dev
        self.tag = 0
        cfg = dev.get_active_configuration()
        self.intf = cfg[(0, 0)]
        self.ep_out = usb.util.find_descriptor(
            self.intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress)
            == usb.util.ENDPOINT_OUT)
        self.ep_in = usb.util.find_descriptor(
            self.intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress)
            == usb.util.ENDPOINT_IN)

    def command(self, cdb: bytes, data_len: int = 0, data_out: bytes | None = None,
                timeout: int = 3000) -> tuple[bytes, int]:
        """Run one SCSI command. Returns (data, status). status 0 = success."""
        self.tag = (self.tag + 1) & 0xFFFFFFFF
        flags = 0x80 if (data_len and data_out is None) else 0x00
        cdb_len = len(cdb)
        if not 1 <= cdb_len <= 16:
            raise ValueError(f"SCSI CDB must be 1..16 bytes, got {cdb_len}")
        cdb = cdb.ljust(16, b"\x00")
        cbw = struct.pack("<IIIBBB16s", CBW_SIG, self.tag,
                          len(data_out) if data_out else data_len,
                          flags, 0, cdb_len, cdb)
        self.ep_out.write(cbw, timeout)

        data = b""
        try:
            if data_out:
                self.ep_out.write(data_out, timeout)
            elif data_len:
                data = bytes(self.ep_in.read(data_len, timeout))
        except usb.core.USBError:
            # A stalled data phase is a normal "command not supported" answer.
            self._clear_stalls()

        try:
            csw = bytes(self.ep_in.read(13, timeout))
        except usb.core.USBError:
            self._clear_stalls()
            try:
                csw = bytes(self.ep_in.read(13, timeout))
            except usb.core.USBError:
                return data, -1

        if len(csw) < 13 or struct.unpack("<I", csw[:4])[0] != CSW_SIG:
            return data, -2
        return data, csw[12]

    def _clear_stalls(self) -> None:
        for ep in (self.ep_in, self.ep_out):
            try:
                self.dev.clear_halt(ep.bEndpointAddress)
            except usb.core.USBError:
                pass


def unmount_volume() -> bool:
    """Free the interface from the kernel's mass-storage driver.

    macOS will not hand us the bulk endpoints while the volume is mounted; the
    device stays enumerated after an unmount, which is what we want.
    """
    out = subprocess.run(["diskutil", "list"], capture_output=True).stdout.decode()
    for line in out.splitlines():
        if "NO NAME" in line:
            node = line.split()[-1]
            subprocess.run(["diskutil", "unmount", f"/dev/{node}"], capture_output=True)
            time.sleep(1)
            return True
    return False


def open_bot(dev) -> BOT:
    unmount_volume()
    try:
        usb.util.claim_interface(dev, 0)
    except usb.core.USBError as exc:
        sys.exit(f"could not claim interface 0: {exc}\n"
                 "the volume may still be mounted -- eject it in Finder and retry")
    return BOT(dev)


# -- commands ------------------------------------------------------------

def cmd_info(dev) -> None:
    print(f"\ndevice   {VID:04x}:{PID:04x}")
    for name, idx in (("manufacturer", dev.iManufacturer), ("product", dev.iProduct)):
        try:
            raw = usb.util.get_string(dev, idx)
        except usb.core.USBError:
            raw = None
        if raw:
            print(f"  {name:12} {raw!r}")
            print(f"  {'':12} -> unswapped: {decode_swapped(raw)!r}")

    cfg = dev.get_active_configuration()
    print(f"\nconfigurations: {dev.bNumConfigurations}")
    for intf in cfg:
        print(f"  interface {intf.bInterfaceNumber} alt {intf.bAlternateSetting}: "
              f"class {intf.bInterfaceClass} sub {intf.bInterfaceSubClass} "
              f"proto {intf.bInterfaceProtocol}")

    bot = open_bot(dev)
    data, status = bot.command(bytes([0x12, 0, 0, 0, 36, 0]), 36)
    print(f"\nSCSI INQUIRY (status {status}):")
    if data and len(data) >= 36:
        print(f"  vendor   {data[8:16].decode(errors='replace')!r}")
        print(f"  product  {data[16:32].decode(errors='replace')!r}")
        print(f"  revision {data[32:36].decode(errors='replace')!r}")
        print(f"  raw      {data.hex()}")


def cmd_vpd(dev) -> None:
    """Vendor/EVPD inquiry pages often advertise nonstandard capabilities."""
    bot = open_bot(dev)
    print("\nEVPD inquiry pages:")
    for page in [0x00, 0x80, 0x83, 0xB0, 0xB1, 0xB2, 0xC0, 0xC1, 0xD0, 0xF0, 0xFF]:
        data, status = bot.command(bytes([0x12, 0x01, page, 0, 64, 0]), 64)
        if status == 0 and data:
            printable = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
            print(f"  page {page:02x}: {data[:32].hex()}  |{printable[:32]}|")


def cmd_probe(dev) -> None:
    """Probe vendor opcode space for anything the firmware answers.

    Only read-shaped commands, and never anything in DANGEROUS_OPCODES. A
    command the device accepts (status 0) on a vendor opcode is a live firmware
    entry point worth investigating.
    """
    bot = open_bot(dev)
    print("\nprobing vendor opcode space (read-shaped, destructive opcodes skipped)")
    hits = []
    # SCSI reserves 0xC0-0xFF for vendor use; 0x80-0xBF holds some too.
    for opcode in list(range(0xC0, 0x100)) + [0x80, 0x81, 0x82, 0x83, 0x84, 0x85]:
        if opcode in DANGEROUS_OPCODES:
            continue
        data, status = bot.command(bytes([opcode, 0, 0, 0, 0, 0]), 64, timeout=800)
        if status == 0:
            printable = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
            print(f"  ACCEPTED opcode {opcode:02x}: {data[:32].hex()} |{printable[:32]}|")
            hits.append(opcode)
    print(f"\n  {len(hits)} vendor opcodes accepted: {[hex(h) for h in hits]}")
    if not hits:
        print("  (no vendor opcodes answered -- firmware likely has no SCSI command channel)")


def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else "info"
    dev = get_device()
    {"info": cmd_info, "vpd": cmd_vpd, "probe": cmd_probe,
     "ctrl": lambda d: probe_control(d)}.get(action, cmd_info)(dev)
    print()


if __name__ == "__main__":
    main()
