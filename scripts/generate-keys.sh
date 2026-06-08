#!/usr/bin/env bash
# Generate all keys and certificates the stack needs.
# Run once after filling in .env:  bash scripts/generate-keys.sh
set -uo pipefail
. "$(dirname "$0")/_env.sh"
need TELEMETRY_HOST

cd "$ROOT"
# Runtime dirs (created here, owned by you, so compose doesn't auto-create root-owned ones)
mkdir -p keys proxy certs pubkey/.well-known/appspecific config mosquitto/config mosquitto/data mosquitto/log

echo "1/3  Fleet command key (EC prime256v1) + public key for partner registration"
if [ ! -f keys/fleet-key.pem ]; then
  openssl ecparam -name prime256v1 -genkey -noout -out keys/fleet-key.pem
fi
openssl ec -in keys/fleet-key.pem -pubout -out pubkey/.well-known/appspecific/com.tesla.3p.public-key.pem

echo "2/3  Command-proxy self-signed TLS cert (the bridge trusts this)"
if [ ! -f proxy/tls-cert.pem ]; then
  openssl req -x509 -nodes -newkey ec -pkeyopt ec_paramgen_curve:secp384r1 -pkeyopt ec_param_enc:named_curve \
    -subj '/CN=tesla-http-proxy' -addext 'subjectAltName=DNS:tesla-http-proxy' \
    -addext 'extendedKeyUsage=serverAuth' -addext 'keyUsage=digitalSignature,keyCertSign,keyAgreement' \
    -keyout proxy/tls-key.pem -out proxy/tls-cert.pem -sha256 -days 3650
fi

echo "3/3  Telemetry CA + server cert for ${TELEMETRY_HOST} (CA is handed to Tesla at registration)"
cd certs
if [ ! -f ca.crt ]; then
  openssl ecparam -name prime256v1 -genkey -noout -out ca.key
  openssl req -x509 -new -nodes -key ca.key -sha256 -days 3650 -subj "/CN=${TELEMETRY_HOST} Telemetry CA" -out ca.crt
fi
openssl ecparam -name prime256v1 -genkey -noout -out server.key
openssl req -new -key server.key -subj "/CN=${TELEMETRY_HOST}" -out server.csr
printf 'subjectAltName=DNS:%s\n' "$TELEMETRY_HOST" > san.ext
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 3650 -sha256 -extfile san.ext -out server.crt
rm -f server.csr san.ext
cd "$ROOT"

# Make readable by the container user (containers run as PUID:PGID, not the file owner).
chmod 644 keys/fleet-key.pem proxy/tls-key.pem proxy/tls-cert.pem certs/server.key certs/server.crt certs/ca.crt
chmod 755 keys proxy certs

echo
echo "Done. Public key to host at https://${PARTNER_DOMAIN:-<PARTNER_DOMAIN>}/.well-known/appspecific/com.tesla.3p.public-key.pem :"
cat pubkey/.well-known/appspecific/com.tesla.3p.public-key.pem
