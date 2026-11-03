"""The fixed shell scripts this package runs in a guest, and the argv that carries them.

important: nothing a person typed and nothing a guest printed is ever pasted into a script. A
script is a constant, run as `/bin/sh -c SCRIPT orcastra-<name> ARG...`, and its data arrives as
positional parameters that were validated first (a user name, a home folder, a uid) or on stdin.
The audit record names the script and its digest, and LXD's operation metadata shows its text,
which holds nothing secret. `tests/test_the_guest_scripts_are_fixed_and_never_interpolate.py` pins
the digests, so a change here is a reviewed change.

The scripts are POSIX sh and run under dash, bash and busybox ash alike. Each prints marker lines
(`ORCA_...`, `key=value`) that the parsers read; anything else a guest prints is ignored.
"""

from __future__ import annotations

import hashlib
from typing import Dict, List

# Facts for the overview. Runs as root. /etc/shadow leaves the guest only as an awk summary of
# name, state, hash scheme id and last change: never the hash.
INVENTORY = r"""
set -u
for t in useradd usermod gpasswd chpasswd chage passwd adduser addgroup delgroup sudo restorecon \
    selinuxenabled flock sha256sum base64 stat mktemp timeout awk bash sshd systemctl rc-service; do
  command -v "$t" >/dev/null 2>&1 && printf 'tool=%s\n' "$t"
done
# Ask passwd itself: on Alpine it is bbsuid, a set-uid wrapper, so a link check misses busybox.
command -v passwd >/dev/null 2>&1 && passwd --help 2>&1 | head -n 2 | grep -q BusyBox && echo busybox=yes
command -v selinuxenabled >/dev/null 2>&1 && selinuxenabled 2>/dev/null && echo selinux=enabled
echo ORCA_BEGIN os-release
head -c 8192 /etc/os-release 2>/dev/null
echo; echo ORCA_END
echo ORCA_BEGIN login.defs
grep -E '^[[:space:]]*(UID_MIN|UID_MAX|ENCRYPT_METHOD)[[:space:]]' /etc/login.defs 2>/dev/null
echo ORCA_END
echo ORCA_BEGIN passwd
head -c 1048576 /etc/passwd 2>/dev/null
echo; echo ORCA_END
echo ORCA_BEGIN groups
grep -E '^(sudo|wheel|admin):' /etc/group 2>/dev/null
echo ORCA_END
echo ORCA_BEGIN shadow
if command -v awk >/dev/null 2>&1 && [ -r /etc/shadow ]; then
  awk -F: 'NF >= 2 {
    h = $2; st = "set"; sch = ""
    if (h == "") st = "empty"
    else if (substr(h, 1, 1) == "!") { x = h; sub(/^!+/, "", x); if (x == "" || x == "*") st = "none"; else st = "locked" }
    else if (substr(h, 1, 1) == "*") st = "none"
    if (st == "set" || st == "locked") {
      x = h; sub(/^!+/, "", x)
      if (substr(x, 1, 1) == "$") { n = split(x, p, "$"); if (n >= 3 && length(p[2]) >= 1 && length(p[2]) <= 3 && p[2] ~ /^[0-9a-z]+$/) sch = p[2] }
      else if (length(x) == 13) sch = "des"
    }
    print $1 ":" st ":" sch ":" $3
  }' /etc/shadow
fi
echo ORCA_END
"""

# One line per account named in the arguments: whether sudo lets it run a command, and whether
# sudo asks for the target's or root's password instead of the user's own (openSUSE's default).
SUDO_VERDICTS = r"""
set -u
command -v sudo >/dev/null 2>&1 || { echo ORCA_SUDO_ABSENT; exit 0; }
for n in "$@"; do
  if sudo -n -l -U "$n" /bin/true >/dev/null 2>&1; then v=yes; else v=no; fi
  if [ "$v" = yes ] && sudo -n -l -U "$n" 2>/dev/null | grep -Eq '(^|[ ,])(targetpw|rootpw)([ ,]|$)'; then v=rootpw; fi
  printf 'ORCA_SUDO %s %s\n' "$n" "$v"
done
"""

# Reads one account's authorized_keys. Runs AS THAT ACCOUNT, so a link it planted can only lead
# to files it could read anyway. $1 home, $2 uid.
KEYS_READ = r"""
set -u
home=$1; uid=$2
fail() { echo "ORCA_FAIL $1"; exit 0; }
bounded() { if command -v timeout >/dev/null 2>&1; then timeout 10 "$@"; else "$@"; fi; }
[ "$(id -u)" = "$uid" ] || fail wrong_identity
cd -P -- "$home" 2>/dev/null || fail home_missing
[ "$(stat -c %u . 2>/dev/null)" = "$uid" ] || fail home_not_owned
printf 'ORCA_HOME_MODE %s\n' "$(stat -c %a .)"
if [ ! -e .ssh ] && [ ! -L .ssh ]; then echo "ORCA_STATE absent"; exit 0; fi
cd -P -- .ssh 2>/dev/null || fail ssh_dir_not_owned
[ "$(stat -c %u . 2>/dev/null)" = "$uid" ] || fail ssh_dir_not_owned
printf 'ORCA_SSH_MODE %s\n' "$(stat -c %a .)"
[ -L authorized_keys ] && { echo "ORCA_STATE link"; exit 0; }
[ -e authorized_keys ] || { echo "ORCA_STATE absent"; exit 0; }
[ -f authorized_keys ] || { echo "ORCA_STATE not_regular"; exit 0; }
[ -r authorized_keys ] || { echo "ORCA_STATE unreadable"; exit 0; }
printf 'ORCA_FILE_MODE %s %s\n' "$(stat -c %a authorized_keys)" "$(stat -c %u authorized_keys)"
echo "ORCA_STATE present"
echo "ORCA_BODY"
bounded head -c 262145 authorized_keys | base64
"""

# Replaces one account's authorized_keys with stdin, if it still is what was read. Runs AS THAT
# ACCOUNT. $1 home, $2 uid, $3 sha256 of the content the edit was made against, or "absent".
KEYS_WRITE = r"""
set -u
home=$1; uid=$2; expected=$3
fail() { echo "ORCA_FAIL $1"; exit 0; }
bounded() { if command -v timeout >/dev/null 2>&1; then timeout 10 "$@"; else "$@"; fi; }
[ "$(id -u)" = "$uid" ] || fail wrong_identity
umask 077
cd -P -- "$home" 2>/dev/null || fail home_missing
[ "$(stat -c %u . 2>/dev/null)" = "$uid" ] || fail home_not_owned
if [ ! -e .ssh ] && [ ! -L .ssh ]; then mkdir -m 700 .ssh 2>/dev/null || fail ssh_dir_create; fi
cd -P -- .ssh 2>/dev/null || fail ssh_dir_not_owned
[ "$(stat -c %u . 2>/dev/null)" = "$uid" ] || fail ssh_dir_not_owned
chmod 700 . 2>/dev/null || true
[ -L authorized_keys ] && fail key_file_is_link
if [ -e authorized_keys ] && [ ! -f authorized_keys ]; then fail key_file_not_regular; fi
if ( exec 9<. ) 2>/dev/null; then
  exec 9<.
  if command -v flock >/dev/null 2>&1; then
    i=0; got=no
    while [ "$i" -lt 5 ]; do
      if flock -n -x 9 2>/dev/null; then got=yes; break; fi
      i=$((i + 1)); sleep 1
    done
    [ "$got" = yes ] || fail locked
  fi
fi
if [ -e authorized_keys ]; then current=$(bounded sha256sum < authorized_keys | cut -d' ' -f1); else current=absent; fi
[ "$current" = "$expected" ] || fail conflict
tmp=$(mktemp ./.authorized_keys.XXXXXX 2>/dev/null) || fail tmp
trap 'rm -f -- "$tmp"' EXIT
cat > "$tmp" || fail write
chmod 600 "$tmp" || fail write
mv -f -- "$tmp" authorized_keys || fail rename
trap - EXIT
printf 'ORCA_OK %s %s\n' "$(bounded sha256sum < authorized_keys | cut -d' ' -f1)" "$(pwd -P)"
"""

# The SSH server, read only: installed, running, the effective global settings, host keys.
SSHD_FACTS = r"""
set -u
bin=""
for b in sshd /usr/sbin/sshd /usr/bin/sshd; do if command -v "$b" >/dev/null 2>&1; then bin=$(command -v "$b"); break; fi; done
if [ -n "$bin" ]; then echo installed=yes; else echo installed=no; fi
if command -v pgrep >/dev/null 2>&1; then pgrep -x sshd >/dev/null 2>&1 && echo process=yes; fi
if command -v systemctl >/dev/null 2>&1; then
  for u in ssh.service sshd.service ssh.socket sshd.socket; do
    s=$(systemctl is-active "$u" 2>/dev/null) && printf 'unit=%s %s\n' "$u" "$s"
  done
fi
if command -v rc-service >/dev/null 2>&1; then rc-service sshd status >/dev/null 2>&1 && echo openrc=started; fi
if [ -n "$bin" ]; then
  # -G first: it prints the effective settings without the runtime checks of -T, which fails on
  # socket-activated Ubuntu until the first connection has created /run/sshd. Older sshd lacks -G.
  if out=$("$bin" -G 2>/dev/null) || out=$("$bin" -T 2>/dev/null); then
    printf '%s\n' "$out" | grep -Ei '^(port|passwordauthentication|permitrootlogin|pubkeyauthentication|kbdinteractiveauthentication|authorizedkeysfile|authorizedkeyscommand|usepam) ' | sed 's/^/eff=/'
  else
    echo effective=unknown
  fi
fi
for f in /etc/ssh/ssh_host_*_key.pub; do
  [ -f "$f" ] && printf 'hostkey=%s\n' "$(head -c 16384 "$f" | head -n 1)"
done
"""

# Relabels an account's .ssh and authorized_keys to the policy default on an SELinux guest. Runs as
# root, so it never takes a path the account printed: it walks from the home in /etc/passwd ($1),
# requires the folder it lands in to be the account's ($2), and names both relative to that folder,
# which the account cannot swap away once root is inside it. restorecon without -R relabels a link
# itself, never its target.
RESTORECON = r"""
set -u
home=$1; uid=$2
command -v restorecon >/dev/null 2>&1 || exit 0
cd -P -- "$home/.ssh" 2>/dev/null || exit 0
[ "$(stat -c %u . 2>/dev/null)" = "$uid" ] || exit 0
[ -L authorized_keys ] && exit 0
restorecon -- . authorized_keys
"""

SCRIPTS: Dict[str, str] = {
    "inventory": INVENTORY,
    "sudo-verdicts": SUDO_VERDICTS,
    "keys-read": KEYS_READ,
    "keys-write": KEYS_WRITE,
    "sshd-facts": SSHD_FACTS,
    "restorecon": RESTORECON,
}


def digest(name: str) -> str:
    return hashlib.sha256(SCRIPTS[name].encode("utf-8")).hexdigest()[:16]


def argv(name: str, *args: str) -> List[str]:
    """The argv that runs script `name` with `args` as $1..$n. Arguments must be validated already."""
    for value in args:
        if not isinstance(value, str) or "\0" in value:
            raise ValueError("a script argument must be a string without NUL")
    return ["/bin/sh", "-c", SCRIPTS[name], f"orcastra-{name}", *args]
