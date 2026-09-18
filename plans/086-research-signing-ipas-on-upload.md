# Plan 086 (research): Signing IPAs on upload — what it would actually take

> **Status**: RESEARCH — no code written.
> **Question**: What would it take for Feather to hold a signing certificate and sign uploaded `.ipa` files server-side on upload, instead of serving them unsigned and letting the device re-sign?
> **Date**: 2026-09-18.
> **Method**: primary sources only — Apple's own documentation and licence agreements, the source of `zsign`/`ldid`/`AltStore`/`SideStore` at the commits named below, and this repo. Every claim is cited inline; anything I could not verify in a primary source is marked **UNVERIFIED**.

Short answer up front: for the two clients this catalogue actually serves, signing on upload is wasted work — both unconditionally discard whatever signature the IPA already carries and re-sign it with the *subscriber's* own Apple identity. The signing step itself is impossible in this container (`codesign` is macOS-only, by Apple's own words), and doing it with the operator's own certificate for arbitrary subscribers is exactly the use the Apple Developer Program and Enterprise licences forbid. The one thing that would technically work — an in-house/Enterprise profile, which is the only profile type not bound to a UDID list — is the one Apple licenses most narrowly.

---

## 1. What iOS actually requires of a signed app

**A signature is mandatory, and its absence is a hard failure.** Apple: *"As a security measure, iOS refuses to launch an app that has a missing or invalid signature."* — [Using the latest code signature format](https://developer.apple.com/documentation/xcode/using-the-latest-code-signature-format). That is the whole of the requirement: not "a signature that says a particular team", but a signature that is *internally consistent* with the files present ([TN3161 §Verify a code signature](https://developer.apple.com/documentation/technotes/tn3161-inside-code-signing-certificates): *"it says that the code is internally consistent"* — all expected files present, no extra files, nothing modified, and a basic X.509 trust evaluation of the leaf certificate succeeded).

**What a signed `.app` contains.** Per [TN3126 §Code signature storage](https://developer.apple.com/documentation/technotes/tn3126-inside-code-signing-hashes), for a *"bundle wrapped around a Mach-O image, the code signature is stored within the image using the `LC_CODE_SIGNATURE` load command"*; for a bundle with no Mach-O image it is a `_CodeSignature` directory holding `CodeResources`, `CodeDirectory`, `CodeRequirements`, `CodeRequirements-1`, `CodeSignature`. The central structure is the **code directory**, which holds *"all of the info about the code that was signed"* and whose hashes *"seal the executable pages, resources, and metadata"*. The signature itself *"uses Cryptographic Message Syntax"* (CMS / RFC 5652).

The three pieces an upload pipeline has to get right:

| Piece | Where | What Apple says |
|---|---|---|
| `_CodeSignature/CodeResources` | bundle root, sibling of the executable | TN3126 §Resources: *"Slot -3 in the code directory holds the hash of that file … So, if that file changes, the code directory hash changes and you break the seal."* It is *"a property list with four top-level dictionaries: `files`, `files2`, `rules`, and `rules2`. Amusingly, three out of four of these items are vestigial. The one that matters is `files2`."* `files` is SHA-1, *"present for compatibility purposes"*. |
| `embedded.mobileprovision` | `MyApp.app/embedded.mobileprovision` | TN3125 §Profile location: *"Other Apple platforms expect to find the profile at `MyApp.app/embedded.mobileprovision`."* The profile is *"a property list wrapped within a Cryptographic Message Syntax (CMS) signature"*, ties together *"who/what/where/when/how"*, and its `Entitlements` property *"act[s] as an allowlist. This isn't the same as the entitlements claimed by the app … Every entitlement claimed by the app must be in the profile's allowlist."* |
| Entitlements + DR | CD special slots | TN3126 §Special slots: slot **-1** = `Info.plist` hash, **-3** = resources, **-5** = entitlements. TN3127 §Designated requirement: *"Most code has a designated requirement (DR) … The DR is part of the code signature, making it an internal requirement."* |

**The DER trap.** TN3125 §The future is DER and the Xcode page both state that from **iOS 15** the system requires DER-encoded entitlements: *"Apps signed with a previous signature format will not launch"*, and *"If -5 contains a value and -7 contains a zero value, or is not present, you need to re-sign your app to include the new DER entitlements."* Any hand-rolled signer has to emit `DER-Encoded-Profile` into the profile and a DER entitlements blob into the CD, or produce artifacts that a modern device will not launch.

**What `codesign` does, and where it runs.** It has three operations — sign, display, verify — and its *input on the signing side is a "code-signing identity"*, i.e. a certificate **plus** the matching private key, conventionally packaged as PKCS#12 (TN3161 §Digital identity: *"As a certificate only contains a public key, you can't use it to sign code."*; RFC 7292 is the PKCS#12 spec, cited there). Identity lookup happens *"in a keychain that is on the calling user's keychain search list"* — a macOS keychain concept (TN3137, [On Mac keychains](https://developer.apple.com/documentation/technotes/tn3137-on-mac-keychains)).

**`codesign` is macOS-only, stated by Apple.** All four *Inside Code Signing* technotes carry the identical Important aside:

> "When signing code, use Xcode (all platforms) or the `codesign` tool (macOS only). To get information or validate a code signature, use the `codesign` tool or the Code Signing Services API."
> — TN3125, TN3126, TN3127, TN3161 §Overview

And the same *"Don't encode this information in your product"* note continues into the strongest single statement in this whole note:

> "The exact format of provisioning profiles isn't documented and could change at any time. Use the techniques shown here for understanding and debugging purposes. **Avoid building a product based on these details**; if you do build such a product, be prepared to update it as the Apple development story evolves."
> — TN3125 §Unpack a profile

and, on re-signing specifically:

> "Only re-sign apps from the command-line as a last resort to update your code signature to include the DER entitlements. **Re-signing apps from the command-line is not supported for iOS**, iPadOS, tvOS, visionOS, and watchOS."
> — [Using the latest code signature format](https://developer.apple.com/documentation/xcode/using-the-latest-code-signature-format)

**The `security(1)` tool** is used throughout the same technotes for the keychain and CMS operations (`security cms -D -i Profile.mobileprovision`, `security find-identity -p codesigning`, TN3125 §Unpack a profile, TN3161 §Sign code). Apple labels `codesign` "(macOS only)" explicitly; it does **not** write the same words about `security` — **UNVERIFIED** as a verbatim statement. [INFERENCE] `security(1)` is part of the same macOS/Xcode toolchain and its documented subject matter is the macOS file-based keychain (TN3137), so the conclusion is the same, but the citable quote is only for `codesign`.

**Does this container have it? No.** `feather/Dockerfile:1` is `FROM python:3.11-slim`, final `USER altstore` (`Dockerfile:34`), and `CMD ["python", "app.py"]` (`Dockerfile:44`) started under Waitress (`app.py:5295-5296`). There is no macOS, no Xcode, no `codesign`, no keychain. **The signing step cannot happen in-process with Apple's own tools, and Apple's tools are the only ones it supports.** The alternatives are the third-party re-signers of §3 (which Apple explicitly does not support for iOS), or moving the signing to the Mac that already produces the artifact.

---

## 2. Certificate and profile types — which one could produce a generally-installable IPA?

The answer is "none of them, legally" — but the mechanics matter, because they determine whether server-side signing could even work.

### 2a. Free / personal team (no membership)

[Developer account overview](https://developer.apple.com/help/account/basics/about-your-developer-account) (which is where `https://developer.apple.com/support/compare-memberships/` now redirects to):

> "You can register up to **10 App IDs, which expire after 7 days**."
> "You can register up to **3 devices, which expire after 7 days**."
> "You can install up to **3 apps per device**. Provisioning profiles that enable apps to be installed on a device will **expire 7 days from issuance**. You'll need to rebuild and reinstall your app to your device after expiration."

Mechanically: *"Your account's App IDs, devices, certificates, and provisioning profiles are managed directly in Xcode"* (same page). There is no server-side representation of a personal team: the certificate is minted inside that user's Xcode and the profile lists that user's three devices. A server cannot hold "the subscriber's" personal-team certificate without the subscriber's Apple Account credentials — and the Apple Developer Program License Agreement (ADP LA, [PDF](https://developer.apple.com/support/downloads/terms/apple-developer-program/Apple-Developer-Program-License-Agreement-English.pdf)) §2.1 forbids soliciting exactly that: *"you agree not to solicit or request Apple Developer Program members to provide You with their Apple Accounts, authentication credentials, and/or related account information and materials (e.g., Apple Certificates used for distribution or submission to the App Store or TestFlight)"*.

### 2b. Paid Apple Developer Program

Certificate types ([Certificates overview](https://developer.apple.com/help/account/certificates/certificates-overview) §Certificate types): *"Apple Development — Run an iOS, iPadOS, macOS, tvOS, visionOS, watchOS app on devices and use certain app services during development."* and *"Apple Distribution — Distribute your iOS, iPadOS, macOS, tvOS, visionOS, watchOS app on devices **on designated devices for testing** or submit it to App Store Connect."*

Validity: *"For Apple code-signing certificates that's typically a year from the date of issue, although the exact duration varies based on the certificate type"* (TN3161 §Certificate expiration). Profiles: *"Every profile has an `ExpirationDate` … typically not more than a year"* (TN3125 §The when).

**Device binding is the crux.** A development or ad-hoc distribution profile carries `ProvisionedDevices`:

> "Most profiles apply to a specific list of devices. This is encoded in the `ProvisionedDevices` property …"
> — TN3125 §The where

and:

> "To create a provisioning profile containing a subset of devices … you select which registered devices to include. … **Testers can run your app only on the devices that you add to the provisioning profile.**"
> — [Distributing your app to registered devices](https://developer.apple.com/documentation/xcode/distributing-your-app-to-registered-devices)

with a hard ceiling of *"up to **100** … devices, per product family, per membership year"* ([Devices overview](https://developer.apple.com/help/account/devices/devices-overview)).

So an IPA signed on upload with a Development/Ad-Hoc identity installs **only on the ≤100 registered devices already in that profile**, and fails everywhere else. That is strictly *worse* than unsigned, which is the same conclusion the sibling project already recorded for its own build pipeline (`mangasync/docs/distribution.md:56-58`: *"a Development-signed one would be worse than wasted: it is bound to the provisioning profile's device list, so it would install on the phones already in that profile and fail everywhere else"*; `mangasync/docs/distribution.md:45-54` builds **unsigned** with `CODE_SIGNING_ALLOWED=NO`).

### 2c. Apple Developer Enterprise Program (in-house)

This is the only certificate type whose profile is *not* UDID-bound — hence the only one that could in principle yield an IPA installable on an arbitrary subscriber's device:

> "Developer ID and **In-House (Enterprise) distribution profiles have the `ProvisionsAllDevices` property, indicating that they apply to all devices.**"
> — TN3125 §The where

and Apple's own [Enterprise Program page](https://developer.apple.com/programs/enterprise/) forbids precisely the use that fact enables:

> "The Apple Developer Enterprise Program allows large organizations to develop and deploy proprietary, **internal-use** apps to their employees."
> "**Have 100 or more employees.**"
> "**Use the program only to create proprietary, in-house apps for internal use, and to distribute these apps privately and securely to employees within the organization.**"
> "Have systems in place to ensure **only employees can download your internal-use apps**, and to protect membership credentials and assets."
> (USD 299/year.)

The Apple Developer Enterprise Program License Agreement (public PDF, [here](https://developer.apple.com/support/downloads/terms/apple-developer-enterprise-program/Apple-Developer-Enterprise-Program-License-Agreement-English.pdf)) is explicit: *"§2.1(g): Except as set forth in Section 2.1, You may not use, distribute or otherwise make Your Internal Use Applications available to any third parties in any way."*; *"§7 No Other Distribution: Except for internal deployment of Your Internal Use Application to employees or Permitted Users … no other distribution of programs or applications developed using the Apple Software is authorized or permitted."*; and the definition of an Internal Use Application requires it be *"solely for internal use (e.g., **not downloadable on a public website**) by Your employees or Permitted Users"*.

### 2d. Where the terms forbid the pattern, plainly

Three clauses together close the door on "sign once with our certificate, serve to anyone who subscribes to the source":

- **ADP LA §7.3** — Ad-hoc distribution is *"to individuals within Your company, organization, educational institution, group, or who are otherwise affiliated with You"*, on *"a limited number of Registered Devices"*, where **"Registered Devices"** means *"Apple-branded hardware units owned or controlled by You, or owned by individuals who are affiliated with You"*. An anonymous source subscriber is neither.
- **ADP LA §7.6** — *"In the absence of a separate agreement with Apple, You agree not to distribute Your Application for iOS … to third parties via other distribution methods or to enable or permit others to do so."*
- **ADP LA §5.1(d)** — *"You will not provide or transfer Apple Certificates or keys provided under this Program to any third party … and **You will not use Your Apple Certificates to sign any third party's application**, pass, extension, notification, implementation, or site"*, reinforced by §5.1(b) (*"solely responsible for preventing any unauthorized person or organization from having access to Your Apple Certificates and keys"*), and by [Certificates overview](https://developer.apple.com/help/account/certificates/certificates-overview): *"Do not share Apple Certificates outside of your organization."*

### 2e. Revocation

- *"Provisioning profiles that contain a revoked certificate become invalid."* — [Revoke a certificate](https://developer.apple.com/help/account/certificates/revoke-a-certificate)
- *"iOS Distribution Certificate (in-house, internal-use apps): **Users will no longer be able to run apps that have been signed with this certificate.** You must distribute a new version of your app that is signed with a new certificate."* — [Certificates overview](https://developer.apple.com/help/account/certificates/certificates-overview) §Expired or revoked certificates
- *"Apple can revoke digital certificates at any time at its sole discretion."* — same page

Practical consequence: a single server-held certificate becomes a single point of failure for the whole catalogue. Losing it, or having Apple revoke it, invalidates every artifact the server ever signed — the same blast radius the F-Droid keystore already has, but for iOS there is no "re-add the repo" recovery: the user's already-installed apps stop launching.

---

## 3. The tools that sign on Linux

Two candidates were read at source. Both are third-party reimplementations of an undocumented, Apple-unsupported format (§1).

### 3a. `zsign` — [github.com/zhlynn/zsign](https://github.com/zhlynn/zsign), master @ `614caa8`

**Licence: MIT.** `LICENSE:1-3` ("MIT License / Copyright (c) 2026 zhlynn"). Vendoring or invoking it is not a licence problem.

**Inputs** (`src/zsign.cpp:25-57` option table, `:200` getopt string `"dfva2LhiqwCRSEWUPc:k:m:o:p:e:b:n:z:l:D:t:r:x:M:I:"`):

| Flag | Meaning | Line |
|---|---|---|
| `-k` | private key **or p12** file (PEM or DER) | `src/zsign.cpp:212-214` |
| `-c` | certificate file | `src/zsign.cpp:209-211` |
| `-m` | provisioning profile; repeatable, one per app extension | `src/zsign.cpp:215-218` |
| `-p` | password for the key or p12 | `src/zsign.cpp:222-224` |
| `-e` | replacement entitlements plist | `src/zsign.cpp:234-236` |
| `-o` | output `.ipa` | `src/zsign.cpp:249-251` |
| `-a` | ad-hoc (no certificate) | option table |

**p12 handling — OpenSSL.** `ZSignAsset::Init` (`src/openssl.cpp:828`): `PEM_read_bio_PrivateKey` (`:880`) → `d2i_PrivateKey_bio` (`:883`) → `d2i_PKCS12_bio` (`:887`, preceded by `OSSL_PROVIDER_load(NULL, "legacy")` at `:886`) → **`PKCS12_parse(p12, strPassword.c_str(), &evpPKey, &x509Cert, &caCerts)`** (`:890`). CMS is built with `CMS_add1_signer` (`src/openssl.cpp:446`), gets the Apple-specific attribute OID `1.2.840.113635.100.9.1` (`:479-491`), and is serialised with `i2d_CMS_bio` (`:506`).

**The password can only come from `argv`.** `case 'p': strPassword = optarg;` (`src/zsign.cpp:222-223`); it is passed straight into `Init`. There is no file, stdin, or environment-variable path in the tool. A subprocess invocation therefore writes the p12 passphrase into the container's process table (`/proc/<pid>/cmdline`), where it is readable by anything that can exec in that container and is captured by a `ps` in any diagnostic dump. This is the opposite of Feather's existing F-Droid pattern (§6), which uses `keytool -storepass:env FDROID_KEYSTORE_PASSWORD` (`scripts/fdroid_index_loop.sh:22-24`) — a *reference* to an environment variable, never the value in `argv`.

**What it emits.** A `CSMAGIC_EMBEDDED_SIGNATURE` superblob written into `__LINKEDIT` (`src/archo.cpp:327`, `:532`, `:600`; `LC_CODE_SIGNATURE` added if missing at `:636-650`) containing `CSSLOT_CODEDIRECTORY`, `CSSLOT_REQUIREMENTS`, `CSSLOT_ENTITLEMENTS`, `CSSLOT_DER_ENTITLEMENTS`, `CSSLOT_ALTERNATE_CODEDIRECTORIES`, `CSSLOT_SIGNATURESLOT` (`src/archo.cpp:491-526`), the CMS via `CSMAGIC_BLOBWRAPPER` (`src/signing.cpp:735`). CodeDirectory version `0x20400` (`src/signing.cpp:472`), page size 12 (`:488`). It regenerates `_CodeSignature/CodeResources` emitting **both** formats in one file — `files` (SHA-1) at `src/bundle.cpp:214-218` and `files2` (SHA-1 + SHA-256 `hash`/`hash2`) at `:222-226`, plus `rules`/`rules2` at `:230-252` — and writes it at `src/bundle.cpp:424-466`. It writes `embedded.mobileprovision` when a profile was supplied (`src/bundle.cpp:826-831` for the app, `:413-418` per matched nested bundle), emitting it **before** CodeResources because the seal covers every file (`:403-406`).

Nested-code handling is real: `GetObjectsToSign` walks `.app`/`.appex`/`.framework`/`.xctest` deepest-first (`src/bundle.cpp:86`, `:98-111`) plus every file with a Mach-O/FAT magic (`:124-148`), and `-P` injects dylibs into `PlugIns/`/`Extensions/` too (`:296-306`).

**Stated limitations: none that are quotable.** The repo's own README contains no limitations section — no statement about CodeResources v1/v2 gaps, nested frameworks, extensions, or entitlement propagation. Its only scope claim is *"a fast, open-source, cross-platform `codesign` alternative for **iOS 12+**"* (README, opening paragraph; also the usage banner at `src/zsign.cpp:122`). Anything beyond what the code shows above is **UNVERIFIED**.

### 3b. `ldid` — [github.com/ProcursusTeam/ldid](https://github.com/ProcursusTeam/ldid), master @ `af86971`

**Licence: GNU Affero GPL v3** (`COPYING:1-2`; `docs/ldid.1:2` declares `SPDX-License-Identifier: AGPL-3.0-or-later`). This is a material difference from `zsign` and would need a licensing decision before Feather vendors or links it — the AGPL's §13 network-use condition is the clause that makes a server-side tool different from a desktop one. Whether invoking an *unmodified* binary as a separate process triggers it is a legal question, not a code question; **I am not asserting an outcome, only that it must be reviewed before adoption.**

**`ldid -S` is a pseudo-signer, not a signer.** The parser's default signer is `new NoSigner()` (`ldid.cpp:3561`); `NoSigner` converts to null key/cert and, decisively, `operator bool() const { return false; }` (`ldid.cpp:1863-1865`). In `Sign(...)`, insertion of the CMS slot is guarded by `if (signer)` (`ldid.cpp:2734`), so with the default the emitted superblob is a CodeDirectory plus hashes and **no signature slot** (`ldid.cpp:2778`) — which is exactly what the man page means by `-S` "Pseudo-sign" (`docs/ldid.1:139-140`) and by *"adds SHA1 and SHA256 hashes … validation, but not signature verification"* (`docs/ldid.1:36-37`).

**It can produce a real CMS signature — only with `-K key.p12`.** `-K` selects `P12Signer` (`ldid.cpp:3812`), which does `d2i_PKCS12_bio` (`:1877`), `PKCS12_verify_mac(value_, "", 0)` (`:1887`), then — unless `-U password` was passed — **prompts interactively** with `EVP_read_pw_string(passbuf, 2048, "Enter password: ", 0)` (`:1889`), before `PKCS12_parse` (`:1893`). `-U password` is again an `argv` value (documented as *"This is a Procursus extension"*, `docs/ldid.1`), so the password-in-`argv` problem is the same as zsign's. The PKCS#7/CMS assembly is at `ldid.cpp:2156-2227`, including the same Apple attribute OID at `:2175-2225`.

**It regenerates `CodeResources` but never touches `embedded.mobileprovision`.** The `_CodeSignature/CodeResources` constants and the v1+v2 rule sets are at `ldid.cpp:3217-3218`, `:3220-3252`; nested-bundle cdhash/requirement entries at `:3413-3420`; the write at `:3445-3453`. A search for `mobileprovision` across `ldid.cpp` returns **nothing** — the only provision-related rule is the macOS filename rule `^embedded\.provisionprofile$` in `rules2` (`ldid.cpp:3251`). **An ldid-only pipeline therefore cannot produce an IPA that a device will accept as a provisioned app**: the `embedded.mobileprovision` half of §1's requirement is simply not implemented. That is a decisive functional gap, independent of the licence.

### 3c. The unavoidable framing

Apple's own words in TN3125 — *"Avoid building a product based on these details"* and *"Re-signing apps from the command-line is not supported for iOS"* — describe precisely what §3a and §3b are. Adopting either means owning a maintenance liability against a format Apple documents as unstable, with no support path, and with the DER-entitlements breakage of iOS 15 as the concrete precedent.

---

## 4. What the clients expect

**Both AltStore and SideStore re-sign unconditionally. Signing on upload changes nothing on the install path.**

### AltStore ([altstoreio/AltStore](https://github.com/altstoreio/AltStore), `develop`)

The install is a fixed, unconditional operation group:

```swift
// AltStore/Managing Apps/AppManager.swift:1353
let operations = [downloadOperation, verifyOperation, deactivateAppsOperation,
                  patchAppOperation, refreshAnisetteDataOperation,
                  fetchProvisioningProfilesOperation, resignAppOperation,
                  sendAppOperation, installOperation]
```

with `resignAppOperation.addDependency(fetchProvisioningProfilesOperation)` (`AppManager.swift:1310`) and `sendAppOperation.addDependency(resignAppOperation)` (`:1323`). There is no branch anywhere in that group on the *downloaded file's* signature state.

- `DownloadAppOperation` treats the download as opaque bytes: `session.downloadTask(with: sourceURL)` then `FileManager.default.unzipAppBundle(at:toDirectory:)` (`AltStore/Operations/DownloadAppOperation.swift`, `downloadIPA`). It performs no signature inspection. The only reader of `embedded.mobileprovision` anywhere in the stack is `ALTApplication.provisioningProfile` (`AltSign/AltSign/Model/ALTApplication.mm:195-204`), which reads it lazily as metadata and is never consulted to decide *whether* to sign.
- `ResignAppOperation` copies the bundle to `App.app`, rewrites `Info.plist`, and always calls `ALTSigner(team:certificate:).signApp(at:provisioningProfiles:)` (`AltStore/Operations/ResignAppOperation.swift`, `resignAppBundle`, `:250-251`).
- **The client has to re-sign even a perfectly signed IPA**, because it has already invalidated it: `prepareAppBundle` rewrites `CFBundleIdentifier` to the *profile's* bundle ID, records the original in `ALTBundleID`, appends its own URL scheme, and adds an exported UTI (`ResignAppOperation.swift:100+`). After that, an incoming signature's seal no longer matches the bundle it covers.
- **There is no as-is fallback if resigning fails.** Both consumers require the resigned artifact: `SendAppOperation` returns early on `context.error` and then `guard let resignedApp = self.context.resignedApp …` (`AltStore/Operations/SendAppOperation.swift:36-42`); `InstallAppOperation` guards `let resignedApp = self.context.resignedApp` (`AltStore/Operations/InstallAppOperation.swift:42-46`). A resign failure aborts the install — the original IPA is never installed. (`InstalledApp.needsResign` is unrelated: it is set `false` after a successful install at `InstallAppOperation.swift:72` and is local profile bookkeeping.)
- **The pre-install verification step checks everything *except* the signature.** `VerifyAppOperation.main` compares the source's bundle identifier, the published `sha256` (a no-op when the source omits a hash), the bundle's `version`/`buildVersion`, and the entitlements + privacy keys — and never inspects the code signature. That last item is the only place an upload-time signature could still have consequences (§4c below).
- `ALTSigner` performs the signature with a **vendored `ldid`**: `ldid::Sign("", appBundle, key, "", …)` where `key` is a PKCS#12 *built in memory* from the user's certificate plus Apple's chain shipped as `AltSign/Resources/apple.pem`, and where the per-file entitlements come from the user's own provisioning profile (`AltSign/AltSign/Signing/ALTSigner.mm`). Immediately before signing it writes the user's profile straight over the existing one: `[profile.data writeToURL:profileURL atomically:YES]` where `profileURL = [app.fileURL URLByAppendingPathComponent:@"embedded.mobileprovision"]`. The vendored file is `AltSign/AltSign/ldid/alt_ldid.cpp`, which `#include`s `../../Dependencies/ldid/ldid.cpp`.

So: the client does not strip-and-resign *conditionally*, and does not refuse an already-signed IPA, and does not install it as-is. **It overwrites both the code signature and the embedded profile with the subscriber's own identity.**

### SideStore ([SideStore/SideStore](https://github.com/SideStore/SideStore), `develop`)

Same shape, and now explicit about the strip. `SideStore/Core/Operations/PipelineOperations/ResignAppOperation.swift`, in `prepare(_:bundleID:…)`:

```swift
// Remove _CodeSignature folder (if it exists) because it will be added when resigning and it may have files
// that aren't overwritten when resigning
// These files might be the cause of some ApplicationVerificationFailed errors
let codeSignatureURL = appBundle.fileURL.appendingPathComponent("_CodeSignature")
if FileManager.default.fileExists(atPath: codeSignatureURL.path) {
    try FileManager.default.removeItem(at: codeSignatureURL)
}
```

then `signer.signApp(at: fileURL, provisioningProfiles: profiles, …)`.

SideStore is the easier of the two to audit because the pipeline is a declared list rather than a hand-built graph: `PipelineStepDefinition.install` (`SideStore/Core/Operations/OperationStepDefinition.swift:39-59`) contains `.resignApp` at `:52` between `.fetchProvisioningProfiles` and `.createIPA`, with `.sendApp` at `:55` and `.installApp` at `:56` — no conditional guards the resign step — and `PipelineExecutor` maps `.resignApp → ResignAppOperation` and assigns `context.resignedAppBundle` (`SideStore/Core/Operations/PipelineExecutor.swift:142-147`). Downstream, `SendAppOperation` throws unless `context.resignedAppBundle` is set (`SideStore/Core/Operations/PipelineOperations/SendAppOperation.swift:26-28`) and `InstallAppOperation` throws unless the resigned bundle exists *and* carries a provisioning profile (`InstallAppOperation.swift:38-51`). No fallback to the raw download.

One piece of supporting evidence that the client regards a signature as irrelevant to app identity: SideStore's cache key deliberately excludes signing artefacts. `AppBundleFingerprint.compute` skips `_CodeSignature` and `embedded.mobileprovision` when hashing the bundle (`SideStore/Utils/common/AppBundleFingerprint.swift:24-26`, cited by `CacheAppOperation.swift:30-38`).

**SideStore's pairing file and local VPN are transport, not credentials.** `.gitmodules` pulls `Dependencies/minimuxer`; `MinimuxerWrapper` selects `.localVPN` vs `.remoteServer` (`SideStore/Core/DeviceApi/MinimuxerWrapper.swift:112-129`) and drives `sendIpaAfc`/`sendAppBundleAfc`/`installIPA` (`:245-281`). The signing identity is still an Apple-ID certificate with a profile fetched from Apple's portal: `FetchProvisioningProfilesOperation` calls `AuthManager.shared.getAuthenticatedTeam()` and `DeveloperPortalProxy.shared.downloadProvisioningProfile(for:deviceType:team:)` (`SideStore/Core/Operations/PipelineOperations/FetchProvisioningProfilesOperation.swift:51`, `:187`). So SideStore removes the *desktop* requirement, not the *per-user Apple identity* requirement — a server-side signature would still be discarded.

SideStore's `CodeSignValidator.swift` validates the signature of *SideStore's own running installation* against the current certificate (expired / revoked / differentTeam / differentAccount / privateKeyLost / externalSigner) — it is about SideStore's self-renewal, not about the downloaded app, and it cannot cause a refusal to re-sign one.

### Does the source schema have a "signed" field? No.

The decoded field set is fixed at the client's decoder. **AltStore** `AltStoreCore/Model/AppVersion.swift`:

```swift
private enum CodingKeys: String, CodingKey {
    case version; case buildVersion; case date; case localizedDescription
    case downloadURL; case size; case sha256; case minOSVersion; case maxOSVersion
}
```

**SideStore** `AltStore/Core/Model/AppVersion.swift` has the identical set. The same is true one level up in both clients: AltStore's `Source` decoder keys are `name, identifier, sourceURL, subtitle, localizedDescription, iconURL, headerImageURL, websiteURL, tintColor, apps, news, featuredApps, userInfo` (`AltStoreCore/Model/Source.swift:114-130`) and its `StoreApp` keys are `name, bundleIdentifier, developerName, localizedDescription, version, versionDescription, versionDate, iconURL, screenshotURLs, downloadURL, tintColor, subtitle, permissions, size, isBeta, versions` (`AltStoreCore/Model/StoreApp.swift:122-140`). SideStore's are a superset of the same. No `signed`, `isSigned`, `unsigned`, `requiresResign`, or equivalent anywhere at source, app, or version level.

The published schema agrees: the App Versions section of [AltStore's own documentation](https://faq.altstore.io/developers/make-a-source) lists `version`, `buildVersion`, `marketingVersion`, `date`, `localizedDescription`, `downloadURL`, `size`, `assetURLs`, `minOSVersion`, `maxOSVersion`. The only `signature` key in the schema is inside `assetURLs`, and that exists solely for **AltStore PAL** notarized ADPs — Apple-distributed artifacts, a different distribution channel entirely. A source author therefore **cannot** signal "this IPA is pre-signed, install it as-is"; the concept does not exist in the protocol.

(Two citation notes: `https://github.com/rileytestut/AltStore` now redirects to `https://github.com/altstoreio/AltStore`, and the repo has **no** `Documentation/Source.md` on `develop` — `GET .../contents/Documentation?ref=develop` is a 404. The authoritative schema document is the first-party FAQ repo that backs `faq.altstore.io`, `altstoreio/FAQ/developers/make-a-source.md`.)

### 4c. The one place this could bite

AltStore's docs: *"For security purposes, AltStore requires that sources list **all** entitlements and privacy permissions for every app. These will be checked against the downloaded `.ipa`, and AltStore will refuse to install any app whose permissions do not match."* ([make-a-source](https://faq.altstore.io/developers/make-a-source) §App Permissions).

The check is real and I read it. `VerifyAppOperation.verifyPermissions` collects `app.entitlements.keys` — which in AltSign is read **out of the code signature** — across the app and every app extension, plus every `NS*UsageDescription` key from the Info.plists, filters out `.applicationIdentifier` and `.teamIdentifier`, and then requires *"EVERY permission in localPermissions must also appear in sourcePermissions"*, throwing `VerificationError.undeclaredPermissions` otherwise (`AltStore/Operations/VerifyAppOperation.swift`, `verifyPermissions(of:match:)`). It runs on the **downloaded** bundle, before resigning.

So an upload-time signature *is* read by the client — just not for trust, only for its entitlement set. Feather currently publishes an empty list (`app.py:240`, `app.py:1456`, `app.py:2457` — `{"entitlements": [], "privacy": …}`), which today matches an unsigned artifact. If a signing step stamped a **non-empty** entitlement set (which is the normal case: `zsign` propagates the profile's `application-identifier` and its `Entitlements` dictionary, `src/openssl.cpp:861-866`), then either the published `appPermissions` must be kept exactly in sync or installs will fail the check.

Two caveats, both from source: the check can be switched off by the user (`UserDefaults.shared.permissionCheckingDisabled ? .none : permissionReviewMode`, `AltStore/Managing%20Apps/AppManager.swift:1145`), and the mode is per-call — the batch path passes `.all` for a fresh install and `.added` for an update (`AppManager.swift:986`, `:992`), while `_install`'s own default is `.none` (`:1085`), and `verifyPermissions` returns immediately on `.none`. So this is a real but bypassable consequence — not a reason to sign, and a reason to be careful if anyone ever does.

Relatedly, `verifyHash` compares the source's published `sha256` against the downloaded bytes (`VerifyAppOperation.swift`), and `verifyDownloadedVersion` compares the bundle's `version`/`buildVersion` with the source entry. Feather publishes no `sha256` today, so the hash check is a no-op here — but it means a signing step must not rewrite `CFBundleShortVersionString`/`CFBundleVersion`, which `zsign` will do on request (`-r, --bundle_version  New bundle version`, its README's usage block), or the catalogue and the client will disagree.

**Conclusion for §4**: for AltStore and SideStore, signing on upload is pure waste. The subscriber's device always trusts the *client's* signature, never Feather's. The only scenario where it would matter is a client that installs the bytes as-is — and neither of these does.

---

## 5. Feather's own code path, and where a signing step would sit

### The four (five) ingest paths

All of them converge on two routes and one storage choke point. This is the single most useful fact in the plan.

**1. Admin UI / HTTP API.** `POST /api/add-app` → `add_app()` (`app.py:2429`), `POST /api/add-version` → `add_version()` (`app.py:2569`). Both read the multipart field `ipaFile` (`app.py:2459`, `app.py:2575`) or `downloadFromUrl=true` + `downloadURL`, and both end in `source_manager.add_app_manual(...)` (`app.py:2469`) / `source_manager.add_version(...)` (`app.py:2602`).

**2. Telegram ingest worker.** `scripts/telegram_bot_ingest.py` — the bot reads the file off the self-hosted Bot API volume, validates it (`inspect_ipa_metadata` at `:316`, called from `:356` and `:581`), computes its sha256 (`:571`), and publishes **over HTTP** to the same two routes: `FeatherClient.add_version` posts `files={"ipaFile": …}` to `/api/add-version` (`scripts/telegram_bot_ingest.py:444-453`) and `add_app` to `/api/add-app` (`:478-484`).

**3. Scheduled release importer.** `scripts/release_source_ingest.py` — downloads the release asset (`stream_download`), preflights it (`inspect_ipa_metadata` at `:883`, called at `:944` and `:1303`), then publishes over HTTP: `FeatherClient.add_version` → `/api/add-version` (`scripts/release_source_ingest.py:1015`) and `add_app` → `/api/add-app` (`:1047`).

**4. In-process auto-import scheduler.** `_run_auto_import_ios_candidate` (`app.py:3243`): downloads to a temp file under `UPLOAD_FOLDER` (`app.py:3255`), preflights (`app.py:3262`), then wraps the temp file in a Werkzeug `FileStorage` and calls `source_manager.add_version(..., ipa_file=fs)` (`app.py:3288-3290`), falling back to `add_app_manual` (`app.py:3320`, `:3329`, `:3332`).

**5. The UI "import release" flow** is the same shape in-process: `/api/import-release` (`app.py:3791`) downloads to a temp file and wraps it at `app.py:3864-3866`.

So there are five entry points, four of which are HTTP clients of the other two routes.

### Where the bytes actually land

`SourceManager.save_ipa_file` (`app.py:1038-1075`) is the **single choke point for uploaded bytes** — it ends at `ipa_storage.put(file, bundle_id, version)` (`app.py:1063`), or at the `dest_path` staging write used by `update_version` (`app.py:1051-1062`). `SourceManager.download_ipa_from_url` (`app.py:1076-1140`) is the same for URL-sourced bytes, ending at `ipa_storage.put(filepath, ...)` (`app.py:1121`).

Call sites of those two methods: `app.py:1401`/`1408` (in `add_app_manual`, `app.py:1371`), `app.py:1677`/`1684` (in `add_version`, `app.py:1637`), and `app.py:1753`/`1769` (in `update_version`, `app.py:1710`). Every ingest path above reaches storage through one of these.

Backends (`app.py:2256-2258`): `LocalIpaStorage.put` (`app.py:362`) writes via `FileStorage.save` / `os.replace` into `IPA_FOLDER` (`app.py:80`); `GarageIpaStorage.put` (`app.py:511`) uploads with `upload_file`/`upload_fileobj` (multipart, not buffered whole) and verifies with `head_object`. Serving then either `send_file`s or 302-redirects (`serve_ipa`, `app.py:2314-2340`).

### The seam

**Exactly one: between "the bytes are complete on local disk" and `ipa_storage.put(...)`** — i.e. inside `save_ipa_file` (`app.py:1063`) and `download_ipa_from_url` (`app.py:1121`). Anywhere higher (in each of the five callers) multiplies the work fivefold and misses future callers; anywhere lower (inside `ipa_storage.put`) signs bytes that nothing re-inspects.

For the `FileStorage` case the upload is not on disk yet, so the signing step needs a staging write first — and **that pattern already exists**: `update_version` stages to `<path>.new` via `dest_path=` and only commits after the write fully succeeds, *"so a failed fetch must never destroy a binary that is still being served"* (`app.py:1744-1769`, rationale in the docstring at `app.py:1041-1062`). That is the template, and it also defines the failure semantics a signer would inherit: a failed signing must leave the previous artifact untouched and the upload rejected.

### The single-process constraint and what a re-zip costs

`app.py:5283-5296`:

```python
logging.info("Starting AltStore Source Manager (Waitress)...")
_start_auto_import_scheduler()
_start_health_monitor()
# Single process, many threads: SourceManager's in-process lock and the
# auto-import scheduler thread both assume exactly one process. Do NOT
# scale this out to multiple Waitress/gunicorn/uWSGI worker processes
# without first externalizing the lock and ensuring only one process
# owns the scheduler.
from waitress import serve
serve(app, host='0.0.0.0', port=PORT, threads=int(os.environ.get("WAITRESS_THREADS", "8")))
```

Plan 052 states the invariant as load-bearing (`plans/052-waitress-wsgi-server.md:40-46`, `:147-149`). Three concrete consequences for a signing step in this process:

1. **It would hold the global lock.** `add_app_manual`, `add_version` and `update_version` all open with `with self._lock:` (`app.py:1373`, `:1639`, `:1712`) and call `save_ipa_file` *inside* that block (`:1401`, `:1677`, `:1753`). A multi-minute unpack → re-sign → re-zip would therefore block every other catalogue mutation for its whole duration. The eight Waitress threads do not help: they would queue on the same lock.
2. **Temp space.** The bytes arrive under `UPLOAD_FOLDER` = `DATA_DIR/uploads` (`app.py:79`); a signing step needs the original, the extracted tree, and the re-zipped output simultaneously — roughly 2–3× the artifact — all under `DATA_DIR` (`app.py:77-80`), which `compose.yml:14-17` bind-mounts from the host (`./data`, `./data/ipas`, `./data/icons`, `./data/fdroid`). `data/ipas/` alone was already 1.3 GB at plan-011 time (`plans/011-garage-s3-ipa-storage.md:32`, `:47`).
3. **Size ceiling and memory.** `MAX_CONTENT_LENGTH` defaults to **2 GiB** (`app.py:129`, `.env.example:12`), enforced by Flask on the inbound multipart body (`app.py:161`) and re-checked on URL downloads (`app.py:1104`, `app.py:1190`). So a 2 GiB IPA is admissible, and signing must handle 2 GiB with a 4 GiB container memory cap (`compose.yml:25-27`). `zsign` streams and uses a temp folder (`-t`), but any pure-Python re-zip under `zipfile` would hold large buffers per concurrent request — eight threads' worth of headroom is not there.

**Also worth recording**: adopting a signer would make every ingest path's success depend on it. Today an upload that passes inspection is published; afterwards, an upload the signer cannot handle becomes a failed upload — including the automated Telegram and scheduled-release paths, which have no operator watching.

---

## 6. Key custody — what the F-Droid keystore already establishes

Feather already holds a signing key, and it already has a discipline for it. The relevant facts, all from this repo:

| Fact | Where |
|---|---|
| The keystore lives at `DATA_DIR/fdroid/keystore.p12` and is **owned by the sidecar (root); feather never reads it** | `app.py:84-93` (comment block: *"config.yml / keystore.p12   owned by the sidecar (root); feather never reads them"*) |
| It is generated on first start, `-storetype PKCS12`, 4096-bit RSA, **`chmod 600`** | `scripts/fdroid_index_loop.sh:21-25` |
| The password is read **by environment-variable reference**, never as a literal in `argv` — `keytool … -storepass:env FDROID_KEYSTORE_PASSWORD` | `scripts/fdroid_index_loop.sh:22-24` |
| The sidecar refuses to start without it: `: "${FDROID_KEYSTORE_PASSWORD:?FDROID_KEYSTORE_PASSWORD is required (the repo signing key password)}"` | `scripts/fdroid_index_loop.sh:7` |
| `fdroidserver` consumes it the same way: `keystorepass: {env: FDROID_KEYSTORE_PASSWORD}` / `keypass: {env: …}` inside `config.yml` | `scripts/fdroid_index_loop.sh:65-68` |
| Compose requires it at parse time and feeds it to the sidecar **and nothing else** — the `environment:` list contains only `FDROID_KEYSTORE_PASSWORD`, `FDROID_UPDATE_INTERVAL`, `FDROID_REPO_URL`, `FEATHER_UID`, and there is **no `env_file`** on that service | `compose.yml:134-138` |
| The sidecar runs as **root** by necessity (the upstream image is unusable by any other uid) and "its only mount is this one directory" | `compose.yml:118-127`, `plans/073-android-fdroid-repo.md:43` |
| Feather's process holds neither the key nor the password; it writes `repo-config.json`, touches `.update-requested`, and reads `fingerprint.txt` / `index-v1.json` | `app.py:84-92`, `scripts/fdroid_index_loop.sh:4-6`, `plans/073-android-fdroid-repo.md:284-291`, `:353-355` |
| The operator-facing warning is explicit about loss: *"LOSING THE KEYSTORE OR ITS PASSWORD MEANS EVERY SUBSCRIBED DEVICE MUST RE-ADD THE REPO … The sidecar receives only FDROID_* variables and FEATHER_UID, never the rest of .env."* | `.env.example:91-99` |
| The decision to do it this way was deliberate: *"let the reference `fdroidserver` build and sign the index in a sidecar rather than hand-rolling JAR signing in Python"* | `plans/073-android-fdroid-repo.md:32` |
| The web process also has a redaction list for secret *values* it might otherwise log | `app.py:49-56` (`_redact_secret`, names enumerated) |

**What the same discipline implies for an Apple p12.** By this repo's own standard the p12 does **not** belong in the web process: not in the Flask/Waitress interpreter, not in its environment, and — because both candidate signers take the passphrase as an `argv` argument (§3) — not reachable as a subprocess argument either. The F-Droid shape (key owned by a separate container, password delivered by environment reference, web process learns only the outcome plus a public fingerprint) is the pattern to copy, and `fingerprint.txt` is the exact precedent for "publish the certificate's public fingerprint, never the key".

But the analogy has a real limit, and it should not be over-sold. The F-Droid sidecar signs an **index it generates itself** from inputs Feather already owns in plaintext (`metadata/<package>.yml`, `repo/<apk>`). An Apple signature is not a separate artifact — it is *inside* each IPA (§1), so a signing sidecar would have to receive the full upload, unpack it, re-sign it, re-zip it, and hand back a different artifact, i.e. a second full-size copy of every payload across the shared volume, plus the same 2–3× temp-space and lock-hold costs from §5. And unlike the F-Droid key — where losing it costs subscribed devices one re-add — losing or having revoked an Apple signing identity is described by Apple as *"Users will no longer be able to run apps that have been signed with this certificate"* ([Certificates overview](https://developer.apple.com/help/account/certificates/certificates-overview)).

---

## 7. Verdict

**For this deployment, upload-time signing is not worth doing.** The assessment, and what it rests on:

**What it buys: nothing, for the clients this catalogue serves.** AltStore and SideStore both run the subscriber's certificate over every download, unconditionally, and both mutate the bundle before signing — AltStore rewrites the bundle ID and adds a URL scheme and an exported UTI (`ResignAppOperation.swift:100+`), SideStore deletes `_CodeSignature` outright (`ResignAppOperation.swift:182-188`). Neither ever inspects an incoming signature; neither falls back to installing the download as-is (`SendAppOperation`/`InstallAppOperation` both require the resigned artifact). The user's device trusts the client's signature, always. And there is no field in the source schema by which a subscriber could even be *told* an IPA is pre-signed (§4).

**What it cannot fix.** Every problem the sign-on-upload idea is usually reached for:
- It does not make an app installable on an arbitrary device. With a Development/Ad-Hoc identity the profile's `ProvisionedDevices` list is still ≤100 registered UDIDs (§2b); with an Enterprise identity (`ProvisionsAllDevices`, the only non-UDID-bound type, §2c) the licence forbids serving anyone outside the organisation.
- It does not extend the 7-day personal-team app lifetime (§2a). That clock is a property of the *subscriber's* profile, which Feather never sees.
- It does not reduce the client's work: the client still unpacks, patches, re-signs, and re-zips (§4).
- It does not remove the licence exposure; it *creates* it. `ADP LA §7.3/§7.6/§5.1(d)` and `Enterprise LA §2.1(g)/§7` (§2d) are about the act of signing for others and distributing outside the permitted channels, which is precisely what a server-side signer does.

**What it costs.**
- A third-party re-signer with no Apple support path, against a format Apple documents as unstable (*"Avoid building a product based on these details"*), for a platform on which Apple says *"Re-signing apps from the command-line is not supported"* (§1, §3c). Only `zsign` is permissively licensed (MIT); `ldid` is AGPL-3.0 and would need a licensing review before vendoring or linking (§3b), and it cannot embed `embedded.mobileprovision` at all.
- A p12 passphrase in a subprocess `argv` (§3a, §3b) — a regression against the repo's own F-Droid custody standard (§6).
- Roughly 2–3× the artifact in temp space under `DATA_DIR`, a 2 GiB `MAX_CONTENT_LENGTH` ceiling, a 4 GiB container memory cap, and the whole unpack/sign/re-zip inside the global `SourceManager._lock` (§5).
- A new hard dependency for all five ingest paths, including two unattended ones (§5).
- Possibly new publishing obligations: if a signature stamps non-empty entitlements — which is the normal outcome, since `zsign` propagates the profile's `Entitlements` (`src/openssl.cpp:861-866`) — then `appPermissions.entitlements` must be kept exactly in sync or AltStore's `VerifyAppOperation.verifyPermissions` will refuse the install (§4c).

**What a decision hinges on.** Two facts, both established above and both falsifiable:
1. **Do the clients re-sign anyway?** Yes — AltStore appends `ResignAppOperation` to the install graph unconditionally (`AltStore/Managing Apps/AppManager.swift:1302`, `:1353`) and SideStore's declared install pipeline contains `.resignApp` unconditionally (`SideStore/Core/Operations/OperationStepDefinition.swift:52`) (§4). If a future client ever installed a pre-signed IPA as-is, the calculus changes; the field to watch is `AppVersion.CodingKeys`, which is where such a flag would have to appear.
2. **Is there a certificate whose profile is not UDID-bound and whose licence permits the distribution?** No — `ProvisionsAllDevices` exists only for Developer ID (macOS) and In-House/Enterprise profiles (§2c), and the Enterprise licence restricts distribution to employees (§2c, §2d).

**The alternatives, each with its documented constraint:**

| Option | Constraint (primary source) |
|---|---|
| **Leave unsigned; let the client re-sign** (status quo) | Requires each subscriber to have their own Apple identity. Free personal team = 7-day profiles, 3 devices, 10 App IDs, 3 apps per device (§2a) → weekly re-provisioning, which is the client's business, not Feather's. This is already the deliberate choice of the sibling build pipeline (`mangasync/docs/distribution.md:45-58`). |
| **Sign on the build machine that has macOS and a real identity** | `codesign` is macOS-only (§1); requires a Mac, Xcode, and a keychain (`security(1)`) — and the repo already has a study of the unattended-keychain problem (`mangasync/docs/research-ios-unattended-signing.md`, esp. §2 "What must be true for `codesign` to sign with no GUI present"). A Development-signed artifact is UDID-bound and therefore *worse than unsigned* for distribution (`mangasync/docs/distribution.md:56-58`, TN3125 §The where). |
| **TestFlight** | Apple's channel, needs App Store Connect and a paid membership; the *first* build of an app offered to an external group goes to App Review; external testers capped at 10,000, internal at 100; builds expire after 90 days; *"For builds to be eligible for TestFlight, they must include application identifiers within the provisioning profiles"* — and Apple re-signs them, like the App Store ([TestFlight](https://developer.apple.com/testflight/), [TestFlight overview](https://developer.apple.com/help/app-store-connect/test-a-beta-version/testflight-overview)). |
| **App Store / Custom App Distribution** | Requires Apple's selection; not applicable to a self-hosted third-party source (`ADP LA §7.6`). |
| **Apple Developer Enterprise Program (in-house)** | Technically the only fit for "install on an arbitrary device", and explicitly licensed only for employees, ≥100 of them, with systems ensuring only employees can download (Enterprise page + Enterprise LA §2.1(g)/§7). **Not available for a public app source.** |

The honest summary is that the question "can Feather sign IPAs on upload?" has a technical answer (only with an unsupported third-party tool, on Linux, at a real operational cost) and a licensing answer (not for these audiences with any certificate the operator could obtain) — and a product answer that makes both moot: **the clients this catalogue serves ignore the signature it would produce.**

---

## 8. The upload-button workflow: what a `.p12` + `.mobileprovision` pair actually requires

Sections 1–7 answered whether Feather *should* sign. This section answers the prior question an "upload your certificate" button poses: what the two files are, what a server can check at upload time, what the closest precedent (the Feather iOS app) actually does, what custody costs, and what the button would buy. Nothing here changes §7's verdict; where it adds a qualification, it says so.

### 8a. The two files, and how they relate

**The `.p12` is a digital identity, not a certificate.** TN3161 §Digital identity: *"To sign code you need a certificate and the private key that matches the public key in that certificate. This combination is called a digital identity"*, and *"As a certificate only contains a public key, you can't use it to sign code."*; Apple's tools *"generally prefer the PKCS#12 format"* (same section), which is RFC 7292. RFC 7292 §4 gives the container: `PFX ::= SEQUENCE { version, authSafe ContentInfo, macData MacData OPTIONAL }`, where `authSafe` carries an `AuthenticatedSafe` (`SEQUENCE OF ContentInfo`, §4.1) and password integrity mode adds a MAC *"computed from a secret integrity password, salt bits, an iteration count, and the contents of the AuthenticatedSafe"* (§3.4). A p12 may also carry the chain — pyca's loader returns `(private_key, certificate, additional_certificates)` ([Key serialization](https://cryptography.io/en/latest/hazmat/primitives/asymmetric/serialization/) §PKCS12).

**The `.mobileprovision` is a property list inside a CMS SignedData.** TN3125 §Unpack a profile: *"A provisioning profile is a property list wrapped within a Cryptographic Message Syntax (CMS) signature"*, and *"To view the original property list, remove the CMS wrapper using the `security` tool"*. The wrapper is RFC 5652: `SignedData ::= SEQUENCE { version, digestAlgorithms, encapContentInfo, certificates … OPTIONAL, crls … OPTIONAL, signerInfos }` with `EncapsulatedContentInfo ::= SEQUENCE { eContentType ContentType, eContent [0] EXPLICIT OCTET STRING OPTIONAL }` (RFC 5652 §5.1, §5.2) — i.e. the plist is carried **in the clear** as `eContent`, which is why `security cms -D` needs no key. Apple signs it: *"When the Apple Developer website creates a profile for you, it cryptographically signs it. When you run an app on a device, the device checks this signature to determine if the profile is valid and, if so, checks that the app meets the criteria in the profile."* (TN3125 §Provisioning profile fundamentals.)

**What makes them a *pair*** — five bindings, all from TN3125:

| Binding | Rule | Source |
|---|---|---|
| Who may sign | `DeveloperCertificates` holds *"the certificates of each developer who can sign code covered by the profile"*; each entry is a certificate's DER (the example extracts index 0, base64-decodes it to `cert0.cer`, and dumps it) | TN3125 §The who |
| The same rule, DER profile | In the profile's `DER-Encoded-Profile`, the field holds *"a SHA-256 checksum of the certificate"* rather than the certificate itself | TN3125 §The future is DER |
| Which app | `Entitlements` → `application-identifier` is an App ID *"composed of an App ID prefix and a bundle ID"*; `com.apple.developer.team-identifier` names the team | TN3125 §The what, §The how |
| Where | `ProvisionedDevices` (a UDID list) or `ProvisionsAllDevices` | TN3125 §The where |
| When | `ExpirationDate`, *"typically not more than a year"* (Developer ID excepted) | TN3125 §The when |
| How (allowlist) | `Entitlements` *"act as an allowlist … Every entitlement claimed by the app must be in the profile's allowlist"* | TN3125 §The how (quoted in §1) |

A p12 whose leaf is not in `DeveloperCertificates` cannot legitimately sign against that profile: nothing in either file *says* so, but the pipeline of §1 stamps the profile in as `embedded.mobileprovision` while the CMS is produced with the p12's key, and the device checks both. **And there are two independent expiries** — the profile's `ExpirationDate` (TN3125 §The when) and the leaf certificate's own `notAfter` (*"typically a year from the date of issue, although the exact duration varies based on the certificate type"*, TN3161 §Certificate expiration). Neither is derived from the other, and either can kill an artifact.

The field list an implementer should satisfy already exists in the iOS app's own decoder: `Feather/Utilities/CertificateReader/Models/CertificateModel.swift:10-40` decodes `AppIDName`, `ApplicationIdentifierPrefix`, `CreationDate`, `Platform`, `IsXcodeManaged`, `DeveloperCertificates`, `DER-Encoded-Profile`, `PPQCheck`, `Entitlements`, `ExpirationDate`, `Name`, `ProvisionsAllDevices`, `ProvisionedDevices`, `TeamIdentifier`, `TeamName`, `TimeToLive`, `UUID`, `Version`.

### 8b. What the server can validate at upload time, and what it cannot

The checklist, with the library call that would do each check named from that library's own documentation. "Cheap" means stdlib-only; "crypto" means a new dependency.

| # | Check | Verdict | How |
|---|---|---|---|
| 1 | p12 parses at all, **passphrase correct** | crypto — and the two failures must be told apart | pyca `load_key_and_certificates(data, password)`: a DER-level failure raises `ValueError("Could not deserialize PKCS12 data")` (`src/rust/src/pkcs12.rs:563`), while anything after that — wrong passphrase included — raises `ValueError("Invalid password or PKCS12 data")` (`:577`) |
| 2 | contains a private key **and** a certificate | crypto | same call; `certificate` *"is the `Certificate` whose public key matches the private key in the PKCS 12 object or `None`"* and `private_key` is likewise optional (serialization docs §PKCS12) — either being `None` is a reject. pyca documents support for *"a single private key and associated certificates"*, which is exactly what an identity is |
| 3 | leaf `notBefore`/`notAfter`, subject, issuer | crypto | `x509.load_der_x509_certificate`, `Certificate.not_valid_before_utc` / `not_valid_after_utc`, `Name.rfc4514_string()` ([x509 reference](https://cryptography.io/en/latest/x509/reference/)) |
| 4 | is it an Apple code-signing certificate at all? | partially assertable — see below | EKU contains `x509.oid.ExtendedKeyUsageOID.CODE_SIGNING` (`1.3.6.1.5.5.7.3.3`, x509 reference) + validity + chain to an Apple root (Apple PKI page) |
| 5 | read the profile plist **without** verifying Apple's signature | cheap (stdlib) or one more dep | three routes, below |
| 6 | verify Apple's signature on the profile | possible only with Apple's roots as data; not via pyca | `openssl cms -verify -CAfile …`; see below |
| 7 | profile `ExpirationDate` | cheap | `plistlib` on the extracted payload (TN3125 §The when) |
| 8 | **will this install on a device not in the list?** | cheap, and the most useful output | `ProvisionedDevices` ⇒ UDID-bound; `ProvisionsAllDevices: true` ⇒ *"apply to all devices"*; neither ⇒ App Store distribution profile (*"App Store distribution profiles have no `ProvisionedDevices` property"*) — all TN3125 §The where. Keep all three states |
| 9 | `Entitlements` keys, `TeamIdentifier`, `Name`, `AppIDName`, `ApplicationIdentifierPrefix`, `UUID` | cheap | `plistlib` |
| 10 | **the pair actually matches**: p12 leaf ∈ `DeveloperCertificates` | cheap once the leaf DER exists | compare DER bytes against the plist array (TN3125 §The who), or recompute `hashlib.sha256(leaf_der).digest()` against the DER profile's checksums (TN3125 §The future is DER) |
| 11 | revocation | needs the network | OCSP at the URI in the certificate's AIA extension (as the iOS app does, §8c), or Apple's WWDR CRL (Apple PKI page). `revoked` is `CertStatus` `revoked [1]` (RFC 6960 §2.2) |
| 12 | profile still valid; team limits; will a device accept it | impossible offline | nothing in the files answers it; Apple's own position is that `codesign --verify` *"isn't saying that the code is fit for some specific purpose … Rather, it says that the code is internally consistent"*, with fitness checked by other Apple tooling (TN3161 §Verify a code signature) |

**On (1), plainly.** Wrong passphrase and corrupt file are distinguishable *by message* today, so the UI can say which one happened — but both are plain `ValueError`, so a handler that catches `ValueError` and renders one string will report a typo as a corrupt file. That is the first failure an operator will hit.

**On (4), what is actually assertable.** TN3161 §Sign code lists exactly what `codesign` checks on an identity: *"It checks that it can build a chain of trust from the certificate to a trusted root. It checks that the current time is within the certificate's valid range. It checks that the certificate supports code signing. Specifically, it looks for Code Signing within the certificate's Extended Key Usage extension."* The same note adds that *"When signing code, `codesign` doesn't check that the code-signing identity's certificate was issued by Apple"* and that *"Most Apple platforms only run code with an Apple-issued certificate"* — so "is this Apple-issued?" is the *device's* requirement, and asserting it server-side means the chain check. Also useful: *"For code signing certificates, Apple places the developer's Team ID into the subject's Organization Unit (`OU`) field"* (TN3161 §Digital certificate) — the OU is where a displayed team ID belongs, cross-checked against the profile's `TeamIdentifier`. A **certificate *type*** label ("Apple Development" vs "Apple Distribution" vs in-house) is **UNVERIFIED**: TN3161 says only that code-signing certificates carry *"industry standard extensions"* and *"Apple-specific"* ones, *"documented on the Apple PKI page"*, and no list of those OIDs was found in the pages read — a CN-derived label would be a heuristic, not an assertion. AltSign's `AltSign/Resources/apple.pem` (cited in §4) is the same asset class as Apple's published roots: Apple's chain shipped as data so a signer can build trust without a keychain.

**On (5), reading the profile.** Three routes, in increasing cost:

- **Stdlib, and what the precedent does.** The iOS app neither decrypts nor verifies: it finds the first `<?xml` byte and hands the remainder to `PropertyListDecoder` (`CertificateReader.swift:21-37`), then validates the decoded object with its `Codable` model. The Linux equivalent is a byte scan for `<?xml` plus `plistlib.loads` — **zero new dependencies**, and exactly as fragile as it looks (it assumes the plist is the tail of the file).
- **`openssl cms -verify -inform DER -in profile.mobileprovision -noverify -out payload.plist`.** `-verify` *"Verify signed data … outputs the signed data"*, `-noverify` is *"Do not verify the signers certificate of a signed message"*; that the default *does* check signatures is evidenced by the separate `-no_content_verify` and `-nosigs` options ([openssl-cms(1)](https://docs.openssl.org/master/man1/openssl-cms/)). Requires the `openssl` binary in the image — **UNVERIFIED** whether `python:3.11-slim` (`Dockerfile:1`) ships it.
- **`asn1crypto`** (MIT; *"No third-party packages required"*): parse `cms.ContentInfo`, read `SignedData.encapContentInfo.eContent` (*"eContent is the content itself, carried as an octet string"*, RFC 5652 §5.2), hand to `plistlib`. It is *"a fast, pure Python library for parsing and serializing ASN.1 structures"* — parsing only; its readme points at the sibling `oscrypto`/`certvalidator` projects for anything cryptographic.
- **What pyca cannot do, stated plainly:** `cryptography.hazmat.primitives.serialization.pkcs7` documents only signing builders, envelope *de*cryption, and `load_pem_pkcs7_certificates` / `load_der_pkcs7_certificates` — *"Deserialize a … PKCS7 blob to a list of certificates. PKCS7 can contain many other types of data, including CRLs, but this function will ignore everything except certificates"* (serialization docs §PKCS7). There is **no documented API to extract the SignedData payload and none to verify a PKCS7/CMS signature**. `cryptography` alone therefore cannot read a `.mobileprovision`; assuming otherwise is the likeliest single mistake in this feature.

**On (6), verifying Apple's signature.** Feasible *as a signature check* — the signer chain travels inside the blob (RFC 5652 §5.1 `certificates`) and Apple's root is downloadable (Apple PKI page: Apple Inc. Root, Apple Root CA - G2/G3) — but not through pyca; it means `openssl cms -verify -CAfile AppleRootCA-G3.cer …` or hand-rolled `SignedData` verification against `signedAttrs`. What it buys: TN3125 says the device checks this signature, so a profile that fails it is worthless. What it does **not** buy: liveness. A profile Apple invalidated still carries a valid Apple signature, and nothing in either file says "revoked" — **UNVERIFIED** whether Apple documents any server-side profile-validity interface at all.

**Which checks are cheap, in one line.** The profile half — expiry, device binding, entitlement keys, `DeveloperCertificates`, and even the pair match — is reachable with the standard library alone; everything about the p12 needs `cryptography`; revocation needs the network; and profile validity plus "will a device install it" are not answerable from these files at all.

**The dependency decision.** `requirements.txt:1-10` is ten pins containing nothing cryptographic, and several plans treat a new dependency as a STOP condition. This feature needs:

- **`cryptography`** — mandatory for any p12 validation (parse, passphrase, key presence, leaf, chain). Its own installation docs: *"Most Linux platforms will receive a binary wheel and require no compiler if you have an updated `pip`"*, and it *"ships `manylinux` wheels (as of 2.0) so all dependencies are included"* ([installation](https://cryptography.io/en/latest/installation/)) — so `python:3.11-slim` takes a wheel rather than a build, but it is still a large compiled pin entering a deliberately small set.
- **`asn1crypto`** — optional, needed only if the CMS payload should be parsed structurally rather than by scanning for `<?xml`. Pure Python, MIT, no transitive deps.
- **Zero new Python dependencies** is achievable only for the profile half alone (stdlib `plistlib`), which cannot answer "is this p12 usable": nothing in the standard library decrypts PKCS#12, and the interesting part of a p12 is encrypted.

So: `cryptography` (+ optionally `asn1crypto`), or no p12 validation at all. That is a decision to record, not a line to add in passing.

### 8c. The precedent: how the Feather iOS app does it

The repo the catalogue serves has been **renamed**: `api.github.com/repos/khcrysalis/Feather` now reports `full_name: claration/Feather` (default branch `main`, GPL-3.0). Everything below is read at commit `a162b1ac` of `claration/Feather`.

**What the user is asked for** — p12, mobileprovision, passphrase, optional nickname:

- `Feather/Views/Settings/Certificates/CertificatesAddView.swift:26-28` — `_p12URL`, `_provisionURL`, `_p12Password`, `_certificateName`.
- `:24-26` — Save stays disabled until *both* files are chosen; `:64` / `:74` — the pickers are restricted to `[.p12]` and `[.mobileProvision]`.
- `:41` — a `SecureField` "Enter Password", footer: *"Enter the password associated with the private key. Leave it blank if theres no password required."*
- `:47` — "Nickname (Optional)".

**What it validates on import** — one passphrase check and one syntactical parse, and nothing else:

- `:105-118` — `_saveCertificate()` requires `FR.checkPasswordForCertificate(for: p12URL, with: _p12Password, using: provisionURL)`; on failure it raises an alert titled **"Bad Password"** (`:112`) — i.e. the precedent does distinguish a wrong passphrase from a corrupt file, the distinction §8b(1) asks for.
- `FR.swift:106-121` — that helper calls `password_check_fix_WHAT_THE_FUCK(provision.path)` and then `p12_password_check(key.path, password)`.
- Both live in the `Zsign` package submodule (`khcrysalis/Zsign-Package`, pinned at `6ffe703` by `.gitmodules:1-4` + the tree): `src/openssl_tools.mm:24-54` opens the p12 with `d2i_PKCS12_bio` (`:36`) and accepts it if `PKCS12_verify_mac(p12, NULL, 0)` (`:44`) or `PKCS12_verify_mac(p12, strPass, -1)` (`:46`) succeeds. It never calls `PKCS12_parse` — so **the p12's private key is not checked at import time**.
- `src/openssl_tools.mm:64-72` — the "fix" reads the profile and calls `d2i_CMS_bio(in, NULL)`, discarding the result; the function's own comment block (`:56-63`) calls it a workaround with a `TODO: FIX`. That is the entire profile validation: parsed syntactically, **never signature-verified**.
- **Nothing anywhere compares the p12 leaf against the profile's `DeveloperCertificates`.** The pair check of §8b(10) does not exist in the precedent. The import's only other gate is that the profile plist decoded at all: `CertificateFileHandler.swift:36,41-50` throws `certNotValid` when `CertificateReader` returns nil.

**What it merely stores.** `CertificateFileHandler.swift:55-64` → `Storage.addCertificate(uuid:password:nickname:ppq:expiration:isDefault:)` (`Feather/Backend/Storage/Storage+Certificate.swift:14-47`). The two files are copied into `Documents/Feather/Certificates/<UUID>/` keeping their original filenames (`CertificateFileHandler.swift:41-52,69-74`; lookup is by extension — `.certificate = "p12"`, `.provision = "mobileprovision"`, `Storage+Certificate.swift:79-83`); the row keeps the **passphrase**, nickname, `ppq` (the profile's `PPQCheck`) and the profile's `ExpirationDate`.

**Where the password lives.** In a plain string column: the `CertificatePair` entity declares `<attribute name="password" optional="YES" attributeType="String"/>` (`Feather/Backend/Storage/Feather.xcdatamodeld/Feather.xcdatamodel/contents`) and `addCertificate` assigns `new.password = password` (`Storage+Certificate.swift:28`) — no keychain, no encryption at rest. The app is explicit about the consequence: its "Export Certificate" action base64s the p12 into a URL *together with* the password (`FR.swift:188-245`, `$(BASE64_CERT)` at `:208`, `$(PASSWORD)` beside it), after warning *"That app will be able to sign apps using your certificate."* (`:242`).

**How it reports expired and revoked.**

- **Expiry** — offline, from the *profile*: `CertificatesInfoView.swift:64-65` prints `data.ExpirationDate.expirationInfo()` with a colour; the certificate's own `notAfter` is not what is shown.
- **Revocation** — online, on demand: the info view shows `cert.revoked` (`:67`) and the context menu offers *"Check Revokage"* (`CertificatesView.swift:144-156`) → `Storage.revokagedCertificate` (`Storage+Certificate.swift:61-78`) → `Zsign.checkRevokage` → `checkCert` (`zsign.mm:242-391`), which requires the leaf's issuer hash to be one of two hardcoded Apple WWDR CAs (`:275-282`), reads the OCSP URI from the AIA extension (`:297-318`), POSTs an OCSP request (`:320-338`), and sets `revoked = true` when the response status is `1` (`Storage+Certificate.swift:69`) — the OCSP `revoked` state (RFC 6960 §2.2).
- Revocation is also checked **at import**, because `addCertificate` calls `revokagedCertificate(for: new)` before saving (`Storage+Certificate.swift:33`). So the precedent's import is: passphrase verified locally, profile parsed locally, revocation queried over the network, pair match never checked. (The OCSP path also parses the certificate's `notAfter` — `zsign.mm:355-365` — but the caller ignores the returned date.)

**Which signer it invokes.** `Feather/Utilities/Handlers/ZsignHandler.swift:9,42-55`: `Zsign.sign(appPath:provisionPath:p12Path:p12Password:entitlementsPath:removeProvision:completion:)` from module `ZsignSwift`, which is a product of the local `Zsign` SwiftPM package (`Feather.xcodeproj/project.pbxproj:15` product ref, `:514-517` local package with `relativePath = Zsign`), which is the submodule → `khcrysalis/Zsign-Package` at `6ffe703`. That revision's `Package.swift` declares products `zsign` and `ZsignSwift`, and its `Zsign` target compiles zsign's own C++ (`archo.cpp`, `bundle.cpp`, `macho.cpp`, `openssl.cpp`, `signing.cpp`) plus the ObjC++ bridges `zsign.mm` and `openssl_tools.mm` and `common/*`. **Confirmed from source: the app is a zsign derivative, packaged as a Swift module.** (The branch tip of `Zsign-Package` has since renamed the `openssl_tools.mm` symbols and moved the Swift wrapper to `Sources/`; `6ffe703` is the revision whose symbol names match this Feather commit, which is why it is the one cited.)

**One more precedent behaviour that matters for §8e:** it does not always sign. `Options.signingOption` is `.default` or `.onlyModify` (`.adhoc` commented out) (`Feather/Backend/Observable/OptionsManager.swift:63,200-208`); `SigningHandler.modify()` signs only when `signingOption == .default && appCertificate != nil`, does nothing at all for `.onlyModify`, and throws `missingCertifcate` otherwise (`Feather/Utilities/Handlers/SigningHandler.swift:109-119`); and the UI permits starting with no certificate when the option is not `.default` (`Feather/Views/Signing/SigningView.swift:266-282`). So an "import and install without re-signing" path *does* exist — but the same `modify()` rewrites `Info.plist` unconditionally (`SigningHandler.swift:226`, `:260`) and may remove files (`:88-89`), which is exactly the seal-breaking §4 describes. Whether an incoming signature survives that path is **UNVERIFIED** (Open question 7).

### 8d. Custody of an uploaded p12

**Where it lands.** Like every other upload: `UPLOAD_FOLDER = DATA_DIR/uploads` (`app.py:79`), created at startup (`app.py:1022`), used for all staged artifacts (`app.py:1100`, `:1186`, `:3255`, `:5032-5040`), under `DATA_DIR` inside `./data` — which `compose.yml:14` bind-mounts from the operator's home directory and `.gitignore:1-3` treats as runtime state (*"Never commit: data/source.json is the product and data/ipas/ is irreplaceable"*), not as secret material. `MAX_CONTENT_LENGTH` is irrelevant to a few-kilobyte p12 (`app.py:129`, `:161`; `.env.example:12`). **The passphrase is not a file at all**: it needs a form field (the precedent uses a `SecureField`, `CertificatesAddView.swift:41`), a code path that never logs it, and an entry in the redaction logic — which today is a list of *environment-variable names* whose values get masked (`app.py:49-56`) and would not know about a value arriving in a request body.

**Can the passphrase be stored for unattended signing later?** Only as a reusable secret at rest, and there is nothing here to encrypt it with. No KMS, no key material, no crypto library: `app.py` imports `hmac`/`hashlib` for `compare_digest` (`:2286`, `:1348`) and file digests (`:4471-4475`) and nothing else. The only secret-shaped config is `ADMIN_PASSWORD`, `SECRET_KEY` and the Garage key (`app.py:120-121`, `:52`). Using `SECRET_KEY` as an encryption key would fuse *"can forge an admin session"* with *"can recover the signing identity"*, and when it is unset the app generates a fresh `os.urandom(32)` **per boot** (`app.py:170-172`) — anything encrypted under it is unrecoverable after a restart. Putting the passphrase in `.env` copies the `FDROID_KEYSTORE_PASSWORD` pattern at a much larger blast radius: the F-Droid sidecar is deliberately starved (*"The sidecar receives only FDROID_* variables and FEATHER_UID, never the rest of .env"*, `.env.example:91-99`) and receives a *reference* rather than a value (`-storepass:env FDROID_KEYSTORE_PASSWORD`, `scripts/fdroid_index_loop.sh:22-24`), whereas in `compose.yml` three services read the whole `.env` — the app (`:21`), the Telegram ingest worker running as uid 101 (`:77`, `:81`) and the release importer (`:103`). A `P12_PASSPHRASE` there would be readable by two containers with nothing to do with signing. The F-Droid shape is the correct pattern and, per §6, also harder here because a signer must receive and return a full-size artifact. [INFERENCE] Note that the argv exposure of §3 is a property of the **CLI front ends**, not of the crypto: the library entry point takes the passphrase in-process (`ZSignAsset::Init(..., strPassword, ...)`, §3a) and the precedent passes a Swift `String` straight in (`ZsignHandler.swift:47-53`) — so any future design should prefer a pipe/fd handoff over `argv`.

**The argv exposure, concretely.** `/proc/<pid>/cmdline` *"holds the complete command line for the process"* ([proc_pid_cmdline(5)](https://man7.org/linux/man-pages/man5/proc_pid_cmdline.5.html)), and `ps`'s `args` shows *"A command with all its arguments as a string"* ([ps(1)](https://man7.org/linux/man-pages/man1/ps.1.html)). Reading another process's `/proc/PID/*` files is gated by a ptrace access-mode check — the kernel documents it as *"reading process is required to have either `CAP_SYS_PTRACE` capability with `PTRACE_MODE_READ` access permissions, or, alternatively, `CAP_PERFMON` capability"* ([Documentation/filesystems/proc.rst §1.1](https://www.kernel.org/doc/html/latest/filesystems/proc.html)). No service in `compose.yml` sets `pid: host`, so every service has its own PID namespace and the exposure is: root inside `altstore-source-manager`, anything else executing in that container, and host root — **not** the sibling containers. That is still precisely the defect class this repo has already fixed once: plan 010's Dockerfile healthcheck was changed so that *"no credential appears in `docker inspect` or the process table"* (`plans/010-login-session-auth.md:89`). The same-uid case under the ptrace check is **UNVERIFIED** in the pages read; the root/`CAP_SYS_PTRACE` case is documented. Worth recording in the other direction: OpenSSL's CLI need not put the literal in `argv` — `-passin env:VAR`, `file:PATH`, `fd:N` and `stdin` all exist, and it documents that the literal form is *"visible to utilities (like 'ps' under Unix)"* ([openssl-passphrase-options(1)](https://docs.openssl.org/master/man1/openssl-passphrase-options/)) — so a CLI path *can* avoid this, while `zsign -p` and `ldid -U` cannot.

**What the upload adds to the admin surface.** The route would carry `@requires_auth` (`app.py:2268-2274`), whose entire state is a Flask session cookie set by `POST /api/login` after `hmac.compare_digest` against `ADMIN_PASSWORD` (`app.py:2282-2291`), with `SESSION_COOKIE_HTTPONLY=True`, `SESSION_COOKIE_SAMESITE="Lax"`, `SESSION_COOKIE_SECURE=False` (`app.py:162-167`) and `session.permanent = True` (`:2288`). There are **no CSRF tokens** anywhere in `app.py` or `templates/index.html` (case-insensitive search: no hits), **no rate limiting** on login (`Flask-Limiter` is pinned at `requirements.txt:2` and never imported; plan 010 deferred it explicitly, `plans/010-login-session-auth.md:206`), a single shared password with no second factor and no per-user attribution (`plans/010-login-session-auth.md:205`), and plain HTTP on a private network by decision (`app.py:164-167`). Today one leaked `ADMIN_PASSWORD` buys "rewrite the catalogue and delete apps *and their IPAs*"; with a p12 upload it additionally buys "exfiltrate a code-signing identity and its passphrase, and sign arbitrary code as that team until Apple revokes it". That is the duty `ADP LA §5.1(b)/(d)` places on the holder — *"solely responsible for preventing any unauthorized person or organization from having access to Your Apple Certificates and keys"*, *"You will not use Your Apple Certificates to sign any third party's application"* (§2d, not re-quoted). One affordance to deliberately *not* build: a p12 that can be downloaded back out through the UI is a credential-exfiltration endpoint — the precedent has that button and warns about it in the same dialog (`FR.swift:188-245`).

### 8e. What the button would actually buy

Given §1–§4, a server-held identity changes an outcome in exactly one shape of situation: **the consumer does not re-sign** *and* **every consumer device is legitimately covered by the profile**. Both halves must hold.

- **Clients that re-sign: nothing changes.** AltStore and SideStore overwrite the signature and the embedded profile with the subscriber's own identity, unconditionally (§4). For them a signed upload is behaviourally identical to an unsigned one.
- **TrollStore-style installers: nothing changes, for the opposite reason.** TrollStore *"can permanently install any IPA you open in it"* via a CoreTrust bug and re-signs with its own fake root — *"The binaries inside an IPA can have arbitrary entitlements, fakesign them with ldid …, and TrollStore will preserve the entitlements when resigning them with the fake root certificate on installation"* (TrollStore README). It is the clearest case of a consumer that ignores the incoming signature by design, and therefore not a reason to sign.
- **The Feather app's own import path is the only candidate found.** Its `.onlyModify` mode moves an imported app to the install path without invoking the signer at all (`SigningHandler.swift:109-119`, `SigningView.swift:266-282`, §8c). Where an operator's own devices install through that path, an IPA pre-signed with an identity those devices trust would be installed under that identity. Whether it survives `modify()`'s unconditional `Info.plist` rewrite is **UNVERIFIED**.
- **A known, enrolled device set is the only licence-clean case.** An Ad-Hoc/Development profile installs only on the UDIDs in `ProvisionedDevices` (≤100 per product family per membership year, §2b); the only `ProvisionsAllDevices` iOS type is Enterprise, licensed to employees only (§2c, §2d). *"Sign for the devices already enrolled in our organisation's profile"* is a coherent feature; *"sign for the subscribers of a public source"* is the pattern the licences forbid and §7 rejected.

So the honest answer is: **it helps only when the consumer is not AltStore/SideStore and the audience is the organisation's own enrolled devices — and neither condition holds for this catalogue.** §7's verdict stands unchanged, and nothing here softens it. The single piece of new evidence that would change it is a client that both installs the bytes as-is *and* is actually used by these subscribers; the only candidate found is unverified and rewrites `Info.plist` anyway.

**The narrow legitimate version — validate-and-display-only.** Upload the p12, the profile and the passphrase; parse both; display subject, issuer, team (from the certificate OU, cross-checked against `TeamIdentifier`), **both** expiries, `ProvisionedDevices` count vs `ProvisionsAllDevices`, the entitlement keys, and the headline — *whether the p12's leaf appears in the profile's `DeveloperCertificates`* — then sign nothing. No licence exposure (nothing is signed for anyone), no `argv` exposure (no signer process exists), no MIT/AGPL question (§3), no unpack/re-zip, no global-lock hold and no 2–3× temp spike (§5), no second full-size copy of every artifact (§6), and no new hard dependency on any ingest path (§5). It answers the operator's real question — *is this certificate usable, and for whom?* — and it is the dependency-cheap subset of §8b: **one new pin (`cryptography`)** with the p12 half, or **zero** with only the profile half. If it is ever extended to signing, everything in §8d becomes load-bearing first.

**The smallest thing that works.** A `scripts/certificate_inspection.py` modelled on `scripts/ipa_inspection.py` (whose docstring is *"Dependency-free inspection of an IPA's top-level application plist"*, `scripts/ipa_inspection.py:1-6`), returning a frozen dataclass — profile fields, both expiries, device binding, entitlement keys, pair-match result — plus one `@requires_auth` POST route that takes the three inputs, never persists the passphrase (ideally not the p12 either), and returns the summary; and a settings panel in the admin UI, following the existing file-input pattern at `templates/index.html:691`. Add `cryptography` deliberately, as the one dependency decision. No zsign, no ldid, no subprocess, no `argv`, no signing.

---

## Sources

**Apple documentation**

- [Using the latest code signature format](https://developer.apple.com/documentation/xcode/using-the-latest-code-signature-format) — iOS refuses to launch unsigned/invalid apps; DER entitlements from iOS 15; `-5`/`-7`; *"Re-signing apps from the command-line is not supported for iOS…"*
- [TN3125: Inside Code Signing: Provisioning Profiles](https://developer.apple.com/documentation/technotes/tn3125-inside-code-signing-provisioning-profiles) — profile structure, CMS wrapper, `DeveloperCertificates`, `application-identifier`, `ProvisionedDevices`, `ProvisionsAllDevices`, `ExpirationDate`, entitlements allowlist, `embedded.mobileprovision`, DER-encoded profile, *"codesign (macOS only)"*, *"Avoid building a product based on these details"*
- [TN3126: Inside Code Signing: Hashes](https://developer.apple.com/documentation/technotes/tn3126-inside-code-signing-hashes) — `LC_CODE_SIGNATURE`, `_CodeSignature/CodeResources`, `files`/`files2`/`rules`/`rules2`, special slots -1/-3/-5
- [TN3127: Inside Code Signing: Requirements](https://developer.apple.com/documentation/technotes/tn3127-inside-code-signing-requirements) — designated requirement as an internal requirement
- [TN3161: Inside Code Signing: Certificates](https://developer.apple.com/documentation/technotes/tn3161-inside-code-signing-certificates) — X.509, digital identity, PKCS#12, CMS, certificate expiration ("typically a year")
- [TN3137: On Mac keychains](https://developer.apple.com/documentation/technotes/tn3137-on-mac-keychains) — file-based vs data-protection keychain
- [Developer account overview](https://developer.apple.com/help/account/basics/about-your-developer-account) — personal team limits (10 App IDs / 3 devices / 3 apps per device / 7-day profiles); also the current target of `https://developer.apple.com/support/compare-memberships/`
- [Devices overview](https://developer.apple.com/help/account/devices/devices-overview) — 100 devices per product family per membership year
- [Certificates overview](https://developer.apple.com/help/account/certificates/certificates-overview) — certificate types; expired/revoked behaviour; *"Do not share Apple Certificates outside of your organization."*; *"Apple can revoke digital certificates at any time at its sole discretion."*
- [Revoke a certificate](https://developer.apple.com/help/account/certificates/revoke-a-certificate) — profiles containing a revoked certificate become invalid
- [Create an ad hoc provisioning profile](https://developer.apple.com/help/account/provisioning-profiles/create-an-ad-hoc-provisioning-profile) — needs registered devices + a distribution certificate
- [Distributing your app to registered devices](https://developer.apple.com/documentation/xcode/distributing-your-app-to-registered-devices) — ad-hoc testers can run only on listed devices
- [Apple Developer Enterprise Program](https://developer.apple.com/programs/enterprise/) — internal-use only, ≥100 employees, employee-only download
- [TestFlight](https://developer.apple.com/testflight/) and [TestFlight overview](https://developer.apple.com/help/app-store-connect/test-a-beta-version/testflight-overview) — 100 internal / 10,000 external testers, 90-day builds, review for external testers
- [Apple Developer Program License Agreement (PDF)](https://developer.apple.com/support/downloads/terms/apple-developer-program/Apple-Developer-Program-License-Agreement-English.pdf) — §2.1 (no credential solicitation), §5.1(b)/(d) (certificate custody; no signing third-party apps), §7.3 (Ad Hoc to affiliated individuals on Registered Devices), §7.6 (no other distribution); "Registered Devices" definition
- [Apple Developer Enterprise Program License Agreement (PDF)](https://developer.apple.com/support/downloads/terms/apple-developer-enterprise-program/Apple-Developer-Enterprise-Program-License-Agreement-English.pdf) — §2.1(g), §7 No Other Distribution, "Internal Use Application" definition
- [Apple PKI](https://www.apple.com/certificateauthority/) — root and intermediate certificate downloads (Apple Inc. Root, Apple Root CA - G2/G3; WWDR G2–G6; Developer ID - G1/G2) and the WWDR CRL; TN3161 §Digital certificate names this page as where Apple-specific certificate extensions are documented (no OID list was found there)

**Tool source**

- `zsign` — <https://github.com/zhlynn/zsign> @ `614caa8`: `src/zsign.cpp:25-57,200,209-251`; `src/openssl.cpp:446,479-491,506,828,843-847,861-866,878-896`; `src/archo.cpp:327,389-403,457,491-532,600,636-650`; `src/signing.cpp:23-88,141,297,314,449,472,488,670,735`; `src/bundle.cpp:86,98-111,124-148,155,214-252,296-306,403-418,424-466,777,826-831`; `src/macho.cpp:124,150-154`; `LICENSE:1-3`; `README.md`
- `ldid` — <https://github.com/ProcursusTeam/ldid> @ `af86971`: `ldid.cpp:1847-1866,1876-1931,2156-2227,2437,2664,2734,2774,2778,3161,3217-3218,3220-3252,3413-3420,3445-3462,3557,3561,3743-3749,3806-3812`; `docs/ldid.1:1-4,36-37,139-144`; `COPYING:1-2`; `Makefile`; `README.md`
- AltStore — <https://github.com/altstoreio/AltStore> (`develop`; `rileytestut/AltStore` redirects here): `AltStore/Managing Apps/AppManager.swift:530,986,992,1085,1129,1145-1146,1288,1302,1310,1323,1353`; `AltStore/Operations/DownloadAppOperation.swift`; `AltStore/Operations/VerifyAppOperation.swift`; `AltStore/Operations/ResignAppOperation.swift:56,100+,250-251`; `AltStore/Operations/SendAppOperation.swift:36-42`; `AltStore/Operations/InstallAppOperation.swift:42-46,72`; `AltStoreCore/Model/Source.swift:114-130`; `AltStoreCore/Model/StoreApp.swift:122-140`; `AltStoreCore/Model/AppVersion.swift:59-70`
- AltSign — <https://github.com/rileytestut/AltSign> (`master`): `AltSign/Signing/ALTSigner.mm:212-213,286-289`; `AltSign/Model/ALTApplication.mm:195-204`; `AltSign/ldid/alt_ldid.cpp`; `AltSign/Resources/apple.pem`
- SideStore — <https://github.com/SideStore/SideStore> (`develop`): `SideStore/Core/Operations/OperationStepDefinition.swift:39-59`; `SideStore/Core/Operations/PipelineExecutor.swift:142-147`; `SideStore/Core/Operations/PipelineOperations/ResignAppOperation.swift:182-188,191-193`; `…/SendAppOperation.swift:26-28`; `…/InstallAppOperation.swift:38-51`; `…/FetchProvisioningProfilesOperation.swift:51,187`; `SideStore/Core/Certificates/CodeSignValidator.swift`; `SideStore/Core/DeviceApi/MinimuxerWrapper.swift:112-129,245-281`; `SideStore/Utils/common/AppBundleFingerprint.swift:24-26`; `AltStore/Core/Model/AppVersion.swift:71-82`
- AltStore source-format documentation (first-party FAQ repo, `altstoreio/FAQ`) — <https://faq.altstore.io/developers/make-a-source>. Note: AltStore has **no** `Documentation/Source.md` on `develop` (404).
- Feather (iOS app) — <https://github.com/khcrysalis/Feather>, **renamed** to <https://github.com/claration/Feather> (`api.github.com/repos/khcrysalis/Feather` reports `full_name: claration/Feather`, default branch `main`, GPL-3.0) @ `a162b1ac`: `Feather/Views/Settings/Certificates/CertificatesAddView.swift:24-26,41,47,57,64,74,105-118`; `Feather/Views/Settings/Certificates/CertificatesView.swift:144-156`; `Feather/Views/Settings/Certificates/Info/CertificatesInfoView.swift:64-70,88-96`; `Feather/Utilities/FR.swift:106-121,188-245`; `Feather/Utilities/CertificateReader/CertificateReader.swift:21-37`; `Feather/Utilities/CertificateReader/Models/CertificateModel.swift:10-40`; `Feather/Utilities/Handlers/CertificateFileHandler.swift:36,41-52,55-64,69-74`; `Feather/Utilities/Handlers/SigningHandler.swift:88-89,109-119,226,260,447-459`; `Feather/Utilities/Handlers/ZsignHandler.swift:9,42-55`; `Feather/Backend/Storage/Storage+Certificate.swift:14-47,61-78,79-83`; `Feather/Backend/Storage/Feather.xcdatamodeld/Feather.xcdatamodel/contents` (`CertificatePair.password` is a plain String attribute); `Feather/Backend/Observable/OptionsManager.swift:63,200-208`; `Feather/Views/Signing/SigningView.swift:266-282`; `.gitmodules:1-4`; `Feather.xcodeproj/project.pbxproj:15,514-517`
- `Zsign` Swift package — the iOS app's signer and a zsign derivative — <https://github.com/khcrysalis/Zsign-Package> @ `6ffe703` (the submodule revision pinned by the Feather commit above; the branch tip has renamed the `openssl_tools.mm` symbols and moved the Swift wrapper to `Sources/`): `Package.swift` (products `zsign`/`ZsignSwift`; `Zsign` target = `archo.cpp`,`bundle.cpp`,`macho.cpp`,`openssl.cpp`,`signing.cpp`,`zsign.mm`,`openssl_tools.mm`,`common/*`); `Sources/zsign.swift:62-116`; `src/openssl_tools.mm:24-54,64-72`; `src/zsign.mm:166-239,242-391`
- TrollStore — <https://github.com/opa334/TrollStore> (`main`): `README.md` — *"a permasigned jailed app that can permanently install any IPA you open in it"*, re-signing with a fake root and preserving fakesigned entitlements

**This repo**

- `app.py:49-56` (`_redact_secret`), `:78-93` (data dirs, F-Droid layout comment), `:129`/`:161` (`MAX_CONTENT_LENGTH`), `:362`/`:511` (storage `put`s), `:1038-1075` (`save_ipa_file`), `:1076-1140` (`download_ipa_from_url`), `:1371`/`:1373`/`:1401`/`:1408` (`add_app_manual`), `:1637`/`:1639`/`:1677`/`:1684` (`add_version`), `:1710`/`:1712`/`:1753`/`:1769` (`update_version`), `:2256-2258` (backend selection), `:2314-2340` (`serve_ipa`), `:2429`/`:2469` (`/api/add-app`), `:2569`/`:2602` (`/api/add-version`), `:3243-3290` (`_run_auto_import_ios_candidate`), `:3791`/`:3864-3866` (`/api/import-release`), `:5283-5296` (`__main__`, Waitress)
- `scripts/ipa_inspection.py:48` (`inspect_ipa`), `scripts/ipa_inspection.py` (whole file)
- `scripts/telegram_bot_ingest.py:316,356,444-453,478-484,571,581`
- `scripts/release_source_ingest.py:883,944,1015,1047,1303`
- `scripts/fdroid_index_loop.sh:4-8,21-25,65-68`
- `compose.yml:12-17,25-27,118-127,134-142`; `.env.example:12,91-99`; `Dockerfile:1,34,47`; `Dockerfile.bot:1,11,15`; `.dockerignore`
- `plans/011-garage-s3-ipa-storage.md:32,47,117,351,444-446`; `plans/052-waitress-wsgi-server.md:40-46,147-149`; `plans/068-shared-ipa-preflight.md:1-60`; `plans/073-android-fdroid-repo.md:32,43,284-291,353-355`
- `app.py:162-167` (session cookie flags), `:2268-2274` (`requires_auth`), `:2282-2300` (`/api/login`, `/api/logout`, `/api/session`), `:4471-4475` (`_sha256_file`); `requirements.txt:1-10` (the ten existing pins — nothing cryptographic); `.gitignore:1-3`; `templates/index.html:691` (file-input pattern); `compose.yml:14,21,77,81,103` (`./data` bind mount; `env_file` on three services, the middle one running as uid 101); `plans/010-login-session-auth.md:89,205-206`

**Sibling project**

- `mangasync/docs/distribution.md:10,45-58,71` — unsigned-by-design iOS build (`CODE_SIGNING_ALLOWED=NO`), and why a Development-signed artifact is worse than unsigned for distribution
- `mangasync/docs/research-ios-unattended-signing.md` — the existing study of `codesign` in a non-GUI session (`errSecInternalComponent`), unified keychain state, and `security import -T /usr/bin/codesign`; useful if the signing ever moves to the build Mac

**Standards**

- [RFC 7292: PKCS #12: Personal Information Exchange Syntax v1.1](https://www.rfc-editor.org/rfc/rfc7292.txt) — `PFX ::= SEQUENCE { version, authSafe, macData }` and `MacData` (§4), integrity/privacy modes and the MAC over the AuthenticatedSafe (§3.4, §4.1), MAC key derivation (Appendix B)
- [RFC 5652: Cryptographic Message Syntax](https://www.rfc-editor.org/rfc/rfc5652.txt) — `SignedData ::= SEQUENCE { … }` (§5.1), `EncapsulatedContentInfo` and `eContent` (§5.2)
- [RFC 6960: Online Certificate Status Protocol](https://www.rfc-editor.org/rfc/rfc6960.txt) — `CertStatus ::= CHOICE { good [0], revoked [1], unknown [2] }` (§2.2)

**Libraries**

- pyca/cryptography — [Key serialization (PKCS12, PKCS7)](https://cryptography.io/en/latest/hazmat/primitives/asymmetric/serialization/) — `load_key_and_certificates`, `load_der_pkcs7_certificates`, the "only supports a single private key and associated certificates" note, and the PKCS7 section that ignores everything but certificates; [x509 reference](https://cryptography.io/en/latest/x509/reference/) — `load_der_x509_certificate`, `not_valid_before_utc`/`not_valid_after_utc`, `rfc4514_string`, `ExtendedKeyUsageOID.CODE_SIGNING` (`1.3.6.1.5.5.7.3.3`); [installation](https://cryptography.io/en/latest/installation/) — binary wheel / `manylinux` wheel guarantees; and its own source for the two distinct p12 failures: `src/rust/src/pkcs12.rs:563,577`
- `asn1crypto` — <https://github.com/wbond/asn1crypto> (`readme.md`): pure-Python ASN.1 parsing/serialisation with no third-party dependencies, `asn1crypto.cms` and `asn1crypto.pkcs12`; parsing only — its readme points at the sibling `oscrypto`/`certvalidator` for cryptographic operations

**Platform documentation**

- Linux [`proc_pid_cmdline(5)`](https://man7.org/linux/man-pages/man5/proc_pid_cmdline.5.html) — `/proc/pid/cmdline` holds the complete command line; [`ps(1)`](https://man7.org/linux/man-pages/man1/ps.1.html) — `args` shows a command with all its arguments as a string; kernel [`Documentation/filesystems/proc.rst` §1.1](https://www.kernel.org/doc/html/latest/filesystems/proc.html) — reading another process's `/proc/PID/*` requires `CAP_SYS_PTRACE` with `PTRACE_MODE_READ` or `CAP_PERFMON`
- OpenSSL [`openssl-cms(1)`](https://docs.openssl.org/master/man1/openssl-cms/) — `-verify`, `-noverify`, `-no_content_verify`, `-nosigs`; [`openssl-passphrase-options(1)`](https://docs.openssl.org/master/man1/openssl-passphrase-options/) — `pass:` (visible to `ps`), `env:`, `file:`, `fd:`, `stdin`

---

## Open questions

1. **Would an AltStore user with permission-checking enabled actually be blocked by a signed-but-undeclared artifact?** The check itself is now read and cited (§4c): `VerifyAppOperation.verifyPermissions` runs before resigning and throws `VerificationError.undeclaredPermissions` when a claimed entitlement is absent from the source. What is *not* verified is the end-to-end runtime behaviour — the mode is per-call and user-overridable (`AppManager.swift:1145`), so the practical failure rate on a real device is **UNVERIFIED**.
2. **Is the Apple-side `security(1)` tool macOS-only in so many words?** Apple states "(macOS only)" for `codesign` but not, in the pages read, for `security`. **UNVERIFIED** as a quote.
3. **Would invoking an unmodified AGPL-3.0 `ldid` binary as a subprocess from Feather trigger the AGPL's §13 network condition?** A licensing question, not a code question; needs a real review before adoption. Not asserted either way here.
4. **Does `zsign`'s generated CMS satisfy a modern iOS device's full check** (DER entitlements, `-7` slot, chain-to-Apple-root, the secure-timestamp question for anything non-App-Store)? The code path is present (`slot -7` at `src/archo.cpp:512`, DER encoder at `src/signing.cpp:23-88`), but **no primary source was found that certifies the output** — the only honest statement is that this would need to be measured on a real device. **UNVERIFIED.**
5. **What does Feather's live catalogue look like in terms of actual IPA sizes** — i.e. is the 2 GiB `MAX_CONTENT_LENGTH` ceiling, and the resulting 2–3× temp spike, a real operational risk or theoretical? `data/ipas/` was 1.3 GB at plan-011 time (`plans/011-garage-s3-ipa-storage.md:32`); current figures were not measured here.
6. **Would a signing step require re-running `inspect_ipa` after signing?** Signing rewrites `Info.plist`-adjacent state and the bundle seal, so the preflight data (`bundleIdentifier`, `version`, `buildVersion`, privacy keys) that all five ingest paths publish is computed from the *pre-signing* bytes (`scripts/ipa_inspection.py:48`). Nothing in this note resolves the ordering; it is a design decision for whoever picks this up.
7. **Does the iOS app's `.onlyModify` path leave an incoming signature valid?** This decides whether the "consumer that installs as-is" of §8e actually exists. The signer is skipped (`SigningHandler.swift:109-119`), but the same step rewrites `Info.plist` unconditionally (`:226`, `:260`) — and `Info.plist` is sealed in code-directory slot −1 (§1) — and may delete files (`:88-89`). Read at source, not run. **UNVERIFIED**; needs a device.
8. **Which Apple-specific certificate extensions identify "Apple Development" vs "Apple Distribution" vs in-house?** TN3161 §Digital certificate says Apple-specific extensions are documented on the Apple PKI page, but no list of OIDs was found there or in the technote. A type label derived from the subject CN would be a heuristic, not an assertion. **UNVERIFIED.**
9. **Is there any documented way to learn that a *provisioning profile* has been invalidated?** TN3125 documents the on-device signature check; nothing offline (or found server-side) reports profile revocation, so "this profile is still valid" cannot be asserted at upload time. **UNVERIFIED.**
10. **Does `python:3.11-slim` (`Dockerfile:1`) ship the `openssl` CLI, and does its OpenSSL 3 build load the legacy provider?** Both the `openssl cms -verify` profile-read route and any `openssl pkcs12` route depend on it, and both zsign and ldid call `OSSL_PROVIDER_load(NULL, "legacy")` for old PKCS#12 encryption (§3a, §8c). Not verified in this environment.
11. **Can a non-root process of the *same uid* read another process's `/proc/<pid>/cmdline`?** The kernel documentation is phrased in terms of `CAP_SYS_PTRACE`/`CAP_PERFMON` (which the root case satisfies), while the classic ptrace access-mode rule also admits same-uid readers. The precise rule for the `altstore` uid inside this container is **UNVERIFIED**; the root/`CAP_SYS_PTRACE` exposure of §8d does not depend on the answer.
