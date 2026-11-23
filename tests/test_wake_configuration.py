#!/usr/bin/env python3
"""Run the production post-discovery configuration pass against a fake bus and registry."""

from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def method(source, signature):
    start = source.index(signature)
    end = source.index("\n}\n", start) + 3
    return source[start:end]


def main():
    if "--baseline" in sys.argv:
        race = subprocess.check_output(
            ["git", "show", "HEAD:components/gensam/race.cpp"], cwd=ROOT, text=True
        )
    else:
        race = (ROOT / "components/gensam/race.cpp").read_text()
    methods = "\n".join(method(race, signature) for signature in (
        "void GenSAMHub::race_step_configuring_(",
    ))
    if "GenSAMHub::group_after_discovery_()" in race:
        methods += "\n".join(method(race, signature) for signature in (
            "const GroupPreset *GenSAMHub::group_after_discovery_()",
            "bool GenSAMHub::group_reconfigures_(",
        ))
    else:
        # Code predating the helpers; stubs let the old pass run and fail the assertions.
        methods += r'''
const GroupPreset *GenSAMHub::group_after_discovery_() const { return nullptr; }
bool GenSAMHub::group_reconfigures_(const GroupPreset &, const GenSAMMonitor &) { return false; }
'''
    cpp = r'''
#include <cassert>
#include <cstdint>
#include <map>
#include <string>
#include <vector>
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGD(...) ((void)0)
const char *TAG = "test";
enum class RaceState { CONFIGURING_DEVICES, APPLYING_GROUP, POLLING_MONITORS };
constexpr uint32_t POLL_STEP_INTERVAL_MS = 20;
struct GroupDevice { uint32_t unique_id; bool enabled; };
struct GroupPreset { const char *name; const GroupDevice *devices; uint8_t device_count; };
struct GenSAMMonitor { uint8_t address; uint32_t unique_id; };
struct Frame { int kind; };
Frame make_stay_online() { return {1}; }
struct Registry {
  std::map<uint8_t, GenSAMMonitor> items;
  GenSAMMonitor *find(uint8_t address) {
    const auto it = items.find(address);
    return it == items.end() ? nullptr : &it->second;
  }
  GenSAMMonitor *find_by_serial_or_id(const std::string &id) {
    for (auto &kv : items) {
      if (std::to_string(kv.second.unique_id) == id) return &kv.second;
    }
    return nullptr;
  }
};
class GenSAMHub {
 public:
  Registry registry_;
  std::vector<uint8_t> poll_addrs_;
  std::vector<GroupPreset> groups_;
  size_t current_poll_index_ = 0;
  uint8_t current_query_addr_ = 0, current_query_cmd_ = 0, query_retries_ = 0;
  uint32_t race_step_time_ = 0, last_poll_cycle_time_ = 0, last_poll_step_time_ = 0;
  uint32_t poll_interval_ms_ = 1000;
  int pending_group_ = -1, selected_group_ = -1, active_group_ = -1, default_group_ = -1;
  const GroupPreset *configure_skip_group_ = nullptr;
  RaceState race_state_ = RaceState::CONFIGURING_DEVICES;
  std::vector<uint8_t> configured;
  int volume_broadcasts = 0, keepalives = 0, pushes = 0;

  const GroupPreset *get_group(uint8_t index) const {
    return index < groups_.size() ? &groups_[index] : nullptr;
  }
  bool configure_monitor_(const GenSAMMonitor &mon) {
    configured.push_back(mon.address);
    return true;
  }
  void finish_temporary_wake_() {}
  bool send_frame(const Frame &frame) { keepalives += frame.kind == 1; return true; }
  void broadcast_volume_and_keepalive_() { ++volume_broadcasts; }
  bool start_group_apply_(uint32_t) {
    ++pushes;
    race_state_ = RaceState::APPLYING_GROUP;
    return true;
  }
  void queue_runtime_settings_() {}
  void update_bus_status_() {}
  void resolve_speaker_groups_() {}

  // Acknowledge each configured monitor at once, as the RX path does.
  void run() {
    for (int i = 0; i < 20 && race_state_ == RaceState::CONFIGURING_DEVICES; i++) {
      race_step_configuring_(i);
      if (current_query_addr_ != 0) {
        current_poll_index_++;
        current_query_addr_ = 0;
      }
    }
  }

  const GroupPreset *group_after_discovery_() const;
  bool group_reconfigures_(const GroupPreset &group, const GenSAMMonitor &mon);
  void race_step_configuring_(uint32_t now);
};
'''
    if "--baseline" in sys.argv:
        cpp += "constexpr uint32_t CONFIG_ACK_TIMEOUT_MS = 200;\n"
    cpp += methods
    cpp += r'''
const GroupDevice STEREO[] = {{100, true}, {200, false}};
const GroupDevice OTHER[] = {{300, true}};

GenSAMHub make_hub() {
  GenSAMHub hub;
  hub.registry_.items = {{2, {2, 100}}, {3, {3, 200}}, {4, {4, 300}}};
  hub.poll_addrs_ = {2, 3, 4};
  hub.groups_ = {{"Stereo", STEREO, 2}, {"Other", OTHER, 1}};
  return hub;
}

int main() {
  // A saved controller selection or catalog default must not overwrite the speaker profile.
  GenSAMHub wake = make_hub();
  wake.selected_group_ = 0;
  wake.default_group_ = 1;
  wake.configure_skip_group_ = wake.group_after_discovery_();
  wake.run();
  assert(wake.configured.empty());
  assert(wake.volume_broadcasts == 1);
  assert(wake.pushes == 0 && wake.pending_group_ == -1);

  // A user request queued during discovery is still applied, without an earlier volume lift.
  GenSAMHub standby = make_hub();
  standby.pending_group_ = 0;
  standby.run();
  assert(standby.configured.empty());
  assert(standby.volume_broadcasts == 0 && standby.pushes == 1);
  return 0;
}
'''
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "test.cpp"
        binary = Path(directory) / "test"
        source.write_text(cpp)
        subprocess.run(["c++", "-std=c++17", "-Wall", str(source), "-o", str(binary)], check=True)
        subprocess.run([str(binary)], check=True)
    print("wake configuration tests passed")


if __name__ == "__main__":
    main()
