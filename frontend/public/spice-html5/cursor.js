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
**  SpiceCursorConn
**      Drive the Spice Cursor Channel
**--------------------------------------------------------------------------*/
function SpiceCursorConn()
{
    SpiceConn.apply(this, arguments);
}

SpiceCursorConn.prototype = Object.create(SpiceConn.prototype);
SpiceCursorConn.prototype.process_channel_message = function(msg)
{
    if (msg.type == SPICE_MSG_CURSOR_INIT)
    {
        var cursor_init = new SpiceMsgCursorInit(msg.data);
        DEBUG > 1 && console.log("SpiceMsgCursorInit");
        if (this.parent && this.parent.inputs &&
            this.parent.inputs.mouse_mode == SPICE_MOUSE_MODE_SERVER)
        {
            this.parent.inputs.mousex = cursor_init.position.x;
            this.parent.inputs.mousey = cursor_init.position.y;
        }
        this.report_position(cursor_init.position.x, cursor_init.position.y);
        return true;
    }

    if (msg.type == SPICE_MSG_CURSOR_SET)
    {
        var cursor_set = new SpiceMsgCursorSet(msg.data);
        DEBUG > 1 && console.log("SpiceMsgCursorSet");
        /* Orcastra: the flags live on the cursor, not on the message. Reading them from the
           message never saw NONE (a hidden pointer then threw on its missing header) nor
           FROM_CACHE (a cached shape arrived without data and was drawn empty). */
        var flags = cursor_set.cursor.flags;
        if ((flags & SPICE_CURSOR_FLAGS_NONE) || !cursor_set.visible)
        {
            this.apply_cursor("none");
            return true;
        }

        var header = cursor_set.cursor.header;
        if (flags & SPICE_CURSOR_FLAGS_FROM_CACHE)
        {
            var cached = this.cursor_cache && this.cursor_cache[header.unique];
            this.apply_cursor(cached || "default", cached ? this.cursor_meta[header.unique] : null);
            return true;
        }

        if (header.type != SPICE_CURSOR_TYPE_ALPHA)
        {
            this.log_warn("FIXME: No support for cursor type " + header.type);
            return false;
        }

        this.set_cursor(cursor_set.cursor, (flags & SPICE_CURSOR_FLAGS_CACHE_ME) != 0);
        this.report_position(cursor_set.position.x, cursor_set.position.y);
        return true;
    }

    if (msg.type == SPICE_MSG_CURSOR_MOVE)
    {
        /* Orcastra: where the guest put its pointer; the console draws it there while the
           browser's own pointer is locked (server mouse mode). */
        var position = new SpicePoint16();
        position.from_dv(new SpiceDataView(msg.data), 0, msg.data);
        this.report_position(position.x, position.y);
        return true;
    }

    if (msg.type == SPICE_MSG_CURSOR_HIDE)
    {
        DEBUG > 1 && console.log("SpiceMsgCursorHide");
        this.apply_cursor("none");
        return true;
    }

    if (msg.type == SPICE_MSG_CURSOR_TRAIL)
    {
        this.known_unimplemented(msg.type, "Cursor Trail");
        return true;
    }

    if (msg.type == SPICE_MSG_CURSOR_RESET)
    {
        DEBUG > 1 && console.log("SpiceMsgCursorReset");
        this.apply_cursor("default");
        return true;
    }

    if (msg.type == SPICE_MSG_CURSOR_INVAL_ONE)
    {
        this.known_unimplemented(msg.type, "Cursor Inval One");
        return true;
    }

    if (msg.type == SPICE_MSG_CURSOR_INVAL_ALL)
    {
        DEBUG > 1 && console.log("SpiceMsgCursorInvalAll");
        this.cursor_cache = {};
        this.cursor_meta = {};
        return true;
    }

    return false;
}

/* Orcastra: one place that shows a cursor, on the screen element the canvas inherits from,
   and tells the console (for its locked-pointer overlay). */
SpiceCursorConn.prototype.apply_cursor = function(css, meta)
{
    var screen = document.getElementById(this.parent.screen_id);
    if (screen)
        screen.style.cursor = css;
    if (this.parent.oncursorshape)
        this.parent.oncursorshape(css, meta || null);
}

SpiceCursorConn.prototype.report_position = function(x, y)
{
    if (this.parent.oncursormove)
        this.parent.oncursormove(x, y);
}

SpiceCursorConn.prototype.set_cursor = function(cursor, cache)
{
    /* Orcastra: width then height, as create_rgba_png takes them (they were swapped, which
       garbled every cursor that is not square). */
    var pngstr = create_rgba_png(cursor.header.width, cursor.header.height, cursor.data);
    var url = 'url(data:image/png,' + pngstr + ')';
    var curstr = url + ' ' + cursor.header.hot_spot_x + ' ' + cursor.header.hot_spot_y + ", default";
    var meta = {
        url: 'data:image/png,' + pngstr,
        width: cursor.header.width,
        height: cursor.header.height,
        hot_x: cursor.header.hot_spot_x,
        hot_y: cursor.header.hot_spot_y,
    };
    if (cache)
    {
        if (!this.cursor_cache)
        {
            this.cursor_cache = {};
            this.cursor_meta = {};
        }
        this.cursor_cache[cursor.header.unique] = curstr;
        this.cursor_meta[cursor.header.unique] = meta;
    }
    this.apply_cursor(curstr, meta);
}
