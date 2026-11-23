/// @file speaker_store.cpp
/// See hub.h and speaker_settings.h for transaction semantics, identity format and trust limits.

#include "hub.h"
#include "esphome/core/log.h"

namespace esphome {
namespace gensam {
namespace {
constexpr uint8_t SETTINGS_RETRIES = 2;
const char *const TAG = "gensam";
}

void GenSAMHub::set_store_status_(const std::string &value) {
  store_status_ = value;
  for (auto &callback : store_status_callbacks_) callback(value);
}

void GenSAMHub::store_speaker_settings() {
  if (!can_transmit() || arbiter_.is_active()) {
    ESP_LOGW(TAG, "Cannot store speaker settings: bus unavailable");
    this->set_store_status_("Blocked: bus unavailable");
    return;
  }
  if (store_requested_ || speaker_transaction_.store) {
    ESP_LOGW(TAG, "Speaker store already requested");
    return;
  }
  if (this->defer_user_command_([this]() { this->store_speaker_settings(); })) return;
  store_requested_ = true;
  this->set_store_status_("Waiting for pending settings");
}

void GenSAMHub::record_setting_frame_(const Frame &frame, bool external) {
  if (frame.address == HOST_ADDRESS) return;
  uint32_t key = 0;
  const bool setting = setting_frame_key(frame, key);
  const bool metadata = frame.command == CMD_NAMES_USER && !frame.payload.empty() &&
                        frame.payload[0] < 0x80;
  const bool profile = frame.command == CMD_SERVICE && !frame.payload.empty() &&
                       (frame.payload[0] == SERVICE_STORE_USER_PARS ||
                        frame.payload[0] == SERVICE_RESTORE_USER_PARS);
  if (!setting && !(external && (metadata || profile))) return;
  if (external) {
    external_settings_observed_ = true;
    active_group_ = selected_group_ = -1;
    for (auto &entry : speaker_settings_) entry.second.group = -1;
  }
  for (const auto &entry : registry_.monitors()) {
    if (frame.address != BROADCAST_ADDRESS && frame.address != entry.first) continue;
    auto &state = speaker_settings_[entry.second.unique_id];
    if (setting) state.frames[key] = frame;
    if (external || (!apply_.active && !speaker_transaction_.active)) {
      state.modified = true;
      state.dirty = true;
      if (!external) {
        note_update_requested_ = true;
        this->set_store_status_("Unsaved changes");
      }
    }
  }
  if (external || (!apply_.active && !speaker_transaction_.active)) this->publish_active_settings_();
}

void GenSAMHub::resolve_speaker_groups_() {
  bool leds_known = !registry_.empty(), leds_enabled = true;
  for (const auto &entry : registry_.monitors()) {
    leds_known &= entry.second.system_leds_known;
    leds_enabled &= entry.second.system_leds_enabled;
  }
  if (leds_known && !system_led_tx_pending_) {
    current_system_leds_enabled_ = leds_enabled;
    this->commit_system_led_state_(leds_enabled);
  }
  for (const auto &entry : registry_.monitors()) {
    const auto &mon = entry.second;
    auto &state = speaker_settings_[mon.unique_id];
    state.group = -1;
    if (profile_restore_failed_.count(mon.address) != 0) {
      ESP_LOGW(TAG, "Monitor 0x%02X profile restore was not acknowledged; identity is untrusted", mon.address);
      state.note_known = false;
      continue;
    }
    SpeakerNote note;
    if (external_settings_observed_ || !state.note_known ||
        !parse_speaker_note(state.note, note) || note.kind == 'X') continue;
    int matched = -1;
    for (uint8_t i = 0; i < group_count_; i++) {
      const GroupPreset *group = this->get_group(i);
      const GroupDevice *dev = group != nullptr ? find_group_device(*group, mon.unique_id) : nullptr;
      if (dev == nullptr || group_setting_fingerprint(*group, *dev, mon.is_subwoofer(),
                                                     peq_design_rate_for(mon)) != note.group) continue;
      if (matched >= 0) {
        ESP_LOGW(TAG, "Ambiguous preset fingerprint for monitor 0x%02X", mon.address);
        matched = -1;
        break;
      }
      matched = i;
    }
    state.group = matched;
    state.modified = note.kind == 'M';
    state.dirty = !note.stored;
    // A modified stored preset cannot seed unknown edits from its unmodified catalog values.
    if (matched >= 0 && !state.modified) {
      const auto *group = this->get_group(static_cast<uint8_t>(matched));
      const auto *dev = find_group_device(*group, mon.unique_id);
      for (uint8_t step = GROUP_STEP_PREPARE; step < GROUP_STEP_DONE; step++) {
        const Frame frame = make_group_setting_frame(mon.address, *dev, mon.is_subwoofer(),
                                                    peq_design_rate_for(mon), step, false);
        uint32_t key;
        if (setting_frame_key(frame, key)) state.frames[key] = frame;
      }
      if (mon.binding != nullptr && dev->enabled) {
        registry_.set_binding_input(*mon.binding, dev->source, dev->aes3_channel);
        registry_.set_binding_crossover(*mon.binding, dev->crossover_hz);
        registry_.set_binding_level(*mon.binding, dev->level_db);
        registry_.set_binding_delay(*mon.binding, dev->delay_samples);
      }
    }
  }
  this->publish_active_settings_();
}

void GenSAMHub::fail_speaker_settings_(const std::string &reason) {
  const bool store = speaker_transaction_.store;
  const size_t committed = speaker_transaction_.committed;
  const size_t total = speaker_transaction_.addresses.size();
  ESP_LOGW(TAG, "Speaker settings operation failed: %s (%u/%u acknowledged commits)", reason.c_str(),
           static_cast<unsigned>(committed), static_cast<unsigned>(total));
  speaker_transaction_ = SpeakerSettingsTransaction{};
  store_requested_ = false;
  current_query_addr_ = 0;
  if (store) this->set_store_status_("Failed: " + reason + " (" + std::to_string(committed) +
                                   "/" + std::to_string(total) + " acknowledged)");
}

bool GenSAMHub::handle_speaker_settings_reply_(const Frame &frame, uint32_t now) {
  auto &tx = speaker_transaction_;
  if (!tx.active || !tx.awaiting || frame.address != HOST_ADDRESS) return false;
  if (frame.command == CMD_COMMAND_REJECTED || frame.command == CMD_UNSUPPORTED) {
    this->fail_speaker_settings_("speaker rejected phase " + std::to_string(tx.phase));
    return true;
  }
  if (frame.command != CMD_REPORT_STATUS && frame.command != CMD_ACK) return false;
  if ((!frame.payload.empty() && frame.payload[0] == 0x41) ||
      (frame.payload.size() == 1 && frame.payload[0] < 0x20)) return true;
  if (auto *mon = registry_.find(tx.addresses[tx.index])) this->mark_monitor_seen_(*mon);
  if (tx.phase == 2 || tx.phase == 4) {
    const std::string reply(frame.payload.begin(), frame.payload.end());
    if (reply != tx.note) {
      this->fail_speaker_settings_("note readback mismatch");
      return true;
    }
    auto *mon = registry_.find(tx.addresses[tx.index]);
    if (mon != nullptr) {
      auto &state = speaker_settings_[mon->unique_id];
      state.note = reply;
      state.note_known = true;
    }
  } else if (!frame.payload.empty()) {
    return false;
  }
  if (tx.phase == 3) {
    tx.committed++;
    if (const auto *mon = registry_.find(tx.addresses[tx.index]))
      speaker_settings_[mon->unique_id].dirty = false;
  }
  tx.awaiting = false;
  tx.retries = 0;
  tx.phase++;
  tx.sent_ms = now;
  current_query_addr_ = 0;
  if ((!tx.store && tx.phase == 3) || tx.phase == 5) {
    tx.index++;
    tx.phase = 0;
    tx.note.clear();
  }
  return true;
}

void GenSAMHub::service_speaker_settings_(uint32_t now) {
  auto &tx = speaker_transaction_;
  if (arbiter_.is_active()) {
    if (tx.active || store_requested_) {
      tx.store |= store_requested_;
      this->fail_speaker_settings_("external controller active");
    }
    note_update_requested_ = false;
    return;
  }
  if (!can_transmit() || resuming_) return;
  if (power_off_pending_) {
    if (tx.active && tx.awaiting && now - tx.sent_ms < SPEAKER_SETTINGS_REPLY_TIMEOUT_MS) return;
    if (tx.active || store_requested_) {
      tx.store |= store_requested_;
      this->fail_speaker_settings_("power off requested");
    }
    note_update_requested_ = false;
    return;
  }
  if (!tx.active) {
    if ((!store_requested_ && !note_update_requested_) || !initial_discovery_done_ ||
        race_state_ != RaceState::POLLING_MONITORS || current_query_addr_ != 0 ||
        pending_group_ >= 0 || !user_commands_.empty() ||
        volume_tx_pending_ || mute_tx_pending_ || system_led_tx_pending_ ||
        iss_tx_pending_ || filter_bypass_tx_pending_ || now - last_poll_step_time_ < 20 ||
        now - arbiter_.last_transmit() < 20) return;
    tx = SpeakerSettingsTransaction{};
    tx.store = store_requested_;
    store_requested_ = false;
    note_update_requested_ = false;
    for (const auto &entry : registry_.monitors()) {
      const auto &mon = entry.second;
      if (tx.store && (!mon.online || mon.mute || this->is_calibration_bypassed_(mon) ||
                       phase_correction_bypass_)) {
        this->fail_speaker_settings_("all speakers must be online, unmuted and unbypassed");
        return;
      }
      if (mon.online) tx.addresses.push_back(entry.first);
    }
    const auto missing_member = [this](const GroupPreset *group) {
      for (uint8_t i = 0; group != nullptr && i < group->device_count; i++) {
        const auto *mon = registry_.find_by_serial_or_id(std::to_string(group->devices[i].unique_id));
        if (mon == nullptr || !mon->online) return true;
      }
      return false;
    };
    bool missing = tx.store && missing_member(selected_group_ >= 0 ?
        this->get_group(static_cast<uint8_t>(selected_group_)) : nullptr);
    for (uint8_t address : tx.addresses) {
      if (!tx.store || missing) break;
      const auto *target = registry_.find(address);
      const int index = speaker_settings_[target->unique_id].group;
      const auto *group = index >= 0 ? this->get_group(static_cast<uint8_t>(index)) : nullptr;
      missing |= missing_member(group);
    }
    if (missing) {
      this->fail_speaker_settings_("a preset member is missing");
      return;
    }
    if (tx.addresses.empty()) {
      this->fail_speaker_settings_("no online speakers");
      return;
    }
    tx.active = true;
    tx.sent_ms = now;
    tx.keepalive_ms = now;
    if (tx.store) this->set_store_status_("Storing 0/" + std::to_string(tx.addresses.size()));
  }
  if (tx.index >= tx.addresses.size()) {
    const bool stored = tx.store;
    const size_t count = tx.committed;
    tx = SpeakerSettingsTransaction{};
    if (stored) {
      external_settings_observed_ = false;
      this->set_store_status_("Stored " + std::to_string(count) + " speakers");
      ESP_LOGI(TAG, "Stored current settings to %u speakers", static_cast<unsigned>(count));
    }
    this->publish_active_settings_();
    last_poll_step_time_ = now;
    last_timeout_check_ = now;
    return;
  }
  if (tx.awaiting) {
    if (now - tx.sent_ms < SPEAKER_SETTINGS_REPLY_TIMEOUT_MS) return;
    if (tx.phase == 3 || tx.retries++ >= SETTINGS_RETRIES) {
      this->fail_speaker_settings_("reply timeout at phase " + std::to_string(tx.phase));
      return;
    }
    ESP_LOGW(TAG, "Retrying speaker settings phase %u", tx.phase);
    tx.awaiting = false;
    current_query_addr_ = 0;
  }
  const uint8_t address = tx.addresses[tx.index];
  const auto *mon = registry_.find(address);
  if (mon == nullptr || !mon->online) {
    this->fail_speaker_settings_("target went offline");
    return;
  }
  if (now - tx.keepalive_ms >= 500) {
    if (!this->send_frame(make_stay_online())) {
      this->fail_speaker_settings_("keepalive blocked");
      return;
    }
    tx.keepalive_ms = now;
    return;
  }
  Frame frame;
  if (tx.phase == 0) {
    if (tx.store && (mon->system_leds_known || system_led_state_known_)) {
      frame = make_iss_led_state(address, this->live_leds_enabled_(mon));
    } else {
      tx.phase = 1;
    }
  }
  if (tx.phase == 1) {
    auto &state = speaker_settings_[mon->unique_id];
    SpeakerNote note;
    note.stored = tx.store;
    const auto *group = state.group >= 0 ? this->get_group(static_cast<uint8_t>(state.group)) : nullptr;
    const auto *dev = group != nullptr ? find_group_device(*group, mon->unique_id) : nullptr;
    if (dev != nullptr) {
      note.kind = state.modified ? 'M' : 'G';
      note.group = group_setting_fingerprint(*group, *dev, mon->is_subwoofer(), peq_design_rate_for(*mon));
    }
    note.settings = observed_settings_fingerprint(state.frames);
    tx.note = encode_speaker_note(note);
    frame = make_user_note_write(address, tx.note);
  } else if (tx.phase == 2 || tx.phase == 4) {
    frame = make_user_note_read(address);
  } else if (tx.phase == 3) {
    frame = make_store_user_params(address);
    this->set_store_status_("Storing " + std::to_string(tx.committed) + "/" +
                            std::to_string(tx.addresses.size()));
  }
  if (!this->send_frame(frame)) {
    this->fail_speaker_settings_("transmission blocked");
    return;
  }
  tx.sent_ms = now;
  tx.awaiting = true;
  current_query_addr_ = address;
}

}  // namespace gensam
}  // namespace esphome
