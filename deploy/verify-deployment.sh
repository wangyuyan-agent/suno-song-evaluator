#!/bin/sh
set -eu

usage() {
  cat <<'EOF'
Usage: deploy/verify-deployment.sh BASE_URL USERNAME PASSWORD_FILE

Checks /health, the unauthenticated root, and the authenticated root. The
password is read from a single-line file and is not placed in the curl command
line or environment.

Optional environment variables:
  SONG_EVAL_VERIFY_HOST       exact Host header for a loopback endpoint
  SONG_EVAL_VERIFY_INSECURE   set to 1 only for an isolated local-CA smoke test
  SONG_EVAL_VERIFY_ATTEMPTS   health attempts before failure (default: 20)
EOF
}

if [ "$#" -ne 3 ]; then
  usage >&2
  exit 2
fi

base_url=${1%/}
username=$2
password_file=$3
host_header=${SONG_EVAL_VERIFY_HOST:-}
insecure=${SONG_EVAL_VERIFY_INSECURE:-0}
attempts=${SONG_EVAL_VERIFY_ATTEMPTS:-20}

is_single_line() {
  candidate=$1
  [ -n "$candidate" ] || return 1
  [ "$(printf '%s\n' "$candidate" | wc -l | tr -d ' ')" -eq 1 ] || return 1
  ! printf '%s' "$candidate" | LC_ALL=C grep -q "$(printf '\r')"
}

case "$base_url" in
  http://*|https://*) ;;
  *)
    echo "error: BASE_URL must begin with http:// or https://" >&2
    exit 2
    ;;
esac
if ! is_single_line "$base_url" || ! is_single_line "$username"; then
  echo "error: BASE_URL and USERNAME must be non-empty single-line values" >&2
  exit 2
fi
if [ -n "$host_header" ] && ! is_single_line "$host_header"; then
  echo "error: SONG_EVAL_VERIFY_HOST must be a single-line value" >&2
  exit 2
fi
case "$attempts" in
  ''|*[!0-9]*|0)
    echo "error: SONG_EVAL_VERIFY_ATTEMPTS must be a positive integer" >&2
    exit 2
    ;;
esac
case "$insecure" in
  0|1) ;;
  *)
    echo "error: SONG_EVAL_VERIFY_INSECURE must be 0 or 1" >&2
    exit 2
    ;;
esac

if [ ! -f "$password_file" ] || [ ! -r "$password_file" ]; then
  echo "error: password file is not a readable regular file" >&2
  exit 2
fi
if LC_ALL=C grep -q "$(printf '\r')" "$password_file"; then
  echo "error: password file must not contain carriage returns" >&2
  exit 2
fi
if ! awk 'NR > 1 { exit 1 } END { if (NR == 0) exit 1 }' "$password_file"; then
  echo "error: password file must contain exactly one line" >&2
  exit 2
fi
password=$(sed -n '1p' "$password_file")
if [ -z "$password" ]; then
  echo "error: password must not be empty" >&2
  exit 2
fi

request_status() {
  request_url=$1
  if [ "$insecure" -eq 1 ] && [ -n "$host_header" ]; then
    curl --insecure --silent --show-error --output /dev/null \
      --connect-timeout 5 --max-time 15 --header "Host: $host_header" \
      --write-out '%{http_code}' "$request_url"
  elif [ "$insecure" -eq 1 ]; then
    curl --insecure --silent --show-error --output /dev/null \
      --connect-timeout 5 --max-time 15 \
      --write-out '%{http_code}' "$request_url"
  elif [ -n "$host_header" ]; then
    curl --silent --show-error --output /dev/null \
      --connect-timeout 5 --max-time 15 --header "Host: $host_header" \
      --write-out '%{http_code}' "$request_url"
  else
    curl --silent --show-error --output /dev/null \
      --connect-timeout 5 --max-time 15 \
      --write-out '%{http_code}' "$request_url"
  fi
}

curl_config_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

authenticated_status() {
  escaped_url=$(curl_config_escape "$base_url/")
  escaped_user=$(curl_config_escape "$username")
  escaped_password=$(curl_config_escape "$password")
  {
    echo 'silent'
    echo 'show-error'
    echo 'output = "/dev/null"'
    echo 'connect-timeout = 5'
    echo 'max-time = 15'
    echo 'write-out = "%{http_code}"'
    [ "$insecure" -eq 0 ] || echo 'insecure'
    if [ -n "$host_header" ]; then
      printf 'header = "Host: %s"\n' "$(curl_config_escape "$host_header")"
    fi
    printf 'user = "%s:%s"\n' "$escaped_user" "$escaped_password"
    printf 'url = "%s"\n' "$escaped_url"
  } | curl --config -
}

health_status=000
attempt=1
while [ "$attempt" -le "$attempts" ]; do
  health_status=$(request_status "$base_url/health" || true)
  [ "$health_status" = 200 ] && break
  sleep 1
  attempt=$((attempt + 1))
done
if [ "$health_status" != 200 ]; then
  echo "error: health returned $health_status after $attempts attempts" >&2
  exit 1
fi

unauthenticated_status=$(request_status "$base_url/" || true)
if [ "$unauthenticated_status" != 401 ]; then
  echo "error: unauthenticated root returned $unauthenticated_status, expected 401" >&2
  exit 1
fi

authenticated_root_status=$(authenticated_status || true)
if [ "$authenticated_root_status" != 200 ]; then
  echo "error: authenticated root returned $authenticated_root_status, expected 200" >&2
  exit 1
fi

echo "Deployment verification passed."
echo "  health: 200"
echo "  unauthenticated root: 401"
echo "  authenticated root: 200"
