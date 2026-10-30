# spice-html5

These scripts are spice-html5 1.7.3 (npm package `spice-html5-bower`), loaded by the graphical
console through `frontend/src/lib/spice/loader.ts`. Local changes are marked `Orcastra:` in the
source and tracked in this repository's history. The rest of the files are unmodified.

| File | Change |
| --- | --- |
| `main.js` | Each channel socket takes its own single-use ticket (`channel_uris`); display, inputs and cursor channels report their errors through the main connection; channels are marked closing on `stop()`; mouse-mode and agent-disconnect callbacks; UTF-8 clipboard through the guest agent, both ways; file transfers report to the console instead of drawing their own progress bars. Playback, port and other channels stay unopened. |
| `spiceconn.js` | Errors carry the socket's close code, reason and channel; 4403 reads as revoked access; URLs are logged without their query; MJPEG only (no VP8 `<video>` overlay); no per-message timing log. |
| `display.js` | The console's input engine takes the primary canvas (`external_input`, `oncanvas`); frames drawn are counted (`draws`, `onfirstdraw`); JPEG and MJPEG decode from Blob URLs instead of percent-encoded data URLs; a JPEG before any stream no longer throws; a surface is deleted through its own canvas, not a document-wide id two consoles share; COPY_BITS on an opaque surface copies canvas to canvas without a readback; one reused helper canvas and a 32-bit alpha fill (the last three ported from orcastra-mini). |
| `wire.js` | A message is assembled from its fragments with one copy. |
| `utils.js` | `combine_array_buffers` copies natively; extended keys encode as `e0 xx` (they carried a stray third byte, and Print Screen sent a backtick); `alert()` became `console.warn`; keyCode 160 maps to Space. |
| `inputs.js` | A motion ACK calls `onmotionack`, where the console's pointer engine sends its newest pending position; the counter never goes negative. |
| `cursor.js` | Cursor flags are read from the cursor (hidden and cached cursors work), cached shapes are reused, width and height are passed in order, `CURSOR_MOVE` is reported, and every shape change goes through one function the console listens to. |
| `filexfer.js` | A transfer reports its progress through `task.notify` when the console listens. |
| `spicemsg.js` | Agent clipboard messages (ported from orcastra-mini); a released mouse button clears only its own bit; positions map through `getBoundingClientRect` for a scaled canvas. |
| `enums.js` | Agent clipboard data types. |

The console no longer loads `spice.css` or `resize.js`; it styles and sizes the screen itself.

spice-html5 is licensed under the GNU Lesser General Public License. The license texts are
`COPYING` and `COPYING.LESSER` in this directory, and `LICENSE.txt` is the upstream notice. The
upstream source is https://gitlab.freedesktop.org/spice/spice-html5.
