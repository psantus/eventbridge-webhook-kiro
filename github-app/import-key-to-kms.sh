#!/usr/bin/env bash
# Import a GitHub App private key (.pem) into a new non-exportable KMS key.
# Usage: import-key-to-kms.sh <app.pem> <alias-name>
# Needs OpenSSL 3+ (id-aes256-wrap-pad), the AWS CLI and jq.
# Prints the key ARN. Delete the .pem afterwards: GitHub can issue a new one.
set -euo pipefail
PEM="$1"; ALIAS="alias/$2"
OSSL="${OPENSSL:-openssl}"
D=$(mktemp -d); trap 'rm -rf "$D"' EXIT; chmod 700 "$D"

KEY_ID=$(aws kms create-key --origin EXTERNAL --key-spec RSA_2048 --key-usage SIGN_VERIFY \
  --description "GitHub App private key (imported)" --query KeyMetadata.KeyId --output text)
aws kms create-alias --alias-name "$ALIAS" --target-key-id "$KEY_ID"

aws kms get-parameters-for-import --key-id "$KEY_ID" \
  --wrapping-algorithm RSA_AES_KEY_WRAP_SHA_256 --wrapping-key-spec RSA_4096 --output json > "$D/params.json"
jq -r .PublicKey "$D/params.json" | base64 -d > "$D/wrap_pub.der"
jq -r .ImportToken "$D/params.json" | base64 -d > "$D/token.bin"

# key material: PKCS#8 DER, wrapped with an ephemeral AES-256 key (RFC 5649),
# and that AES key wrapped with KMS's RSA-OAEP-SHA256 public key
"$OSSL" pkcs8 -topk8 -nocrypt -inform PEM -in "$PEM" -outform DER -out "$D/key.der"
"$OSSL" rand -out "$D/aes.bin" 32
"$OSSL" enc -id-aes256-wrap-pad -K "$(xxd -p -c 64 "$D/aes.bin")" -iv A65959A6 \
  -in "$D/key.der" -out "$D/key.wrapped"
"$OSSL" pkeyutl -encrypt -pubin -keyform DER -inkey "$D/wrap_pub.der" -in "$D/aes.bin" \
  -out "$D/aes.wrapped" -pkeyopt rsa_padding_mode:oaep -pkeyopt rsa_oaep_md:sha256 -pkeyopt rsa_mgf1_md:sha256
cat "$D/aes.wrapped" "$D/key.wrapped" > "$D/material.bin"

aws kms import-key-material --key-id "$KEY_ID" --encrypted-key-material "fileb://$D/material.bin" \
  --import-token "fileb://$D/token.bin" --expiration-model KEY_MATERIAL_DOES_NOT_EXPIRE

# check: KMS public key == public key of the .pem
a=$(aws kms get-public-key --key-id "$KEY_ID" --query PublicKey --output text | base64 -d | shasum -a 256)
b=$("$OSSL" pkey -in "$PEM" -pubout -outform DER | shasum -a 256)
[ "$a" = "$b" ] || { echo "public key mismatch" >&2; exit 1; }
aws kms describe-key --key-id "$KEY_ID" --query KeyMetadata.Arn --output text
