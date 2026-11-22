#!/usr/bin/env python3
"""Exercise the actual system switch classes against a controllable host hub."""

from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]

HUB = r"""
#pragma once
#include <cstdint>
#include <functional>
#include <string>
#include <vector>
namespace esphome { namespace gensam {
class GenSAMHub {
 public:
  float volume{-30};
  bool muted{false}, standby{false}, accept{true};
  bool leds_enabled{true};
  bool leds_known{false};
  bool system_led_state_known_{false}, system_led_tx_pending_{false};
  bool current_system_leds_enabled_{true}, last_tx_system_leds_enabled_{true};
  uint32_t iss_sleep_delay_{0};
  int32_t iss_threshold_{0};
  bool iss_tx_pending_{false}, phase_correction_bypass_{false}, filter_bypass_tx_pending_{false};
  void queue_runtime_settings_();
  std::vector<std::function<void(float, bool, bool)>> callbacks;
  std::vector<std::function<void(bool)>> led_callbacks;
  void add_state_callback(std::function<void(float, bool, bool)> cb) { callbacks.push_back(cb); }
  void add_system_led_state_callback(std::function<void(bool)> cb) { led_callbacks.push_back(cb); }
  void notify() { for (auto &cb : callbacks) cb(volume, muted, standby); }
  void notify_leds() { leds_known = true; for (auto &cb : led_callbacks) cb(leds_enabled); }
  void set_group_mute(bool value) { if (accept) { muted = value; notify(); } }
  void set_system_leds_enabled(bool value) { if (accept) leds_enabled = value; }
  void set_standby(bool value) { if (accept) { standby = value; notify(); } }
  bool is_muted() const { return muted; }
  bool are_system_leds_enabled() const { return leds_enabled; }
  bool is_system_led_state_known() const { return leds_known; }
  bool is_standby() const { return standby; }
  void set_monitor_mute_by_serial(const std::string &, bool) {}
  void set_calibration_bypass(bool) {}
  void set_monitor_calibration_bypass_by_serial(const std::string &, bool) {}
  void set_phase_correction_bypass(bool) {}
};
}}
"""
SWITCH = r"""
#pragma once
namespace esphome { namespace switch_ {
class Switch {
 public:
  virtual ~Switch() = default;
  bool state{false};
  bool published{false};
  void publish_state(bool value) { state = value; published = true; }
  void request(bool value) { write_state(value); }
 protected:
  virtual void write_state(bool) = 0;
};
}}
"""
MAIN = r"""
#include "switch.h"
#include <cassert>
using namespace esphome::gensam;
constexpr uint32_t ISS_SLEEP_DELAY_DEFAULT = 0;
constexpr int32_t ISS_THRESHOLD_DEFAULT = 0;
QUEUE_RUNTIME_SETTINGS
int main() {
  GenSAMHub hub;
  GenSAMSystemMuteSwitch mute;
  GenSAMSystemPowerSwitch power;
  GenSAMSystemLEDSwitch leds;
  mute.set_hub(&hub);
  power.set_hub(&hub);
  leds.set_hub(&hub);
  hub.notify();
  assert(!mute.state && power.state);
  assert(!leds.published);
  hub.notify_leds();
  assert(leds.state && leds.published);
  mute.request(true);
  assert(mute.state && hub.muted);
  hub.accept = false;
  mute.request(false);
  assert(mute.state && hub.muted);
  hub.accept = true;
  mute.request(false);
  assert(!mute.state && !hub.muted);
  hub.muted = true; hub.notify();
  assert(mute.state);
  power.request(false);
  assert(!power.state && hub.standby);
  power.request(true);
  assert(power.state && !hub.standby);
  hub.standby = true; hub.notify();
  assert(!power.state);
  leds.request(false);
  assert(leds.state && !hub.leds_enabled);
  hub.notify_leds();
  assert(!leds.state);
  hub.leds_enabled = true; hub.notify_leds();
  assert(leds.state);

  // Initial discovery must not invent an LED preference.
  hub.queue_runtime_settings_();
  assert(!hub.system_led_tx_pending_);
  hub.system_led_tx_pending_ = true;
  hub.current_system_leds_enabled_ = false;
  hub.queue_runtime_settings_();
  assert(hub.system_led_tx_pending_ && !hub.current_system_leds_enabled_);

  // Wake/discovery and group pushes restore either last known LED setting.
  hub.system_led_state_known_ = true;
  for (bool enabled : {false, true}) {
    hub.current_system_leds_enabled_ = enabled;
    hub.last_tx_system_leds_enabled_ = enabled;
    hub.system_led_tx_pending_ = false;
    hub.queue_runtime_settings_();
    assert(hub.system_led_tx_pending_);
    assert(hub.current_system_leds_enabled_ == enabled);
  }

  // A newly accepted setting still wins over the previously transmitted value.
  hub.current_system_leds_enabled_ = false;
  hub.last_tx_system_leds_enabled_ = true;
  hub.system_led_tx_pending_ = true;
  hub.queue_runtime_settings_();
  assert(hub.system_led_tx_pending_ && !hub.current_system_leds_enabled_);
}
"""


def main():
    hub_header = (ROOT / "components/gensam/hub.h").read_text()
    standby_default = re.search(r"bool current_standby_\{(true|false)\};", hub_header)
    assert standby_default is not None, "Hub standby initializer not found"
    hub_stub = HUB.replace("standby{false}", "standby{" + standby_default[1] + "}")
    if "--baseline" in sys.argv:
        source = subprocess.check_output(
            ["git", "show", "HEAD:components/gensam/hub.cpp"], cwd=ROOT, text=True
        )
    else:
        source = (ROOT / "components/gensam/hub.cpp").read_text()
    start = source.index("void GenSAMHub::queue_runtime_settings_()")
    end = source.index("\n}\n", start) + 3
    main_source = MAIN.replace("QUEUE_RUNTIME_SETTINGS", source[start:end])
    with tempfile.TemporaryDirectory(prefix="gensam-controls-") as directory:
        root = Path(directory)
        if "--baseline" in sys.argv:
            header = subprocess.check_output(
                ["git", "show", "HEAD:components/gensam/switch.h"], cwd=ROOT, text=True
            )
        else:
            header = (ROOT / "components/gensam/switch.h").read_text()
        (root / "switch.h").write_text(header)
        (root / "hub.h").write_text(hub_stub)
        for name, text in {
            "esphome/core/component.h": "#pragma once\n",
            "esphome/core/log.h": "#pragma once\n#define ESP_LOGI(...) ((void) 0)\n#define ESP_LOGW(...) ((void) 0)\n",
            "esphome/components/switch/switch.h": SWITCH,
            "main.cpp": main_source,
        }.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        binary = root / "test"
        subprocess.run(["c++", "-std=c++17", "-Wall", "-Wextra", "-I", str(root),
                        str(root / "main.cpp"), "-o", str(binary)], check=True)
        subprocess.run([str(binary)], check=True)
    print("System power, mute and monitor LED wake restoration checks passed")


if __name__ == "__main__":
    main()
