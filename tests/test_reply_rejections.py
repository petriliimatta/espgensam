#!/usr/bin/env python3
"""Exercise production state-machine handling of explicit monitor rejection replies."""

from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
RACE_CPP = ROOT / "components/gensam/race.cpp"
COMPONENT = ROOT / "components/gensam"


def main():
    if "--baseline" in sys.argv:
        source = subprocess.check_output(
            ["git", "show", "HEAD:components/gensam/race.cpp"], cwd=ROOT, text=True
        )
    else:
        source = RACE_CPP.read_text()
    signature = "bool GenSAMHub::handle_command_rejection_("
    start = source.index(signature)
    end = source.index("\n}\n", start) + 3
    cpp = r'''
#include "const.h"
#include <cassert>
#include <cstdint>
using namespace esphome::gensam;

const char *TAG = "test";
void test_log(const char *, ...) {}
#define ESP_LOGW(...) test_log(__VA_ARGS__)
enum class RaceState {
  IDLE, POLLING_MONITORS, APPLYING_GROUP, WAKEUP_SENT, RACE_PING_SENT,
  RACE_SET_RID_SENT, QUERYING_DEVICES, CONFIGURING_DEVICES, SLEEPING
};
constexpr uint8_t SLEEP_PROBE_IDLE = 0;
struct Frame { uint8_t address, command; };
struct GroupApplyState {
  bool active = false, rejected = false, awaiting_reply = false;
  size_t device_idx = 0;
  uint8_t step = 0;
  uint32_t last_tx_ms = 0;
};
class GenSAMHub {
 public:
  RaceState race_state_ = RaceState::IDLE;
  uint8_t current_query_addr_ = 0, current_query_cmd_ = 0, query_retries_ = 0;
  uint8_t current_poll_index_ = 0, iss_query_op_ = 0, sleep_probe_phase_ = 0;
  uint32_t race_step_time_ = 0, last_poll_step_time_ = 0, last_sleep_tx_ms_ = 0;
  size_t device_queries_advanced_ = 0;
  GroupApplyState apply_;
  void advance_device_query_() {
    ++device_queries_advanced_;
    current_query_addr_ = 0;
  }
  bool handle_command_rejection_(const Frame &frame, uint32_t now);
};
'''
    cpp += source[start:end]
    cpp += r'''
int main() {
  GenSAMHub query;
  query.race_state_ = RaceState::QUERYING_DEVICES;
  query.current_query_addr_ = 2;
  assert(query.handle_command_rejection_({HOST_ADDRESS, CMD_UNSUPPORTED}, 10));
  assert(query.device_queries_advanced_ == 1 && query.current_query_addr_ == 0);

  GenSAMHub configuration;
  configuration.race_state_ = RaceState::CONFIGURING_DEVICES;
  configuration.current_query_addr_ = 3;
  configuration.current_poll_index_ = 1;
  assert(configuration.handle_command_rejection_({HOST_ADDRESS, CMD_COMMAND_REJECTED}, 20));
  assert(configuration.current_query_addr_ == 0 && configuration.current_poll_index_ == 2);

  GenSAMHub polling;
  polling.race_state_ = RaceState::POLLING_MONITORS;
  polling.current_query_addr_ = 4;
  polling.iss_query_op_ = WAKEUP_OP_ISS_LEVEL;
  assert(polling.handle_command_rejection_({HOST_ADDRESS, CMD_UNSUPPORTED}, 30));
  assert(polling.current_query_addr_ == 0 && polling.iss_query_op_ == 0 &&
         polling.last_poll_step_time_ == 30);

  GenSAMHub group;
  group.race_state_ = RaceState::APPLYING_GROUP;
  group.apply_.active = true;
  group.apply_.awaiting_reply = true;
  group.apply_.device_idx = 2;
  group.apply_.step = 7;
  group.current_query_addr_ = 6;
  assert(group.handle_command_rejection_({HOST_ADDRESS, CMD_UNSUPPORTED}, 40));
  assert(group.apply_.rejected && !group.apply_.awaiting_reply);
  assert(group.current_query_addr_ == 0 && group.apply_.device_idx == 3 &&
         group.apply_.step == 0 && group.apply_.last_tx_ms == 40);

  GenSAMHub unrelated;
  assert(!unrelated.handle_command_rejection_({HOST_ADDRESS, CMD_UNSUPPORTED}, 50));
  assert(!unrelated.handle_command_rejection_({HOST_ADDRESS, CMD_ACK}, 50));
}
'''
    with tempfile.TemporaryDirectory(prefix="gensam-rejections-") as directory:
        root = Path(directory)
        (root / "test.cpp").write_text(cpp)
        binary = root / "test"
        subprocess.run(
            ["c++", "-std=c++17", "-Wall", "-Wextra", "-I", str(COMPONENT),
             str(root / "test.cpp"), "-o", str(binary)],
            check=True,
        )
        subprocess.run([str(binary)], check=True)
    print("Explicit command rejection checks passed")


if __name__ == "__main__":
    main()
