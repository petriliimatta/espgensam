#!/usr/bin/env python3
"""Exercise the production speaker transaction against a deterministic fake bus."""

from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "components/gensam"


def main():
    header = (COMPONENT / "hub.h").read_text()
    start = header.index("  struct SpeakerSettingsState {")
    end = header.index("\n  /// @brief", start + 1)
    state = header[start:end]
    start = header.index("  struct SpeakerSettingsTransaction {")
    end = header.index("\n  std::map<uint32_t, SpeakerSettingsState>", start)
    transaction = header[start:end]
    source = (COMPONENT / "speaker_store.cpp").read_text()
    source = source.replace('#include "hub.h"', "").replace('#include "esphome/core/log.h"', "")
    hub = (COMPONENT / "hub.cpp").read_text()
    extra = ""
    for signature in ("void GenSAMHub::reset_volatile_settings_()",
                      "void GenSAMHub::publish_active_settings_()"):
        start = hub.index(signature)
        end = hub.index("\n}\n", start) + 3
        extra += hub[start:end]
    cpp = r'''
#include "speaker_settings.h"
#include "iss.h"
#include <algorithm>
#include <cassert>
#include <functional>
#include <iostream>
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGI(...) ((void)0)
namespace esphome { namespace gensam {
struct Binding {};
struct Switch { void publish_state(bool) {} };
struct Select {
  bool has_option(const std::string &) const { return true; }
  void publish_state(const std::string &) {}
};
struct GenSAMMonitor {
  uint8_t address{2};
  uint32_t unique_id{123};
  bool online{true}, mute{false}, system_leds_known{true}, system_leds_enabled{true};
  bool led_gate_known{false}, led_gate_enabled{true};
  Binding *binding{nullptr};
  bool is_subwoofer() const { return false; }
};
uint32_t peq_design_rate_for(const GenSAMMonitor &) { return 48000; }
const GroupDevice *find_group_device(const GroupPreset &group, uint32_t uid) {
  for (uint8_t i = 0; i < group.device_count; i++)
    if (group.devices[i].unique_id == uid) return &group.devices[i];
  return nullptr;
}
struct Registry {
  std::map<uint8_t, GenSAMMonitor> items{{2, {}}};
  const auto &monitors() const { return items; }
  bool empty() const { return items.empty(); }
  GenSAMMonitor *find(uint8_t address) {
    auto it = items.find(address);
    return it == items.end() ? nullptr : &it->second;
  }
  const GenSAMMonitor *find_by_serial_or_id(const std::string &uid) const {
    for (const auto &entry : items)
      if (std::to_string(entry.second.unique_id) == uid) return &entry.second;
    return nullptr;
  }
  void set_binding_input(Binding &, uint8_t, uint8_t) {}
  void set_binding_crossover(Binding &, uint16_t) {}
  void set_binding_level(Binding &, float) {}
  void set_binding_delay(Binding &, uint32_t) {}
  void reset_volatile_settings() {}
};
enum class RaceState { POLLING_MONITORS, APPLYING_GROUP };
class GenSAMHub {
 public:
'''
    cpp += state + transaction
    cpp += r'''
  Registry registry_;
  struct {
    uint32_t last_tx{0};
    bool active{false};
    bool is_active() const { return active; }
    uint32_t last_transmit() const { return last_tx; }
  } arbiter_;
  std::map<uint32_t, SpeakerSettingsState> speaker_settings_;
  SpeakerSettingsTransaction speaker_transaction_;
  std::map<uint8_t, bool> profile_restore_failed_;
  std::string store_status_;
  std::string active_settings_{"Unknown"};
  std::vector<std::function<void(const std::string &)>> settings_callbacks_;
  std::vector<std::function<void(bool)>> system_led_state_callbacks_;
  Select *group_select_{nullptr}, *iss_sleep_delay_select_{nullptr}, *iss_threshold_select_{nullptr};
  Switch *calibration_bypass_switch_{nullptr}, *phase_correction_bypass_switch_{nullptr};
  std::vector<std::function<void(const std::string &)>> store_status_callbacks_;
  std::vector<std::function<void()>> user_commands_;
  bool store_requested_{false}, note_update_requested_{false}, external_settings_observed_{false};
  bool resuming_{false}, power_off_pending_{false}, initial_discovery_done_{true}, available{true};
  bool volume_tx_pending_{false}, mute_tx_pending_{false}, system_led_tx_pending_{false};
  bool iss_tx_pending_{false}, filter_bypass_tx_pending_{false}, phase_correction_bypass_{false};
  bool iss_delay_confirmation_pending_{false}, iss_threshold_confirmation_pending_{false};
  bool system_led_state_known_{false}, current_system_leds_enabled_{true}, bypass{false};
  bool calibration_bypass_{false}, current_mute_{false};
  uint32_t iss_sleep_delay_{ISS_SLEEP_DELAY_DEFAULT};
  int32_t iss_threshold_{ISS_THRESHOLD_DEFAULT};
  uint8_t current_query_addr_{0}, group_count_{1};
  uint32_t last_poll_step_time_{0}, last_timeout_check_{0};
  int active_group_{0}, selected_group_{0}, pending_group_{-1}, seen{0}, published{0};
  struct { bool active{false}; } apply_;
  RaceState race_state_{RaceState::POLLING_MONITORS};
  std::vector<Frame> sent;
  GroupDevice dev;
  GroupPreset group;
  GenSAMHub() {
    dev.unique_id = 123;
    group = {"Seat", &dev, 1};
    auto &state = speaker_settings_[123];
    state.group = 0;
    state.dirty = state.applied = true;
  }
  bool can_transmit() const { return available; }
  bool defer_user_command_(std::function<void()>) { return false; }
  const GroupPreset *get_group(uint8_t index) const { return index == 0 ? &group : nullptr; }
  bool is_calibration_bypassed_(const GenSAMMonitor &) const { return bypass; }
  bool live_leds_enabled_(const GenSAMMonitor *mon) const { return mon->system_leds_enabled; }
  void commit_system_led_state_(bool) {}
  void mark_monitor_seen_(GenSAMMonitor &) { seen++; }
  void publish_active_settings_();
  void reset_volatile_settings_();
  bool send_frame(const Frame &frame) {
    if (!available) return false;
    sent.push_back(frame);
    record_setting_frame_(frame, false);
    return true;
  }
  void store_speaker_settings();
  void set_store_status_(const std::string &value);
  void record_setting_frame_(const Frame &frame, bool external);
  void resolve_speaker_groups_();
  void fail_speaker_settings_(const std::string &reason);
  bool handle_speaker_settings_reply_(const Frame &frame, uint32_t now);
  void service_speaker_settings_(uint32_t now);
};
}}
'''
    cpp += source
    cpp += "\nnamespace esphome { namespace gensam {\n" + extra + "\n}}\n"
    cpp += r'''
using namespace esphome::gensam;
int commits(const GenSAMHub &hub) {
  return std::count_if(hub.sent.begin(), hub.sent.end(), [](const Frame &frame) {
    return frame.command == CMD_SERVICE && frame.payload == std::vector<uint8_t>({0x33, 0});
  });
}
void respond(GenSAMHub &hub, uint32_t now) {
  const auto &tx = hub.speaker_transaction_;
  assert(tx.active && tx.awaiting);
  std::vector<uint8_t> payload;
  if (tx.phase == 2 || tx.phase == 4) payload.assign(tx.note.begin(), tx.note.end());
  assert(hub.handle_speaker_settings_reply_(Frame(HOST_ADDRESS, CMD_REPORT_STATUS, payload), now));
}
void complete(GenSAMHub &hub, uint32_t &now) {
  for (int i = 0; i < 100; i++) {
    hub.service_speaker_settings_(now += 25);
    if (hub.speaker_transaction_.awaiting) respond(hub, now);
    if (!hub.speaker_transaction_.active && !hub.store_requested_ && !hub.note_update_requested_) return;
  }
  assert(false);
}
int main() {
  uint32_t now = 1000;
  GenSAMHub success;
  success.store_speaker_settings();
  success.volume_tx_pending_ = true;
  success.service_speaker_settings_(now);
  assert(success.sent.empty());
  success.volume_tx_pending_ = false;
  success.last_poll_step_time_ = now;
  success.service_speaker_settings_(now + 10);
  assert(success.sent.empty());  // Do not steal a telemetry continuation slot.
  success.last_poll_step_time_ = 0;
  success.arbiter_.last_tx = now;
  success.service_speaker_settings_(now + 10);
  assert(success.sent.empty());  // A just-sent live setting can still have a late ACK.
  complete(success, now);
  assert(commits(success) == 1 && success.store_status_ == "Stored 1 speakers");
  assert(!success.speaker_settings_[123].dirty && success.seen == 5);
  assert(success.active_settings_ == "Seat");
  assert(success.active_settings_.find("tagged") == std::string::npos);
  SpeakerNote note;
  assert(parse_speaker_note(success.speaker_settings_[123].note, note) && note.stored);
  GenSAMHub mismatch;
  mismatch.store_speaker_settings();
  for (int i = 0; i < 2; i++) {
    mismatch.service_speaker_settings_(now += 25);
    respond(mismatch, now);
  }
  mismatch.service_speaker_settings_(now += 25);
  assert(mismatch.speaker_transaction_.phase == 2);
  assert(mismatch.handle_speaker_settings_reply_(Frame(HOST_ADDRESS, CMD_REPORT_STATUS, {0x41, 0, 0}), now));
  assert(mismatch.speaker_transaction_.awaiting);  // Late telemetry is not a failed note.
  mismatch.handle_speaker_settings_reply_(Frame(HOST_ADDRESS, CMD_REPORT_STATUS, {'b', 'a', 'd'}), now);
  assert(commits(mismatch) == 0 && !mismatch.speaker_transaction_.active);
  assert(mismatch.store_status_.find("note readback mismatch") != std::string::npos);
  GenSAMHub volatile_edit;
  volatile_edit.record_setting_frame_(make_level(2, -10), false);
  assert(volatile_edit.active_settings_ == "Seat (modified)");
  complete(volatile_edit, now);
  assert(commits(volatile_edit) == 0 && volatile_edit.speaker_settings_[123].dirty);
  assert(parse_speaker_note(volatile_edit.speaker_settings_[123].note, note) && !note.stored);
  volatile_edit.record_setting_frame_(make_level(2, -12), true);
  assert(volatile_edit.external_settings_observed_ && volatile_edit.active_group_ == -1);
  assert(volatile_edit.speaker_settings_[123].group == -1);
  assert(volatile_edit.active_settings_ == "External settings (observed)");
  GenSAMHub external;
  external.arbiter_.active = true;
  external.store_speaker_settings();
  external.service_speaker_settings_(now += 25);
  assert(external.sent.empty() && external.store_status_.find("Blocked:") == 0);
  for (int mode = 0; mode < 4; mode++) {
    GenSAMHub blocked;
    blocked.bypass = mode == 0;
    blocked.phase_correction_bypass_ = mode == 1;
    blocked.registry_.items[2].online = mode != 2;
    blocked.registry_.items[2].mute = mode == 3;
    blocked.store_speaker_settings();
    blocked.service_speaker_settings_(now += 25);
    assert(commits(blocked) == 0 && blocked.store_status_.find("Failed:") == 0);
  }
  GenSAMHub partial;
  partial.registry_.items[3] = {3, 456};
  partial.store_speaker_settings();
  while (partial.speaker_transaction_.index == 0) {
    partial.service_speaker_settings_(now += 25);
    if (partial.speaker_transaction_.awaiting) respond(partial, now);
  }
  partial.service_speaker_settings_(now += 25);
  partial.handle_speaker_settings_reply_(Frame(HOST_ADDRESS, CMD_COMMAND_REJECTED, {}), now);
  assert(commits(partial) == 1 && partial.store_status_.find("1/2 acknowledged") != std::string::npos);
  GenSAMHub missing;
  GroupDevice members[] = {missing.dev, missing.dev};
  members[1].unique_id = 456;
  missing.group.devices = members;
  missing.group.device_count = 2;
  missing.publish_active_settings_();
  assert(missing.active_settings_ == "Unknown" && missing.active_group_ == -1);
  missing.store_speaker_settings();
  missing.service_speaker_settings_(now += 25);
  assert(commits(missing) == 0 && missing.store_status_.find("preset member is missing") != std::string::npos);
  GenSAMHub timeout;
  timeout.store_speaker_settings();
  for (int i = 0; i < 3; i++) {
    timeout.service_speaker_settings_(now += 25);
    respond(timeout, now);
  }
  timeout.service_speaker_settings_(now += 25);
  assert(commits(timeout) == 1 && timeout.speaker_transaction_.phase == 3);
  timeout.service_speaker_settings_(now += 501);
  assert(commits(timeout) == 1 && !timeout.speaker_transaction_.active);  // Never retry a flash commit.
  assert(timeout.store_status_.find("reply timeout") != std::string::npos);
  GenSAMHub cancel;
  cancel.store_speaker_settings();
  cancel.service_speaker_settings_(now += 25);
  cancel.power_off_pending_ = true;
  cancel.service_speaker_settings_(now + 10);
  assert(cancel.speaker_transaction_.active && cancel.speaker_transaction_.awaiting);
  respond(cancel, now + 15);
  cancel.service_speaker_settings_(now += 25);
  assert(!cancel.speaker_transaction_.active && commits(cancel) == 0 &&
         cancel.store_status_.find("power off requested") != std::string::npos);
  GenSAMHub lease;
  lease.store_speaker_settings();
  lease.service_speaker_settings_(now += 25);
  respond(lease, now);
  lease.service_speaker_settings_(now += 501);
  assert(lease.sent.back().command == CMD_STAY_ONLINE && !lease.speaker_transaction_.awaiting);
  lease.service_speaker_settings_(now += 25);
  assert(lease.sent.back().command == CMD_NAMES_USER && lease.speaker_transaction_.awaiting);
  lease.arbiter_.active = true;
  lease.service_speaker_settings_(now += 25);
  assert(!lease.speaker_transaction_.active && commits(lease) == 0 &&
         lease.store_status_.find("external controller active") != std::string::npos);
  GenSAMHub restore_failed;
  restore_failed.speaker_settings_[123].note = success.speaker_settings_[123].note;
  restore_failed.speaker_settings_[123].note_known = true;
  restore_failed.profile_restore_failed_[2] = true;
  restore_failed.resolve_speaker_groups_();
  assert(restore_failed.speaker_settings_[123].group == -1 &&
         !restore_failed.speaker_settings_[123].note_known);
  GenSAMHub reset;
  reset.iss_sleep_delay_ = 2;
  reset.iss_threshold_ = -61;
  reset.iss_delay_confirmation_pending_ = reset.iss_threshold_confirmation_pending_ = true;
  reset.current_mute_ = reset.phase_correction_bypass_ = true;
  reset.reset_volatile_settings_();
  assert(reset.iss_sleep_delay_ == ISS_SLEEP_DELAY_DEFAULT && !reset.current_mute_ &&
         !reset.phase_correction_bypass_ && !reset.iss_delay_confirmation_pending_ &&
         !reset.iss_threshold_confirmation_pending_);
  reset.iss_sleep_delay_ = 10;
  reset.iss_threshold_ = -75;
  reset.current_system_leds_enabled_ = false;
  reset.current_mute_ = reset.phase_correction_bypass_ = true;
  reset.iss_tx_pending_ = reset.system_led_tx_pending_ = reset.filter_bypass_tx_pending_ =
      reset.mute_tx_pending_ = true;
  reset.iss_delay_confirmation_pending_ = reset.iss_threshold_confirmation_pending_ = true;
  reset.reset_volatile_settings_();
  assert(reset.iss_sleep_delay_ == 10 && reset.iss_threshold_ == -75 &&
         reset.current_mute_ && reset.phase_correction_bypass_ && !reset.current_system_leds_enabled_);
  assert(reset.iss_tx_pending_ && reset.system_led_tx_pending_ && reset.filter_bypass_tx_pending_ &&
         reset.mute_tx_pending_ && reset.iss_delay_confirmation_pending_ &&
         reset.iss_threshold_confirmation_pending_);
  std::cout << "Speaker store serialization, readback, safety and partial-failure checks passed\n";
}
'''
    with tempfile.TemporaryDirectory(prefix="gensam-store-") as directory:
        root = Path(directory)
        (root / "test.cpp").write_text(cpp)
        subprocess.run(
            ["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
             "-Wno-unused-variable", "-Wno-unused-parameter", "-fsanitize=address,undefined",
             "-I", str(COMPONENT), str(root / "test.cpp"),
             str(COMPONENT / "speaker_settings.cpp"), str(COMPONENT / "commands.cpp"),
             str(COMPONENT / "biquad.cpp"), "-o", str(root / "test")], check=True
        )
        subprocess.run([str(root / "test")], check=True)


if __name__ == "__main__":
    main()
