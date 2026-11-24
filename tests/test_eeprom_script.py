"""pi/mcp4725_set_default_zero.py mit simuliertem smbus2"""

import importlib.util
import sys
import types

import pytest

from tests.conftest import ROOT


class FakeMCP:
    def __init__(self, eeprom=2048):
        self.eeprom = eeprom
        self.dac = eeprom
        self.writes = []


def make_smbus(chip):
    mod = types.ModuleType("smbus2")

    class Msg(list):
        pass

    class i2c_msg:
        @staticmethod
        def read(addr, n):
            m = Msg([0] * n)
            m.addr = addr
            return m

    class SMBus:
        def __init__(self, bus): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass

        def i2c_rdwr(self, msg):
            e = chip.eeprom
            msg[:] = [0xC0, (chip.dac >> 4) & 0xFF, (chip.dac << 4) & 0xF0,
                      (e >> 8) & 0x0F, e & 0xFF]

        def write_i2c_block_data(self, addr, cmd, data):
            chip.writes.append((addr, cmd, data))
            if cmd == 0x60:
                chip.eeprom = chip.dac = (data[0] << 4) | (data[1] >> 4)

    mod.SMBus, mod.i2c_msg = SMBus, i2c_msg
    return mod


def load_script():
    spec = importlib.util.spec_from_file_location("eeprom_script", ROOT / "pi" / "mcp4725_set_default_zero.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("argv,eeprom,rc,writes", [
    (["--check"], 2048, 1, 0),
    ([], 2048, 1, 0),                 # ohne --yes kein Schreiben
    (["--yes"], 2048, 0, 1),
    (["--yes"], 0, 0, 0),             # schon 0 → spart Schreibzyklus
])
def test_script(monkeypatch, argv, eeprom, rc, writes):
    chip = FakeMCP(eeprom)
    monkeypatch.setitem(sys.modules, "smbus2", make_smbus(chip))
    monkeypatch.setattr(sys, "argv", ["x"] + argv)
    assert load_script().main() == rc
    assert len(chip.writes) == writes
    if writes:
        assert chip.writes[0] == (0x60, 0x60, [0x00, 0x00]) and chip.eeprom == 0
