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
  void set_has_state(bool value) { published = value; }
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
  hub.leds_known = false;
  for (auto &callback : hub.led_callbacks) callback(true);
  assert(!leds.published);
  hub.notify_leds();
  assert(leds.state && leds.published);

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
    print("System power, mute and volatile monitor LED control checks passed")


def test_led_commands():
    source = (ROOT / "components/gensam/hub.cpp").read_text()
    methods = []
    for signature in (
        "void GenSAMHub::set_system_leds_enabled(",
        "void GenSAMHub::transmit_system_leds_()",
        "void GenSAMHub::commit_system_led_state_(",
        "void GenSAMHub::fail_system_leds_(",
        "bool GenSAMHub::start_led_command_(",
        "bool GenSAMHub::handle_led_reply_(",
        "void GenSAMHub::check_identify_timeouts_()",
    ):
        start = source.index(signature)
        methods.append(source[start:source.index("\n}\n", start) + 3])
    cpp = r'''
#include <cassert>
#include <cstdint>
#include <functional>
#include <map>
#include <string>
#include <vector>
uint32_t clock_ms = 100;
uint32_t millis() { return clock_ms; }
void delayMicroseconds(uint32_t) {}
int warnings = 0;
const char *TAG = "test";
template<typename... Args> void test_log(Args&&...) {}
#define ESP_LOGW(...) (++warnings, test_log(__VA_ARGS__))
#define ESP_LOGI(...) ((void) 0)
#define ESP_LOGD(...) ((void) 0)
#define YESNO(value) ((value) ? "YES" : "NO")
constexpr int HOST_ADDRESS = 1, CMD_BYPASS = 0x2B, CMD_WAKEUP = 0x3A;
constexpr int CMD_REPORT_STATUS = 9, CMD_ACK = 6, CMD_COMMAND_REJECTED = 0x10, CMD_UNSUPPORTED = 0x11;
constexpr uint32_t SPEAKER_SETTINGS_REPLY_TIMEOUT_MS = 500;
constexpr uint8_t IDENTIFY_LED_RESTORE_MAX_ATTEMPTS = 2;
enum class RaceState { POLLING_MONITORS, IDLE };
struct Frame {
  int address, command;
  std::vector<uint8_t> payload;
};
Frame make_iss_led_state(uint8_t address, bool enabled) {
  return {address, CMD_WAKEUP, {6, uint8_t(enabled ? 0 : 1)}};
}
Frame make_bypass(uint8_t address, bool mute, bool, bool enabled) {
  return {address, CMD_BYPASS, {uint8_t((mute ? 1 : 0) | (enabled ? (mute ? 2 : 4) : 8))}};
}
Frame make_bypass_led_state(uint8_t address, bool mute, bool enabled) {
  return make_bypass(address, mute, false, enabled);
}
struct GenSAMMonitor {
  uint8_t address;
  uint32_t unique_id;
  std::string model = "8351B";
  bool online = true, mute = false;
  bool system_leds_known = true, system_leds_enabled = false;
  bool led_gate_known = true, led_gate_enabled = false;
  bool identify_restore_led_disable = false;
  uint8_t identify_led_restore_attempts = 0;
  uint32_t identify_end_ms = 0, identify_duration_ms = 0;
};
struct Registry {
  std::map<uint8_t, GenSAMMonitor> items{{2, {2, 20}}, {3, {3, 30}}};
  bool empty() const { return items.empty(); }
  size_t size() const { return items.size(); }
  auto &monitors() { return items; }
  GenSAMMonitor *find(uint8_t address) {
    auto found = items.find(address);
    return found == items.end() ? nullptr : &found->second;
  }
};
struct SpeakerSettings { bool modified = false, dirty = false; };
class GenSAMHub {
 public:
  Registry registry_;
  std::map<uint32_t, SpeakerSettings> speaker_settings_;
  std::vector<Frame> frames;
  std::vector<std::function<void(bool)>> system_led_state_callbacks_;
  bool current_system_leds_enabled_ = false, last_tx_system_leds_enabled_ = false;
  bool system_led_state_known_ = true, system_led_tx_pending_ = false;
  bool note_update_requested_ = false, transmit_ok = true, accepting = true, resuming_ = false;
  bool led_command_pending_ = false, led_command_enabling_ = false, led_command_for_system_ = false;
  uint8_t led_command_addr_ = 0, current_query_addr_ = 0, current_query_cmd_ = 0;
  uint32_t led_command_ms_ = 0, race_step_time_ = 0, last_poll_step_time_ = 0;
  struct { bool active = false; } apply_, speaker_transaction_;
  RaceState race_state_ = RaceState::POLLING_MONITORS;
  bool accept_user_command_() { return accepting; }
  bool can_transmit() const { return transmit_ok; }
  bool send_frame(const Frame &frame) {
    if (!transmit_ok) return false;
    frames.push_back(frame);
    return true;
  }
  bool send_transient_frame_(const Frame &frame) { return send_frame(frame); }
  bool send_frame_twice(const Frame &frame) { return send_frame(frame); }
  bool live_leds_enabled_(const GenSAMMonitor *mon) const { return mon->system_leds_enabled; }
  bool send_identify_pulse_(GenSAMMonitor &, uint32_t) { return true; }
  void mark_monitor_seen_(GenSAMMonitor &) {}
  void set_store_status_(const std::string &) {}
  void publish_active_settings_() {}
  void set_system_leds_enabled(bool enabled);
  void transmit_system_leds_();
  void commit_system_led_state_(bool enabled);
  bool start_led_command_(GenSAMMonitor &mon, bool enabled, uint32_t now, bool for_system = false);
  bool handle_led_reply_(const Frame &frame, uint32_t now);
  void check_identify_timeouts_();
  void fail_system_leds_(uint8_t address, const char *reason);
};
'''
    cpp += "\n".join(methods) + r'''
int main() {
  GenSAMHub hub;
  int publications = 0;
  hub.system_led_state_callbacks_.push_back([&](bool) { ++publications; });
  hub.registry_.items[3].mute = true;
  hub.registry_.items[2].identify_restore_led_disable = true;
  hub.registry_.items[2].identify_end_ms = 10000;
  const Frame ack{HOST_ADDRESS, CMD_ACK, {}};
  hub.set_system_leds_enabled(true);
  hub.transmit_system_leds_();
  assert(hub.frames.size() == 1 && hub.frames[0].command == CMD_WAKEUP &&
         hub.frames[0].payload == std::vector<uint8_t>({6, 0}));
  assert(hub.led_command_pending_ && hub.current_query_addr_ == 2 && publications == 0);
  assert(!hub.handle_led_reply_({HOST_ADDRESS, CMD_REPORT_STATUS, {0, 0, 0, 0}}, clock_ms));
  assert(hub.handle_led_reply_(ack, clock_ms));
  assert(hub.frames.back().command == CMD_BYPASS && hub.frames.back().payload[0] == 4);
  assert(!hub.registry_.items[2].identify_restore_led_disable &&
         hub.registry_.items[2].identify_end_ms == 0);
  hub.transmit_system_leds_();
  assert(hub.current_query_addr_ == 3 && publications == 0);
  assert(hub.handle_led_reply_(ack, clock_ms));
  assert(hub.frames.back().command == CMD_BYPASS && hub.frames.back().payload[0] == 3);
  hub.transmit_system_leds_();
  assert(!hub.system_led_tx_pending_ && publications == 1 && hub.last_tx_system_leds_enabled_);

  hub.set_system_leds_enabled(false);
  hub.transmit_system_leds_();
  assert(hub.frames.back().payload == std::vector<uint8_t>({6, 1}));
  assert(hub.handle_led_reply_(ack, clock_ms));
  assert(hub.frames.back().payload[0] == 8);
  hub.transmit_system_leds_();
  assert(hub.handle_led_reply_(ack, clock_ms));
  assert(hub.frames.back().payload[0] == 9);
  hub.transmit_system_leds_();
  assert(!hub.system_led_tx_pending_ && publications == 2 && !hub.last_tx_system_leds_enabled_);

  // A newer request supersedes a gate command whose reply is still outstanding.
  hub.set_system_leds_enabled(true);
  hub.transmit_system_leds_();
  hub.set_system_leds_enabled(false);
  const size_t before_stale_ack = hub.frames.size();
  assert(hub.handle_led_reply_(ack, clock_ms));
  assert(hub.frames.size() == before_stale_ack);
  hub.transmit_system_leds_();
  assert(hub.current_query_addr_ == 2 && hub.frames.back().payload[1] == 1);
  assert(hub.handle_led_reply_(ack, clock_ms));
  hub.transmit_system_leds_();
  assert(hub.handle_led_reply_(ack, clock_ms));
  hub.transmit_system_leds_();
  assert(!hub.system_led_tx_pending_ && !hub.last_tx_system_leds_enabled_);

  hub.set_system_leds_enabled(true);
  hub.transmit_system_leds_();
  const size_t before_timeout = hub.frames.size();
  const int before_timeout_warning = warnings;
  clock_ms += SPEAKER_SETTINGS_REPLY_TIMEOUT_MS + 1;
  hub.check_identify_timeouts_();
  assert(!hub.led_command_pending_ && !hub.system_led_tx_pending_ &&
         !hub.system_led_state_known_ && hub.current_query_addr_ == 0);
  assert(hub.frames.size() == before_timeout && warnings == before_timeout_warning + 1);

  hub.set_system_leds_enabled(true);
  hub.transmit_system_leds_();
  assert(hub.handle_led_reply_({HOST_ADDRESS, CMD_UNSUPPORTED, {}}, clock_ms));
  assert(!hub.system_led_tx_pending_ && !hub.system_led_state_known_);
  for (const auto &frame : hub.frames) assert(frame.command != 0x15);
}
'''
    with tempfile.TemporaryDirectory(prefix="gensam-leds-") as directory:
        path = Path(directory)
        (path / "test.cpp").write_text(cpp)
        subprocess.run(["c++", "-std=c++17", "-Wall", "-Wextra", str(path / "test.cpp"),
                        "-o", str(path / "test")], check=True)
        subprocess.run([str(path / "test")], check=True)
    print("Acknowledged LED gate and live-pattern transmission checks passed")


if __name__ == "__main__":
    main()
    test_led_commands()
