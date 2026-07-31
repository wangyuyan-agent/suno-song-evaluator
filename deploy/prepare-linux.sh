#!/bin/sh
set -eu

# Prepare the default Compose bind mounts without printing generated secrets.
# Run this script with sudo from any working directory.

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
audio_dir="$project_dir/audio"
secrets_dir="$project_dir/secrets"
password_file="$secrets_dir/admin_password.txt"
container_uid=10001
container_gid=10001

if [ "$(id -u)" -ne 0 ]; then
  echo "error: run with sudo so numeric container ownership can be set" >&2
  exit 1
fi

for value in "$container_uid" "$container_gid"; do
  case "$value" in
    ''|*[!0-9]*)
      echo "error: invalid numeric container identity" >&2
      exit 1
      ;;
  esac
done

if [ -n "${SUDO_UID:-}" ]; then
  host_uid=$SUDO_UID
else
  host_uid=$(stat -c '%u' "$project_dir")
fi

for path in "$audio_dir" "$secrets_dir" "$password_file"; do
  if [ -L "$path" ]; then
    echo "error: refusing symbolic-link deployment path: $path" >&2
    exit 1
  fi
done

install -d -o "$host_uid" -g "$container_gid" -m 2750 "$audio_dir"
install -d -o 0 -g 0 -m 0700 "$secrets_dir"

created_password=0
if [ ! -e "$password_file" ]; then
  temporary_password="$secrets_dir/.admin_password.$$"
  trap 'rm -f "$temporary_password"' EXIT HUP INT TERM
  umask 077
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 36 | tr -d '\n' > "$temporary_password"
  else
    dd if=/dev/urandom bs=36 count=1 2>/dev/null \
      | base64 \
      | tr -d '\n' > "$temporary_password"
  fi
  test -s "$temporary_password"
  mv "$temporary_password" "$password_file"
  trap - EXIT HUP INT TERM
  created_password=1
fi

if [ ! -f "$password_file" ] || [ ! -s "$password_file" ]; then
  echo "error: administrator password must be a non-empty regular file" >&2
  exit 1
fi
if LC_ALL=C grep -q "$(printf '\r')" "$password_file"; then
  echo "error: administrator password file must not contain carriage returns" >&2
  exit 1
fi
if ! awk 'NR > 1 { exit 1 } END { if (NR == 0) exit 1 }' "$password_file"; then
  echo "error: administrator password must occupy one line" >&2
  exit 1
fi
password_length=$(awk 'NR == 1 { print length($0) }' "$password_file")
if [ "$password_length" -lt 16 ]; then
  echo "error: administrator password must contain at least 16 characters" >&2
  exit 1
fi

chown "$container_uid:$container_gid" "$password_file"
chmod 0400 "$password_file"

# Existing regular files become readable by the non-root container group. The
# invoking deployment user remains their owner and can continue adding audio.
find "$audio_dir" -xdev -type d \
  -exec chown "$host_uid:$container_gid" {} + \
  -exec chmod 2750 {} +
find "$audio_dir" -xdev -type f \
  -exec chown "$host_uid:$container_gid" {} + \
  -exec chmod 0640 {} +

echo "Prepared Compose state directories."
echo "  audio: $audio_dir (owner UID $host_uid, group GID $container_gid)"
echo "  password: $password_file (owner UID/GID $container_uid, mode 0400)"
if [ "$created_password" -eq 1 ]; then
  echo "  generated: yes; retrieve it explicitly with sudo when needed"
else
  echo "  generated: no; preserved the existing password"
fi
