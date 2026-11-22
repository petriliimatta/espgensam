#!/usr/bin/env python3
"""Exercise production GLM snoopers for F0 scope and power request semantics."""

from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "components/gensam"


def extract(source, signature):
    start = source.index(signature)
    end = source.index("\n}\n", start) + 3
    return source[start:end]


def main():
    source = (COMPONENT / "snoop.cpp").read_text()
    methods = "\n".join(
        extract(source, f"void GenSAMHub::{name}(")
        for name in (
            "snoop_power_",
            "snoop_system_leds_",
            "snoop_mute_",
            "snoop_crossover_",
        )
    )
    cpp = r'''
#include "commands.h"
#include "crossover.h"
#include <cassert>
#include <cstdint>
#include <functional>
#include <map>
#include <string>
#include <vector>
using namespace esphome::gensam;

uint32_t clock_ms = 10;
uint32_t millis() { return clock_ms; }
const char *TAG = "test";
#define ESP_LOGD(...) ((void) 0)
#define ESP_LOGI(...) ((void) 0)
#define ESP_LOGW(...) ((void) 0)
#define YESNO(value) ((value) ? "YES" : "NO")

struct Binding {
  bool crossover_configured = false;
  uint16_t crossover_freq = 0;
};
struct GenSAMMonitor {
  uint8_t address = 0;
  Binding *binding = nullptr;
  bool mute = false;
  bool system_leds_enabled = true, system_leds_known = false;
};
class Registry {
 public:
  std::map<uint8_t, GenSAMMonitor> entries;
  auto &monitors() { return entries; }
  bool empty() const { return entries.empty(); }
  GenSAMMonitor *find(uint8_t address) {
    auto it = entries.find(address);
    return it == entries.end() ? nullptr : &it->second;
  }
  GenSAMMonitor &get_or_create(uint8_t address) {
    auto &mon = entries[address];
    mon.address = address;
    return mon;
  }
  void set_mute(GenSAMMonitor &mon, bool value) { mon.mute = value; }
  void publish_mute(GenSAMMonitor &) {}
  void set_crossover(GenSAMMonitor &mon, uint16_t value) {
    if (mon.binding != nullptr) {
      mon.binding->crossover_freq = value;
      mon.binding->crossover_configured = true;
    }
  }
  void bind_if_matched(GenSAMMonitor &) {}
};
class GenSAMHub {
 public:
  Registry registry_;
  bool current_standby_ = false, current_system_leds_enabled_ = true;
  bool last_tx_system_leds_enabled_ = true, system_led_state_known_ = false;
  bool system_led_tx_pending_ = false;
  uint32_t last_standby_command_ = 0;
  std::vector<std::function<void(bool)>> system_led_state_callbacks_;
  void notify_state_callbacks_() {}
  void update_bus_status_() {}
  void evaluate_system_mute_() {}
  void mark_monitor_seen_(GenSAMMonitor &) {}
  void snoop_power_(const Frame &frame);
  void snoop_system_leds_(const Frame &frame);
  void snoop_mute_(const Frame &frame);
  void snoop_crossover_(const Frame &frame);
};
'''
    cpp += methods
    cpp += r'''
int main() {
  GenSAMHub hub;
  Binding first_binding, second_binding;
  auto &first = hub.registry_.get_or_create(0x02);
  auto &second = hub.registry_.get_or_create(0x03);
  first.binding = &first_binding;
  second.binding = &second_binding;

  hub.snoop_mute_(Frame(MULTICAST_ADDRESS, CMD_BYPASS, {BYPASS_MUTE_MASK}));
  hub.snoop_crossover_(Frame(MULTICAST_ADDRESS, CMD_BASS_MANAGE_XO, {0x00, 0x5A}));
  hub.snoop_system_leds_(Frame(MULTICAST_ADDRESS, CMD_BYPASS, {BYPASS_LED_DISABLED_VALUE}));
  assert(!first.mute && !second.mute);
  assert(!first_binding.crossover_configured && !second_binding.crossover_configured);
  assert(!first.system_leds_known && !second.system_leds_known);

  hub.snoop_mute_(Frame(BROADCAST_ADDRESS, CMD_BYPASS, {BYPASS_MUTE_MASK}));
  hub.snoop_crossover_(Frame(BROADCAST_ADDRESS, CMD_BASS_MANAGE_XO, {0x00, 0x5A}));
  hub.snoop_system_leds_(Frame(BROADCAST_ADDRESS, CMD_BYPASS, {BYPASS_LED_DISABLED_VALUE}));
  assert(first.mute && second.mute);
  assert(first_binding.crossover_freq == 90 && second_binding.crossover_freq == 90);
  assert(first.system_leds_known && second.system_leds_known);
  assert(!first.system_leds_enabled && !second.system_leds_enabled);

  hub.snoop_power_(Frame(MULTICAST_ADDRESS, CMD_WAKEUP, {WAKEUP_OP_POWER, WAKEUP_VAL_STANDBY_1}));
  assert(!hub.current_standby_);
  hub.snoop_power_(Frame(BROADCAST_ADDRESS, CMD_WAKEUP, {WAKEUP_OP_POWER, WAKEUP_VAL_STANDBY_2}));
  assert(!hub.current_standby_);  // 0x00 is a short-press/toggle request, not state.
  hub.snoop_power_(Frame(BROADCAST_ADDRESS, CMD_WAKEUP, {WAKEUP_OP_POWER, WAKEUP_VAL_STANDBY_1}));
  assert(hub.current_standby_);
  hub.snoop_power_(Frame(BROADCAST_ADDRESS, CMD_WAKEUP, {WAKEUP_OP_POWER, WAKEUP_VAL_ON_2}));
  assert(!hub.current_standby_);
}
'''
    with tempfile.TemporaryDirectory(prefix="gensam-snoop-addressing-") as directory:
        root = Path(directory)
        (root / "test.cpp").write_text(cpp)
        binary = root / "test"
        subprocess.run(
            ["c++", "-std=c++17", "-Wall", "-Wextra", "-I", str(COMPONENT),
             str(root / "test.cpp"), "-o", str(binary)],
            check=True,
        )
        subprocess.run([str(binary)], check=True)
    print("Snoop F0 scope and power command checks passed")


if __name__ == "__main__":
    main()
