#!/usr/bin/env python3
"""Exercise the production DSP snooper at the quantized level boundary."""

from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "components/gensam"


def main():
    source = (COMPONENT / "snoop.cpp").read_text()
    start = source.index("void GenSAMHub::snoop_dsp_(")
    end = source.index("\n}\n", start) + 3
    cpp = r'''
#include "commands.h"
#include "util.h"
#include <cassert>
using namespace esphome::gensam;
#define ESP_LOGI(...) ((void) 0)
#define ESP_LOGW(...) ((void) 0)

struct GenSAMMonitorBinding {
  bool level_configured = true, delay_configured = false;
  float level_db = -40;
  uint32_t delay_samples = 0;
};
struct GenSAMMonitor {
  GenSAMMonitorBinding *binding;
};
struct Registry {
  void set_level(GenSAMMonitor &mon, float db) {
    mon.binding->level_configured = true;
    mon.binding->level_db = db;
  }
  void set_delay(GenSAMMonitor &mon, uint32_t samples) {
    mon.binding->delay_configured = true;
    mon.binding->delay_samples = samples;
  }
};
class GenSAMHub {
 public:
  GenSAMMonitorBinding binding;
  GenSAMMonitor monitor{&binding};
  Registry registry_;
  GenSAMMonitor *resolve_snooped_monitor_(uint8_t) { return &monitor; }
  void snoop_dsp_(const Frame &frame);
};
'''
    cpp += source[start:end]
    cpp += r'''
int main() {
  GenSAMHub hub;
  const Frame minimum(0x02, CMD_DSP, {0x01, 0x00, 0x00, 0x20, 0xC4});
  hub.snoop_dsp_(minimum);
  assert(hub.binding.level_configured);
  assert(hub.binding.level_db == MIN_LEVEL_DB);
  assert(make_level(0x02, hub.binding.level_db).payload == minimum.payload);

  const Frame above(0x02, CMD_DSP, {0x01, 0x00, 0x00, 0x20, 0xC5});
  hub.snoop_dsp_(above);
  assert(hub.binding.level_configured);
  assert(hub.binding.level_db > MIN_LEVEL_DB);

  const Frame below(0x02, CMD_DSP, {0x01, 0x00, 0x00, 0x20, 0xC3});
  hub.snoop_dsp_(below);
  assert(!hub.binding.level_configured);

  const Frame regular(0x02, CMD_DSP, {0x01, 0x00, 0x0C, 0xCC, 0xCC});
  hub.snoop_dsp_(regular);
  assert(hub.binding.level_configured);
  assert(hub.binding.level_db < -19.9f && hub.binding.level_db > -20.1f);
}
'''
    with tempfile.TemporaryDirectory(prefix="gensam-snoop-dsp-") as directory:
        root = Path(directory)
        (root / "test.cpp").write_text(cpp)
        binary = root / "test"
        subprocess.run(
            ["c++", "-std=c++17", "-Wall", "-Wextra", "-I", str(COMPONENT),
             str(root / "test.cpp"), *(str(COMPONENT / name) for name in
             ("commands.cpp", "frame.cpp", "crc.cpp", "biquad.cpp")), "-o", str(binary)],
            check=True,
        )
        subprocess.run([str(binary)], check=True)
    print("Snooped DSP level boundary checks passed")


if __name__ == "__main__":
    main()
