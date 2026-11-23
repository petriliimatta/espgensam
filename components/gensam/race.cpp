/// @file race.cpp
/// @brief RACE monitor discovery, device interrogation, and telemetry polling state machine.
/// See hub.h for the RACE protocol rationale and complete API documentation.

#include "hub.h"
#include "commands.h"
#include "iss.h"

#include "esphome/core/log.h"
#include "esphome/components/select/select.h"

static const char *const TAG = "gensam";

namespace esphome {
namespace gensam {

namespace {

/// Settling delay after boot before probing the bus, so WiFi association completes first.
constexpr uint32_t BOOT_SETTLE_MS = 10000;

/// Time monitors need to boot their DSP after a wakeup broadcast before they can race.
constexpr uint32_t WAKEUP_BOOT_MS = 400;

/// Silence after a discovery ping that means no unassigned monitor is left to answer.
constexpr uint32_t RACE_PING_TIMEOUT_MS = 350;

/// Time to wait for a monitor to acknowledge its assigned address before resending.
constexpr uint32_t RID_ACK_TIMEOUT_MS = 300;

/// Address assignment attempts before assuming the monitor took the address but lost the ACK.
constexpr uint8_t RID_MAX_RETRIES = 3;

/// Time to wait for a model / firmware / barcode reply before moving on.
constexpr uint32_t DEVICE_QUERY_TIMEOUT_MS = 250;

/// Retries of the same device query before giving up on it.
///
/// Monitors answer these in 30-60 ms, so a silent 250 ms window means the request or the reply
/// was lost rather than delayed. Device metadata is only ever asked for once per discovery, so
/// without a retry a single corrupted frame leaves that monitor's model or serial blank until
/// the next rediscovery - which on a bus with a percent or so of frame errors happens readily.
constexpr uint8_t DEVICE_QUERY_MAX_RETRIES = 2;

/// Time to wait for a telemetry reply before abandoning that monitor's turn in the cycle.
constexpr uint32_t POLL_REPLY_TIMEOUT_MS = 300;

/// Minimum spacing between consecutive monitor polls within one cycle.
constexpr uint32_t POLL_STEP_INTERVAL_MS = 20;

/// How often to retry discovery while no monitors are registered.
constexpr uint32_t DISCOVERY_RETRY_INTERVAL_MS = 10000;

/// RS-485 transceiver direction turnaround allowance between back-to-back frames.
constexpr uint32_t TURNAROUND_US = 300;

/// How often each monitor's ISS level (or, first, its own ISS settings) is read while polling.
constexpr uint32_t ISS_SAMPLE_INTERVAL_MS = 5000;

/// A level sample older than this does not count towards deciding that the input is silent.
constexpr uint32_t ISS_SAMPLE_MAX_AGE_MS = 20000;

/// Wait after the sleep request before the first probe; the monitors' power state is still
/// settling and an early bounce is detected just as well a little later.
constexpr uint32_t SLEEP_FIRST_PROBE_MS = 20000;

/// Spacing of the sleep probes; each one checks a single monitor.
constexpr uint32_t SLEEP_PROBE_INTERVAL_MS = 5000;

/// Time to wait for each half (address ACK, state reply) of a sleep probe.
constexpr uint32_t SLEEP_PROBE_REPLY_TIMEOUT_MS = 300;

/// A stored ISS delay at or above this many minutes means the speaker never sleeps.
constexpr uint32_t ISS_STORED_DELAY_NEVER_MIN = 65535;

/// Phases of one sleep probe, in sleep_probe_phase_.
enum : uint8_t {
  SLEEP_PROBE_IDLE = 0,
  SLEEP_PROBE_ASSIGNING,
  SLEEP_PROBE_READING,
};

/// Spacing between the volume broadcast and the keep-alive that follows it.
constexpr uint32_t VOLUME_KEEPALIVE_GAP_US = 250;

/// Spacing between consecutive frames of a group's DSP block.
///
/// Matches the OEM's own pacing. Monitors acknowledge a PEQ frame in under a millisecond, and
/// GLM does not wait for the acknowledgement before sending the next; it simply spaces them.
/// Doing the same keeps a group push to a few hundred milliseconds while leaving the bus idle
/// between frames for a monitor's reply.
constexpr uint32_t GROUP_FRAME_GAP_MS = 3;

/// Spacing between the frames of the wake burst that opens a group push.
///
/// Wider than GROUP_FRAME_GAP_MS because the OEM's own burst is: measured at ~22 ms between the
/// two 0x7F frames and ~38 ms before the 0x01, consistently across six captured group switches.
/// One figure for both slots is close enough and keeps a single constant.
constexpr uint32_t GROUP_WAKE_GAP_MS = 20;
constexpr uint32_t GROUP_REPLY_TIMEOUT_MS = 3;

/// Abandon a group push that has not finished in this long.
///
/// A push holds the system at silence, so it must not be able to stall there indefinitely --
/// which it otherwise could, since the state machine stops advancing whenever an external GLM
/// adapter takes the bus, and that hold has its own multi-second cooldown.
constexpr uint32_t GROUP_APPLY_TIMEOUT_MS = 15000;

/// Wake burst opening a group push, one value per frame; see GroupApplyState::preamble_step.
constexpr uint8_t GROUP_WAKE_SEQUENCE[] = {WAKEUP_VAL_ON_1, WAKEUP_VAL_ON_1, WAKEUP_VAL_ON_2};
constexpr uint8_t GROUP_WAKE_STEPS = sizeof(GROUP_WAKE_SEQUENCE);

}  // namespace

// --- Discovery entry points --------------------------------------------------------

void GenSAMHub::start_race_discovery() {
  if (!can_transmit()) {
    ESP_LOGW(TAG, "Cannot start RACE discovery: Hub is not in transmitting state");
    return;
  }

  if (race_state_ == RaceState::RACE_PING_SENT || race_state_ == RaceState::RACE_SET_RID_SENT) {
    return;
  }

  initial_discovery_done_ = true;
  ESP_LOGI(TAG, "Starting GLM RACE monitor discovery...");
  race_state_ = RaceState::RACE_PING_SENT;
  this->update_bus_status_();
  next_assign_addr_ = MONITOR_START_ADDR;
  current_racing_bytes_.clear();
  current_racing_id_ = 0;
  rid_retries_ = 0;
  race_step_time_ = millis();

  // Broadcast initial RACE discovery ping (0xFF 0xFE)
  this->send_frame(make_discovery_ping());
}

void GenSAMHub::rediscover_monitors() {
  if (!executing_user_command_ &&
      this->defer_user_command_([this]() { this->discover_monitors_(); })) return;
  this->discover_monitors_();
}

void GenSAMHub::discover_monitors_() {
  if (!can_transmit()) {
    ESP_LOGW(TAG, "Cannot start RACE discovery: Hub is not in transmitting state");
    return;
  }

  initial_discovery_done_ = true;
  resuming_ = false;
  recovery_required_ = false;
  bus_silent_ = false;

  ESP_LOGI(TAG, "Resetting monitor table and initiating RACE discovery...");
  this->reset_volatile_settings_();

  // Mark all currently known monitors offline before clearing cache so HA state does not remain stale.
  registry_.invalidate_all();
  this->evaluate_system_mute_();

  registry_.clear();
  poll_addrs_.clear();
  current_poll_index_ = 0;
  current_query_addr_ = 0;
  current_query_cmd_ = 0;
  query_retries_ = 0;
  iss_query_op_ = 0;
  sleep_probe_phase_ = 0;
  last_signal_ms_ = millis();
  current_racing_bytes_.clear();
  current_racing_id_ = 0;
  rid_retries_ = 0;
  last_queried_addr_ = 0;
  last_queried_cmd_ = 0;
  next_assign_addr_ = MONITOR_START_ADDR;

  // Discovery always begins by waking, exactly as GLM does. A monitor in ISS sleep does answer a
  // RACE ping, but a discovered system is expected to play, and the wake is also what restores a
  // monitor's DSP from hard standby. When the system
  // is meant to remain off, the amplifiers are silenced right behind the wakeup so nothing is
  // audible, and returned to standby by finish_temporary_wake_() once discovery completes.
  this->send_wakeup();
  if (current_standby_) {
    this->silence_system_volume_();
    restore_standby_after_discovery_ = true;
    ESP_LOGI(TAG, "Waking monitors silently for discovery; will return them to standby");
  }
  race_state_ = RaceState::WAKEUP_SENT;
  this->update_bus_status_();
  race_step_time_ = millis();
}

void GenSAMHub::finish_temporary_wake_() {
  if (!restore_standby_after_discovery_) {
    return;
  }
  restore_standby_after_discovery_ = false;
  ESP_LOGI(TAG, "Discovery complete; returning monitors to standby");
  this->send_standby();
  // Monitors keep reporting an active amplifier until the transition lands; hold the commanded
  // state over that telemetry the same way an explicit standby command does.
  last_standby_command_ = millis();
}

void GenSAMHub::complete_rid_assignment_(uint8_t address) {
  uint32_t now = millis();
  GenSAMMonitor &mon = registry_.get_or_create(address);
  mon.unique_id = current_racing_id_;
  this->mark_monitor_seen_(mon);

  ESP_LOGI(TAG, "Assigned monitor at address 0x%02X (ID: %u)", address, (unsigned)mon.unique_id);
  registry_.bind_if_matched(mon);

  next_assign_addr_++;
  current_racing_bytes_.clear();
  current_racing_id_ = 0;
  rid_retries_ = 0;

  delayMicroseconds(TURNAROUND_US);

  if (next_assign_addr_ > MONITOR_MAX_ADDR) {
    ESP_LOGW(TAG, "RACE address space exhausted at 0x%02X; no more monitor RIDs are available",
             MONITOR_MAX_ADDR);
    this->finish_race_discovery_(millis());
    return;
  }

  // Send next RACE ping
  this->send_frame(make_discovery_ping());
  race_state_ = RaceState::RACE_PING_SENT;
  race_step_time_ = now;
}

void GenSAMHub::advance_device_query_() {
  if (current_query_cmd_ == CMD_SERVICE) {
    current_query_cmd_ = CMD_SOFTWARE_QUERY;
  } else if (current_query_cmd_ == CMD_SOFTWARE_QUERY) {
    current_query_cmd_ = CMD_BAR_CODE;
  } else if (current_query_cmd_ == CMD_BAR_CODE) {
    current_query_cmd_ = CMD_DIP_SWITCH;
  } else if (current_query_cmd_ == CMD_DIP_SWITCH) {
    current_query_cmd_ = CMD_NAMES_USER;
  } else if (current_query_cmd_ == CMD_NAMES_USER) {
    current_query_cmd_ = CMD_WAKEUP;
  } else {
    current_query_cmd_ = restore_profiles_on_discovery_ ? CMD_SERVICE : CMD_SOFTWARE_QUERY;
    current_poll_index_++;
  }
  current_query_addr_ = 0;
  query_retries_ = 0;
}

void GenSAMHub::broadcast_volume_and_keepalive_() {
  // The volume broadcast is what re-establishes amplifier gain, so it is skipped while in
  // standby. The keep-alive is not: it refreshes the volatile RACE address leases, and without
  // it monitors stop answering on their assigned addresses a minute or two later.
  if (!current_standby_) {
    this->transmit_volume_();
    delayMicroseconds(VOLUME_KEEPALIVE_GAP_US);
  }
  this->send_frame(make_stay_online());
}

// --- Incoming replies to what the state machine sent --------------------------------

bool GenSAMHub::handle_active_reply_(const Frame &frame, uint32_t now) {
  if (this->handle_speaker_settings_reply_(frame, now)) return true;
  if (this->handle_command_rejection_(frame, now) ||
      this->handle_poll_continuation_(frame, now)) {
    return true;
  }
  if (resuming_ && race_state_ == RaceState::SLEEPING) {
    // The late reply to a sleep probe that a wake request overtook; it only held the slot.
    if (current_query_addr_ == 0 || frame.address != HOST_ADDRESS) return false;
    current_query_addr_ = 0;
    return true;
  }
  if (resuming_) {
    if (current_query_addr_ == 0 || frame.address != HOST_ADDRESS ||
        (frame.command != CMD_REPORT_STATUS && frame.command != CMD_QUERY_STATUS)) return false;
    auto *mon = registry_.find(current_query_addr_);
    if (mon == nullptr) return false;
    mon->standby_known = false;
    parse_telemetry(frame.payload.data(), frame.payload.size(), *mon);
    if (!mon->standby_known) {
      registry_.bind_if_matched(*mon);
      registry_.publish_telemetry_events(*mon);
      return true;  // Keep waiting for actual power telemetry.
    }
    this->mark_monitor_seen_(*mon);
    registry_.publish_telemetry(*mon);
    current_query_addr_ = 0;
    if (mon->standby) {
      resuming_ = false;
      this->discover_monitors_();
    }
    return true;
  }
  switch (race_state_) {
    case RaceState::RACE_PING_SENT: {
      // Expecting winning unassigned monitor response with 3-byte serial
      if (frame.address != HOST_ADDRESS ||
          (frame.command != CMD_REPORT_STATUS && frame.command != CMD_ACK) ||
          frame.payload.size() != 3) {
        return false;
      }
      current_racing_bytes_ = frame.payload;
      current_racing_id_ = (static_cast<uint32_t>(frame.payload[0]) << 16) |
                           (static_cast<uint32_t>(frame.payload[1]) << 8) |
                           static_cast<uint32_t>(frame.payload[2]);
      race_state_ = RaceState::RACE_SET_RID_SENT;
      race_step_time_ = now;
      rid_retries_ = 0;

      delayMicroseconds(TURNAROUND_US);

      // Assign address to this monitor via CMD_SET_RID to multicast (0xF0)
      this->send_frame(make_set_rid(current_racing_bytes_, next_assign_addr_));
      return true;
    }

    case RaceState::RACE_SET_RID_SENT: {
      // Expecting ACK confirming address assignment: a frame addressed to HOST_ADDRESS with
      // CMD_REPORT_STATUS or CMD_ACK, carrying exactly the address we just assigned.
      if (frame.address != HOST_ADDRESS ||
          (frame.command != CMD_REPORT_STATUS && frame.command != CMD_ACK) ||
          frame.payload.size() != 1 || frame.payload[0] != next_assign_addr_) {
        return false;
      }
      this->complete_rid_assignment_(next_assign_addr_);
      return true;
    }

    case RaceState::QUERYING_DEVICES: {
      const bool restore_reply = current_query_cmd_ == CMD_SERVICE &&
                                 (frame.command == CMD_REPORT_STATUS || frame.command == CMD_ACK);
      const bool note_reply = current_query_cmd_ == CMD_NAMES_USER &&
                              frame.command == CMD_REPORT_STATUS;
      const bool dip_reply = current_query_cmd_ == CMD_DIP_SWITCH &&
                             (frame.command == CMD_DIP_SWITCH ||
                              frame.command == CMD_REPORT_STATUS);
      const bool metadata_reply = current_query_cmd_ != CMD_DIP_SWITCH &&
                                 (frame.command == CMD_REPORT_STATUS ||
                                  frame.command == CMD_SOFTWARE_QUERY_SHORT ||
                                  frame.command == CMD_SOFTWARE_QUERY ||
                                  frame.command == CMD_BAR_CODE);
      if (current_query_addr_ == 0 || frame.address != HOST_ADDRESS ||
          (frame.payload.empty() && !restore_reply && !note_reply) ||
          (!restore_reply && !note_reply && !dip_reply && !metadata_reply)) {
        return false;
      }
      GenSAMMonitor &mon = registry_.get_or_create(current_query_addr_);
      if (restore_reply) {
        if (!frame.payload.empty()) return false;
        profile_restore_failed_.erase(current_query_addr_);
      } else if (current_query_cmd_ == CMD_NAMES_USER) {
        auto &state = speaker_settings_[mon.unique_id];
        state.note.assign(frame.payload.begin(), frame.payload.end());
        state.note_known = true;
      } else if (current_query_cmd_ == CMD_WAKEUP) {
        if (frame.payload.size() != 1 || frame.payload[0] > 1) {
          ESP_LOGW(TAG, "Monitor 0x%02X returned no usable stored LED state", mon.address);
        } else {
          mon.system_leds_enabled = frame.payload[0] == 0;
          mon.system_leds_known = true;
        }
      } else if (current_query_cmd_ == CMD_DIP_SWITCH) {
        if (!parse_dip_switch_status(frame.payload.data(), frame.payload.size(), mon)) {
          ESP_LOGW(TAG, "Monitor 0x%02X returned no usable DIP status", current_query_addr_);
        } else {
          ESP_LOGI(TAG, "Monitor 0x%02X DIP words: current=0x%08lX previous=0x%08lX word3=0x%08lX",
                   current_query_addr_, static_cast<unsigned long>(mon.dip_word),
                   static_cast<unsigned long>(mon.previous_dip_word),
                   static_cast<unsigned long>(mon.dip_auxiliary_word));
        }
      } else if (current_query_cmd_ == CMD_BAR_CODE) {
        parse_barcode(frame.payload.data(), frame.payload.size(), mon);
        ESP_LOGI(TAG, "Discovered serial for 0x%02X: %s", current_query_addr_, mon.serial_number.c_str());
      } else {
        parse_device_info(frame.payload.data(), frame.payload.size(), mon);
        ESP_LOGI(TAG, "Discovered: %s", mon.to_string().c_str());
      }
      this->mark_monitor_seen_(mon);
      registry_.bind_if_matched(mon);
      registry_.publish_metadata(mon);

      this->advance_device_query_();
      race_step_time_ = now;
      return true;
    }

    case RaceState::CONFIGURING_DEVICES: {
      if (current_query_addr_ == 0 || frame.address != HOST_ADDRESS ||
          (frame.command != CMD_REPORT_STATUS && frame.command != CMD_ACK)) {
        return false;
      }
      ESP_LOGD(TAG, "Monitor 0x%02X acknowledged configuration", current_query_addr_);
      current_poll_index_++;
      current_query_addr_ = 0;
      race_step_time_ = now;
      return true;
    }

    case RaceState::APPLYING_GROUP: {
      if (!apply_.active || !apply_.awaiting_reply || current_query_addr_ == 0 ||
          frame.address != HOST_ADDRESS ||
          (frame.command != CMD_ACK &&
           (frame.command != CMD_REPORT_STATUS || !frame.payload.empty()))) {
        return false;
      }
      current_query_addr_ = 0;
      apply_.awaiting_reply = false;
      return true;
    }

    case RaceState::POLLING_MONITORS: {
      if (iss_query_op_ != 0) {
        return this->handle_iss_reply_(frame, now);
      }
      if (current_query_addr_ == 0 || frame.address != HOST_ADDRESS ||
          (frame.command != CMD_REPORT_STATUS && frame.command != CMD_QUERY_STATUS)) {
        return false;
      }
      GenSAMMonitor &mon = registry_.get_or_create(current_query_addr_);
      parse_telemetry(frame.payload.data(), frame.payload.size(), mon);
      this->mark_monitor_seen_(mon);
      registry_.bind_if_matched(mon);
      registry_.publish_telemetry(mon);
      this->evaluate_system_standby_();
      telemetry_continuation_addr_ = current_query_addr_;
      telemetry_continuation_ms_ = now;
      if (frame.payload.size() == 1 && is_telemetry_event_tag(frame.payload[0])) {
        ESP_LOGD(TAG, "[0x%02X %s] Telemetry event 0x%02X",
                 current_query_addr_, mon.model.c_str(), frame.payload[0]);
      } else {
        ESP_LOGI(TAG, "[0x%02X %s] Telemetry: %s Temp=%d°C In=%d dBFS Out=%d dBFS",
                 current_query_addr_, mon.model.c_str(),
                 mon.standby_transition ? "TRANSITION" :
                     (mon.standby_known ? (mon.standby ? "STANDBY" : "ACTIVE") : "UNKNOWN"),
                 (int)mon.temperature, (int)mon.input_db, (int)mon.output_db);
      }
      current_query_addr_ = 0;
      last_poll_step_time_ = now;
      return true;
    }

    case RaceState::SLEEPING: {
      if (current_query_addr_ == 0 || frame.address != HOST_ADDRESS ||
          (frame.command != CMD_REPORT_STATUS && frame.command != CMD_ACK) ||
          frame.payload.size() != 1) {
        return false;
      }
      const uint8_t address = current_query_addr_;
      if (sleep_probe_phase_ == SLEEP_PROBE_ASSIGNING) {
        if (frame.payload[0] != address) {
          return false;
        }
        delayMicroseconds(TURNAROUND_US);
        this->send_frame(make_iss_read(address, WAKEUP_OP_POWER));
        sleep_probe_phase_ = SLEEP_PROBE_READING;
        race_step_time_ = now;
        last_sleep_tx_ms_ = now;
        return true;
      }
      if (sleep_probe_phase_ != SLEEP_PROBE_READING) {
        return false;
      }
      sleep_probe_phase_ = SLEEP_PROBE_IDLE;
      current_query_addr_ = 0;
      sleep_probe_index_++;
      if (frame.payload[0] == ISS_STATE_AWAKE) {
        this->wake_from_sleep_(address);
      } else {
        ESP_LOGD(TAG, "Sleep probe: monitor 0x%02X ISS state %u", address,
                 (unsigned) frame.payload[0]);
      }
      return true;
    }

    default:
      return false;
  }
}

// --- State machine ------------------------------------------------------------------

void GenSAMHub::update_race_state_machine_() {
  if (resuming_ || speaker_transaction_.active) return;
  if (power_off_pending_ && !apply_.active && current_query_addr_ == 0) return;
  if (!can_transmit()) {
    return;
  }

  uint32_t now = millis();

  // Wait for WiFi association to settle before active bus probing
  if (now - boot_time_ < BOOT_SETTLE_MS) {
    return;
  }

  // Trigger initial wakeup + discovery cycle
  if (!initial_discovery_done_) {
    this->discover_monitors_();
    return;
  }

  switch (race_state_) {
    case RaceState::WAKEUP_SENT:
      this->race_step_wakeup_(now);
      break;
    case RaceState::RACE_PING_SENT:
      this->race_step_ping_(now);
      break;
    case RaceState::RACE_SET_RID_SENT:
      this->race_step_set_rid_(now);
      break;
    case RaceState::QUERYING_DEVICES:
      this->race_step_querying_(now);
      break;
    case RaceState::CONFIGURING_DEVICES:
      this->race_step_configuring_(now);
      break;
    case RaceState::APPLYING_GROUP:
      this->race_step_applying_group_(now);
      break;
    case RaceState::POLLING_MONITORS:
      this->race_step_polling_(now);
      break;
    case RaceState::IDLE:
      this->race_step_idle_(now);
      break;
    case RaceState::SLEEPING:
      this->race_step_sleeping_(now);
      break;
    default:
      break;
  }
}

void GenSAMHub::race_step_wakeup_(uint32_t now) {
  // Wait for the monitors' DSPs to boot before beginning RACE
  if (now - race_step_time_ >= WAKEUP_BOOT_MS) {
    this->start_race_discovery();
  }
}

void GenSAMHub::race_step_ping_(uint32_t now) {
  // Silence following the discovery ping means no unassigned monitor is left
  if (now - race_step_time_ <= RACE_PING_TIMEOUT_MS) {
    return;
  }

  if (registry_.empty()) {
    ESP_LOGI(TAG, "RACE discovery complete: No monitors responded (will retry in %us)",
             (unsigned)(DISCOVERY_RETRY_INTERVAL_MS / 1000));
    // Nothing answered, but the wakeup may still have powered something up; put it back.
    this->finish_temporary_wake_();
    race_state_ = RaceState::IDLE;
    this->update_bus_status_();
    last_discovery_retry_time_ = now;
    return;
  }

  this->finish_race_discovery_(now);
}

void GenSAMHub::finish_race_discovery_(uint32_t now) {
  ESP_LOGI(TAG, "RACE discovery complete. Total monitors found: %u", (unsigned)registry_.size());
  registry_.assign_discovery_slots();

  // Transition all monitors from discovery to online mode
  this->send_frame(make_stay_online());

  poll_addrs_ = registry_.addresses();
  race_state_ = RaceState::QUERYING_DEVICES;
  this->update_bus_status_();
  race_step_time_ = now;
  current_poll_index_ = 0;
  current_query_addr_ = 0;
  current_query_cmd_ = restore_profiles_on_discovery_ ? CMD_SERVICE : CMD_SOFTWARE_QUERY;
  query_retries_ = 0;
}

void GenSAMHub::race_step_set_rid_(uint32_t now) {
  if (now - race_step_time_ <= RID_ACK_TIMEOUT_MS) {
    return;
  }

  rid_retries_++;
  if (rid_retries_ <= RID_MAX_RETRIES) {
    ESP_LOGW(TAG, "Timeout waiting for RID ACK for address 0x%02X (attempt %u/%u). Retrying CMD_SET_RID...",
             next_assign_addr_, (unsigned)rid_retries_, (unsigned)RID_MAX_RETRIES);
    this->send_frame(make_set_rid(current_racing_bytes_, next_assign_addr_));
    race_step_time_ = now;
    return;
  }

  // Retries exhausted. The monitor may have adopted the address despite the lost ACK, so
  // register it and probe it during the query phase rather than abandoning it.
  ESP_LOGW(TAG, "Retries exhausted for RID ACK at address 0x%02X. Registering monitor and resuming discovery...",
            next_assign_addr_);
  this->complete_rid_assignment_(next_assign_addr_);
}

void GenSAMHub::race_step_querying_(uint32_t now) {
  if (current_query_addr_ != 0 && now - race_step_time_ > DEVICE_QUERY_TIMEOUT_MS) {
    if (current_query_cmd_ != CMD_DIP_SWITCH && query_retries_ < DEVICE_QUERY_MAX_RETRIES) {
      // Re-ask the same monitor the same question: clearing the address alone leaves the poll
      // cursor and current_query_cmd_ untouched, so the block below resends it.
      query_retries_++;
      ESP_LOGW(TAG, "Timeout querying monitor 0x%02X (cmd 0x%02X), attempt %u/%u; retrying...",
               current_query_addr_, current_query_cmd_, (unsigned)query_retries_ + 1,
               (unsigned)DEVICE_QUERY_MAX_RETRIES + 1);
      current_query_addr_ = 0;
    } else {
      ESP_LOGW(TAG, "Monitor 0x%02X did not answer cmd 0x%02X after %u attempts; skipping",
               current_query_addr_, current_query_cmd_,
               current_query_cmd_ == CMD_DIP_SWITCH ? 1U : (unsigned)DEVICE_QUERY_MAX_RETRIES + 1);
      // DIP status is optional and gets one attempt; identity queries are retried.
      // Advance (info -> barcode -> DIP status -> next monitor).
      this->advance_device_query_();
    }
  }

  if (current_query_addr_ != 0) {
    return;  // Still waiting on the outstanding query
  }

  if (current_poll_index_ < poll_addrs_.size()) {
    current_query_addr_ = poll_addrs_[current_poll_index_];
    if (current_query_cmd_ == CMD_SERVICE) profile_restore_failed_[current_query_addr_] = true;
    race_step_time_ = now;
    const Frame query = current_query_cmd_ == CMD_SERVICE ? make_restore_user_params(current_query_addr_) :
                        current_query_cmd_ == CMD_NAMES_USER ? make_user_note_read(current_query_addr_) :
                        current_query_cmd_ == CMD_WAKEUP ? make_iss_read(current_query_addr_, 0x06) :
                        make_query(current_query_addr_, current_query_cmd_);
    this->send_frame(query);
    return;
  }

  ESP_LOGI(TAG, "All discovered monitors queried. Entering device configuration phase.");
  race_state_ = RaceState::CONFIGURING_DEVICES;
  this->update_bus_status_();
  race_step_time_ = now;
  current_poll_index_ = 0;
  current_query_addr_ = 0;
  restore_profiles_on_discovery_ = false;
}

void GenSAMHub::race_step_configuring_(uint32_t now) {
  this->resolve_speaker_groups_();
  this->finish_temporary_wake_();
  if (pending_group_ >= 0 && this->start_group_apply_(now)) {
    return;
  }
  this->broadcast_volume_and_keepalive_();
  ESP_LOGI(TAG, "Using speaker settings; entering live telemetry polling loop.");
  race_state_ = RaceState::POLLING_MONITORS;
  this->update_bus_status_();
  last_poll_cycle_time_ = now - poll_interval_ms_;      // Start polling immediately
  last_poll_step_time_ = now - POLL_STEP_INTERVAL_MS;
  current_poll_index_ = 0;
  current_query_addr_ = 0;
  current_query_cmd_ = 0;
  query_retries_ = 0;
}

// --- Group preset application ------------------------------------------------------
//
// One frame per loop() call, paced by GROUP_FRAME_GAP_MS. See GroupApplyState in hub.h for
// why this cannot be a loop.

uint32_t GenSAMHub::peq_design_rate_for(const GenSAMMonitor &mon) {
  if (mon.binding != nullptr && mon.binding->peq_design_rate != 0) {
    return mon.binding->peq_design_rate;
  }
  return mon.is_subwoofer() ? PEQ_RATE_SUBWOOFER_HZ : PEQ_RATE_DEFAULT_HZ;
}

bool GenSAMHub::start_group_apply_(uint32_t now) {
  if (pending_group_ < 0) {
    return false;
  }
  const uint8_t wanted = static_cast<uint8_t>(pending_group_);
  pending_group_ = -1;

  const GroupPreset *group = this->get_group(wanted);
  if (group == nullptr) {
    ESP_LOGW(TAG, "Group preset %u is out of range; nothing applied", (unsigned) wanted);
    return false;
  }

  ESP_LOGI(TAG, "Applying group preset '%s' (%u devices%s)", group->name,
           (unsigned) group->device_count, calibration_bypass_ ? ", calibration bypassed" : "");

  apply_ = GroupApplyState{};
  apply_.active = true;
  apply_.group_idx = wanted;
  apply_.started_ms = now;
  // Let the first frame go immediately, whichever pacing gate it faces: the wake burst's is
  // the wider of the two, so backdating by that clears both.
  apply_.last_tx_ms = now - GROUP_WAKE_GAP_MS;
  for (uint8_t i = 0; i < group->device_count; i++) {
    auto &state = speaker_settings_[group->devices[i].unique_id];
    state = SpeakerSettingsState{};
  }

  // Duck for the duration. The block retunes filters and levels underneath a playing signal,
  // and the OEM ducks for the same reason. Skipped in standby, where there is nothing to
  // protect and the volume broadcast would re-establish amplifier gain.
  //
  // The wake burst is skipped on the same condition, and for the mirror-image reason: waking
  // the speakers because somebody picked a group would be a surprising side effect, and a
  // queued requests normally resume the system before reaching this phase.
  if (!current_standby_ && this->can_transmit()) {
    this->silence_system_volume_();
    apply_.ducked = true;
  } else {
    apply_.preamble_step = GROUP_WAKE_STEPS;
  }

  race_state_ = RaceState::APPLYING_GROUP;
  this->update_bus_status_();
  return true;
}

void GenSAMHub::finish_group_apply_() {
  // Close the push the way the OEM does: a keep-alive, then the volume restore last, so the
  // audible step is the final thing on the bus. Not broadcast_volume_and_keepalive_(), which
  // sends the volume first and would lift the duck before the keep-alive went out.
  this->send_frame(make_stay_online());
  if (apply_.ducked) {
    this->restore_system_volume_();
  }

  // A partial or rejected push cannot identify the entire system as this preset.
  const uint8_t attempted = apply_.group_idx;
  const auto *attempted_group = this->get_group(attempted);
  const bool reached_anyone = attempted_group != nullptr &&
                             apply_.configured == attempted_group->device_count && !apply_.rejected;
  const bool rejected = apply_.rejected;
  apply_ = GroupApplyState{};

  if (reached_anyone) {
    active_group_ = attempted;
    external_settings_observed_ = false;
  } else if (rejected) {
    ESP_LOGW(TAG, "Group preset '%s' was not marked active because a monitor rejected a command",
             this->get_group(attempted) != nullptr ? this->get_group(attempted)->name : "?");
  } else {
    const GroupPreset *wanted = this->get_group(attempted);
    ESP_LOGW(TAG, "Group preset '%s' was not fully applied; identity remains unknown",
             wanted != nullptr ? wanted->name : "?");
  }
  if (!reached_anyone) {
    active_group_ = -1;
    for (auto &entry : speaker_settings_) entry.second.group = -1;
  }
  note_update_requested_ = true;
  this->set_store_status_("Unsaved changes");

  const GroupPreset *group =
      (active_group_ >= 0) ? this->get_group(static_cast<uint8_t>(active_group_)) : nullptr;
  if (group != nullptr && group_select_ != nullptr && group_select_->has_option(group->name)) {
    group_select_->publish_state(group->name);
  }

  this->queue_runtime_settings_();
  race_state_ = RaceState::POLLING_MONITORS;
  this->update_bus_status_();
  last_poll_cycle_time_ = millis() - poll_interval_ms_;
  last_poll_step_time_ = millis() - POLL_STEP_INTERVAL_MS;
  current_poll_index_ = 0;
  current_query_addr_ = 0;
}

bool GenSAMHub::send_group_step_(const GenSAMMonitor &mon, const GroupDevice &dev, uint8_t step) {
  const uint8_t addr = mon.address;

  // A device switched off in this group plays nothing, so its DSP is left untouched and only
  // its mute is asserted. Reconfiguring a speaker that is about to be silent would spend bus
  // time to no effect, and would overwrite calibration another group still depends on. The
  // keep-alive is skipped along with the rest: one frame is not long enough to need it.
  if (!dev.enabled) {
    if (step == GROUP_STEP_KEEPALIVE) {
      return this->send_frame(make_bypass(addr, true, false, this->live_leds_enabled_(&mon)));
    }
    return false;
  }

  if (step == GROUP_STEP_KEEPALIVE) return this->send_frame(make_stay_online());
  if (step == GROUP_STEP_MUTE)
    return this->send_frame(make_bypass(addr, current_mute_, false, this->live_leds_enabled_(&mon)));
  const Frame frame = make_group_setting_frame(addr, dev, mon.is_subwoofer(), peq_design_rate_for(mon),
                                              step, this->is_calibration_bypassed_(mon));
  return frame.command != 0 && this->send_frame(frame);
}

void GenSAMHub::race_step_applying_group_(uint32_t now) {
  const GroupPreset *group = this->get_group(apply_.group_idx);
  if (group == nullptr) {
    this->finish_group_apply_();
    return;
  }

  if (now - apply_.started_ms > GROUP_APPLY_TIMEOUT_MS) {
    ESP_LOGW(TAG, "Group preset '%s' did not finish applying within %u ms; abandoning at "
                  "device %u step %u so the system does not stay silenced",
             group->name, (unsigned) GROUP_APPLY_TIMEOUT_MS, (unsigned) apply_.device_idx,
             (unsigned) apply_.step);
    this->finish_group_apply_();
    return;
  }

  // The wake burst comes first, at its own wider spacing, and every frame of it is broadcast --
  // so it runs to completion before any device is looked up, and is unaffected by which of them
  // are online.
  if (apply_.preamble_step < GROUP_WAKE_STEPS) {
    if (now - apply_.last_tx_ms < GROUP_WAKE_GAP_MS) {
      return;
    }
    this->send_frame(
        make_wakeup_step(WAKEUP_OP_POWER, GROUP_WAKE_SEQUENCE[apply_.preamble_step]));
    apply_.preamble_step++;
    apply_.last_tx_ms = now;
    return;
  }

  if (apply_.awaiting_reply) {
    if (now - race_step_time_ <= GROUP_REPLY_TIMEOUT_MS) {
      return;
    }
    apply_.awaiting_reply = false;
    current_query_addr_ = 0;
  }

  if (now - apply_.last_tx_ms < GROUP_FRAME_GAP_MS) {
    return;
  }

  while (apply_.device_idx < group->device_count) {
    const GroupDevice &dev = group->devices[apply_.device_idx];
    GenSAMMonitor *mon = registry_.find_by_serial_or_id(std::to_string(dev.unique_id));

    if (mon == nullptr || !mon->online) {
      // Partial application must not identify the whole system as this preset.
      ESP_LOGW(TAG, "Group '%s': monitor %lu is not online; it keeps its previous settings",
               group->name, (unsigned long) dev.unique_id);
      apply_.device_idx++;
      apply_.step = 0;
      continue;
    }

    if (apply_.step < GROUP_STEP_DONE && this->send_group_step_(*mon, dev, apply_.step)) {
      const bool expects_reply = apply_.step != GROUP_STEP_KEEPALIVE || !dev.enabled;
      apply_.step++;
      apply_.last_tx_ms = now;
      if (expects_reply) {
        current_query_addr_ = mon->address;
        apply_.awaiting_reply = true;
        race_step_time_ = now;
      }
      return;
    }

    // Mirror the live values for the per-monitor entities, not as a rediscovery setpoint.
    // A bypassed monitor mirrors the defaults it was actually sent, not the calibration it was spared.
    if (mon->binding != nullptr && dev.enabled) {
      const bool bypassed = this->is_calibration_bypassed_(*mon);
      registry_.set_binding_crossover(*mon->binding, dev.crossover_hz);
      registry_.set_binding_input(*mon->binding, dev.source, dev.aes3_channel);
      registry_.set_binding_level(*mon->binding, bypassed ? 0.0f : dev.level_db);
      registry_.set_binding_delay(*mon->binding, bypassed ? 0 : dev.delay_samples);
    }
    apply_.configured++;
    auto &state = speaker_settings_[mon->unique_id];
    state.group = apply_.group_idx;
    state.modified = this->is_calibration_bypassed_(*mon);
    state.applied = true;
    state.dirty = true;
    mon->mute = dev.enabled ? current_mute_ : true;
    apply_.device_idx++;
    apply_.step = 0;
  }

  ESP_LOGI(TAG, "Group preset '%s' applied to %u of %u monitors", group->name,
           (unsigned) apply_.configured, (unsigned) group->device_count);
  this->finish_group_apply_();
}

bool GenSAMHub::handle_poll_continuation_(const Frame &frame, uint32_t now) {
  if (race_state_ != RaceState::POLLING_MONITORS || current_query_addr_ != 0 ||
      telemetry_continuation_addr_ == 0 ||
      now - telemetry_continuation_ms_ > TELEMETRY_CONTINUATION_WINDOW_MS ||
      frame.address != HOST_ADDRESS || frame.command != CMD_REPORT_STATUS ||
      frame.payload.empty() || frame.payload[0] == 0x41) {
    return false;
  }

  GenSAMMonitor *mon = registry_.find(telemetry_continuation_addr_);
  telemetry_continuation_addr_ = 0;
  if (mon == nullptr) {
    return true;
  }
  parse_telemetry(frame.payload.data(), frame.payload.size(), *mon);
  this->mark_monitor_seen_(*mon);
  registry_.bind_if_matched(*mon);
  registry_.publish_telemetry(*mon);
  this->evaluate_system_standby_();
  ESP_LOGD(TAG, "[0x%02X %s] Telemetry continuation received",
           mon->address, mon->model.c_str());
  return true;
}

bool GenSAMHub::handle_command_rejection_(const Frame &frame, uint32_t now) {
  if (frame.address != HOST_ADDRESS ||
      (frame.command != CMD_COMMAND_REJECTED && frame.command != CMD_UNSUPPORTED)) {
    return false;
  }

  const char *reason = frame.command == CMD_COMMAND_REJECTED ? "rejected" : "unsupported/invalid";
  switch (race_state_) {
    case RaceState::QUERYING_DEVICES:
      if (current_query_addr_ == 0) return false;
      ESP_LOGW(TAG, "Monitor 0x%02X returned %s for query 0x%02X; skipping it",
               current_query_addr_, reason, current_query_cmd_);
      this->advance_device_query_();
      race_step_time_ = now;
      return true;
    case RaceState::CONFIGURING_DEVICES:
      if (current_query_addr_ == 0) return false;
      ESP_LOGW(TAG, "Monitor 0x%02X returned %s during configuration (reply 0x%02X)",
               current_query_addr_, reason, frame.command);
      current_poll_index_++;
      current_query_addr_ = 0;
      race_step_time_ = now;
      return true;
    case RaceState::POLLING_MONITORS:
      if (current_query_addr_ == 0) return false;
      ESP_LOGW(TAG, "Monitor 0x%02X returned %s for poll/read (reply 0x%02X)",
               current_query_addr_, reason, frame.command);
      current_query_addr_ = 0;
      iss_query_op_ = 0;
      last_poll_step_time_ = now;
      return true;
    case RaceState::APPLYING_GROUP:
      if (!apply_.active || !apply_.awaiting_reply || current_query_addr_ == 0) return false;
      ESP_LOGW(TAG, "Monitor 0x%02X returned %s during group application (reply 0x%02X)",
               current_query_addr_, reason, frame.command);
      apply_.rejected = true;
      apply_.awaiting_reply = false;
      current_query_addr_ = 0;
      apply_.device_idx++;
      apply_.step = 0;
      apply_.last_tx_ms = now;
      return true;
    case RaceState::SLEEPING:
      if (current_query_addr_ == 0) return false;
      ESP_LOGW(TAG, "Monitor 0x%02X returned %s to the sleep probe (reply 0x%02X)",
               current_query_addr_, reason, frame.command);
      current_query_addr_ = 0;
      sleep_probe_phase_ = SLEEP_PROBE_IDLE;
      last_sleep_tx_ms_ = now;
      return true;
    default:
      return false;
  }
}

void GenSAMHub::race_step_polling_(uint32_t now) {
  // A group switch requested while polling starts here, between telemetry cycles, so a push
  // never interleaves with an outstanding poll.
  if (pending_group_ >= 0 && current_query_addr_ == 0 && this->start_group_apply_(now)) {
    return;
  }

  // Fall back to IDLE once nothing answers any more, so discovery gets retried. Testing
  // registry_.empty() here would never fire: stale monitors are marked offline but kept.
  if (!registry_.any_online()) {
    race_state_ = RaceState::IDLE;
    this->update_bus_status_();
    return;
  }

  if (current_query_addr_ != 0 && now - race_step_time_ > POLL_REPLY_TIMEOUT_MS) {
    current_query_addr_ = 0;
    iss_query_op_ = 0;
    last_poll_step_time_ = now;
  }

  if (current_query_addr_ != 0 || (now - last_poll_step_time_ < POLL_STEP_INTERVAL_MS) ||
      (now - last_poll_cycle_time_ < poll_interval_ms_)) {
    return;
  }

  if (this->sleep_due_(now)) {
    this->enter_sleep_(now);
    return;
  }

  if (this->send_iss_read_(now)) {
    return;
  }

  // At the start of each polling cycle, refresh address leases and (when awake) monitor gain
  if (current_poll_index_ == 0) {
    this->broadcast_volume_and_keepalive_();
    delayMicroseconds(TURNAROUND_US);
  }

  // Refresh address cache only if monitor registry size changed
  if (poll_addrs_.size() != registry_.size()) {
    poll_addrs_ = registry_.addresses();
  }

  if (current_poll_index_ < poll_addrs_.size()) {
    telemetry_continuation_addr_ = 0;
    current_query_addr_ = poll_addrs_[current_poll_index_++];
    iss_query_op_ = 0;
    race_step_time_ = now;
    last_poll_step_time_ = now;
    this->send_frame(make_query(current_query_addr_, CMD_QUERY_STATUS));
  } else {
    current_poll_index_ = 0;
    last_poll_cycle_time_ = now;
    last_poll_step_time_ = now;
  }
}

void GenSAMHub::race_step_idle_(uint32_t now) {
  // Periodically retry discovery while nothing on the bus is responding.
  //
  // Not while the system is meant to be off. Discovery now has to wake monitors to enumerate
  // them, so retrying here would power amplifiers up and back down every DISCOVERY_RETRY_INTERVAL_MS
  // for as long as the system stays switched off. One attempt is made at boot (via the
  // initial_discovery_done_ path); if that finds nothing, the monitors are left alone until the
  // user switches the system on, which runs a full wake and discovery of its own.
  if (current_standby_) {
    return;
  }

  if (!registry_.any_online() &&
      now - last_discovery_retry_time_ >= DISCOVERY_RETRY_INTERVAL_MS) {
    last_discovery_retry_time_ = now;
    this->discover_monitors_();
  }
}

// --- Hub-managed ISS sleep ----------------------------------------------------------

bool GenSAMHub::auto_sleep_delay_ms_(uint32_t &delay_ms) const {
  uint32_t minutes = 0;
  if (iss_sleep_delay_ != ISS_SLEEP_DELAY_DEFAULT) {
    if (iss_sleep_delay_ == ISS_SLEEP_DELAY_NEVER) {
      return false;
    }
    minutes = iss_sleep_delay_;
  } else {
    bool any = false;
    for (const auto &kv : registry_.monitors()) {
      const GenSAMMonitor &mon = kv.second;
      if (!mon.online) {
        continue;
      }
      if (!mon.iss_stored_delay_known) {
        return false;
      }
      if (mon.iss_stored_delay_min == 0 ||
          mon.iss_stored_delay_min >= ISS_STORED_DELAY_NEVER_MIN) {
        return false;
      }
      minutes = std::max(minutes, mon.iss_stored_delay_min);
      any = true;
    }
    if (!any) {
      return false;
    }
  }
  delay_ms = std::min(minutes, ISS_SLEEP_DELAY_MAX_MIN) * 60000U;
  return true;
}

bool GenSAMHub::iss_threshold_for_(const GenSAMMonitor &mon, int32_t &dbu) const {
  if (iss_threshold_ != ISS_THRESHOLD_DEFAULT) {
    dbu = iss_threshold_;
    return true;
  }
  if (!mon.iss_stored_threshold_known) {
    return false;
  }
  dbu = mon.iss_stored_threshold_dbu;
  return true;
}

bool GenSAMHub::send_iss_read_(uint32_t now) {
  for (auto &kv : registry_.monitors()) {
    GenSAMMonitor &mon = kv.second;
    if (!mon.online || (mon.iss_read_sent && now - mon.iss_last_read_ms < ISS_SAMPLE_INTERVAL_MS)) {
      continue;
    }
    uint8_t op = WAKEUP_OP_ISS_LEVEL;
    if (iss_sleep_delay_ == ISS_SLEEP_DELAY_DEFAULT && !mon.iss_stored_delay_known) {
      op = WAKEUP_OP_ISS_SLEEP_DELAY;
    } else if (iss_threshold_ == ISS_THRESHOLD_DEFAULT && !mon.iss_stored_threshold_known) {
      op = WAKEUP_OP_ISS_THRESHOLD;
    }
    mon.iss_read_sent = true;
    mon.iss_last_read_ms = now;
    current_query_addr_ = mon.address;
    iss_query_op_ = op;
    race_step_time_ = now;
    last_poll_step_time_ = now;
    this->send_frame(make_iss_read(mon.address, op));
    return true;
  }
  return false;
}

bool GenSAMHub::handle_iss_reply_(const Frame &frame, uint32_t now) {
  int32_t value = 0;
  if (current_query_addr_ == 0 || frame.address != HOST_ADDRESS ||
      frame.command != CMD_REPORT_STATUS || !parse_iss_word(frame.payload, value)) {
    return false;
  }
  GenSAMMonitor *mon = registry_.find(current_query_addr_);
  const uint8_t op = iss_query_op_;
  current_query_addr_ = 0;
  iss_query_op_ = 0;
  last_poll_step_time_ = now;
  if (mon == nullptr) {
    return true;
  }
  this->mark_monitor_seen_(*mon);
  switch (op) {
    case WAKEUP_OP_ISS_SLEEP_DELAY:
      mon->iss_stored_delay_min = static_cast<uint32_t>(value);
      mon->iss_stored_delay_known = true;
      ESP_LOGI(TAG, "[0x%02X %s] ISS sleep delay: %u min", mon->address, mon->model.c_str(),
               (unsigned) mon->iss_stored_delay_min);
      break;
    case WAKEUP_OP_ISS_THRESHOLD:
      mon->iss_stored_threshold_dbu = value;
      mon->iss_stored_threshold_known = true;
      ESP_LOGI(TAG, "[0x%02X %s] ISS threshold: %d dBu", mon->address, mon->model.c_str(),
               (int) value);
      break;
    case WAKEUP_OP_ISS_LEVEL: {
      mon->iss_level_dbu = value;
      mon->iss_level_known = true;
      mon->iss_level_ms = now;
      int32_t threshold = 0;
      const bool signal = this->iss_threshold_for_(*mon, threshold) && value >= threshold;
      if (signal) {
        last_signal_ms_ = now;
      }
      if (value == ISS_LEVEL_SILENCE) {
        ESP_LOGD(TAG, "[0x%02X %s] ISS level: no signal", mon->address, mon->model.c_str());
      } else {
        ESP_LOGD(TAG, "[0x%02X %s] ISS level: %d dBu%s", mon->address, mon->model.c_str(),
                 (int) value, signal ? " (signal)" : "");
      }
      break;
    }
    default:
      break;
  }
  return true;
}

bool GenSAMHub::sleep_due_(uint32_t now) const {
  if (current_standby_ || restore_standby_after_discovery_ || power_off_pending_ ||
      pending_group_ >= 0 || apply_.active || !user_commands_.empty() || volume_tx_pending_ ||
      mute_tx_pending_ || system_led_tx_pending_ || iss_tx_pending_ || filter_bypass_tx_pending_) {
    return false;
  }
  uint32_t delay_ms = 0;
  if (!this->auto_sleep_delay_ms_(delay_ms)) {
    return false;
  }
  if (now - last_signal_ms_ < delay_ms || now - last_user_command_ms_ < delay_ms) {
    return false;
  }
  // Every online monitor must have recently reported a level below its threshold; a monitor
  // that is not answering the level read could be the one that is playing.
  bool any = false;
  for (const auto &kv : registry_.monitors()) {
    const GenSAMMonitor &mon = kv.second;
    if (!mon.online) {
      continue;
    }
    int32_t threshold = 0;
    if (mon.identify_end_ms != 0 || !mon.iss_level_known ||
        now - mon.iss_level_ms > ISS_SAMPLE_MAX_AGE_MS ||
        !this->iss_threshold_for_(mon, threshold) || mon.iss_level_dbu >= threshold) {
      return false;
    }
    any = true;
  }
  return any;
}

void GenSAMHub::enter_sleep_(uint32_t now) {
  uint32_t delay_ms = 0;
  this->auto_sleep_delay_ms_(delay_ms);
  ESP_LOGI(TAG, "No input signal for %u min; putting monitors to sleep",
           (unsigned) (delay_ms / 60000U));
  // Once only: a second short press would toggle the monitors straight back awake.
  this->send_frame(make_wakeup_step(WAKEUP_OP_POWER, WAKEUP_VAL_STANDBY_2));
  race_state_ = RaceState::SLEEPING;
  sleep_entered_ms_ = now;
  last_sleep_probe_ms_ = now;
  last_sleep_tx_ms_ = now;
  sleep_probe_phase_ = SLEEP_PROBE_IDLE;
  current_query_addr_ = 0;
  iss_query_op_ = 0;
  current_poll_index_ = 0;
  current_standby_ = true;
  registry_.publish_stale_telemetry();
  this->notify_state_callbacks_();
  this->update_bus_status_();
}

void GenSAMHub::race_step_sleeping_(uint32_t now) {
  if (sleep_probe_phase_ != SLEEP_PROBE_IDLE) {
    if (now - race_step_time_ > SLEEP_PROBE_REPLY_TIMEOUT_MS) {
      // A lost frame is not evidence either way; the next probe asks again.
      ESP_LOGD(TAG, "Sleep probe: no reply from monitor 0x%02X", current_query_addr_);
      sleep_probe_phase_ = SLEEP_PROBE_IDLE;
      current_query_addr_ = 0;
      sleep_probe_index_++;
    }
    return;
  }
  if (now - sleep_entered_ms_ < SLEEP_FIRST_PROBE_MS ||
      now - last_sleep_probe_ms_ < SLEEP_PROBE_INTERVAL_MS) {
    return;
  }
  std::vector<const GenSAMMonitor *> targets;
  for (const auto &kv : registry_.monitors()) {
    if (kv.second.unique_id != 0) {
      targets.push_back(&kv.second);
    }
  }
  if (targets.empty()) {
    return;
  }
  const GenSAMMonitor &mon = *targets[sleep_probe_index_ % targets.size()];
  const std::vector<uint8_t> uid = {static_cast<uint8_t>(mon.unique_id >> 16),
                                    static_cast<uint8_t>(mon.unique_id >> 8),
                                    static_cast<uint8_t>(mon.unique_id)};
  last_sleep_probe_ms_ = now;
  last_sleep_tx_ms_ = now;
  race_step_time_ = now;
  current_query_addr_ = mon.address;
  sleep_probe_phase_ = SLEEP_PROBE_ASSIGNING;
  this->send_frame(make_set_rid(uid, mon.address));
}

void GenSAMHub::wake_from_sleep_(uint8_t address) {
  ESP_LOGI(TAG, "Monitor 0x%02X woke from ISS sleep (input signal); waking all and rediscovering",
           address);
  // Rediscovered through the user-wake path, which waits for the probe's session to lapse.
  // current_standby_ is cleared first: discovery would otherwise shut the system back down.
  current_standby_ = false;
  recovery_required_ = true;
  resuming_ = true;
  current_query_addr_ = 0;
  this->notify_state_callbacks_();
  this->update_bus_status_();
}

}  // namespace gensam
}  // namespace esphome
