#pragma once

#include <stdint.h>

#if defined(_WIN32)
#if defined(HORUS_VOICE_NATIVE_EXPORTS)
#define HORUS_VOICE_API __declspec(dllexport)
#else
#define HORUS_VOICE_API __declspec(dllimport)
#endif
#else
#define HORUS_VOICE_API __attribute__((visibility("default")))
#endif

#ifdef __cplusplus
extern "C" {
#endif

typedef struct horus_voice_peer horus_voice_peer;

typedef void (*horus_voice_description_callback)(
    horus_voice_peer *peer,
    const char *description_type,
    const char *sdp,
    void *user_data);

typedef void (*horus_voice_candidate_callback)(
    horus_voice_peer *peer,
    const char *candidate,
    const char *mid,
    void *user_data);

typedef void (*horus_voice_state_callback)(
    horus_voice_peer *peer,
    int state,
    void *user_data);

typedef void (*horus_voice_pcm_callback)(
    horus_voice_peer *peer,
    const int16_t *samples,
    int sample_count,
    int sample_rate_hz,
    int channels,
    void *user_data);

typedef void (*horus_voice_error_callback)(
    horus_voice_peer *peer,
    const char *message,
    void *user_data);

typedef struct horus_voice_callbacks {
    horus_voice_description_callback description;
    horus_voice_candidate_callback candidate;
    horus_voice_state_callback state;
    horus_voice_pcm_callback pcm;
    horus_voice_error_callback error;
} horus_voice_callbacks;

/**
 * Creates an Opus/WebRTC voice peer.
 *
 * ice_servers is a newline-separated list of STUN/TURN URLs. It may be null or
 * empty for direct same-LAN host candidates. The returned peer owns all native
 * media resources and must be released with horus_voice_destroy().
 */
HORUS_VOICE_API horus_voice_peer *horus_voice_create(
    const char *ice_servers,
    int relay_only,
    const horus_voice_callbacks *callbacks,
    void *user_data);

HORUS_VOICE_API int horus_voice_create_offer(horus_voice_peer *peer);
HORUS_VOICE_API int horus_voice_set_remote_description(
    horus_voice_peer *peer,
    const char *description_type,
    const char *sdp);
HORUS_VOICE_API int horus_voice_add_remote_candidate(
    horus_voice_peer *peer,
    const char *candidate,
    const char *mid);

/** Sends one mono 48 kHz PCM frame. A 20 ms/960-sample frame is preferred. */
HORUS_VOICE_API int horus_voice_send_pcm(
    horus_voice_peer *peer,
    const int16_t *samples,
    int sample_count);

HORUS_VOICE_API void horus_voice_set_muted(horus_voice_peer *peer, int muted);
HORUS_VOICE_API void horus_voice_close(horus_voice_peer *peer);
HORUS_VOICE_API void horus_voice_destroy(horus_voice_peer *peer);
HORUS_VOICE_API const char *horus_voice_version(void);

#ifdef __cplusplus
}
#endif
