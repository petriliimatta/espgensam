/// @file speaker_settings.cpp
/// See speaker_settings.h for identity format, trust limits and public API documentation.

#include "speaker_settings.h"
#include <cstdio>

namespace esphome {
namespace gensam {
namespace {

/// Incremental FNV-1a over explicitly ordered wire bytes.
void hash_byte(uint32_t &hash, uint8_t byte) {
  hash = (hash ^ byte) * 16777619U;
}

void hash_frame(uint32_t &hash, const Frame &frame) {
  hash_byte(hash, frame.command);
  hash_byte(hash, static_cast<uint8_t>(frame.payload.size()));
  for (uint8_t byte : frame.payload) hash_byte(hash, byte);
}

bool hex_word(const std::string &value, size_t offset, uint32_t &out) {
  out = 0;
  for (size_t i = offset; i < offset + 8; i++) {
    const char ch = value[i];
    const int digit = ch >= '0' && ch <= '9' ? ch - '0' :
                      ch >= 'a' && ch <= 'f' ? ch - 'a' + 10 : -1;
    if (digit < 0) return false;
    out = (out << 4) | static_cast<uint32_t>(digit);
  }
  return true;
}

}  // namespace

std::string encode_speaker_note(const SpeakerNote &note) {
  char buffer[22];
  snprintf(buffer, sizeof(buffer), "%c%c1:%08lx:%08lx", note.kind, note.stored ? 'S' : 'V',
           static_cast<unsigned long>(note.group), static_cast<unsigned long>(note.settings));
  return buffer;
}

bool parse_speaker_note(const std::string &value, SpeakerNote &note) {
  if (value.size() != 21 || (value[0] != 'G' && value[0] != 'M' && value[0] != 'X') ||
      (value[1] != 'V' && value[1] != 'S') || value[2] != '1' || value[3] != ':' ||
      value[12] != ':') return false;
  SpeakerNote parsed;
  parsed.kind = value[0];
  parsed.stored = value[1] == 'S';
  if (!hex_word(value, 4, parsed.group) || !hex_word(value, 13, parsed.settings)) return false;
  note = parsed;
  return true;
}

Frame make_group_setting_frame(uint8_t addr, const GroupDevice &dev, bool subwoofer,
                               uint32_t design_rate, uint8_t step, bool bypass) {
  if (!dev.enabled) return {};
  switch (step) {
    case GROUP_STEP_PREPARE: return make_use_stored_settings(addr);
    case GROUP_STEP_DELAY: return make_delay(addr, bypass ? 0 : dev.delay_samples);
    case GROUP_STEP_LEVEL: return make_level(addr, bypass ? 0.0f : dev.level_db);
    case GROUP_STEP_CROSSOVER: return make_crossover(addr, dev.crossover_hz);
    case GROUP_STEP_SOURCE: return make_audio_source(addr, 0, dev.source, dev.aes3_channel);
    case GROUP_STEP_SOURCE_AUX:
      return subwoofer ? make_audio_source(addr, 1, dev.source, dev.lfe_channel) : Frame{};
    case GROUP_STEP_LFE_LEVEL:
      return subwoofer && dev.lfe_channel != 0 ? make_lfe_level(addr, dev.lfe_level_db) : Frame{};
    default: break;
  }
  if (step < GROUP_STEP_PEQ_FIRST || step >= GROUP_STEP_PEQ_FIRST + PEQ_BAND_COUNT) return {};
  const uint8_t band = step - GROUP_STEP_PEQ_FIRST;
  const PeqBand spec = !bypass && band < dev.band_count ? dev.bands[band] : PeqBand{};
  return make_peq_band(addr, band, design_biquad(spec.type, spec.frequency_hz, spec.gain_db, spec.q,
      dev.peq_design_rate_hz != 0 ? dev.peq_design_rate_hz : design_rate));
}

uint32_t group_setting_fingerprint(const GroupPreset &group, const GroupDevice &dev,
                                   bool subwoofer, uint32_t design_rate) {
  uint32_t hash = 2166136261U;
  if (group.name != nullptr)
    for (const char *ch = group.name; *ch != '\0'; ch++) hash_byte(hash, static_cast<uint8_t>(*ch));
  hash_byte(hash, 0);
  for (int shift = 24; shift >= 0; shift -= 8) hash_byte(hash, dev.unique_id >> shift);
  hash_byte(hash, dev.enabled ? 1 : 0);
  for (uint8_t step = GROUP_STEP_PREPARE; step < GROUP_STEP_DONE; step++) {
    const Frame frame = make_group_setting_frame(0, dev, subwoofer, design_rate, step, false);
    if (frame.command != 0) hash_frame(hash, frame);
  }
  return hash;
}

bool setting_frame_key(const Frame &frame, uint32_t &key) {
  if (frame.payload.empty()) return false;
  uint32_t selector = 0;
  switch (frame.command) {
    case CMD_DSP:
      if (frame.payload[0] >= 0x80) return false;
      selector = static_cast<uint32_t>(frame.payload[0]) << 8;
      if (frame.payload[0] == DSP_SUB_LEVEL || frame.payload[0] == DSP_SUB_PEQ) {
        if (frame.payload.size() < 2) return false;
        selector |= frame.payload[1];
      }
      break;
    case CMD_SELECT_AUDIO_SOURCE:
      if (frame.payload[0] >= 0x80) return false;
      selector = frame.payload[0];
      break;
    case CMD_WAKEUP:
      if (frame.payload[0] != WAKEUP_OP_ISS_SLEEP_DELAY &&
          frame.payload[0] != WAKEUP_OP_ISS_THRESHOLD && frame.payload[0] != 0x06) return false;
      selector = frame.payload[0];
      break;
    case CMD_NET_SETTINGS:
      if (frame.payload[0] != NET_SETTINGS_USE_STORED) return false;
      break;
    case CMD_BASS_MANAGE_XO:
    case CMD_SUB_LFE_LEVEL: break;
    case CMD_BYPASS_FILTERS:
      if (frame.payload[0] >= 0x80) return false;
      break;
    default: return false;
  }
  key = (static_cast<uint32_t>(frame.command) << 16) | selector;
  return true;
}

uint32_t observed_settings_fingerprint(const std::map<uint32_t, Frame> &frames) {
  if (frames.empty()) return 0;
  uint32_t hash = 2166136261U;
  for (const auto &entry : frames) hash_frame(hash, entry.second);
  return hash;
}

}  // namespace gensam
}  // namespace esphome
