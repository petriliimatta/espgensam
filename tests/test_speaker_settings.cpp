#include "../components/gensam/speaker_settings.h"
#include "../components/gensam/arbiter.h"

#include <cassert>
#include <iostream>

using namespace esphome::gensam;

int main() {
  BusArbiter arbiter;
  arbiter.note_tx(100);
  const Frame ack(HOST_ADDRESS, CMD_ACK, {});
  assert(arbiter.classify(ack, 500) == BusArbiter::Verdict::EXTERNAL_MASTER);
  assert(arbiter.classify(ack, 500, true) == BusArbiter::Verdict::OUR_REPLY);
  assert(arbiter.classify(make_user_note_read(2), 500, true) == BusArbiter::Verdict::EXTERNAL_MASTER);
  const SpeakerNote original{'G', true, 0x01234567, 0x89abcdef};
  const auto encoded = encode_speaker_note(original);
  assert(encoded == "GS1:01234567:89abcdef");
  SpeakerNote parsed;
  assert(parse_speaker_note(encoded, parsed));
  assert(parsed.kind == 'G' && parsed.stored && parsed.group == original.group &&
         parsed.settings == original.settings);
  for (const auto &invalid : {encoded.substr(0, 20), encoded + "\n",
                             std::string("GS2:01234567:89abcdef"),
                             std::string("GS1:01234567:89abcdeg"), std::string("ESPTEST1"),
                             std::string("ZS1:01234567:89abcdef")}) {
    assert(!parse_speaker_note(invalid, parsed));
    assert(parsed.group == original.group);
  }
  assert(make_user_note_read(2).payload == std::vector<uint8_t>({0x81}));
  const auto write = make_user_note_write(2, encoded);
  assert(write.command == CMD_NAMES_USER && write.payload[0] == 1);
  assert(std::string(write.payload.begin() + 1, write.payload.end()) == encoded);
  assert(make_user_note_write(2, "").payload == std::vector<uint8_t>({1, 0}));
  assert(make_restore_user_params(2).payload == std::vector<uint8_t>({0x34, 0}));
  assert(make_store_user_params(2).payload == std::vector<uint8_t>({0x33, 0}));

  PeqBand bands[] = {{PeqType::PEAKING, 1000, -6, 1}};
  GroupDevice dev;
  dev.unique_id = 123;
  dev.bands = bands;
  dev.band_count = 1;
  const GroupPreset group{"Seat", &dev, 1};
  const auto fingerprint = group_setting_fingerprint(group, dev, false, 48000);
  assert(fingerprint != group_setting_fingerprint(group, dev, false, 96000));
  GroupPreset renamed = group;
  renamed.name = "Desk";
  assert(fingerprint != group_setting_fingerprint(renamed, dev, false, 48000));
  auto changed = dev;
  changed.unique_id++;
  assert(fingerprint != group_setting_fingerprint(group, changed, false, 48000));
  changed = dev;
  changed.delay_samples = 100;
  assert(fingerprint != group_setting_fingerprint(group, changed, false, 48000));
  bands[0].gain_db = -12;
  assert(fingerprint != group_setting_fingerprint(group, dev, false, 48000));
  bands[0].gain_db = -6;

  std::map<uint32_t, Frame> first, second;
  for (uint8_t step = GROUP_STEP_PREPARE; step < GROUP_STEP_DONE; step++) {
    uint32_t key;
    const auto frame = make_group_setting_frame(2, dev, false, 48000, step, false);
    if (setting_frame_key(frame, key)) first[key] = frame;
  }
  for (int step = GROUP_STEP_DONE - 1; step >= GROUP_STEP_PREPARE; step--) {
    uint32_t key;
    const auto frame = make_group_setting_frame(7, dev, false, 48000, step, false);
    if (setting_frame_key(frame, key)) second[key] = frame;
  }
  assert(observed_settings_fingerprint(first) == observed_settings_fingerprint(second));
  uint32_t key;
  auto trim = make_level(7, -12);
  assert(setting_frame_key(trim, key));
  second[key] = trim;
  assert(first.size() == second.size());
  assert(observed_settings_fingerprint(first) != observed_settings_fingerprint(second));
  for (const auto &frame : {make_broadcast_volume_db(-30), make_bypass(2, true, false, true),
                           make_user_note_read(2), make_store_user_params(2),
                           make_iss_read(2, 6), Frame(2, CMD_DSP, {0x81, 0}),
                           Frame(2, CMD_SELECT_AUDIO_SOURCE, {0x80})}) {
    assert(!setting_frame_key(frame, key));
  }
  assert(setting_frame_key(make_iss_led_state(2, false), key));
  assert(observed_settings_fingerprint({}) == 0);
  std::cout << "Speaker identity, fingerprints, safe builders and slow-reply attribution passed\n";
}
