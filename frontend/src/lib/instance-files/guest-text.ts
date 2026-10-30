/**
 * Text that came from a guest, made safe to show.
 *
 * important: a guest chooses its file names. A right-to-left override makes `gpj.exe` read as
 * `exe.jpg`, and a newline splits a row or a toast. React already escapes markup, so what is left is
 * what a person reads: control and bidirectional characters become visible escapes. This is for
 * display only. The API always receives the raw name.
 *
 * note: bytes that are not UTF-8 arrive as U+10FC80 to U+10FCFF (see backend paths.py), and show as
 * the `\xNN` a terminal would print.
 */

const INVISIBLE = new RegExp("[\\u0000-\\u001f\\u007f-\\u009f\\u200b-\\u200f\\u2028-\\u202f\\u2060-\\u2069\\ufeff]", "gu");
const RAW_BYTE = new RegExp("[\\u{10FC80}-\\u{10FCFF}]", "gu");

function hex(value: number, width: number): string {
  return value.toString(16).toUpperCase().padStart(width, "0");
}

export function guestText(value: string): string {
  return value
    .replace(RAW_BYTE, (char) => `\\x${hex((char.codePointAt(0) ?? 0) - 0x10fc00, 2)}`)
    .replace(INVISIBLE, (char) => `\\u${hex(char.charCodeAt(0), 4)}`);
}

export function hasRawBytes(value: string): boolean {
  return /[\u{10FC80}-\u{10FCFF}]/u.test(value);
}
