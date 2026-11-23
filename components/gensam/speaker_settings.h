#pragma once

/// @file speaker_settings.h
/// @brief Wire-setting fingerprints and speaker user-note identity tags.
///
/// Tags describe the configuration this controller sent, not independent DSP readback.
/// The 21-byte ASCII format is [G/M/X][V/S]1:group-hash:settings-hash: G identifies a
/// preset, M a modified preset, X external/unknown settings; V is volatile and S is prepared
/// for an explicit flash store. Hashes are non-cryptographic FNV-1a fingerprints. Addresses
/// and CRCs are excluded because RACE leases change; device UID and name bind preset identity.
/// Unknown firmware settings are deliberately not guessed. The settings hash covers only
/// observed writable configuration payloads, never live volume, mute, power or identify.
///
/// A tag can be stale after an unobserved external upload that preserves the note. Callers
/// must expose this limitation and invalidate identity on observed external configuration.
/// Store transactions verify the complete note before committing, so a firmware with a short
/// note buffer cannot silently store a truncated identity.

#include <map>
#include <string>
#include "commands.h"
#include "groups.h"

namespace esphome {
namespace gensam {

/// @brief Frame positions shared by the group sender and its fingerprint calculation.
enum GroupSettingStep : uint8_t {
  GROUP_STEP_KEEPALIVE = 0,
  GROUP_STEP_MUTE,
  GROUP_STEP_PREPARE,
  GROUP_STEP_DELAY,
  GROUP_STEP_LEVEL,
  GROUP_STEP_PEQ_FIRST,
  GROUP_STEP_CROSSOVER = GROUP_STEP_PEQ_FIRST + PEQ_BAND_COUNT,
  GROUP_STEP_SOURCE,
  GROUP_STEP_SOURCE_AUX,
  GROUP_STEP_LFE_LEVEL,
  GROUP_STEP_DONE,
};

/// @brief A parsed controller-owned identity note; other notes are left as external metadata.
struct SpeakerNote {
  char kind{'X'};               ///< G = preset, M = modified preset, X = external/unknown.
  bool stored{false};           ///< S marks a tag prepared for an explicit profile store.
  uint32_t group{0};            ///< Preset/device fingerprint, or zero when unknown.
  uint32_t settings{0};         ///< Fingerprint of observed configuration frames, or zero.
};

/// @brief Serialize a note in the fixed version-1 ASCII format.
/// @param note Identity to serialize.
/// @return Exactly 21 ASCII bytes.
std::string encode_speaker_note(const SpeakerNote &note);

/// @brief Parse only complete version-1 controller tags, without accepting a truncated tag.
/// @param value Raw user note.
/// @param[out] note Parsed tag.
/// @return True when the format and both hex words are valid.
bool parse_speaker_note(const std::string &value, SpeakerNote &note);

/// @brief Build exactly the configuration frame a group step sends, or an empty frame to skip.
/// @param addr Target address.
/// @param dev Preset settings.
/// @param subwoofer Whether this device has the secondary LFE input.
/// @param design_rate Validated or model-derived PEQ design rate.
/// @param step GroupSettingStep position.
/// @param bypass Whether calibration is bypassed.
/// @return Configuration frame; command zero means no frame at this step.
Frame make_group_setting_frame(uint8_t addr, const GroupDevice &dev, bool subwoofer,
                               uint32_t design_rate, uint8_t step, bool bypass);

/// @brief Fingerprint one preset's wire settings for a stable hardware ID and its display name.
/// @param group Preset.
/// @param dev Member device.
/// @param subwoofer Device class.
/// @param design_rate Effective PEQ design rate.
/// @return Stable fingerprint independent of leased address and transmitted frame CRC.
uint32_t group_setting_fingerprint(const GroupPreset &group, const GroupDevice &dev,
                                   bool subwoofer, uint32_t design_rate);

/// @brief Identify a writable configuration frame, excluding all queries and session controls.
/// @param frame Frame to classify.
/// @param[out] key Canonical setting key, including PEQ slot/input index when relevant.
/// @return True if this is a tracked setting write.
bool setting_frame_key(const Frame &frame, uint32_t &key);

/// @brief Hash the latest observed payload for each canonical setting in deterministic order.
/// @param frames Latest configuration writes by setting key.
/// @return Fingerprint, or zero when no configuration payload is known.
uint32_t observed_settings_fingerprint(const std::map<uint32_t, Frame> &frames);

}  // namespace gensam
}  // namespace esphome
