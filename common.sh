export SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

replace() {
    local org="$2" new="$3"
    find "$1" -type f -exec sed -i "s@${org}@${new}@g" {} \;
}

set_keys() {
    : "${LOCAL_TEST_JKS:?LOCAL_TEST_JKS must contain the base64 local.properties}"
    : "${STORE_TEST_JKS:?STORE_TEST_JKS must contain the base64 keystore}"
    mkdir -p "$SCRIPT_DIR/keys"
    printf '%s' "$LOCAL_TEST_JKS" | base64 -d > "$SCRIPT_DIR/keys/local.properties"
    printf '%s' "$STORE_TEST_JKS" | base64 -d > "$SCRIPT_DIR/keys/test.jks"
    unset LOCAL_TEST_JKS STORE_TEST_JKS
}

sign_apk() {
    local apksigner
    apksigner="$(find "$ANDROID_HOME/build-tools" -type f -name apksigner | sort -V | tail -n 1 || true)"
    if [ -z "$apksigner" ] || [ ! -x "$apksigner" ]; then
        echo "ERROR: apksigner not found under $ANDROID_HOME/build-tools" >&2
        return 1
    fi
    source "$SCRIPT_DIR/keys/local.properties"
    for var in storePassword keyPassword keyAlias; do
        if [ -z "${!var:-}" ]; then
            echo "ERROR: the decoded local.properties must define $var" >&2
            return 1
        fi
    done
    "$apksigner" sign -verbose -ks "$SCRIPT_DIR/keys/test.jks" \
        --ks-type "${KEYSTORE_TYPE:-PKCS12}" \
        --ks-pass "pass:$storePassword" --key-pass "pass:$keyPassword" \
        --ks-key-alias "$keyAlias" --out "$2" "$1"
}

sign_aab() {
    source "$SCRIPT_DIR/keys/local.properties"
    jarsigner -verbose -sigalg SHA256withRSA -digestalg SHA-256 \
        -storetype "${KEYSTORE_TYPE:-PKCS12}" \
        -keystore "$SCRIPT_DIR/keys/test.jks" -storepass "$storePassword" \
        -keypass "$keyPassword" -signedjar "$2" "$1" "$keyAlias"
}

version_lt() {
  [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n1)" = "$1" ]
}
