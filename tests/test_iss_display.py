#!/usr/bin/env python3
"""Exercise production ISS readback and aggregate display with a fake bus."""

from pathlib import Path
import ast
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "components/gensam"


def method(source, signature):
    start = source.index(signature)
    return source[start:source.index("\n}\n", start) + 3]


def select_control(source, class_name, test_class):
    start = source.index("  void control(const std::string &value) override {",
                         source.index("class " + class_name))
    end = source.index("\n  }\n", start) + len("\n  }\n")
    return source[start:end].replace(
        "  void control(const std::string &value) override {",
        "void " + test_class + "::control(const std::string &value) {", 1)


def main():
    schema = ast.parse((COMPONENT / "__init__.py").read_text())
    options = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in schema.body
        if isinstance(node, ast.Assign) and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in ("ISS_SLEEP_DELAY_OPTIONS", "ISS_SENSITIVITY_OPTIONS")
    }
    for values in options.values():
        assert "Unknown" in values and "Speaker default" not in values
    assert len(options) == 2
    hub = (COMPONENT / "hub.cpp").read_text()
    race = (COMPONENT / "race.cpp").read_text()
    select = (COMPONENT / "select.h").read_text()
    methods = "\n".join((
        method(hub, "void GenSAMHub::set_iss_sleep_delay("),
        method(hub, "void GenSAMHub::set_iss_threshold("),
        method(hub, "void GenSAMHub::publish_iss_settings_()"),
        method(hub, "void GenSAMHub::transmit_iss_settings_()"),
        method(race, "void GenSAMHub::fail_iss_readback_()"),
        method(race, "void GenSAMHub::race_step_polling_("),
        method(race, "bool GenSAMHub::send_iss_read_("),
        method(race, "bool GenSAMHub::handle_iss_reply_("),
        select_control(select, "GenSAMIssSleepDelaySelect", "DelaySelect"),
        select_control(select, "GenSAMIssSensitivitySelect", "ThresholdSelect"),
    ))
    cpp = r'''
#include "iss.h"
#include <cassert>
#include <map>
#include <optional>
#include <string>
#include <vector>
#define ESP_LOGI(...) ((void) 0)
#define ESP_LOGD(...) ((void) 0)
int warnings = 0;
#define ESP_LOGW(...) (++warnings)
using namespace esphome::gensam;
constexpr uint32_t ISS_SAMPLE_INTERVAL_MS = 5000;
constexpr uint32_t POLL_REPLY_TIMEOUT_MS = 300, POLL_STEP_INTERVAL_MS = 20, TURNAROUND_US = 300;
enum class RaceState { POLLING_MONITORS, IDLE };
void delayMicroseconds(uint32_t) {}
struct Frame { int address = HOST_ADDRESS, command = CMD_REPORT_STATUS; std::vector<uint8_t> payload; };
Frame make_iss_read(uint8_t, uint8_t op) { return {0, 0, {op}}; }
Frame make_iss_sleep_delay(uint8_t, uint32_t) { return {0, 0, {WAKEUP_OP_ISS_SLEEP_DELAY}}; }
Frame make_iss_threshold(uint8_t, int32_t) { return {0, 0, {WAKEUP_OP_ISS_THRESHOLD}}; }
Frame make_query(uint8_t, uint8_t) { return {}; }
bool parse_iss_word(const std::vector<uint8_t> &p, int32_t &value) {
  if (p.size() != 4) return false;
  value = static_cast<int32_t>((uint32_t(p[0]) << 24) | (uint32_t(p[1]) << 16) |
                               (uint32_t(p[2]) << 8) | p[3]);
  return true;
}
struct Select {
  std::string state;
  int publications = 0;
  void publish_state(const char *s) { state = s; ++publications; }
  void publish_state(const std::string &s) { state = s; ++publications; }
  void publish_state(size_t) { ++publications; }
  bool has_state() const { return !state.empty(); }
  std::optional<size_t> active_index() const { return has_state() ? std::optional<size_t>(1) : std::nullopt; }
  std::optional<size_t> index_of(const std::string &option) const { return option == state ? 1 : 0; }
};
struct Monitor {
  uint8_t address = 0;
  std::string model = "test";
  bool online = true, iss_read_sent = false, iss_level_read_due = false;
  uint32_t iss_last_read_ms = 0;
  uint32_t iss_stored_delay_min = 0;
  int32_t iss_stored_threshold_dbu = 0, iss_level_dbu = 0;
  bool iss_stored_delay_known = false, iss_stored_threshold_known = false, iss_level_known = false;
  uint32_t iss_level_ms = 0;
};
using GenSAMMonitor = Monitor;
struct Registry {
  std::map<uint8_t, Monitor> items;
  auto &monitors() { return items; }
  bool any_online() const {
    for (const auto &item : items) if (item.second.online) return true;
    return false;
  }
  size_t size() const { return items.size(); }
  std::vector<uint8_t> addresses() const {
    std::vector<uint8_t> result;
    for (const auto &item : items) result.push_back(item.first);
    return result;
  }
  Monitor *find(uint8_t a) {
    auto it = items.find(a);
    return it == items.end() ? nullptr : &it->second;
  }
};
struct Arbiter { bool active = false; bool is_active() const { return active; } };
class GenSAMHub {
 public:
  Registry registry_;
  Arbiter arbiter_;
  bool bus_silent_ = false, resuming_ = false, yield_to_glm_ = true, iss_tx_pending_ = false;
  bool transmit_ok = true;
  bool accepting = true;
  bool iss_delay_confirmation_pending_ = false, iss_threshold_confirmation_pending_ = false;
  bool led_command_pending_ = false;
  uint32_t iss_sleep_delay_ = ISS_SLEEP_DELAY_DEFAULT, race_step_time_ = 0, last_poll_step_time_ = 0;
  uint32_t last_signal_ms_ = 0, last_poll_cycle_time_ = 0, poll_interval_ms_ = 1000;
  int32_t iss_threshold_ = ISS_THRESHOLD_DEFAULT;
  uint8_t current_query_addr_ = 0, iss_query_op_ = 0, led_command_addr_ = 0;
  uint8_t telemetry_continuation_addr_ = 0;
  int pending_group_ = -1;
  size_t current_poll_index_ = 0;
  RaceState race_state_ = RaceState::POLLING_MONITORS;
  std::vector<uint8_t> poll_addrs_;
  Select *iss_sleep_delay_select_ = nullptr, *iss_threshold_select_ = nullptr;
  std::string iss_display_delay_, iss_display_threshold_;
  std::vector<Frame> sent;
  bool accept_user_command_() { return accepting; }
  bool can_transmit() const { return transmit_ok; }
  bool send_frame(const Frame &f) { sent.push_back(f); return true; }
  bool send_frame_twice(const Frame &f) { return transmit_ok && send_frame(f); }
  void mark_monitor_seen_(Monitor &) {}
  bool start_group_apply_(uint32_t) { return false; }
  void update_bus_status_() {}
  bool sleep_due_(uint32_t) const { return false; }
  void enter_sleep_(uint32_t) {}
  void broadcast_volume_and_keepalive_() {}
  bool iss_threshold_for_(const Monitor &mon, int32_t &threshold) const {
    if (!mon.iss_stored_threshold_known) return false;
    threshold = mon.iss_stored_threshold_dbu;
    return true;
  }
  void publish_iss_settings_();
  void transmit_iss_settings_();
  void fail_iss_readback_();
  void race_step_polling_(uint32_t now);
  void set_iss_sleep_delay(uint32_t value);
  void set_iss_threshold(int32_t value);
  bool send_iss_read_(uint32_t now);
  bool handle_iss_reply_(const Frame &frame, uint32_t now);
};
class TestSelect : public Select {
 public:
  GenSAMHub *hub_ = nullptr;
};
class DelaySelect : public TestSelect {
 public:
  void control(const std::string &value);
};
class ThresholdSelect : public TestSelect {
 public:
  void control(const std::string &value);
};
'''
    cpp += methods + r'''
Frame word(uint32_t value) {
  return {HOST_ADDRESS, CMD_REPORT_STATUS, {uint8_t(value >> 24), uint8_t(value >> 16),
                                           uint8_t(value >> 8), uint8_t(value)}};
}
int main() {
  GenSAMHub controls;
  DelaySelect delay_control;
  ThresholdSelect threshold_control;
  delay_control.hub_ = threshold_control.hub_ = &controls;
  controls.iss_sleep_delay_select_ = &delay_control;
  controls.iss_threshold_select_ = &threshold_control;
  auto &first = controls.registry_.items[2]; first.address = 2;
  auto &second = controls.registry_.items[3]; second.address = 3;
  for (auto *mon : {&first, &second}) {
    mon->iss_stored_delay_known = mon->iss_stored_threshold_known = true;
    mon->iss_stored_delay_min = 2;
    mon->iss_stored_threshold_dbu = -75;
  }
  controls.publish_iss_settings_();
  delay_control.control("Unknown");
  threshold_control.control("Unknown");
  assert(controls.iss_sleep_delay_ == ISS_SLEEP_DELAY_DEFAULT &&
         controls.iss_threshold_ == ISS_THRESHOLD_DEFAULT && !controls.iss_tx_pending_);
  delay_control.control("10 min");
  assert(delay_control.state == "10 min" && threshold_control.state == "High (-75 dBu)");
  controls.publish_iss_settings_();
  assert(delay_control.state == "10 min" && threshold_control.state == "High (-75 dBu)");
  controls.transmit_ok = false;
  controls.transmit_iss_settings_();
  controls.publish_iss_settings_();
  assert(controls.iss_tx_pending_ && delay_control.state == "10 min" &&
         threshold_control.state == "High (-75 dBu)");
  controls.transmit_ok = true;
  controls.transmit_iss_settings_();
  controls.publish_iss_settings_();
  assert(!controls.iss_tx_pending_ && delay_control.state == "10 min" &&
         threshold_control.state == "High (-75 dBu)");
  assert(controls.send_iss_read_(100) && controls.current_query_addr_ == 2);
  assert(controls.handle_iss_reply_(word(10), 101));
  controls.publish_iss_settings_();
  assert(delay_control.state == "10 min");
  assert(controls.send_iss_read_(102) && controls.current_query_addr_ == 3);
  assert(controls.handle_iss_reply_(word(10), 103));
  controls.publish_iss_settings_();
  assert(delay_control.state == "10 min" && threshold_control.state == "High (-75 dBu)");
  controls.accepting = false;
  const int delay_publications = delay_control.publications;
  delay_control.control("30 min");
  assert(delay_control.state == "10 min" && !controls.iss_tx_pending_ &&
         delay_control.publications == delay_publications + 1);
  const int threshold_publications = threshold_control.publications;
  threshold_control.control("Low (-61 dBu)");
  assert(threshold_control.state == "High (-75 dBu)" && !controls.iss_tx_pending_ &&
         threshold_control.publications == threshold_publications + 1);
  controls.accepting = true;

  threshold_control.control("Medium (-68 dBu)");
  controls.publish_iss_settings_();
  assert(delay_control.state == "10 min" && threshold_control.state == "Medium (-68 dBu)");
  controls.transmit_iss_settings_();
  controls.publish_iss_settings_();
  assert(delay_control.state == "10 min" && threshold_control.state == "Medium (-68 dBu)");
  assert(controls.send_iss_read_(200) && controls.current_query_addr_ == 2);
  assert(controls.handle_iss_reply_(word(10), 201));
  assert(controls.send_iss_read_(202) && controls.current_query_addr_ == 3);
  assert(controls.handle_iss_reply_(word(10), 203));
  controls.publish_iss_settings_();
  assert(delay_control.state == "10 min" && threshold_control.state == "Medium (-68 dBu)");
  assert(controls.send_iss_read_(5200) && controls.iss_query_op_ == WAKEUP_OP_ISS_THRESHOLD);
  assert(controls.handle_iss_reply_(word(static_cast<uint32_t>(-68)), 5201));
  assert(controls.send_iss_read_(5202) && controls.iss_query_op_ == WAKEUP_OP_ISS_THRESHOLD);
  assert(controls.handle_iss_reply_(word(static_cast<uint32_t>(-68)), 5203));
  controls.publish_iss_settings_();
  assert(threshold_control.state == "Medium (-68 dBu)");

  // Once every speaker has replied, readback wins over an unconfirmed request.
  threshold_control.control("Low (-61 dBu)");
  assert(threshold_control.state == "Low (-61 dBu)");
  controls.transmit_iss_settings_();
  assert(controls.send_iss_read_(5300) && controls.iss_query_op_ == WAKEUP_OP_ISS_SLEEP_DELAY);
  assert(controls.handle_iss_reply_(word(10), 5301));
  assert(controls.send_iss_read_(5302) && controls.iss_query_op_ == WAKEUP_OP_ISS_SLEEP_DELAY);
  assert(controls.handle_iss_reply_(word(10), 5303));
  assert(controls.send_iss_read_(10300) && controls.iss_query_op_ == WAKEUP_OP_ISS_THRESHOLD);
  assert(controls.handle_iss_reply_(word(static_cast<uint32_t>(-75)), 10301));
  assert(controls.send_iss_read_(10302) && controls.iss_query_op_ == WAKEUP_OP_ISS_THRESHOLD);
  assert(controls.handle_iss_reply_(word(static_cast<uint32_t>(-75)), 10303));
  controls.publish_iss_settings_();
  assert(threshold_control.state == "High (-75 dBu)" && delay_control.state == "10 min");

  // An accepted request stays visible while wake-up and rediscovery delay transmission.
  GenSAMHub waking;
  DelaySelect waking_delay;
  waking_delay.hub_ = &waking;
  waking.iss_sleep_delay_select_ = &waking_delay;
  for (uint8_t address : {2, 3}) {
    auto &mon = waking.registry_.items[address];
    mon.address = address;
    mon.iss_stored_delay_known = mon.iss_stored_threshold_known = true;
    mon.iss_stored_delay_min = 2;
    mon.iss_stored_threshold_dbu = -75;
  }
  waking.publish_iss_settings_();
  waking.resuming_ = true;
  waking_delay.control("10 min");
  waking.publish_iss_settings_();
  assert(waking.iss_tx_pending_ && waking_delay.state == "10 min");
  waking.resuming_ = false;
  waking.registry_.items.clear();
  waking.publish_iss_settings_();
  assert(waking.iss_tx_pending_ && waking_delay.state == "10 min");
  for (uint8_t address : {2, 3}) waking.registry_.items[address].address = address;
  waking.transmit_iss_settings_();
  waking.publish_iss_settings_();
  assert(!waking.iss_tx_pending_ && waking_delay.state == "10 min");
  assert(waking.send_iss_read_(100) && waking.handle_iss_reply_(word(10), 101));
  assert(waking.send_iss_read_(102) && waking.handle_iss_reply_(word(10), 103));
  waking.publish_iss_settings_();
  assert(waking_delay.state == "10 min");
  waking_delay.control("30 min");
  waking.registry_.items[3].online = false;
  waking.publish_iss_settings_();
  assert(waking_delay.state == "30 min");
  waking.transmit_iss_settings_();
  waking.publish_iss_settings_();
  assert(waking_delay.state == "Unknown");

  // An unanswered read is not treated as confirmation of the broadcast.
  GenSAMHub timeout;
  DelaySelect timeout_delay;
  Select timeout_threshold;
  timeout_delay.hub_ = &timeout;
  timeout.iss_sleep_delay_select_ = &timeout_delay;
  timeout.iss_threshold_select_ = &timeout_threshold;
  for (uint8_t address : {2, 3}) {
    auto &mon = timeout.registry_.items[address];
    mon.address = address;
    mon.iss_stored_delay_known = mon.iss_stored_threshold_known = true;
    mon.iss_stored_delay_min = 2;
    mon.iss_stored_threshold_dbu = -75;
  }
  timeout.publish_iss_settings_();
  timeout_delay.control("30 min");
  timeout.transmit_iss_settings_();
  assert(timeout.send_iss_read_(100));
  assert(timeout.handle_iss_reply_(word(30), 101));
  assert(timeout.send_iss_read_(102) && timeout.current_query_addr_ == 3);
  const int warnings_before_timeout = warnings;
  timeout.race_step_polling_(403);
  timeout.publish_iss_settings_();
  assert(warnings == warnings_before_timeout + 1);
  assert(timeout_delay.state == "Unknown" && timeout_threshold.state == "High (-75 dBu)");

  // A later matching retry recovers; a disagreement still reports Unknown.
  assert(timeout.send_iss_read_(5103) && timeout.iss_query_op_ == WAKEUP_OP_ISS_LEVEL);
  assert(timeout.handle_iss_reply_(word(static_cast<uint32_t>(-80)), 5104));
  assert(timeout.send_iss_read_(5105) && timeout.iss_query_op_ == WAKEUP_OP_ISS_LEVEL);
  assert(timeout.handle_iss_reply_(word(static_cast<uint32_t>(-80)), 5106));
  assert(timeout.send_iss_read_(10103) && timeout.iss_query_op_ == WAKEUP_OP_ISS_LEVEL);
  assert(timeout.handle_iss_reply_(word(static_cast<uint32_t>(-80)), 10104));
  assert(timeout.send_iss_read_(10105) && timeout.iss_query_op_ == WAKEUP_OP_ISS_SLEEP_DELAY);
  assert(timeout.handle_iss_reply_(word(30), 10106));
  timeout.publish_iss_settings_();
  assert(timeout_delay.state == "30 min");
  timeout_delay.control("10 min");
  timeout.transmit_iss_settings_();
  assert(timeout.send_iss_read_(11000));
  assert(timeout.handle_iss_reply_(word(10), 11001));
  assert(timeout.send_iss_read_(11002));
  assert(timeout.handle_iss_reply_(word(30), 11003));
  timeout.publish_iss_settings_();
  assert(timeout_delay.state == "Unknown");

  // Failed setting reads must not starve the input samples used for auto-sleep.
  GenSAMHub stalled;
  auto &speaker = stalled.registry_.items[2];
  speaker.address = 2;
  speaker.iss_stored_threshold_known = true;
  stalled.iss_sleep_delay_ = 30;
  stalled.iss_threshold_ = -75;
  assert(stalled.send_iss_read_(100) && stalled.iss_query_op_ == WAKEUP_OP_ISS_SLEEP_DELAY);
  stalled.race_step_polling_(401);
  assert(stalled.send_iss_read_(5100) && stalled.iss_query_op_ == WAKEUP_OP_ISS_LEVEL);
  assert(stalled.handle_iss_reply_(word(static_cast<uint32_t>(-80)), 5101));
  assert(speaker.iss_level_known && speaker.iss_level_ms == 5101);
  assert(stalled.send_iss_read_(10100) && stalled.iss_query_op_ == WAKEUP_OP_ISS_SLEEP_DELAY);
  stalled.race_step_polling_(10401);
  assert(stalled.send_iss_read_(15100) && stalled.iss_query_op_ == WAKEUP_OP_ISS_LEVEL);
  assert(stalled.handle_iss_reply_(word(static_cast<uint32_t>(-80)), 15101));
  speaker.iss_stored_delay_known = true;
  speaker.iss_stored_threshold_known = false;
  assert(stalled.send_iss_read_(20100) && stalled.iss_query_op_ == WAKEUP_OP_ISS_THRESHOLD);
  stalled.race_step_polling_(20401);
  assert(stalled.send_iss_read_(25100) && stalled.iss_query_op_ == WAKEUP_OP_ISS_LEVEL);

  GenSAMHub hub;
  Select delay, threshold;
  hub.iss_sleep_delay_select_ = &delay;
  hub.iss_threshold_select_ = &threshold;
  hub.publish_iss_settings_();
  assert(delay.state == "Unknown" && threshold.state == "Unknown");
  auto &a = hub.registry_.items[2]; a.address = 2;
  auto &b = hub.registry_.items[3]; b.address = 3;
  hub.publish_iss_settings_();
  assert(delay.state == "Unknown" && threshold.state == "Unknown");

  // Read every speaker before reporting a single system-wide value.
  assert(hub.send_iss_read_(100));
  assert(hub.sent.back().payload[0] == WAKEUP_OP_ISS_SLEEP_DELAY);
  assert(hub.handle_iss_reply_(word(2), 101));
  hub.publish_iss_settings_();
  assert(delay.state == "Unknown");
  a.iss_read_sent = true; a.iss_last_read_ms = 100;
  assert(hub.send_iss_read_(102));
  assert(hub.current_query_addr_ == 3 && hub.sent.back().payload[0] == WAKEUP_OP_ISS_SLEEP_DELAY);
  assert(hub.handle_iss_reply_(word(2), 103));
  hub.publish_iss_settings_();
  assert(delay.state == "2 min" && threshold.state == "Unknown");
  a.iss_stored_threshold_known = b.iss_stored_threshold_known = true;
  a.iss_stored_threshold_dbu = b.iss_stored_threshold_dbu = -75;
  hub.publish_iss_settings_();
  assert(threshold.state == "High (-75 dBu)");

  // A disagreement never masquerades as a majority or a default.
  b.iss_stored_delay_min = 10;
  hub.publish_iss_settings_();
  assert(delay.state == "Unknown" && threshold.state == "High (-75 dBu)");
  auto &c = hub.registry_.items[4];
  c.address = 4;
  c.iss_stored_delay_known = c.iss_stored_threshold_known = true;
  c.iss_stored_delay_min = 2;
  c.iss_stored_threshold_dbu = -75;
  hub.publish_iss_settings_();
  assert(delay.state == "Unknown");  // Two matching speakers are not unanimity.
  hub.registry_.items.erase(4);
  b.iss_stored_threshold_dbu = -61;
  hub.publish_iss_settings_();
  assert(threshold.state == "Unknown");
  b.iss_stored_delay_min = 2;
  b.iss_stored_threshold_dbu = -75;
  hub.publish_iss_settings_();
  assert(delay.state == "2 min" && threshold.state == "High (-75 dBu)");

  // Never has several equivalent wire encodings; unlisted values remain unknown.
  a.iss_stored_delay_min = 0; b.iss_stored_delay_min = 65535;
  hub.publish_iss_settings_();
  assert(delay.state == "Never");
  a.iss_stored_delay_min = b.iss_stored_delay_min = 17;
  hub.publish_iss_settings_();
  assert(delay.state == "Unknown");
  b.online = false;
  hub.publish_iss_settings_();
  assert(delay.state == "Unknown" && threshold.state == "Unknown");
  b.online = true;
  hub.iss_tx_pending_ = true;
  hub.publish_iss_settings_();
  assert(delay.state == "Unknown" && threshold.state == "High (-75 dBu)");
  hub.iss_tx_pending_ = false;
  hub.bus_silent_ = true;
  hub.publish_iss_settings_();
  assert(delay.state == "Unknown" && threshold.state == "Unknown");
  const int count = delay.publications;
  hub.publish_iss_settings_();
  assert(delay.publications == count);

  // A selected override must not suppress the readback needed by the UI.
  hub.bus_silent_ = false;
  hub.iss_sleep_delay_ = 30;
  hub.iss_threshold_ = -61;
  a.iss_stored_delay_known = b.iss_stored_delay_known = true;
  a.iss_stored_threshold_known = b.iss_stored_threshold_known = true;
  hub.iss_tx_pending_ = true;
  hub.transmit_ok = false;
  hub.transmit_iss_settings_();
  assert(hub.iss_tx_pending_ && a.iss_stored_delay_known && a.iss_stored_threshold_known);
  hub.transmit_ok = true;
  hub.transmit_iss_settings_();
  assert(!hub.iss_tx_pending_ && !a.iss_stored_delay_known && !b.iss_stored_delay_known &&
         !a.iss_stored_threshold_known && !b.iss_stored_threshold_known);
  assert(hub.send_iss_read_(6000));
  assert(hub.sent.back().payload[0] == WAKEUP_OP_ISS_SLEEP_DELAY);
  assert(hub.handle_iss_reply_(word(30), 6001));
  assert(a.iss_stored_delay_known && !b.iss_stored_delay_known);
  hub.publish_iss_settings_();
  assert(delay.state == "Unknown");
}
'''
    with tempfile.TemporaryDirectory(prefix="gensam-iss-") as directory:
        path = Path(directory)
        (path / "test.cpp").write_text(cpp)
        subprocess.run(["c++", "-std=c++17", "-Wall", "-Wextra", "-I", str(COMPONENT),
                        str(path / "test.cpp"), "-o", str(path / "test")], check=True)
        subprocess.run([str(path / "test")], check=True)
    print("ISS readback and unanimous display checks passed")


if __name__ == "__main__":
    main()
