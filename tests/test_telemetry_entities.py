#!/usr/bin/env python3
"""Exercise production publishing of per-monitor protection and clipping telemetry."""

from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "components/gensam/registry.cpp"


def extract(source, signature):
    start = source.index(signature)
    return source[start:source.index("\n}\n", start) + 3]


def main():
    source = SOURCE.read_text()
    signatures = [
        "void MonitorRegistry::publish_telemetry(",
        "void MonitorRegistry::publish_telemetry_events(",
        "void MonitorRegistry::publish_initial_telemetry_events(",
        "void MonitorRegistry::publish_stale_telemetry(",
        "bool MonitorRegistry::expire_stale(",
        "void MonitorRegistry::invalidate_all(",
    ]
    methods = "\n".join(extract(source, sig) for sig in signatures)
    invalidate = extract(source, "void invalidate_protection(")

    cpp = r'''
#include <cassert>
#include <cmath>
#include <cstdint>
#include <map>
#include <string>
#include <utility>
#include <vector>
#include "monitor.h"
#define ESP_LOGW(...) ((void) 0)
namespace esphome {
namespace sensor {
class Sensor {
 public:
  void publish_state(float) {}
};
}
namespace binary_sensor {
class BinarySensor {
 public:
  bool known = false, state = false;
  std::vector<bool> history;
  void publish_state(bool value) { known = true; state = value; history.push_back(value); }
  void invalidate_state() { known = false; }
};
}
namespace gensam {
class MonitorRegistry {
 public:
  std::vector<GenSAMMonitorBinding> bindings_;
  std::map<int, GenSAMMonitor> monitors_;
  void publish_online(const GenSAMMonitor &) {}
  void publish_telemetry(const GenSAMMonitor &mon);
  void publish_telemetry_events(const GenSAMMonitor &mon);
  void publish_initial_telemetry_events();
  void publish_stale_telemetry();
  bool expire_stale(uint32_t now, uint32_t stale_timeout_ms);
  void invalidate_all();
};
'''
    cpp += invalidate + methods
    cpp += r'''
}  // namespace gensam
}  // namespace esphome
using namespace esphome::gensam;
int main() {
  esphome::binary_sensor::BinarySensor protection, clipping;
  MonitorRegistry registry;
  registry.bindings_.emplace_back();
  auto &binding = registry.bindings_.back();
  binding.protection_sensor = &protection;
  binding.clipping_sensor = &clipping;
  auto &mon = registry.monitors_[2];
  mon.binding = &binding;

  // Boot: off before any monitor is discovered, not unknown.
  registry.publish_initial_telemetry_events();
  assert(protection.known && !protection.state && clipping.known && !clipping.state);

  // No report means neither condition: both read off, never unknown, once telemetry arrives.
  const uint8_t quiet[] = {0x41, 30};
  parse_telemetry(quiet, sizeof(quiet), mon);
  registry.publish_telemetry(mon);
  assert(protection.known && !protection.state && clipping.known && !clipping.state);

  const uint8_t started[] = {0x41, 30, 0x02, 0x04, 0x04};
  parse_telemetry(started, sizeof(started), mon);
  registry.publish_telemetry(mon);
  assert(protection.state && clipping.state);

  // A continuation frame belongs to the same poll cycle and must not clear clipping.
  const uint8_t continued[] = {0x81, 0x00, 32};
  parse_telemetry(continued, sizeof(continued), mon);
  registry.publish_telemetry(mon);
  assert(protection.state && clipping.state);

  const uint8_t ended[] = {0x03, 0x41, 31};
  parse_telemetry(ended, sizeof(ended), mon);
  registry.publish_telemetry(mon);
  assert(protection.known && !protection.state && clipping.known && !clipping.state);

  // Offline or stale: unknown, and a protection start missed meanwhile is not carried over.
  parse_telemetry(started, sizeof(started), mon);
  registry.publish_telemetry(mon);
  mon.online = true;
  mon.last_seen_ms = 100;
  assert(registry.expire_stale(200, 50));
  assert(!protection.known && !clipping.known && !mon.protection_active && !mon.clipping_active);
  parse_telemetry(quiet, sizeof(quiet), mon);
  registry.publish_telemetry(mon);
  assert(protection.known && !protection.state && clipping.known && !clipping.state);

  parse_telemetry(started, sizeof(started), mon);
  registry.publish_telemetry(mon);
  registry.publish_stale_telemetry();
  assert(!protection.known && !clipping.known && !mon.protection_active);

  parse_telemetry(started, sizeof(started), mon);
  registry.publish_telemetry(mon);
  registry.invalidate_all();
  assert(!protection.known && !clipping.known && !mon.protection_active);

  // An unbound monitor publishes nothing.
  mon.binding = nullptr;
  const size_t count = protection.history.size();
  parse_telemetry(started, sizeof(started), mon);
  registry.publish_telemetry(mon);
  assert(protection.history.size() == count);
}
'''
    with tempfile.TemporaryDirectory(prefix="gensam-telemetry-entities-") as directory:
        root = Path(directory)
        (root / "test.cpp").write_text(cpp)
        binary = root / "test"
        subprocess.run(
            ["c++", "-std=c++17", "-Wall", "-Wextra", "-I",
             str(ROOT / "components/gensam"), str(root / "test.cpp"),
             str(ROOT / "components/gensam/monitor.cpp"), "-o", str(binary)],
            check=True,
        )
        subprocess.run([str(binary)], check=True)
    print("Protection and clipping state checks passed")


if __name__ == "__main__":
    main()
