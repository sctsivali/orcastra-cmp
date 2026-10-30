"use strict";
/*
   Copyright (C) 2012 by Jeremy P. White <jwhite@codeweavers.com>

   This file is part of spice-html5.

   spice-html5 is free software: you can redistribute it and/or modify
   it under the terms of the GNU Lesser General Public License as published by
   the Free Software Foundation, either version 3 of the License, or
   (at your option) any later version.

   spice-html5 is distributed in the hope that it will be useful,
   but WITHOUT ANY WARRANTY; without even the implied warranty of
   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
   GNU Lesser General Public License for more details.

   You should have received a copy of the GNU Lesser General Public License
   along with spice-html5.  If not, see <http://www.gnu.org/licenses/>.
*/

/*----------------------------------------------------------------------------
**  SpiceMainConn
**      This is the master Javascript class for establishing and
**  managing a connection to a Spice Server.
**
**      Invocation:  You must pass an object with properties as follows:
**          uri         (required)  Uri of a WebSocket listener that is
**                                  connected to a spice server.
**          password    (required)  Password to send to the spice server
**          message_id  (optional)  Identifier of an element in the DOM
**                                  where SpiceConn will write messages.
**                                  It will use classes spice-messages-x,
**                                  where x is one of info, warning, or error.
**          screen_id   (optional)  Identifier of an element in the DOM
**                                  where SpiceConn will create any new
**                                  client screens.  This is the main UI.
**          dump_id     (optional)  If given, an element to use for
**                                  dumping every single image + canvas drawn.
**                                  Sometimes useful for debugging.
**          onerror     (optional)  If given, a function to receive async
**                                  errors.  Note that you should also catch
**                                  errors for ones that occur inline
**          onagent     (optional)  If given, a function to be called when
**                                  a VD agent is connected; a good opportunity
**                                  to request a resize
**          channel_uris (optional) Orcastra: one URI per further channel
**                                  socket.  Each carries its own single-use
**                                  ticket, so a channel must not reuse the
**                                  main socket's URI when these are given.
**
**  Throws error if there are troubles.  Requires a modern (by 2012 standards)
**      browser, including WebSocket and WebSocket.binaryType == arraybuffer
**
**--------------------------------------------------------------------------*/
function SpiceMainConn()
{
    if (typeof WebSocket === "undefined")
        throw new Error("WebSocket unavailable.  You need to use a different browser.");

    SpiceConn.apply(this, arguments);

    this.agent_msg_queue = [];
    this.file_xfer_tasks = {};
    this.file_xfer_task_id = 0;
    this.file_xfer_read_queue = [];
    this.ports = [];
    var options = arguments[0] || {};
    this.channel_uris = Array.isArray(options.channel_uris) ? options.channel_uris.slice() : [];
}

SpiceMainConn.prototype = Object.create(SpiceConn.prototype);
/* Orcastra: the next channel's own URI, falling back to the main socket's when none was given. */
SpiceMainConn.prototype.next_channel_uri = function()
{
    return this.channel_uris.length > 0 ? this.channel_uris.shift() : this.ws.url;
}
SpiceMainConn.prototype.process_channel_message = function(msg)
{
    if (msg.type == SPICE_MSG_MAIN_MIGRATE_BEGIN)
    {
        this.known_unimplemented(msg.type, "Main Migrate Begin");
        return true;
    }

    if (msg.type == SPICE_MSG_MAIN_MIGRATE_CANCEL)
    {
        this.known_unimplemented(msg.type, "Main Migrate Cancel");
        return true;
    }

    if (msg.type == SPICE_MSG_MAIN_INIT)
    {
        this.log_info("Connected to " + String(this.ws.url).split("?")[0]);
        this.report_success("Connected")
        this.main_init = new SpiceMsgMainInit(msg.data);
        this.connection_id = this.main_init.session_id;
        this.agent_tokens = this.main_init.agent_tokens;

        if (DEBUG > 0)
        {
            // FIXME - there is a lot here we don't handle; mouse modes, agent,
            //          ram_hint, multi_media_time
            this.log_info("session id "                 + this.main_init.session_id +
                          " ; display_channels_hint "   + this.main_init.display_channels_hint +
                          " ; supported_mouse_modes "   + this.main_init.supported_mouse_modes +
                          " ; current_mouse_mode "      + this.main_init.current_mouse_mode +
                          " ; agent_connected "         + this.main_init.agent_connected +
                          " ; agent_tokens "            + this.main_init.agent_tokens +
                          " ; multi_media_time "        + this.main_init.multi_media_time +
                          " ; ram_hint "                + this.main_init.ram_hint);
        }

        this.our_mm_time = Date.now();
        this.mm_time = this.main_init.multi_media_time;

        this.handle_mouse_mode(this.main_init.current_mouse_mode,
                               this.main_init.supported_mouse_modes);

        if (this.main_init.agent_connected)
            this.connect_agent();

        var attach = new SpiceMiniData;
        attach.type = SPICE_MSGC_MAIN_ATTACH_CHANNELS;
        attach.size = attach.buffer_size();
        this.send_msg(attach);
        return true;
    }

    if (msg.type == SPICE_MSG_MAIN_MOUSE_MODE)
    {
        var mode = new SpiceMsgMainMouseMode(msg.data);
        DEBUG > 0 && this.log_info("Mouse supported modes " + mode.supported_modes + "; current " + mode.current_mode);
        this.handle_mouse_mode(mode.current_mode, mode.supported_modes);
        return true;
    }

    if (msg.type == SPICE_MSG_MAIN_MULTI_MEDIA_TIME)
    {
        this.known_unimplemented(msg.type, "Main Multi Media Time");
        return true;
    }

    if (msg.type == SPICE_MSG_MAIN_CHANNELS_LIST)
    {
        var i;
        var chans;
        DEBUG > 0 && console.log("channels");
        chans = new SpiceMsgChannels(msg.data);
        for (i = 0; i < chans.channels.length; i++)
        {
            var conn = {
                        uri: null,
                        parent: this,
                        connection_id : this.connection_id,
                        type : chans.channels[i].type,
                        chan_id : chans.channels[i].id,
                        /* Orcastra: a display, inputs or cursor channel that failed used to
                           fail silently (no onerror), leaving a frozen screen or dead input
                           under a "connected" status. */
                        onerror : this.child_error.bind(this)
                    };
            // For LXD console, only create essential channels to reduce WebSocket connections
            // This prevents browser freeze and operation flood
            if (chans.channels[i].type == SPICE_CHANNEL_DISPLAY)
            {
                if (chans.channels[i].id == 0) {
                    conn.uri = this.next_channel_uri();
                    this.display = new SpiceDisplayConn(conn);
                } else {
                    this.log_warn("The spice-html5 client does not handle multiple heads.");
                }
            }
            else if (chans.channels[i].type == SPICE_CHANNEL_INPUTS)
            {
                conn.uri = this.next_channel_uri();
                this.inputs = new SpiceInputsConn(conn);
                this.inputs.mouse_mode = this.mouse_mode;
            }
            else if (chans.channels[i].type == SPICE_CHANNEL_CURSOR)
            {
                // Cursor channel is optional but useful for custom cursor rendering
                conn.uri = this.next_channel_uri();
                this.cursor = new SpiceCursorConn(conn);
            }
            else
            {
                // Skip all other channels (PLAYBACK, PORT, SMARTCARD, USBREDIR, etc.)
                // to reduce WebSocket connections and prevent browser freeze
                // These are not essential for basic console functionality
                DEBUG > 0 && this.log_info("Skipping non-essential channel type " + chans.channels[i].type);
            }

        }

        return true;
    }

    if (msg.type == SPICE_MSG_MAIN_AGENT_CONNECTED)
    {
        this.connect_agent();
        return true;
    }

    if (msg.type == SPICE_MSG_MAIN_AGENT_CONNECTED_TOKENS)
    {
        var connected_tokens = new SpiceMsgMainAgentTokens(msg.data);
        this.agent_tokens = connected_tokens.num_tokens;
        this.connect_agent();
        return true;
    }

    if (msg.type == SPICE_MSG_MAIN_AGENT_TOKEN)
    {
        var remaining_tokens, tokens = new SpiceMsgMainAgentTokens(msg.data);
        this.agent_tokens += tokens.num_tokens;
        this.send_agent_message_queue();

        remaining_tokens = this.agent_tokens;
        while (remaining_tokens > 0 && this.file_xfer_read_queue.length > 0)
        {
            var xfer_task = this.file_xfer_read_queue.shift();
            this.file_xfer_read(xfer_task, xfer_task.read_bytes);
            remaining_tokens--;
        }
        return true;
    }

    if (msg.type == SPICE_MSG_MAIN_AGENT_DISCONNECTED)
    {
        this.agent_connected = false;
        if (this.onagentdisconnected)
            this.onagentdisconnected(this);
        return true;
    }

    if (msg.type == SPICE_MSG_MAIN_AGENT_DATA)
    {
        var agent_data = new SpiceMsgMainAgentData(msg.data);
        if (agent_data.type == VD_AGENT_ANNOUNCE_CAPABILITIES)
        {
            var agent_caps = new VDAgentAnnounceCapabilities(agent_data.data);
            if (agent_caps.request)
                this.announce_agent_capabilities(0);
            return true;
        }
        else if (agent_data.type == VD_AGENT_FILE_XFER_STATUS)
        {
            this.handle_file_xfer_status(new VDAgentFileXferStatusMessage(agent_data.data));
            return true;
        }
        /* Orcastra: the clipboard, both ways, as UTF-8 text through the guest agent. */
        else if (agent_data.type == VD_AGENT_CLIPBOARD_GRAB)
        {
            var grab = new VDAgentClipboardGrab(agent_data.data);
            if (grab.types.indexOf(VD_AGENT_CLIPBOARD_UTF8_TEXT) >= 0)
                this.send_agent_message(VD_AGENT_CLIPBOARD_REQUEST,
                                        new VDAgentClipboardRequest(VD_AGENT_CLIPBOARD_UTF8_TEXT));
            return true;
        }
        else if (agent_data.type == VD_AGENT_CLIPBOARD)
        {
            var clip = new VDAgentClipboard(agent_data.data);
            if (clip.type == VD_AGENT_CLIPBOARD_UTF8_TEXT && typeof this.clipboard_on_guest_data === "function")
            {
                try {
                    this.clipboard_on_guest_data(new TextDecoder("utf-8").decode(clip.bytes));
                } catch (e) {}
            }
            return true;
        }
        else if (agent_data.type == VD_AGENT_CLIPBOARD_REQUEST)
        {
            var self = this;
            if (typeof this.clipboard_get_host_data === "function")
            {
                Promise.resolve(this.clipboard_get_host_data()).then(function(text) {
                    var bytes = new TextEncoder().encode(text == null ? "" : String(text));
                    self.send_agent_message(VD_AGENT_CLIPBOARD,
                                            new VDAgentClipboard(VD_AGENT_CLIPBOARD_UTF8_TEXT, bytes));
                }).catch(function() {});
            }
            return true;
        }
        else if (agent_data.type == VD_AGENT_CLIPBOARD_RELEASE)
        {
            return true;
        }

        return false;
    }

    if (msg.type == SPICE_MSG_MAIN_MIGRATE_SWITCH_HOST)
    {
        this.known_unimplemented(msg.type, "Main Migrate Switch Host");
        return true;
    }

    if (msg.type == SPICE_MSG_MAIN_MIGRATE_END)
    {
        this.known_unimplemented(msg.type, "Main Migrate End");
        return true;
    }

    if (msg.type == SPICE_MSG_MAIN_NAME)
    {
        this.known_unimplemented(msg.type, "Main Name");
        return true;
    }

    if (msg.type == SPICE_MSG_MAIN_UUID)
    {
        this.known_unimplemented(msg.type, "Main UUID");
        return true;
    }

    if (msg.type == SPICE_MSG_MAIN_MIGRATE_BEGIN_SEAMLESS)
    {
        this.known_unimplemented(msg.type, "Main Migrate Begin Seamless");
        return true;
    }

    if (msg.type == SPICE_MSG_MAIN_MIGRATE_DST_SEAMLESS_ACK)
    {
        this.known_unimplemented(msg.type, "Main Migrate Dst Seamless ACK");
        return true;
    }

    if (msg.type == SPICE_MSG_MAIN_MIGRATE_DST_SEAMLESS_NACK)
    {
        this.known_unimplemented(msg.type, "Main Migrate Dst Seamless NACK");
        return true;
    }

    return false;
}

SpiceMainConn.prototype.child_error = function(e)
{
    if (this.state == "closing" || this.state == "error")
        return;
    if (this.onerror !== undefined)
        this.onerror(e);
}

SpiceMainConn.prototype.stop = function(msg)
{
    this.state = "closing";
    /* Orcastra: mark the channels closing before they close, or each reports its own close. */
    [this.inputs, this.cursor, this.display].forEach(function(channel) {
        if (channel)
            channel.state = "closing";
    });

    if (this.inputs)
    {
        this.inputs.cleanup();
        this.inputs = undefined;
    }

    if (this.cursor)
    {
        this.cursor.cleanup();
        this.cursor = undefined;
    }

    if (this.display)
    {
        this.display.cleanup();
        this.display.destroy_surfaces();
        this.display = undefined;
    }

    this.cleanup();

    if ("extra_channels" in this)
        for (var e in this.extra_channels)
            this.extra_channels[e].cleanup();
    this.extra_channels = undefined;
}

SpiceMainConn.prototype.send_agent_message_queue = function(message)
{
    if (!this.agent_connected)
        return;

    if (message)
        this.agent_msg_queue.push(message);

    while (this.agent_tokens > 0 && this.agent_msg_queue.length > 0)
    {
        var mr = this.agent_msg_queue.shift();
        this.send_msg(mr);
        this.agent_tokens--;
    }
}

SpiceMainConn.prototype.send_agent_message = function(type, message)
{
    var agent_data = new SpiceMsgcMainAgentData(type, message);
    var sb = 0, maxsize = VD_AGENT_MAX_DATA_SIZE - SpiceMiniData.prototype.buffer_size();
    var data = new ArrayBuffer(agent_data.buffer_size());
    agent_data.to_buffer(data);
    while (sb < agent_data.buffer_size())
    {
        var eb = Math.min(sb + maxsize, agent_data.buffer_size());
        var mr = new SpiceMiniData();
        mr.type = SPICE_MSGC_MAIN_AGENT_DATA;
        mr.size = eb - sb;
        mr.data = data.slice(sb, eb);
        this.send_agent_message_queue(mr);
        sb = eb;
    }
}

SpiceMainConn.prototype.announce_agent_capabilities = function(request)
{
    /* Orcastra: clipboard support (UTF-8 text, on demand), without the selection variant so
       the messages keep the simple wire format. */
    var caps = new VDAgentAnnounceCapabilities(request, (1 << VD_AGENT_CAP_MOUSE_STATE) |
                                                        (1 << VD_AGENT_CAP_MONITORS_CONFIG) |
                                                        (1 << VD_AGENT_CAP_REPLY) |
                                                        (1 << VD_AGENT_CAP_CLIPBOARD) |
                                                        (1 << VD_AGENT_CAP_CLIPBOARD_BY_DEMAND));
    this.send_agent_message(VD_AGENT_ANNOUNCE_CAPABILITIES, caps);
}

/* Orcastra: tell the guest the browser holds UTF-8 text; its next paste asks for it. */
SpiceMainConn.prototype.clipboard_grab = function()
{
    this.send_agent_message(VD_AGENT_CLIPBOARD_GRAB,
                            new VDAgentClipboardGrab([VD_AGENT_CLIPBOARD_UTF8_TEXT]));
}

SpiceMainConn.prototype.file_xfer_cancel = function(task_id)
{
    var task = this.file_xfer_tasks[task_id];
    if (task)
        task.cancelled = true;
}

SpiceMainConn.prototype.resize_window = function(flags, width, height, depth, x, y)
{
    var monitors_config = new VDAgentMonitorsConfig(flags, width, height, depth, x, y);
    this.send_agent_message(VD_AGENT_MONITORS_CONFIG, monitors_config);
}

SpiceMainConn.prototype.file_xfer_start = function(file)
{
    var task_id, xfer_start, task;

    task_id = this.file_xfer_task_id++;
    task = new SpiceFileXferTask(task_id, file);
    /* Orcastra: the console draws transfers itself when it listens. */
    if (this.onfiletransfer)
        task.notify = this.onfiletransfer;
    task.create_progressbar();
    this.file_xfer_tasks[task_id] = task;
    xfer_start = new VDAgentFileXferStartMessage(task_id, file.name, file.size);
    this.send_agent_message(VD_AGENT_FILE_XFER_START, xfer_start);
}

SpiceMainConn.prototype.handle_file_xfer_status = function(file_xfer_status)
{
    var xfer_error, xfer_task;
    if (!this.file_xfer_tasks[file_xfer_status.id])
    {
        return;
    }
    xfer_task = this.file_xfer_tasks[file_xfer_status.id];
    switch (file_xfer_status.result)
    {
        case VD_AGENT_FILE_XFER_STATUS_CAN_SEND_DATA:
            this.file_xfer_read(xfer_task);
            return;
        case VD_AGENT_FILE_XFER_STATUS_CANCELLED:
            xfer_error = "transfer is cancelled by spice agent";
            break;
        case VD_AGENT_FILE_XFER_STATUS_ERROR:
            xfer_error = "some errors occurred in the spice agent";
            break;
        case VD_AGENT_FILE_XFER_STATUS_SUCCESS:
            break;
        default:
            xfer_error = "unhandled status type: " + file_xfer_status.result;
            break;
    }

    this.file_xfer_completed(xfer_task, xfer_error)
}

SpiceMainConn.prototype.file_xfer_read = function(file_xfer_task, start_byte)
{
    var FILE_XFER_CHUNK_SIZE = 32 * VD_AGENT_MAX_DATA_SIZE;
    var _this = this;
    var sb, eb;
    var slice, reader;

    if (!file_xfer_task ||
        !this.file_xfer_tasks[file_xfer_task.id] ||
        (start_byte > 0 && start_byte == file_xfer_task.file.size))
    {
        return;
    }

    if (file_xfer_task.cancelled)
    {
        var xfer_status = new VDAgentFileXferStatusMessage(file_xfer_task.id,
                                                           VD_AGENT_FILE_XFER_STATUS_CANCELLED);
        this.send_agent_message(VD_AGENT_FILE_XFER_STATUS, xfer_status);
        file_xfer_task.remove_progressbar();
        delete this.file_xfer_tasks[file_xfer_task.id];
        return;
    }

    sb = start_byte || 0,
    eb = Math.min(sb + FILE_XFER_CHUNK_SIZE, file_xfer_task.file.size);

    if (!this.agent_tokens)
    {
        file_xfer_task.read_bytes = sb;
        this.file_xfer_read_queue.push(file_xfer_task);
        return;
    }

    reader = new FileReader();
    reader.onload = function(e)
    {
        var xfer_data = new VDAgentFileXferDataMessage(file_xfer_task.id,
                                                       e.target.result.byteLength,
                                                       e.target.result);
        _this.send_agent_message(VD_AGENT_FILE_XFER_DATA, xfer_data);
        _this.file_xfer_read(file_xfer_task, eb);
        file_xfer_task.update_progressbar(eb);
    };

    slice = file_xfer_task.file.slice(sb, eb);
    reader.readAsArrayBuffer(slice);
}

SpiceMainConn.prototype.file_xfer_completed = function(file_xfer_task, error)
{
    if (error)
        this.log_err(error);
    else
        this.log_info("transfer of '" + file_xfer_task.file.name +"' was successful");

    file_xfer_task.finished = error ? "error" : "done";
    file_xfer_task.error = error || null;
    file_xfer_task.remove_progressbar();

    delete this.file_xfer_tasks[file_xfer_task.id];
}

SpiceMainConn.prototype.connect_agent = function()
{
    this.agent_connected = true;

    var agent_start = new SpiceMsgcMainAgentStart(~0);
    var mr = new SpiceMiniData();
    mr.build_msg(SPICE_MSGC_MAIN_AGENT_START, agent_start);
    this.send_msg(mr);

    this.announce_agent_capabilities(1);

    if (this.onagent !== undefined)
        this.onagent(this);

}

SpiceMainConn.prototype.handle_mouse_mode = function(current, supported)
{
    this.mouse_mode = current;
    if (current != SPICE_MOUSE_MODE_CLIENT && (supported & SPICE_MOUSE_MODE_CLIENT))
    {
        var mode_request = new SpiceMsgcMainMouseModeRequest(SPICE_MOUSE_MODE_CLIENT);
        var mr = new SpiceMiniData();
        mr.build_msg(SPICE_MSGC_MAIN_MOUSE_MODE_REQUEST, mode_request);
        this.send_msg(mr);
    }

    if (this.inputs)
        this.inputs.mouse_mode = current;
    /* Orcastra: the console shows the mode and locks the pointer only in server mode. */
    if (this.onmousemode)
        this.onmousemode(current);
}

/* Shift current time to attempt to get a time matching that of the server */
SpiceMainConn.prototype.relative_now = function()
{
    var ret = (Date.now() - this.our_mm_time) + this.mm_time;
    return ret;
}
