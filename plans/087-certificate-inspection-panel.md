# Plan 087: Inspect a certificate pair from the admin UI

- **Priority**: P2
- **Effort**: S–M
- **Risk**: LOW — one new read-only route, one new module, one new UI panel; no existing code path changed.
- **Depends on**: [086](086-research-signing-ipas-on-upload.md) (research), 010 (session auth)
- **Status**: DONE
- **Executed at**: 2026-09-18

## Why this and not signing

Plan 086 asked what it would take for Feather to hold a signing certificate and
sign uploaded IPAs. The answer, from primary sources, was: don't.

- AltStore and SideStore both discard the incoming signature and re-sign with
  the *subscriber's* identity, unconditionally (086 §4), so a signed upload is
  behaviourally identical to an unsigned one for every client this catalogue
  actually serves.
- `codesign` is macOS-only by Apple's own words and this container is
  `python:3.11-slim` (086 §1). The Linux re-signers both take the p12
  passphrase via `argv` (086 §3).
- A Development/Ad-Hoc identity is UDID-bound, so signing would make an IPA
  install on *fewer* devices, and the `ProvisionsAllDevices` alternative is
  licensed to employees only (086 §2).

What was actually being asked, underneath the request for an upload button, is
*"is this certificate usable, and on which devices?"*. That question is cheap
to answer, needs no signer, no `argv` passphrase, and no licence exposure. This
plan implements 086 §8e: **validate and display, sign nothing.**

## What was built

**`scripts/certificate_inspection.py`** — a new inspection module in the shape
of the existing `ipa_inspection.py` / `apk_inspection.py`: frozen dataclasses,
a typed error, no I/O, no network. Two halves with deliberately different
dependency weights:

- The profile is parsed with the standard library alone. A `.mobileprovision`
  is a plist inside a CMS `SignedData` whose payload is cleartext (RFC 5652
  §5.2), so the plist is sliced out between `<?xml` and the last `</plist>` and
  handed to `plistlib` — bounded by the closing tag rather than taking the tail
  of the file.
- The p12 needs `cryptography` (`pkcs12.load_key_and_certificates`), because
  the interesting half of an RFC 7292 container is encrypted.

`CertificateInspectionError.reason` is one of `NOT_A_PKCS12`,
`WRONG_PASSPHRASE`, `NO_PRIVATE_KEY`, `NOT_A_PROFILE`, `TOO_LARGE`. The first
two matter most: pyca reports both as `ValueError`, distinguishable only by
message, and reporting a typo as a corrupt certificate is the first thing an
operator would hit.

**`POST /api/certificate/inspect`** (`app.py`, session-gated like the rest of
the admin API) takes `p12File`, `provisionFile` and an optional `p12Password`,
reads both files into memory — small by definition, and a temp file would leave
key material on disk for no gain — and returns the summary. Nothing is written,
nothing is persisted, the passphrase is never logged, and an unexpected
exception returns a fixed message rather than the crypto error text (plan 080).

**The Certificate panel** (`templates/index.html`) uploads the pair and renders
the result, headline first. The headline is the pair match: whether the p12's
leaf appears in the profile's `DeveloperCertificates` (TN3125 "The who"), which
is the one thing that decides whether the pair can legitimately sign at all —
and the one check the iOS Feather app never performs (086 §8c). Below it: the
certificate (CN, subject, issuer, team, validity, Code Signing EKU, chain
length), the profile (name, app ID, team, application identifier, UUID,
creation/expiry), the entitlement keys, and the warnings.

Two things are rendered in words rather than as booleans, because that is where
the useful information is:

- **Both expiries.** The certificate's `notAfter` and the profile's
  `ExpirationDate` are independent, neither derived from the other, and either
  one kills an install.
- **Device binding, as three states.** `udid-bound` → installs only on the N
  devices listed; `all-devices` → any device; `app-store` → no device list, so
  it cannot be installed directly.

## What was deliberately not built

No signing, no `zsign`/`ldid`, no subprocess, no stored passphrase, no stored
p12. Storing the pair server-side would put a reusable code-signing identity
behind a single shared admin password, with nothing here to encrypt it under —
`SECRET_KEY` is regenerated per boot when unset, and using it would fuse
"can forge a session" with "can recover the signing identity" (086 §8d).
Revocation is not checked: it needs the network (OCSP), and the route is
deliberately offline.

## Dependency

`cryptography==50.0.1` added to `requirements.txt`, the first cryptographic pin
in the file. It is unavoidable: nothing in the standard library decrypts
PKCS#12. `asn1crypto` was considered for structural CMS parsing and rejected —
the cleartext-payload slice needs no ASN.1 parser.

## Verification

- `tests/test_certificate_inspection.py`: 16 tests. Fixtures are real — a
  self-signed leaf and a real PKCS#12 minted in-process, and a profile plist
  behind a CMS-shaped prefix/suffix. Covers the match, a stranger certificate
  that is not a match, wrong passphrase vs not-a-p12 vs not-a-profile, a
  passphraseless p12, the three device-binding states, both expiries reported
  separately, a missing Code Signing EKU, the size cap, and the route
  (verdict + no files left behind + wrong passphrase + missing file + 401).
- Full suite: **381 passed, 1 skipped** (365 before).
- Live smoke test against a real server on port 5099 with a temp `DATA_DIR`,
  driven in a browser: matched pair (headline green, UDID-bound wording with
  the device count), mismatched pair (headline red), wrong passphrase
  (`WRONG_PASSPHRASE` surfaced, not a generic failure), and an expired
  certificate + expired profile (both flagged, `all-devices` wording). The
  passphrase field is cleared after a successful inspection.
