#!/usr/bin/env python3
"""Exercise production attribution of second-frame telemetry continuations."""

from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "components/gensam"
RACE_CPP = COMPONENT / "race.cpp"


def main():
    source = RACE_CPP.read_text()
    signature = "bool GenSAMHub::handle_poll_continuation_("
    start = source.index(signature)
    end = source.index("\n}\n", start) + 3
    cpp = r'''
#include "monitor.h"
#include <cassert>
#include <cmath>
#include <cstdint>
#include <map>
using namespace esphome::gensam;

const char *TAG = "test";
#define ESP_LOGD(...) ((void) 0)
enum class RaceState { IDLE, POLLING_MONITORS };
struct Frame {
  uint8_t address, command;
  std::vector<uint8_t> payload;
};
class Registry {
 public:
  std::map<uint8_t, GenSAMMonitor> entries;
  GenSAMMonitor *find(uint8_t address) {
    auto it = entries.find(address);
    return it == entries.end() ? nullptr : &it->second;
  }
  void bind_if_matched(GenSAMMonitor &) {}
  void publish_telemetry(GenSAMMonitor &) {}
};
class GenSAMHub {
 public:
  RaceState race_state_ = RaceState::POLLING_MONITORS;
  uint8_t current_query_addr_ = 0, telemetry_continuation_addr_ = 0;
  uint32_t telemetry_continuation_ms_ = 0;
  Registry registry_;
  bool seen = false, evaluated_standby = false;
  void mark_monitor_seen_(GenSAMMonitor &) { seen = true; }
  void evaluate_system_standby_() { evaluated_standby = true; }
  bool handle_poll_continuation_(const Frame &frame, uint32_t now);
};
'''
    cpp += source[start:end]
    cpp += r'''
int main() {
  GenSAMHub hub;
  auto &monitor = hub.registry_.entries[2];
  monitor.address = 2;
  hub.telemetry_continuation_addr_ = 2;
  hub.telemetry_continuation_ms_ = 100;
  Frame continuation{HOST_ADDRESS, CMD_REPORT_STATUS, {0x81, 0x00, 42}};
  assert(hub.handle_poll_continuation_(continuation, 110));
  assert(std::isfinite(monitor.tw_coil_temperature) && hub.seen && hub.evaluated_standby);
  assert(hub.telemetry_continuation_addr_ == 0);

  hub.telemetry_continuation_addr_ = 2;
  assert(!hub.handle_poll_continuation_(
      {HOST_ADDRESS, CMD_REPORT_STATUS, {0x81, 0x00, 42}}, 121));
  assert(hub.telemetry_continuation_addr_ == 2);
  assert(!hub.handle_poll_continuation_(
      {HOST_ADDRESS, CMD_REPORT_STATUS, {0x41, 30}}, 105));

  hub.current_query_addr_ = 3;
  assert(!hub.handle_poll_continuation_(continuation, 110));
  assert(hub.telemetry_continuation_addr_ == 2);
}
'''
    with tempfile.TemporaryDirectory(prefix="gensam-telemetry-continuation-") as directory:
        root = Path(directory)
        (root / "test.cpp").write_text(cpp)
        binary = root / "test"
        subprocess.run(
            ["c++", "-std=c++17", "-Wall", "-Wextra", "-I", str(COMPONENT),
             str(root / "test.cpp"), str(COMPONENT / "monitor.cpp"), "-o", str(binary)],
            check=True,
        )
        subprocess.run([str(binary)], check=True)
    print("Telemetry continuation attribution checks passed")


if __name__ == "__main__":
    main()
