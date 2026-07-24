#include "horus_voice_native.h"

#include <opus/opus.h>
#include <rtc/rtc.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <memory>
#include <mutex>
#include <random>
#include <sstream>
#include <string>
#include <vector>

namespace {

constexpr int kSampleRateHz = 48000;
constexpr int kChannels = 1;
constexpr int kMaxFrameSamples = 5760;
constexpr int kMaxEncodedBytes = 4000;
constexpr int kOpusPayloadType = 111;
constexpr uint32_t kOpusClockRate = 48000;

uint32_t random_u32() {
    std::random_device source;
    const auto high = static_cast<uint32_t>(source());
    const auto low = static_cast<uint32_t>(source());
    return (high << 16U) ^ low;
}

std::vector<std::string> split_lines(const char *value) {
    std::vector<std::string> result;
    if (value == nullptr || *value == '\0') {
        return result;
    }

    std::istringstream input(value);
    std::string line;
    while (std::getline(input, line)) {
        if (!line.empty() && line.back() == '\r') {
            line.pop_back();
        }
        if (!line.empty()) {
            result.push_back(std::move(line));
        }
    }
    return result;
}

bool parse_rtp_payload(
    const uint8_t *packet,
    size_t packet_size,
    uint16_t &sequence,
    const uint8_t *&payload,
    size_t &payload_size) {
    if (packet == nullptr || packet_size < 12 || (packet[0] >> 6U) != 2U) {
        return false;
    }

    const bool has_padding = (packet[0] & 0x20U) != 0;
    const bool has_extension = (packet[0] & 0x10U) != 0;
    const size_t csrc_count = packet[0] & 0x0FU;
    size_t offset = 12 + csrc_count * 4;
    if (offset > packet_size) {
        return false;
    }

    if (has_extension) {
        if (offset + 4 > packet_size) {
            return false;
        }
        const size_t extension_words =
            (static_cast<size_t>(packet[offset + 2]) << 8U) |
            static_cast<size_t>(packet[offset + 3]);
        offset += 4 + extension_words * 4;
        if (offset > packet_size) {
            return false;
        }
    }

    size_t end = packet_size;
    if (has_padding) {
        const size_t padding = packet[packet_size - 1];
        if (padding == 0 || padding > end - offset) {
            return false;
        }
        end -= padding;
    }
    if (end <= offset) {
        return false;
    }

    sequence = static_cast<uint16_t>(
        (static_cast<uint16_t>(packet[2]) << 8U) |
        static_cast<uint16_t>(packet[3]));
    payload = packet + offset;
    payload_size = end - offset;
    return true;
}

} // namespace

struct horus_voice_peer {
    int peer_connection = -1;
    int local_track = -1;
    int remote_track = -1;
    OpusEncoder *encoder = nullptr;
    OpusDecoder *decoder = nullptr;
    horus_voice_callbacks callbacks{};
    void *user_data = nullptr;
    std::atomic<bool> muted{false};
    std::atomic<bool> closing{false};
    std::mutex codec_mutex;
    bool has_received_sequence = false;
    uint16_t last_received_sequence = 0;
    uint32_t next_send_timestamp = 0;
    uint32_t local_ssrc = 0;
    std::vector<std::string> ice_server_storage;
    std::vector<const char *> ice_server_pointers;

    void report_error(const std::string &message) const {
        if (callbacks.error != nullptr) {
            callbacks.error(
                const_cast<horus_voice_peer *>(this),
                message.c_str(),
                user_data);
        }
    }

    void deliver_decoded(const unsigned char *payload, int payload_size, int decode_fec) {
        if (decoder == nullptr || closing.load()) {
            return;
        }

        std::array<int16_t, kMaxFrameSamples> decoded{};
        const int samples = opus_decode(
            decoder,
            payload,
            payload_size,
            decoded.data(),
            static_cast<int>(decoded.size()),
            decode_fec);
        if (samples < 0) {
            report_error(std::string("Opus decode failed: ") + opus_strerror(samples));
            return;
        }
        if (samples > 0 && callbacks.pcm != nullptr) {
            callbacks.pcm(
                this,
                decoded.data(),
                samples,
                kSampleRateHz,
                kChannels,
                user_data);
        }
    }
};

namespace {

void on_local_description(int, const char *sdp, const char *type, void *pointer) {
    auto *peer = static_cast<horus_voice_peer *>(pointer);
    if (peer == nullptr || peer->closing.load() || peer->callbacks.description == nullptr) {
        return;
    }
    peer->callbacks.description(peer, type, sdp, peer->user_data);
}

void on_local_candidate(int, const char *candidate, const char *mid, void *pointer) {
    auto *peer = static_cast<horus_voice_peer *>(pointer);
    if (peer == nullptr || peer->closing.load() || peer->callbacks.candidate == nullptr) {
        return;
    }
    peer->callbacks.candidate(peer, candidate, mid, peer->user_data);
}

void on_state_change(int, rtcState state, void *pointer) {
    auto *peer = static_cast<horus_voice_peer *>(pointer);
    if (peer == nullptr || peer->closing.load() || peer->callbacks.state == nullptr) {
        return;
    }
    peer->callbacks.state(peer, static_cast<int>(state), peer->user_data);
}

void on_remote_rtp(int, const char *message, int size, void *pointer) {
    auto *peer = static_cast<horus_voice_peer *>(pointer);
    if (peer == nullptr || peer->closing.load() || message == nullptr || size <= 0) {
        return;
    }

    uint16_t sequence = 0;
    const uint8_t *payload = nullptr;
    size_t payload_size = 0;
    if (!parse_rtp_payload(
            reinterpret_cast<const uint8_t *>(message),
            static_cast<size_t>(size),
            sequence,
            payload,
            payload_size)) {
        peer->report_error("Received an invalid Opus RTP packet");
        return;
    }

    std::lock_guard<std::mutex> lock(peer->codec_mutex);
    if (peer->has_received_sequence) {
        const auto distance = static_cast<uint16_t>(sequence - peer->last_received_sequence);
        if (distance == 0 || distance >= 0x8000U) {
            return;
        }
        const int missing = std::min<int>(distance - 1, 3);
        for (int index = 0; index < missing; ++index) {
            peer->deliver_decoded(nullptr, 0, 0);
        }
    }

    peer->last_received_sequence = sequence;
    peer->has_received_sequence = true;
    peer->deliver_decoded(
        reinterpret_cast<const unsigned char *>(payload),
        static_cast<int>(payload_size),
        0);
}

void on_remote_track(int, int track, void *pointer) {
    auto *peer = static_cast<horus_voice_peer *>(pointer);
    if (peer == nullptr || peer->closing.load()) {
        return;
    }

    peer->remote_track = track;
    rtcSetUserPointer(track, peer);
    if (rtcChainRtcpReceivingSession(track) < 0 ||
        rtcSetMessageCallback(track, on_remote_rtp) < 0) {
        peer->report_error("Could not initialize the remote Opus track");
    }
}

bool initialize_codecs(horus_voice_peer &peer) {
    int error = OPUS_OK;
    peer.encoder = opus_encoder_create(kSampleRateHz, kChannels, OPUS_APPLICATION_VOIP, &error);
    if (error != OPUS_OK || peer.encoder == nullptr) {
        peer.report_error(std::string("Could not create Opus encoder: ") + opus_strerror(error));
        return false;
    }

    peer.decoder = opus_decoder_create(kSampleRateHz, kChannels, &error);
    if (error != OPUS_OK || peer.decoder == nullptr) {
        peer.report_error(std::string("Could not create Opus decoder: ") + opus_strerror(error));
        return false;
    }

    opus_encoder_ctl(peer.encoder, OPUS_SET_BITRATE(32000));
    opus_encoder_ctl(peer.encoder, OPUS_SET_COMPLEXITY(5));
    opus_encoder_ctl(peer.encoder, OPUS_SET_INBAND_FEC(1));
    opus_encoder_ctl(peer.encoder, OPUS_SET_PACKET_LOSS_PERC(5));
    opus_encoder_ctl(peer.encoder, OPUS_SET_DTX(1));
    opus_encoder_ctl(peer.encoder, OPUS_SET_SIGNAL(OPUS_SIGNAL_VOICE));
    return true;
}

bool initialize_local_track(horus_voice_peer &peer) {
    rtcTrackInit track{};
    track.direction = RTC_DIRECTION_SENDRECV;
    track.codec = RTC_CODEC_OPUS;
    track.payloadType = kOpusPayloadType;
    track.ssrc = peer.local_ssrc;
    track.mid = "audio";
    track.name = "horus-voice";
    track.msid = "horus-voice-stream";
    track.trackId = "horus-voice-track";
    track.profile = "minptime=10;useinbandfec=1;usedtx=1";

    peer.local_track = rtcAddTrackEx(peer.peer_connection, &track);
    if (peer.local_track < 0) {
        peer.report_error("Could not add the local Opus track");
        return false;
    }
    rtcSetUserPointer(peer.local_track, &peer);

    rtcPacketizerInit packetizer{};
    packetizer.ssrc = peer.local_ssrc;
    packetizer.cname = "horus-voice";
    packetizer.payloadType = kOpusPayloadType;
    packetizer.clockRate = kOpusClockRate;
    packetizer.sequenceNumber = static_cast<uint16_t>(random_u32());
    packetizer.timestamp = peer.next_send_timestamp;

    if (rtcSetOpusPacketizer(peer.local_track, &packetizer) < 0 ||
        rtcChainRtcpSrReporter(peer.local_track) < 0 ||
        rtcChainRtcpNackResponder(peer.local_track, 256) < 0) {
        peer.report_error("Could not configure the local Opus RTP packetizer");
        return false;
    }
    return true;
}

} // namespace

extern "C" {

horus_voice_peer *horus_voice_create(
    const char *ice_servers,
    int relay_only,
    const horus_voice_callbacks *callbacks,
    void *user_data) {
    auto peer = std::make_unique<horus_voice_peer>();
    if (callbacks != nullptr) {
        peer->callbacks = *callbacks;
    }
    peer->user_data = user_data;
    peer->local_ssrc = random_u32();
    peer->next_send_timestamp = random_u32();
    peer->ice_server_storage = split_lines(ice_servers);
    peer->ice_server_pointers.reserve(peer->ice_server_storage.size());
    for (const auto &server : peer->ice_server_storage) {
        peer->ice_server_pointers.push_back(server.c_str());
    }

    if (!initialize_codecs(*peer)) {
        horus_voice_destroy(peer.release());
        return nullptr;
    }

    rtcConfiguration config{};
    config.iceServers = peer->ice_server_pointers.empty()
        ? nullptr
        : peer->ice_server_pointers.data();
    config.iceServersCount = static_cast<int>(peer->ice_server_pointers.size());
    config.iceTransportPolicy = relay_only != 0
        ? RTC_TRANSPORT_POLICY_RELAY
        : RTC_TRANSPORT_POLICY_ALL;
    config.disableAutoNegotiation = true;
    config.forceMediaTransport = true;

    peer->peer_connection = rtcCreatePeerConnection(&config);
    if (peer->peer_connection < 0) {
        peer->report_error("Could not create the WebRTC peer connection");
        horus_voice_destroy(peer.release());
        return nullptr;
    }

    rtcSetUserPointer(peer->peer_connection, peer.get());
    rtcSetLocalDescriptionCallback(peer->peer_connection, on_local_description);
    rtcSetLocalCandidateCallback(peer->peer_connection, on_local_candidate);
    rtcSetStateChangeCallback(peer->peer_connection, on_state_change);
    rtcSetTrackCallback(peer->peer_connection, on_remote_track);

    if (!initialize_local_track(*peer)) {
        horus_voice_destroy(peer.release());
        return nullptr;
    }
    return peer.release();
}

int horus_voice_create_offer(horus_voice_peer *peer) {
    if (peer == nullptr || peer->closing.load() || peer->peer_connection < 0) {
        return RTC_ERR_INVALID;
    }
    return rtcSetLocalDescription(peer->peer_connection, "offer");
}

int horus_voice_set_remote_description(
    horus_voice_peer *peer,
    const char *description_type,
    const char *sdp) {
    if (peer == nullptr || peer->closing.load() || description_type == nullptr || sdp == nullptr) {
        return RTC_ERR_INVALID;
    }
    const int result = rtcSetRemoteDescription(peer->peer_connection, sdp, description_type);
    if (result < 0) {
        return result;
    }
    if (std::strcmp(description_type, "offer") == 0) {
        return rtcSetLocalDescription(peer->peer_connection, "answer");
    }
    return result;
}

int horus_voice_add_remote_candidate(
    horus_voice_peer *peer,
    const char *candidate,
    const char *mid) {
    if (peer == nullptr || peer->closing.load() || candidate == nullptr) {
        return RTC_ERR_INVALID;
    }
    return rtcAddRemoteCandidate(peer->peer_connection, candidate, mid == nullptr ? "audio" : mid);
}

int horus_voice_send_pcm(
    horus_voice_peer *peer,
    const int16_t *samples,
    int sample_count) {
    if (peer == nullptr || peer->closing.load() || samples == nullptr || sample_count <= 0) {
        return RTC_ERR_INVALID;
    }
    if (peer->muted.load()) {
        return RTC_ERR_SUCCESS;
    }
    if (peer->local_track < 0 || !rtcIsOpen(peer->local_track)) {
        return RTC_ERR_NOT_AVAIL;
    }

    std::array<unsigned char, kMaxEncodedBytes> encoded{};
    int encoded_size = 0;
    {
        std::lock_guard<std::mutex> lock(peer->codec_mutex);
        encoded_size = opus_encode(
            peer->encoder,
            samples,
            sample_count,
            encoded.data(),
            static_cast<opus_int32>(encoded.size()));
    }
    if (encoded_size < 0) {
        peer->report_error(std::string("Opus encode failed: ") + opus_strerror(encoded_size));
        return RTC_ERR_FAILURE;
    }

    rtcSetTrackRtpTimestamp(peer->local_track, peer->next_send_timestamp);
    const int result = rtcSendMessage(
        peer->local_track,
        reinterpret_cast<const char *>(encoded.data()),
        encoded_size);
    if (result >= 0) {
        peer->next_send_timestamp += static_cast<uint32_t>(sample_count);
    }
    return result;
}

void horus_voice_set_muted(horus_voice_peer *peer, int muted) {
    if (peer != nullptr) {
        peer->muted.store(muted != 0);
    }
}

void horus_voice_close(horus_voice_peer *peer) {
    if (peer == nullptr || peer->closing.exchange(true)) {
        return;
    }
    if (peer->peer_connection >= 0) {
        rtcClosePeerConnection(peer->peer_connection);
    }
}

void horus_voice_destroy(horus_voice_peer *peer) {
    if (peer == nullptr) {
        return;
    }
    horus_voice_close(peer);
    if (peer->local_track >= 0) {
        rtcDeleteTrack(peer->local_track);
        peer->local_track = -1;
    }
    if (peer->remote_track >= 0) {
        rtcDeleteTrack(peer->remote_track);
        peer->remote_track = -1;
    }
    if (peer->peer_connection >= 0) {
        rtcDeletePeerConnection(peer->peer_connection);
        peer->peer_connection = -1;
    }
    if (peer->encoder != nullptr) {
        opus_encoder_destroy(peer->encoder);
    }
    if (peer->decoder != nullptr) {
        opus_decoder_destroy(peer->decoder);
    }
    delete peer;
}

const char *horus_voice_version(void) {
    return "horus_voice_native/0.1.0 libdatachannel/0.24.5 opus/1.5.2";
}

} // extern "C"
