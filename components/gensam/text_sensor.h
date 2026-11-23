#pragma once

/// @file text_sensor.h
/// @brief Read-only Home Assistant views of speaker identity and explicit store progress.
///
/// These entities subscribe to protocol-core callbacks. Preset identity is tagged metadata,
/// not independently verified DSP readback; the core reports unknown and mixed states explicitly.

#include "esphome/components/text_sensor/text_sensor.h"
#include "hub.h"

namespace esphome {
namespace gensam {

/// @brief Displays the current preset identity and externally observed/unknown state.
class GenSAMActiveSettingsSensor : public text_sensor::TextSensor {
 public:
  /// @brief Subscribe to the hub's identity updates.
  /// @param hub Parent protocol hub.
  void set_hub(GenSAMHub *hub) {
    hub->add_settings_callback([this](const std::string &value) { this->publish_state(value); });
    this->publish_state(hub->get_active_settings());
  }
};

/// @brief Displays store progress, blocked operations and partial failures.
class GenSAMStoreStatusSensor : public text_sensor::TextSensor {
 public:
  /// @brief Subscribe to the hub's store result updates.
  /// @param hub Parent protocol hub.
  void set_hub(GenSAMHub *hub) {
    hub->add_store_status_callback([this](const std::string &value) { this->publish_state(value); });
    this->publish_state(hub->get_store_status());
  }
};

}  // namespace gensam
}  // namespace esphome
