#!/bin/bash
set -euo pipefail
umask 077

source common.sh

# Defaults preserve the historical behaviour (ARMv7 + ARM64 APK + ARM64 AAB).
# The release workflow overrides these to build only the ARM64 APK.
BUILD_ABIS="${BUILD_ABIS:-arm arm64}"
BUILD_BUNDLE="${BUILD_BUNDLE:-true}"
SYMBOL_LEVEL="${SYMBOL_LEVEL:-}"

for abi in $BUILD_ABIS; do
    case "$abi" in
        arm|arm64) ;;
        *) echo "::error::unsupported ABI in BUILD_ABIS: $abi" >&2; exit 2 ;;
    esac
done

if [ "$BUILD_BUNDLE" = "true" ]; then
    case " $BUILD_ABIS " in
        *" arm64 "*) ;;
        *) echo "::error::BUILD_BUNDLE=true requires arm64 in BUILD_ABIS" >&2; exit 2 ;;
    esac
else
    BUILD_BUNDLE=false
fi

if [ -n "$SYMBOL_LEVEL" ]; then
    case "$SYMBOL_LEVEL" in
        0|1|2) ;;
        *) echo "::error::SYMBOL_LEVEL must be 0, 1 or 2" >&2; exit 2 ;;
    esac
fi

# Set SIGN_RELEASE=false on a machine that must never see the release keystore.
# The output is then an unsigned APK named *-unsigned.apk, which is not a
# release artifact and must be signed elsewhere before it can be installed.
SIGN_RELEASE="${SIGN_RELEASE:-true}"
case "$SIGN_RELEASE" in
    true|false) ;;
    *) echo "::error::SIGN_RELEASE must be true or false" >&2; exit 2 ;;
esac

# RESUME=1 accepts a chromium/ tree left behind by an earlier job and skips
# everything that prepares it: the Chromium fetch, git am, gclient sync and
# patch.sh. A Chromium build does not fit in GitHub's 360-minute job limit on a
# 4-core hosted runner, so .github/workflows/chained-build.yml carries the tree
# between jobs and each job continues the same ninja build. Re-preparing a
# resumed tree is not merely wasteful: git am fails on an already-patched
# checkout, and patch.sh has edits written as `s|X|&Y|`, which would append a
# second Y.
RESUME="${RESUME:-0}"
case "$RESUME" in
    0|1) ;;
    *) echo "::error::RESUME must be 0 or 1" >&2; exit 2 ;;
esac

trap 'rm -rf "$SCRIPT_DIR/keys"' EXIT
if [ "$SIGN_RELEASE" = "true" ]; then
    set_keys
fi

VERSION="$(grep -m1 -o '[0-9]\+\(\.[0-9]\+\)\{3\}' vanadium/args.gn || true)"
if [ -z "$VERSION" ]; then
    echo "::error::could not read the Chromium version from vanadium/args.gn" >&2
    exit 1
fi
export VERSION

CHROMIUM_SOURCE=https://chromium.googlesource.com/chromium/src.git # https://github.com/chromium/chromium.git
export DEBIAN_FRONTEND=noninteractive

# A runner takes jobs unattended, so it can never answer a "[sudo] password"
# prompt: the step would sit there until the job timeout. Fail here instead of
# forty minutes into provisioning. GitHub-hosted runners already grant
# passwordless sudo to the runner user.
if ! sudo -n true 2>/dev/null; then
    echo "::error::this build host needs passwordless sudo for $(whoami): build.sh and build/install-build-deps.sh call sudo repeatedly and a runner cannot answer a password prompt. Add a sudoers drop-in (for a dedicated build host: '<user> ALL=(ALL) NOPASSWD:ALL' in /etc/sudoers.d/, mode 0440), or run the job on a GitHub-hosted runner." >&2
    exit 1
fi

# python3-pil is the real Pillow binary package; python3-pillow, the name used
# upstream, only works as the virtual package it Provides.
sudo apt-get update
# bzip2 has to be here rather than left to install-build-deps.sh below: the
# AFDO hook that `gclient runhooks` runs (src/tools/download_optimization_profile.py)
# shells out to bzip2, and runhooks happens before install-build-deps.sh, so a
# host without it dies ~25 minutes into the sync with a bare FileNotFoundError.
# unzip is what the workflow's artifact check uses to test the .aab.
sudo apt-get install -y sudo lsb-release file nano git curl python3 python3-pil imagemagick librsvg2-bin bzip2 zip unzip
sudo dpkg --add-architecture i386; sudo apt-get update; sudo apt-get install -y libgcc-s1:i386

if [ ! -d depot_tools ]; then
    git clone --depth 1 https://chromium.googlesource.com/chromium/tools/depot_tools.git \
        || { echo "::error::failed to clone depot_tools" >&2; exit 1; }
fi
export PATH="$PWD/depot_tools:$PATH"

mkdir -p chromium/src
cd chromium/src

resume=0
if [ "$RESUME" = "1" ] && [ -f out/Default/.titanium-prepared ]; then
    resume=1
    echo "::notice::resuming the cached checkout in $PWD: skipping fetch, patches, runhooks and patch.sh"
fi

if [ "$resume" = "0" ]; then
    mkdir -p out/Default
    git init
    if git remote get-url origin >/dev/null 2>&1; then
        git remote set-url origin "$CHROMIUM_SOURCE"
    else
        git remote add origin "$CHROMIUM_SOURCE"
    fi
    git fetch --depth 1 "$CHROMIUM_SOURCE" "+refs/tags/$VERSION:chromium_$VERSION" \
        || { echo "::error::failed to fetch Chromium $VERSION" >&2; exit 1; }
    git checkout "$VERSION"
    cp "$SCRIPT_DIR/.gclient" ../.gclient

    # https://grapheneos.org/build#browser-and-webview
    rm -rf "$SCRIPT_DIR"/vanadium/patches/*trichrome-{apk-build-targets,browser-apk-targets}.patch
    rm -rf "$SCRIPT_DIR"/vanadium/patches/*{detailed,supported}-language*.patch
    rm -rf "$SCRIPT_DIR"/vanadium/patches/*javascript-optimizer-{site-setting,settings-UI}.patch
    rm -rf "$SCRIPT_DIR"/vanadium/patches/*component-updates.patch
    rm -rf "$SCRIPT_DIR"/vanadium/patches/*{pdf,PDF,for-content-public,toolbar-button,configs-from-config-app,config-app-parsing,new-tab-card,predictive-back}*.patch
    # rm -rf "$SCRIPT_DIR"/vanadium/patches/*crashpad*.patch
    replace "$SCRIPT_DIR/vanadium/patches" "VANADIUM" "TITANIUM"
    replace "$SCRIPT_DIR/vanadium/patches" "Vanadium" "Titanium"
    replace "$SCRIPT_DIR/vanadium/patches" "vanadium" "titanium"
    # git am records commits, so it needs a committer identity, and it needs one for
    # every repository it writes to: src here, plus the v8 and
    # third_party/search_engines_data/resources checkouts that `gclient runhooks`
    # patches through vanadium/tools/common/apply_subprojects_patches.py. A
    # repo-local setting only covers src. Set it globally.
    # git am aborts *before* printing "Applying:" when the identity is missing, and
    # that hook reads git's stdout, so on a host with no global identity (a fresh
    # WSL distro, any plain build box) runhooks dies as
    # "IndexError: list index out of range" and the real reason, which git wrote to
    # stderr, is discarded. GitHub-hosted runners ship a global identity, which is
    # why only self-hosted runs ever showed the crash.
    git config --global user.email "titanium-build@localhost" \
        || { echo "::error::could not set a global git committer identity (is HOME set for $(whoami)?)" >&2; exit 1; }
    git config --global user.name "Titanium Build"
    git am --whitespace=nowarn --keep-non-patch "$SCRIPT_DIR"/vanadium/patches/*.patch \
        || { echo "::error::applying the Vanadium/Titanium patch series failed" >&2; exit 1; }

    gclient sync -D --no-history --nohooks \
        || { echo "::error::gclient sync failed" >&2; exit 1; }
    gclient runhooks \
        || { echo "::error::gclient runhooks failed" >&2; \
             echo "  Two causes have bitten here. apply_subprojects_patches.py hides" >&2; \
             echo "  git's stderr, so an IndexError means git refused to run at all:" >&2; \
             echo "  with the identity now set globally, that leaves ownership (this" >&2; \
             echo "  build runs as $(whoami) and git rejects a checkout written by" >&2; \
             echo "  another user). A FileNotFoundError instead names a missing host" >&2; \
             echo "  tool, bzip2 for the AFDO profile hook, and is fixed by the" >&2; \
             echo "  apt-get install above." >&2; \
             exit 1; }

    # Marked only now: out/Default exists from the first line of this branch, and
    # a job killed during the sync would otherwise look ready to resume.
    : > out/Default/.titanium-prepared
fi

# depot_tools is a fresh shallow clone in every job. A resumed tree skips
# gclient, so nothing has bootstrapped the python shim depot_tools puts first on
# PATH, and install-build-deps.sh resolves python3 to it and dies with
# "python3_bin_reldir.txt not found. need to initialize depot_tools".
if [ "$resume" = "1" ]; then
    if [ -x "$SCRIPT_DIR/depot_tools/ensure_bootstrap" ]; then
        ( cd "$SCRIPT_DIR/depot_tools" && ./ensure_bootstrap ) \
            || { echo "::error::could not bootstrap depot_tools on the resumed tree" >&2; exit 1; }
    else
        "$SCRIPT_DIR/depot_tools/gclient" --version >/dev/null \
            || { echo "::error::could not bootstrap depot_tools on the resumed tree" >&2; exit 1; }
    fi
fi

# Runs in both paths: a resumed tree is on a fresh OS image, so the host
# packages are always missing.
./build/install-build-deps.sh --no-prompt \
    || { echo "::error::install-build-deps.sh failed" >&2; exit 1; }

if [ "$resume" = "0" ]; then
    source "$SCRIPT_DIR/patch.sh"
fi
cp "$SCRIPT_DIR/args.gn" out/Default/args.gn
if [ -n "$SYMBOL_LEVEL" ]; then
    if grep -q '^symbol_level = ' out/Default/args.gn; then
        sed -i -E "s/^symbol_level = .*/symbol_level = ${SYMBOL_LEVEL}/" out/Default/args.gn
    else
        echo "symbol_level = ${SYMBOL_LEVEL}" >> out/Default/args.gn
    fi
fi
if ! printf '%s\n' "$BUILD_ABIS" | tr ' ' '\n' | grep -qx arm; then
    sed -i 's/^target_cpu = "arm"$/target_cpu = "arm64"/' out/Default/args.gn
fi
gn gen out/Default # gn args out/Default; echo 'treat_warnings_as_errors = false' >> out/Default/args.gn
mkdir -p out/tmp out/release

stage_apk() {
    local suffix="$1" found
    mapfile -t found < <(find out/Default/apks -name 'Chrome*.apk')
    if [ "${#found[@]}" -ne 1 ]; then
        echo "::error::expected exactly one APK in out/Default/apks after the build, found ${#found[@]}" >&2
        exit 1
    fi
    mv "${found[0]}" "out/tmp/${VERSION}-${suffix}.apk"
}

for abi in $BUILD_ABIS; do
    case "$abi" in
        arm)
            autoninja -C out/Default chrome_public_apk \
                || { echo "::error::ARMv7 APK build failed" >&2; exit 1; }
            stage_apk armeabi-v7a
            ;;
        arm64)
            sed -i 's/^target_cpu = "arm"$/target_cpu = "arm64"/' out/Default/args.gn
            targets=(chrome_public_apk)
            if [ "$BUILD_BUNDLE" = "true" ]; then
                targets+=(chrome_public_bundle)
            fi
            autoninja -C out/Default "${targets[@]}" \
                || { echo "::error::ARM64 build failed" >&2; exit 1; }
            stage_apk arm64-v8a
            if [ "$BUILD_BUNDLE" = "true" ]; then
                mapfile -t bundles < <(find out/Default/apks -name 'Chrome*.aab')
                if [ "${#bundles[@]}" -ne 1 ]; then
                    echo "::error::expected exactly one AAB, found ${#bundles[@]}" >&2
                    exit 1
                fi
                mv "${bundles[0]}" "out/tmp/${VERSION}-arm64-v8a.aab"
            fi
            ;;
    esac
done

export PATH="$PWD/third_party/jdk/current/bin/:$PATH"
export ANDROID_HOME="$PWD/third_party/android_sdk/public"

if [ "$SIGN_RELEASE" = "false" ]; then
    for abi in $BUILD_ABIS; do
        case "$abi" in
            arm) suffix=armeabi-v7a ;;
            arm64) suffix=arm64-v8a ;;
        esac
        cp "out/tmp/${VERSION}-${suffix}.apk" \
            "out/release/${VERSION}-${suffix}-unsigned.apk"
    done
    if [ "$BUILD_BUNDLE" = "true" ]; then
        cp "out/tmp/${VERSION}-arm64-v8a.aab" \
            "out/release/${VERSION}-arm64-v8a-unsigned.aab"
    fi
    echo "::warning::SIGN_RELEASE=false: out/release holds UNSIGNED artifacts that are"
    echo "::warning::neither installable nor releasable; sign them before use."
else
    for abi in $BUILD_ABIS; do
        case "$abi" in
            arm) suffix=armeabi-v7a ;;
            arm64) suffix=arm64-v8a ;;
        esac
        sign_apk "out/tmp/${VERSION}-${suffix}.apk" "out/release/${VERSION}-${suffix}.apk"
    done
    if [ "$BUILD_BUNDLE" = "true" ]; then
        sign_aab "out/tmp/${VERSION}-arm64-v8a.aab" "out/release/${VERSION}-arm64-v8a.aab"
    fi
fi

echo "Built Chromium ${VERSION} for: ${BUILD_ABIS} (bundle=${BUILD_BUNDLE})"
