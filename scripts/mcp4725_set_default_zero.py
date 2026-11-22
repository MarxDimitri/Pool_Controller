#!/usr/bin/env python3
"""
MCP4725: Einschalt-Startwert dauerhaft auf 0 V setzen (EEPROM) — EINMALIG VON HAND.

Hintergrund: Der MCP4725 lädt beim Einschalten den Wert aus seinem EEPROM.
Werkseitig ist das meist die Mitte (Code 2048 ≈ 50 % → ca. 5 V am LM358-
Ausgang). Bis der Dienst startet und 0 V schreibt, würde die Pumpe also auf
halber Drehzahl anlaufen bzw. nach einem DAC-Reset dorthin springen.

NICHT bei jedem Dienststart ausführen: Das EEPROM verträgt nur begrenzt viele
Schreibzyklen (typ. 1 Mio.). Ein einziger Aufruf genügt.

Ausführen auf dem Pi:
    sudo systemctl stop pool_marx          # empfohlen, damit nichts dazwischenfunkt
    /opt/pool_marx/pool_controller/venv/bin/python scripts/mcp4725_set_default_zero.py --yes
    sudo systemctl start pool_marx

Nur lesen (kein Schreiben):
    /opt/pool_marx/pool_controller/venv/bin/python scripts/mcp4725_set_default_zero.py --check

Ergebnis prüfen:
    1. Das Skript liest das EEPROM zurück und meldet "EEPROM = 0".
    2. Praktischer Test: Pool-Steuerung (bzw. den MCP4725) komplett stromlos
       machen, wieder einschalten und — BEVOR der Dienst startet, z. B. mit
       `sudo systemctl stop pool_marx` vor dem Ausschalten bzw. mit
       abgezogenem Pumpen-Steuerkabel — am Ausgang des LM358 mit dem
       Multimeter messen: Soll 0 V (± 0,05 V), nicht ca. 5 V.
"""

import argparse
import sys
import time

ADDR = 0x60
CMD_WRITE_DAC_AND_EEPROM = 0x60   # C2..C0 = 011, Power-Down-Bits = 00


def read_status(bus):
    """5 Bytes lesen: Status, DAC-Register (2), EEPROM (2)."""
    from smbus2 import i2c_msg
    msg = i2c_msg.read(ADDR, 5)
    bus.i2c_rdwr(msg)
    b = list(msg)
    ready = bool(b[0] & 0x80)
    dac_code = (b[1] << 4) | (b[2] >> 4)
    eeprom_pd = (b[3] >> 5) & 0x03
    eeprom_code = ((b[3] & 0x0F) << 8) | b[4]
    return ready, dac_code, eeprom_code, eeprom_pd


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="wirklich ins EEPROM schreiben")
    ap.add_argument("--check", action="store_true", help="nur auslesen")
    ap.add_argument("--bus", type=int, default=1, help="I²C-Bus (Standard 1)")
    args = ap.parse_args()

    try:
        from smbus2 import SMBus
    except ImportError:
        print("smbus2 fehlt — nur auf dem Raspberry Pi im venv ausführen.")
        return 2

    with SMBus(args.bus) as bus:
        ready, dac, eeprom, pd = read_status(bus)
        print(f"Vorher: DAC-Register={dac}, EEPROM-Startwert={eeprom}, PD-Bits={pd}")
        if args.check:
            return 0 if eeprom == 0 and pd == 0 else 1
        if eeprom == 0 and pd == 0:
            print("EEPROM ist bereits 0 — nichts zu tun (spart einen Schreibzyklus).")
            return 0
        if not args.yes:
            print("Abbruch: zum Schreiben --yes angeben.")
            return 1

        bus.write_i2c_block_data(ADDR, CMD_WRITE_DAC_AND_EEPROM, [0x00, 0x00])
        # EEPROM-Schreiben dauert typ. 25 ms, max. 50 ms → RDY-Bit abwarten
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            time.sleep(0.02)
            ready, dac, eeprom, pd = read_status(bus)
            if ready:
                break
        print(f"Nachher: DAC-Register={dac}, EEPROM-Startwert={eeprom}, PD-Bits={pd}, bereit={ready}")
        if ready and eeprom == 0 and pd == 0:
            print("OK: EEPROM = 0 → MCP4725 startet künftig mit 0 V. "
                  "Jetzt stromlos machen und am Ausgang 0 V messen (siehe Kopfkommentar).")
            return 0
        print("FEHLER: EEPROM nicht wie erwartet — Verkabelung/Adresse prüfen.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
