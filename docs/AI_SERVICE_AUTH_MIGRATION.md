# AI Service Socket.IO Authentication — Migration to Replay-Resistant Signatures

**Audience:** the AI service (Python) engineering team
**Status:** backend implementation complete and merged behind a rollout flag; awaiting the AI-side client change
**Internal reference:** task EX-01

This document is self-contained. You do not need access to the backend repository to implement
against it. Everything the client must produce is specified here byte-for-byte.

---

## 1. What is changing, and why

**The risk, plainly.** Today the AI service authenticates to the backend's Socket.IO handshake with
an HMAC-SHA256 signature computed over a **hardcoded constant string**. Because the signed input
never varies, the signature never varies. It is one fixed value that is valid forever. Anyone who
obtains it once — from a log line, a screenshot, a `.env` file, a CI variable dump, a proxy capture,
an old ticket attachment — can impersonate the AI service permanently, and the AI service identity is
exempt from the per-case authorization checks that constrain ordinary users. A single leaked string
therefore reaches every customer's privileged legal material, and rotating the shared secret is the
only possible response, which requires both our teams and so in practice never happens promptly.

**What replaces it.** The signature is now computed over a canonical string that includes a **UTC
timestamp** and a **single-use random nonce**, both sent as headers alongside the signature. The
backend rejects a timestamp outside a ±300-second window, and rejects any nonce it has already seen
(recorded in a database collection with a self-expiring TTL). The two checks together make a captured
handshake worthless: the timestamp makes it go stale within minutes, and the nonce means it cannot be
used even twice inside those minutes. Nothing else about the connection changes — same URL, same
`x-client-type: AI_Maverick`, same `ai_service` room, same events and payloads. Only the three
handshake headers change.

---

## 2. The canonical string — the exact bytes you sign

This is the highest-risk part of the integration: if our two sides disagree by a single character,
every connection fails with `bad_signature` and there is nothing in the payload to hint why. Treat
this section as normative.

```
v1:AI_Maverick:<timestamp>:<nonce>
```

Precisely:

| # | Field | Value |
|---|-------|-------|
| 1 | scheme version | the literal ASCII string `v1` (lowercase v, digit one) |
| 2 | client id | the literal ASCII string `AI_Maverick` (exact casing, underscore) |
| 3 | timestamp | current UTC Unix time in **whole seconds**, decimal digits only |
| 4 | nonce | 32 **lowercase** hexadecimal characters |

Rules, all of which matter:

- **Delimiter: exactly one ASCII colon `:` (U+003A) between fields.** Not `::`, not `: `, not a
  pipe.
- **Field order is fixed** as above. There is no sorting, no key names, no JSON.
- **No leading or trailing delimiter.** The string starts with `v` and ends with the last nonce
  character.
- **No trailing newline.** A `\n` at the end changes the HMAC. If you build the string with a
  `print`-style helper or read it from a file, check for this first.
- **No whitespace anywhere**, including none around the colons.
- **Encode the string as UTF-8** before feeding it to HMAC. Since every character is ASCII this is
  the same as ASCII, but say UTF-8 explicitly in your code so nobody later introduces a locale
  encoding.
- The delimiter is unambiguous because no field can contain a colon: fields 1 and 2 are literals,
  field 3 is validated as digits only, field 4 as lowercase hex only. There is exactly one way to
  parse the result.

**Worked example.** With timestamp `1755763200` and nonce `0f3a1c9d2b47e6580a1b2c3d4e5f6071`, the
canonical string is exactly:

```
v1:AI_Maverick:1755763200:0f3a1c9d2b47e6580a1b2c3d4e5f6071
```

That is **58 characters**. If your code produces 59, you have a trailing newline.

---

## 3. Headers, formats and encoding

Send all three on the Socket.IO handshake HTTP request, in addition to the existing
`x-client-type: AI_Maverick`.

| Header | Value |
|--------|-------|
| `x-signature` | the HMAC, encoded as described below |
| `x-signature-timestamp` | the same value used as field 3 of the canonical string |
| `x-signature-nonce` | the same value used as field 4 of the canonical string |

**Header names are lowercase** as written. Node lowercases inbound header names, so any casing you
send will arrive lowercased and will match — but write them lowercase so the two sides read
identically.

**Send them as HTTP headers, not query-string parameters.** We deliberately do not read these from
the query string (unlike `x-client-type`, which is accepted either way for historical reasons). Query
strings are written verbatim into proxy and load-balancer access logs, which is one of the main ways
a credential ends up captured in the first place.

### Timestamp format

- UTC Unix time in **whole seconds** — e.g. `1755763200`.
- Decimal ASCII digits only. **No** milliseconds, **no** decimal point, **no** sign, **no** `+00:00`
  suffix, **no** ISO-8601. `int(time.time())` is exactly right; `time.time()` is not, because it
  yields a float with a decimal point.
- Do not zero-pad. (A padded value would in fact still verify, because we sign the literal
  characters you send rather than a re-normalised copy — but do not rely on that.)
- **At most 15 digits.** Anything longer is refused as `malformed_timestamp` rather than
  `stale_timestamp`, so a nanosecond timestamp (19 digits) reports as a *format* error, not a clock
  error. A millisecond timestamp (13 digits) does pass the format check and then fails freshness.
- The header value and canonical field 3 must be **character-identical**. Build the string once and
  use it in both places.

### Signature encoding — pinned, read this

> **The signature is HMAC-SHA256, encoded as base64url (RFC 4648 §5, the `-` and `_` alphabet),
> with padding STRIPPED. No `=` characters. Always exactly 43 characters.**

- **base64url**, *not* standard base64. Characters 62 and 63 are `-` and `_`, never `+` and `/`.
- **Unpadded.** Any trailing `=` must be removed.
- Lowercase/uppercase is not a choice here — base64url output is mixed-case by nature; do not alter
  the case of the digest.
- Do **not** send hex.

This paragraph exists because of a real bug we are closing. The current Python client emits
**padded standard base64** while the Node backend produces **unpadded base64url**, so the two
differ by up to two trailing `=` characters. The backend has carried an ugly workaround for years
that compares the common prefix and then permits up to two trailing `=`. That workaround is retained
**only** on the legacy path, and is **not** present on the new `v1` path. On `v1`, a padded
signature, a standard-base64 signature, or a hex signature is rejected outright as `bad_signature`.
Pin it now so no one needs another workaround.

In Python the correct one-liner is:

```python
base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
```

We have verified that this produces byte-identical output to the Node
`crypto.createHmac('sha256', secret).update(canonical, 'utf8').digest('base64url')` used on our
side, for the worked example above.

### Nonce requirements

- **32 lowercase hexadecimal characters**, i.e. **16 random bytes / 128 bits** of entropy.
  `secrets.token_hex(16)` produces exactly this.
- Uppercase hex is rejected (`malformed_nonce`). Any other length is rejected. Do not use
  `secrets.token_urlsafe`, a UUID string with hyphens, or a counter.
- **Cryptographically random.** Use `secrets`, never `random`. A predictable nonce lets an attacker
  pre-spend the values you are about to use.
- **Single-use, generated fresh per connection attempt.** Not per process, not per day, not cached.
  Socket.IO treats every reconnect as a new connection, so **your automatic-reconnect path must
  generate a new nonce and a new timestamp and re-sign.** This is the single most common way this
  migration goes wrong: a client that computes the headers once at startup will connect successfully
  and then fail every reconnect thereafter with `replayed_nonce`.
- Reuse is refused. There is no grace and no retry-with-the-same-value.

---

## 4. Complete, runnable Python example

Standard library only — `hmac`, `hashlib`, `time`, `secrets`, `base64`. No third-party dependency is
needed to build the headers.

> **The secret below is an obvious placeholder.** `SOCKET_SECRET_KEY` is the shared secret already
> configured on both sides; read it from your environment or secret manager. Never hardcode it,
> never log it, never paste it into a ticket, and never commit it.

```python
"""
Build the replay-resistant handshake headers for the SimplerToday backend.
Standard library only. Python 3.8+.
"""

import base64
import hashlib
import hmac
import os
import secrets
import time

# ---------------------------------------------------------------------------
# Contract constants. Do not edit without agreeing a new scheme version.
# ---------------------------------------------------------------------------
SCHEME_VERSION = "v1"
CLIENT_ID = "AI_Maverick"
DELIMITER = ":"

HEADER_SIGNATURE = "x-signature"
HEADER_TIMESTAMP = "x-signature-timestamp"
HEADER_NONCE = "x-signature-nonce"
HEADER_CLIENT_TYPE = "x-client-type"


def build_auth_headers(secret: str) -> dict:
    """
    Return a fresh set of handshake headers.

    MUST be called immediately before every connection attempt, including every
    automatic reconnect: the nonce is single-use and the timestamp expires.
    """
    if not secret:
        raise RuntimeError("SOCKET_SECRET_KEY is not set; refusing to connect")

    # Whole seconds, UTC. int() truncates the float from time.time(); a decimal
    # point in this value is the most common cause of a bad_signature.
    timestamp = str(int(time.time()))

    # 16 random bytes -> exactly 32 lowercase hex characters (128 bits).
    # secrets, never random: a predictable nonce can be pre-spent by an attacker.
    nonce = secrets.token_hex(16)

    # The canonical string: four fields, fixed order, ONE colon between each,
    # no trailing delimiter, no trailing newline, no whitespace.
    canonical = DELIMITER.join([SCHEME_VERSION, CLIENT_ID, timestamp, nonce])

    digest = hmac.new(
        secret.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).digest()

    # base64url (- and _ alphabet), padding stripped. Always 43 characters.
    signature = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    return {
        HEADER_CLIENT_TYPE: CLIENT_ID,
        HEADER_SIGNATURE: signature,
        HEADER_TIMESTAMP: timestamp,
        HEADER_NONCE: nonce,
    }


# ---------------------------------------------------------------------------
# Self-check: verifies your build against the published worked example.
# Run this once before you touch the socket client. If it fails, the socket
# will fail too, and this tells you so in one second instead of at 2am.
# ---------------------------------------------------------------------------
def self_check() -> None:
    fixed_secret = "REPLACE_ME_placeholder"  # placeholder, matches the doc example
    canonical = "v1:AI_Maverick:1755763200:0f3a1c9d2b47e6580a1b2c3d4e5f6071"
    assert len(canonical) == 58, f"canonical string is {len(canonical)} chars, expected 58"

    digest = hmac.new(fixed_secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).digest()
    signature = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    expected = "3G0xegMwS8d5QOnJOA759UxyxeggnzrdP-TIZy1gOSM"
    assert signature == expected, f"got {signature}, expected {expected}"
    assert len(signature) == 43, "signature must be 43 unpadded base64url characters"
    print("self-check OK")


# ---------------------------------------------------------------------------
# Socket.IO usage (python-socketio). The ONLY change on your side is passing
# fresh headers on every connect.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    self_check()

    import socketio  # third-party, you already depend on it

    SECRET = os.environ["SOCKET_SECRET_KEY"]  # read from the environment
    BACKEND_URL = os.environ["BACKEND_SOCKET_URL"]

    # reconnection=False so WE control every attempt and can re-sign each time.
    # If you keep the library's automatic reconnection instead, you must supply
    # fresh headers per attempt (see the note below) or every reconnect will be
    # refused with replayed_nonce.
    sio = socketio.Client(reconnection=False, logger=False)

    @sio.event
    def connect() -> None:
        print("connected to backend")

    @sio.event
    def connect_error(data) -> None:
        # The backend disconnects on a failed handshake; see section 8 for how to
        # tell the failure modes apart.
        print(f"handshake refused: {data}")

    def connect_with_retry(max_attempts: int = 5) -> None:
        for attempt in range(1, max_attempts + 1):
            try:
                # Fresh nonce + fresh timestamp on EVERY attempt.
                sio.connect(
                    BACKEND_URL,
                    headers=build_auth_headers(SECRET),
                    transports=["websocket"],
                )
                return
            except Exception as exc:  # noqa: BLE001 - retry loop
                print(f"attempt {attempt} failed: {exc}")
                time.sleep(min(2 ** attempt, 30))
        raise RuntimeError("could not connect to the backend")

    connect_with_retry()
    sio.wait()
```

**On automatic reconnection.** `python-socketio` reuses the headers passed to the original
`connect()` call for its automatic reconnect attempts. Because the nonce is single-use, that will be
refused. Either drive reconnection yourself as shown above, or hook your reconnect path so that
`build_auth_headers()` is called again for each attempt. Please confirm which approach you have taken
before we schedule the rollout — this is the failure mode we most expect.

---

## 5. Timestamp tolerance and clock sync

- The accepted window is **±300 seconds** (5 minutes) around the backend's clock, applied
  symmetrically: a timestamp too far in the past *and* too far in the future are both refused.
- It is configurable on our side via `AI_SIGNATURE_TOLERANCE_SECONDS`. Assume 300 unless we tell you
  otherwise in writing.
- **Keep the AI host's clock synchronised with NTP** (`chronyd` / `systemd-timesyncd`, or your cloud
  provider's time service). This is a real operational requirement, not boilerplate: a host that
  drifts more than five minutes will fail *every* connection with `stale_timestamp`, and the symptom
  looks like an authentication failure rather than a clock problem. If you run in containers, the
  clock comes from the host — check the host.
- Use the system clock in UTC (`time.time()` is already UTC-based and unaffected by the local
  timezone). Do not apply a timezone offset to it.

---

## 6. Rollout sequence

Both sides must change, so there is a coordinated window during which the backend accepts **both**
the old and the new signature. That window is opened by a backend environment flag,
`AI_SIGNATURE_ALLOW_LEGACY`, which **defaults to off** — meaning the new scheme only. It is turned on
deliberately for the window and turned off immediately after.

Do the steps in this order.

1. **AI team** — implement section 4 and run `self_check()` locally. Confirm it prints `self-check
   OK`. Confirm your reconnect path re-signs (section 4, final note). Report readiness.
2. **Backend team** — deploy the backend with the new verification to the target environment, with
   `AI_SIGNATURE_ALLOW_LEGACY=false` (the default) still in place, and confirm the AI service — still
   on the old client — is **refused**. This proves the new code path is actually live. Expect
   `legacy_scheme_disabled` in the backend logs.
3. **Backend team** — set `AI_SIGNATURE_ALLOW_LEGACY=true` and restart. Confirm the AI service on the
   **old** client reconnects successfully. Confirm the backend logs the loud warning naming the flag.
   **The window is now open.** Announce the time it opened in the shared channel.
4. **AI team** — deploy the new client to the same environment. Verify success on your side by
   confirming (a) your client reaches the `connect` event, and (b) it survives a deliberate
   reconnect — restart your process, or drop the connection — because the reconnect path is where a
   single-use nonce bites.
5. **Backend team** — confirm success on our side: the backend log shows the AI handshake verified on
   scheme `v1`, and the loud `AI_SIGNATURE_ALLOW_LEGACY` warning has **stopped appearing** for new
   connections. A warning still appearing means something is still using the old client.
6. **Both** — smoke-test one real workspace end to end (create a workspace with a document, confirm
   the analysis features come back). Handshake success is not feature success.
7. **Backend team** — set `AI_SIGNATURE_ALLOW_LEGACY=false` and restart. **The window is now closed.**
   Confirm the AI service reconnects successfully afterwards, which proves it is genuinely on `v1` and
   was not silently riding the legacy path.
8. **Backend team** — record the closing time, and confirm no environment (dev, staging, production)
   is left with the flag on. Repeat steps 2–8 per environment; do staging first.
9. **Both** — because the old signature was replayable for its whole life, treat the shared
   `SOCKET_SECRET_KEY` as potentially exposed and **rotate it** after the window closes, as a separate
   coordinated change.

**Keep the window short** — ideally under an hour, and no more than a working day. While it is open,
the old replayable signature is still accepted and the vulnerability is still live. The backend logs a
warning on *every* legacy acceptance, naming the flag, specifically so this cannot be forgotten.

---

## 7. Rollback

If your deploy fails mid-window, nothing needs to be undone on our side, provided the flag is still
on:

1. **Do not close the flag.** If step 7 has not happened yet, `AI_SIGNATURE_ALLOW_LEGACY=true` is
   still set and the **old client still works**. Roll your service back to the previous version and
   it will reconnect on the legacy signature. No backend change and no backend restart is required.
2. **If the flag was already closed** and you need to roll back, ask the backend team to set
   `AI_SIGNATURE_ALLOW_LEGACY=true` and restart. That reopens the window; your old client reconnects
   within seconds of it coming back up.
3. **Tell us in the shared channel either way**, with the time. An open flag that nobody is tracking
   is worse than the original bug, because it is invisible.
4. Reschedule, and start again from step 3. There is no partial state to clean up: nonces already
   spent are simply forgotten by their TTL, and a rolled-back client generates fresh ones.
5. The backend also refuses all AI connections if `SOCKET_SECRET_KEY` is absent, exactly as before —
   so a rollback that loses the secret presents as a total AI outage, not as a silent downgrade.

---

## 8. Failure modes — what you will see, and what it means

On a failed handshake the backend **disconnects the socket**; it does not return a descriptive error
body, so your client sees a generic connection failure. The table below is how you diagnose it
without our logs. Work down it in order — the causes are listed roughly by likelihood.

| Backend code (in our logs) | What you observe | Cause | Fix |
|---|---|---|---|
| `missing_header` | connection refused immediately | one of `x-signature`, `x-signature-timestamp`, `x-signature-nonce` absent or empty. Sending *either* new header but not both is also this. | send all three; check your HTTP client is not dropping unknown headers |
| `bad_signature` | connection refused immediately | canonical string or encoding mismatch — trailing newline, wrong delimiter, padded/standard base64, hex output, timestamp header not identical to canonical field 3, or the secret differs between our sides | run `self_check()`; print `repr(canonical)` and check length; confirm signature is 43 chars with no `=` |
| `stale_timestamp` | **every** connection refused, consistently | host clock more than 300s from ours, or a non-second timestamp (milliseconds are ~1000x too large) | fix NTP; confirm `int(time.time())`, not `time.time()` and not `time.time() * 1000` |
| `replayed_nonce` | **first** connection succeeds, every **reconnect** refused | headers computed once and reused | generate fresh headers per attempt (section 4, final note) |
| `malformed_nonce` | connection refused immediately | nonce not exactly 32 lowercase hex chars — uppercase hex, a hyphenated UUID, or `token_urlsafe` output | use `secrets.token_hex(16)` |
| `malformed_timestamp` | connection refused immediately | timestamp contains a non-digit — a `.`, a `+`, a `-`, or an ISO-8601 string — or runs past 15 digits (a nanosecond clock) | use `str(int(time.time()))` |
| `legacy_scheme_disabled` | connection refused immediately | you sent only `x-signature` (old client) and the rollout window is **not** open | either deploy the new client, or ask us to open the flag |
| `secret_not_configured` | all AI connections refused | `SOCKET_SECRET_KEY` missing on the **backend** | our side; escalate to the backend team |
| `nonce_store_unavailable` | intermittent refusals | our database could not record the nonce, so single-use could not be proven, and we fail closed | our side; escalate to the backend team |
| `replay_guard_unverified` | **every** connection refused, from the moment our service starts | our nonce ledger is missing the unique index that enforces single-use. Nothing is wrong with your client — your signature was already accepted before this check. We refuse rather than run without replay protection | entirely our side. Send us this code and we will rebuild the index and restart. **Do not change anything on yours** |

Two useful diagnostic notes:

- A wrong signature does **not** consume your nonce. Retrying after fixing the signature is safe, and
  a `bad_signature` never turns into a spurious `replayed_nonce`.
- If your v1 signature is wrong, we do **not** silently fall back to the legacy path, even while the
  flag is open. Sending any new header commits you to the new scheme in full. This is deliberate:
  a fallback would let an attacker pair a junk timestamp with the old captured constant signature and
  defeat the whole change.

When you raise an issue with us, please include: the UTC time of the attempt to the second, the
`x-signature-timestamp` value you sent, the `x-signature-nonce` value you sent, the length of your
signature, and `repr()` of your canonical string. **Never** include the signature itself or the
secret.

---

## 9. Quick reference card

```
Headers (all lowercase, on the handshake HTTP request):
  x-client-type:          AI_Maverick
  x-signature:            HMAC-SHA256, base64url, NO padding, 43 chars
  x-signature-timestamp:  UTC Unix seconds, decimal digits only
  x-signature-nonce:      32 lowercase hex chars (secrets.token_hex(16))

Signed string (UTF-8, no trailing newline, single ':' delimiter):
  v1:AI_Maverick:<timestamp>:<nonce>

Key = SOCKET_SECRET_KEY (shared secret, never logged)
Tolerance = +/-300s   |   Nonce = single-use, fresh EVERY connect and reconnect
```
