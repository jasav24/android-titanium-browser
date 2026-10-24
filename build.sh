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

# python3-pil is the real Pillow binary package; python3-pillow, the name used
# upstream, only works as the virtual package it Provides.
sudo apt-get update
sudo apt-get install -y sudo lsb-release file nano git curl python3 python3-pil imagemagick librsvg2-bin
sudo dpkg --add-architecture i386; sudo apt-get update; sudo apt-get install -y libgcc-s1:i386

if [ ! -d depot_tools ]; then
    git clone --depth 1 https://chromium.googlesource.com/chromium/tools/depot_tools.git \
        || { echo "::error::failed to clone depot_tools" >&2; exit 1; }
fi
export PATH="$PWD/depot_tools:$PATH"

mkdir -p chromium/src/out/Default
cd chromium/src
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
# git am records commits, so it needs a committer identity. GitHub-hosted
# runners arrive with one; a plain build host does not, and git am then dies
# with "unable to auto-detect email address".
git config user.email "titanium-build@localhost"
git config user.name "Titanium Build"
git am --whitespace=nowarn --keep-non-patch "$SCRIPT_DIR"/vanadium/patches/*.patch \
    || { echo "::error::applying the Vanadium/Titanium patch series failed" >&2; exit 1; }

gclient sync -D --no-history --nohooks \
    || { echo "::error::gclient sync failed" >&2; exit 1; }
gclient runhooks \
    || { echo "::error::gclient runhooks failed" >&2; exit 1; }
./build/install-build-deps.sh --no-prompt \
    || { echo "::error::install-build-deps.sh failed" >&2; exit 1; }

source "$SCRIPT_DIR/patch.sh"
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
